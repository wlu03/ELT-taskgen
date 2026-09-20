"""Expose the ELT task-production stages as CLI commands.

Exit 0 means passed, 1 means rejected, and 2 means usage, infrastructure, or a
blocked measurement. Metrology commands use 1 for not admitted.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from elt_taskgen import demo_fixture
from elt_taskgen import repair
from elt_taskgen.engine import (
    BLOCKED_ON_ENVIRONMENT,
    BLOCKED_ON_HUMAN,
    BLOCKED_ON_KEY,
    EVIDENCE_SUPERSESSION_KEY,
    RETRY_GUARD_EXPLICIT,
    RETRY_GUARD_KEY,
    Engine,
    EngineError,
    InfrastructureFailure,
    ReportRevalidationEvidence,
    ReportSupersession,
    StageName,
    StageOutcome,
    StagePayload,
    STAGE_ORDER,
    VERDICT_BLOCKED,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
    FINAL_ACCEPTED,
    FINAL_REJECTED,
    blocked_retry_requires_explicit_recovery,
    validate_author_revalidation_records,
    variant_gate_stage,
)
from elt_taskgen.models import (
    AttackKind,
    AuditApproval,
    CouncilRole,
    DifficultyMeasurement,
    Finding,
    FindingProvenance,
    FindingScreenStatus,
    Origin,
    PopulationName,
    ProposedAttackCase,
    RepairRoute,
    RLVR_TASK_VARIANTS,
    Severity,
    TaskIR,
    TaskStatus,
    TaskVariant,
    canonical_json,
    readable_json,
    sha256_hex,
    task_from_json,
    validate_task_id_segment,
)

#: Population column order for every human-readable matrix this module prints.
POP_ORDER: tuple[PopulationName, ...] = (
    PopulationName.DEVELOPMENT,
    PopulationName.PRIMARY,
    PopulationName.RESAMPLED,
    PopulationName.COUNTERFACTUAL,
    PopulationName.STRESS,
)

# Workspace path policy lives in elt_taskgen.workspace; re-exported here
# because the CLI is where callers and tests have always found it.
from elt_taskgen.workspace import (  # noqa: E402
    DEFAULT_WORKSPACE,
    NEVER_A_WORKSPACE as _NEVER_A_WORKSPACE,  # noqa: F401 — re-export
    assert_workspace_is_not_a_container as _assert_workspace_is_not_a_container,
)


class CliUsageError(ValueError):
    """The caller asked a question this command cannot answer as posed.

    A ValueError subclass so the `except ValueError` handlers that already wrap
    these parsers keep working; `main()` turns it into 'error: <msg>' + exit 2 — a
    usage error is not a rejected task, and a traceback is not an answer."""


class _LockedRunRefusal(RuntimeError):
    """A lock-scoped pre-run check refused execution without judging the task."""


CRITIC_PROTOCOL_FAILURE_CLASS = "protocol_failure"
CRITIC_PROTOCOL_FAILURE_CODE = "critic_attack_handoff_invalid"
CRITIC_PROTOCOL_RECOVERY = "correct_or_replace_critic_handoff"
CRITIC_MANIFEST_FAILURE_CODE = "critic_transcript_manifest_invalid"
CRITIC_MANIFEST_RECOVERY = "rebuild_or_correct_review_transcript_manifest"
CRITIC_DILIGENCE_FAILURE_CODE = "critic_shortcut_diligence_incomplete"
CRITIC_DILIGENCE_RECOVERY = "replace_shortcut_critic_handoff"
CRITIC_AMBIGUITY_FAILURE_CODE = "critic_ambiguity_requires_adjudication"
CRITIC_GOLD_DISAGREEMENT_CODE = "critic_gold_disagreement"
#: Signal for a proposal still invalid after bounded compile correction.
UNCOMPILABLE_AFTER_CORRECTIONS_SIGNAL = "uncompilable_after_corrections"

#: The same void, for a seat that never had a correction channel at all: its
#: declared `session:` block runs no compile validator on what it submits, so
#: nothing upstream could tell it its proposal was malformed.
UNCOMPILABLE_NO_CHANNEL_SIGNAL = "uncompilable_no_correction_channel"

#: A MAJOR finding its own provider withdrew (structured `disposition`, R02)
#: stays in the screen record but does not block. Explanation text never
#: withdraws a finding.
WITHDRAWN_BY_DISPOSITION_SIGNAL = "withdrawn_by_disposition"


#: A proposal that removes behavior absent from the contract is not a fork;
#: preserve the finding in the screen record without blocking.
MUTATION_TARGETS_NO_DECLARED_RULE_SIGNAL = "mutation_targets_no_declared_rule"


MUTATION_NAMES_NO_SECOND_HOP_SIGNAL = "mutation_names_no_second_hop"


def _proposal_names_a_hop_the_task_never_joins(task, proposal) -> bool:
    """Return whether a second-hop mutation targets a task with no second hop."""
    from elt_taskgen.models import AttackKind, MartOpKind

    kind = getattr(proposal, "kind", None)
    if kind is not AttackKind.INNER_JOIN:
        return False
    params = getattr(proposal, "params", None) or {}
    if str(params.get("variant", "") or "") != "second_hop":
        return False
    sources = {table.name for table in task.tables}
    for mart in task.marts:
        ops = getattr(getattr(mart, "plan", None), "ops", None) or ()
        hops = [
            op
            for op in ops
            if getattr(op, "kind", None) is MartOpKind.JOIN
            and any(name in sources for name in tuple(op.tables)[1:])
        ]
        if len(hops) >= 2:
            return False
    return True


def _proposal_targets_a_rule_the_task_never_states(task, proposal) -> bool:
    from elt_taskgen.models import AttackKind, MartOpKind

    kind = getattr(proposal, "kind", None)
    if kind is not AttackKind.NO_DEDUP:
        return False
    declared = any(
        getattr(op, "kind", None) is MartOpKind.DEDUPE
        for mart in task.marts
        for op in (getattr(getattr(mart, "plan", None), "ops", None) or ())
    )
    return not declared


def _seat_has_compile_channel(role, *, agents_config=None) -> bool:
    """Return whether a seat declares an enabled proposal or probe compiler."""
    from elt_taskgen.review import providers as providers_mod
    from elt_taskgen.review.tools import critic_validators as critic_tools

    role_name = getattr(role, "value", str(role or ""))
    if not role_name:
        return False
    try:
        block = providers_mod.role_loop_limits(role_name, agents_config=agents_config)
    except Exception:  # a role with no block, or an unreadable document
        return False
    if not (isinstance(block, dict) and block.get("enabled", False)):
        return False
    declared = {str(name) for name in (block.get("harness_validators") or ())}
    return bool(
        declared
        & {critic_tools.COMPILE_PROPOSAL_TOOL, critic_tools.COMPILE_PROBE_TOOL}
    )


def _claimed_critic_handoff(
    finding: Finding,
) -> tuple[Severity, AttackKind | None, ProposedAttackCase | None]:
    """Return a provider finding's preserved pre-screen executable claims.

    Harness-voided proposals remain final after their bounded correction.
    """

    screen = finding.screen
    if screen is None:
        return finding.severity, finding.suggested_attack, finding.proposed_case
    if (
        screen.status is FindingScreenStatus.VOID
        and UNCOMPILABLE_AFTER_CORRECTIONS_SIGNAL in tuple(screen.signals)
    ):
        return finding.severity, finding.suggested_attack, finding.proposed_case
    severity = screen.claimed_severity or finding.severity
    attack = finding.suggested_attack
    proposal = finding.proposed_case
    if screen.withheld_attack is not None:
        attack = screen.withheld_attack
    if screen.withheld_proposal is not None:
        proposal = screen.withheld_proposal
    return severity, attack, proposal


def _critic_protocol_block(
    detail: str,
    *,
    failure_code: str = CRITIC_PROTOCOL_FAILURE_CODE,
    recovery_prerequisite: str = CRITIC_PROTOCOL_RECOVERY,
) -> StageOutcome:
    """A critic transport/compile contract failed; the task is not judged.

    The digest is stable for an identical rejected handoff.  It gives the
    orchestration layer an evidence key with which to refuse an unchanged
    ordinary resume instead of repeatedly paying for/replaying the same bad
    critic output.  Recovery must replace that handoff (or otherwise change the
    protocol evidence), not retry it until it happens to pass.
    """
    handoff_digest = sha256_hex(
        canonical_json(
            {
                "failure_code": failure_code,
                "detail": detail,
            }
        )
    )
    return StageOutcome(
        VERDICT_BLOCKED,
        StagePayload(
            error=detail,
            data={
                "failure_class": CRITIC_PROTOCOL_FAILURE_CLASS,
                "failure_code": failure_code,
                BLOCKED_ON_KEY: "provider",
                "handoff_digest": handoff_digest,
                "recovery_prerequisite": recovery_prerequisite,
                RETRY_GUARD_KEY: RETRY_GUARD_EXPLICIT,
            },
        ),
    )


def _critic_adjudication_block(findings: list[Finding]) -> StageOutcome | None:
    """Hold fatal ambiguity and reference disputes for human adjudication.

    Major ambiguity claims route to specification repair; alternatives remain
    evidence and are not promoted.
    """
    disputes: list[Finding] = []
    for finding in findings:
        if (
            finding.screen is not None
            and finding.screen.status
            in {FindingScreenStatus.VOID, FindingScreenStatus.DUPLICATE}
        ):
            continue
        claimed_severity, claimed_attack, claimed_proposal = (
            _claimed_critic_handoff(finding)
        )
        if finding.provenance is not FindingProvenance.PROVIDER:
            continue
        if claimed_severity not in {Severity.MAJOR, Severity.FATAL}:
            continue
        gold_dispute = finding.route_hint is RepairRoute.REFERENCE
        ambiguity = finding.role is CouncilRole.AMBIGUITY_CRITIC
        if not (ambiguity or gold_dispute):
            continue
        if (
            ambiguity
            and not gold_dispute
            and claimed_severity is Severity.MAJOR
        ):
            # Ambiguity proposals are evidence, not gate cases: withhold them
            # from promotion and route the unresolved finding to specification
            # repair. A bare `suggested_attack` remains informational only.
            continue
        disputes.append(finding)
    if not disputes:
        return None
    is_gold_disagreement = any(
        finding.route_hint is RepairRoute.REFERENCE for finding in disputes
    )
    failure_code = (
        CRITIC_GOLD_DISAGREEMENT_CODE
        if is_gold_disagreement
        else CRITIC_AMBIGUITY_FAILURE_CODE
    )
    evidence_digest = sha256_hex(
        canonical_json(
            [
                finding.model_dump(mode="json")
                for finding in sorted(disputes, key=lambda item: item.finding_id)
            ]
        )
    )
    return StageOutcome(
        VERDICT_BLOCKED,
        StagePayload(
            error=(
                "critic evidence identifies an unresolved ambiguity or gold "
                "disagreement; any attached executable alternative is held, "
                "and the task, gold, and critic claim remain unjudged pending "
                "explicit adjudication"
            ),
            data={
                "failure_class": "pending_adjudication",
                "failure_code": failure_code,
                BLOCKED_ON_KEY: BLOCKED_ON_HUMAN,
                "critic_evidence_sha256": evidence_digest,
                "recovery_prerequisite": (
                    "bound_adjudication_or_revised_critic_evidence"
                ),
                RETRY_GUARD_KEY: RETRY_GUARD_EXPLICIT,
            },
        ),
    )


class _CollisionLike(Protocol):
    """Structural type shared by every adapter's contamination result."""

    kind: str
    against: str
    detail: str
    fatal: bool


# --- Typed ledger payloads owned by the CLI's stage runners ---

class ContaminationPayload(BaseModel):
    """check_pre/check_post result recorded in the ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_point: str
    collisions: tuple[dict, ...] = ()
    fatal_count: int = 0
    seeded_embedded_deny_lists: bool = False
    #: WHAT the index could see: grade plus the per-store, per-kind fingerprint
    #: census. Recorded because "0 collisions" is meaningless without it.
    coverage_level: str = ""
    coverage: dict = Field(default_factory=dict)
    detail: str = ""


class ReviewPayload(BaseModel):
    """Council findings recorded in the ledger (findings only, never a verdict
    vocabulary — the council cannot accept)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: tuple[Finding, ...] = ()
    fatal_count: int = 0
    detail: str = ""
    #: WHICH ADMISSION this review ran under. Recorded because revocation is
    #: PROSPECTIVE: without it, an auditor who later revokes an admission cannot
    #: list the reviews that ran on it. Defaulted, so old ledger rows still validate.
    admission: dict[str, str] = Field(default_factory=dict)
    #: Every council exchange consumed by this review, explicitly rebound to
    #: this task identity. Empty only for non-routed test doubles/legacy callers.
    transcript_manifest: tuple[dict, ...] = ()


class AttackFindingDescriptor(BaseModel):
    """One privacy-safe critic finding selected for an attack repair.

    The attack payload is a ledger artifact, not a model view.  It retains only
    the role/severity stamped by the harness and identifiers that the projector
    proved belong to the task's public surface; no finding prose or promoter
    reason is carried into the bounded repair session.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: CouncilRole
    severity: Severity
    identifiers: tuple[str, ...] = Field(default=(), max_length=16)


class AttackPayload(BaseModel):
    """Measured per-population rewards of every executed attack case."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rewards: dict[str, dict[str, float]] = Field(default_factory=dict)
    #: Per-variant rewards from the same execution; variant gates never use legacy rewards.
    rewards_by_variant: dict[str, dict[str, dict[str, float]]] = Field(
        default_factory=dict
    )
    cases: tuple[str, ...] = ()
    #: case -> no-surface reason, for informational probes this task's DATA offers
    #: no surface for. They carry NO rewards entry (an empty one would read as
    #: deleted evidence) but are named here, never silently skipped.
    inapplicable_cases: dict[str, str] = Field(default_factory=dict)
    #: Executed council attack-case PROPOSALS, promoted and rejected. Recorded so
    #: a rejection is VISIBLE in the ledger, never merely absent.
    promoted_proposals: tuple[str, ...] = ()
    rejected_proposals: tuple[dict, ...] = ()
    #: Full predicted/measured combined and EL/T matrices for every proposal,
    #: promoted or rejected. Names alone are not audit evidence.
    proposal_outcomes: tuple[dict, ...] = ()
    #: Value-free repair subject for one blocked critic-to-mutation handoff.
    blocking_finding: AttackFindingDescriptor | None = None
    detail: str = ""


# --- Small helpers ---

def _open_engine(workspace: Path, **kwargs) -> Engine:
    """THE ONLY WAY THIS MODULE CONSTRUCTS AN ENGINE.

    `Engine.__init__` creates state/ and tasks/ on construction, so every bare
    `Engine(workspace)` is a writer that skips the container guard. The guard
    belongs here and not in Engine.__init__, which is the library layer where
    programmatic callers legitimately root an Engine anywhere."""
    workspace = Path(workspace).resolve()
    _assert_workspace_is_not_a_container(workspace)
    return Engine(workspace, **kwargs)


def _answer_key_dir(engine: Engine, task: TaskIR) -> Path:
    return engine.task_dir(task.task_id) / "answer_key"


def _public_dir(engine: Engine, task: TaskIR) -> Path:
    return engine.task_dir(task.task_id) / "task"


def _evidence_dir(engine: Engine, task: TaskIR) -> Path:
    d = engine.task_dir(task.task_id) / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _workspace_rel(engine: Engine, path: Path) -> str:
    """Workspace-relative POSIX path for report payloads — absolute paths must
    never reach report copies or they break cross-location byte-identity."""
    return path.relative_to(engine.workspace).as_posix()


def _audit_dir(engine: Engine) -> Path:
    """Workspace-level human audit queue state, OUTSIDE the per-task reports tree
    so a repair sweep never clobbers approvals."""
    return engine.workspace / "audit"


def _approval_path(engine: Engine, task_id: str) -> Path:
    return _audit_dir(engine) / f"{task_id}.approval.json"


def _rejection_path(engine: Engine, task_id: str) -> Path:
    return _audit_dir(engine) / f"{task_id}.rejection.json"


def _collision_fingerprint(collision: dict) -> str:
    """Stable id of ONE recorded contamination collision: sha256 over the canonical
    {kind, against, detail}. Approvals bind to these ids, so sign-off coverage is
    checked mechanically, never by prose matching."""
    return sha256_hex(
        canonical_json(
            {
                "against": collision.get("against"),
                "detail": collision.get("detail"),
                "kind": collision.get("kind"),
            }
        )
    )


def _pending_borderline(
    engine: Engine, task: TaskIR
) -> tuple[dict[str, str], str | None]:
    """Pending borderline (non-fatal) collisions from the recorded post-scan.

    Returns ({fingerprint: detail}, problem). Missing or stale evidence is a
    problem string: the queue is only trustworthy while the recorded scan is bound
    to the CURRENT task content hash (fail closed)."""
    # OBSERVE/OFF: collisions are still detected and recorded; there is
    # simply nobody to sign off, so nothing queues.
    from elt_taskgen.verification import contamination as _cont
    if not _cont.enforcing():
        return {}, None
    path = _evidence_dir(engine, task) / "contamination_post.json"
    if not path.is_file():
        return {}, "no post-generation contamination evidence (fail closed)"
    recorded = json.loads(path.read_text(encoding="utf-8"))
    current = task.content_hash()
    if recorded.get("task_content_hash") != current:
        return {}, (
            "post-generation contamination evidence is stale (bound to "
            f"{str(recorded.get('task_content_hash'))[:12]}, task is {current[:12]}) "
            "— re-run gates (fail closed)"
        )
    pending = {
        _collision_fingerprint(c): str(c.get("detail", ""))
        for c in recorded.get("collisions", [])
        if not c.get("fatal")
    }
    return pending, None


def _coverage_payload(engine: Engine, coverage) -> dict:
    """IndexCoverage as JSON, with `index_dir` made WORKSPACE-RELATIVE.

    Nothing reads the value (it is provenance), but the persisted absolute paths
    outlived the directories they named. A path outside the workspace is left
    absolute rather than guessed at."""
    dumped = coverage.model_dump(mode="json")
    raw = dumped.get("index_dir")
    if isinstance(raw, str) and raw:
        try:
            dumped["index_dir"] = _workspace_rel(engine, Path(raw))
        except ValueError:
            pass  # not under this workspace: leave it as recorded
    return dumped


def _contamination_index(engine: Engine):
    from elt_taskgen.verification.contamination import ContaminationIndex

    return ContaminationIndex(engine.workspace / "state" / "contamination")


def _seed_embedded_deny_lists(idx) -> None:
    """Arm an empty index with the embedded benchmark family deny lists.

    SEEDING IS NOT ARMING: this writes benchmark family NAMES and nothing
    structural, so the index answers "is this task NAMED like a benchmark?".
    Callers go through `_arm_contamination_index`, which says so out loud."""
    from elt_taskgen.verification import contamination as cont

    for name, families in (
        ("eltbench", cont.ELTBENCH_FAMILIES),
        ("spider2_dbt", cont.SPIDER2_DBT_FAMILIES),
        ("ade_bench", cont.ADE_BENCH_FAMILIES),
    ):
        idx.add_benchmark(
            name, sorted({f"family:{cont.normalize_name(f)}" for f in families})
        )


def _arm_contamination_index(idx, *, announce: bool = True):
    """Seed an empty index, then REPORT what the firewall actually contains.

    `is_armed()` is satisfied by this function's own seeding, so it proves nothing
    — hence the coverage banner, and hence every caller records the returned
    IndexCoverage beside its collisions. WARNS rather than refuses by default (the
    tests have no benchmark checkout); `ELT_TASKGEN_REQUIRE_FIREWALL=1` refuses."""
    if not idx.is_armed():
        _seed_embedded_deny_lists(idx)
    coverage = idx.coverage()
    if announce and not coverage.is_firewall:
        print(f"  {coverage.summary()}")
    return coverage


def _required_firewall_coverage():
    """Minimum contamination coverage this process REFUSES below, or None."""
    from elt_taskgen.verification import contamination as cont

    return cont.required_coverage_from_env()


def _require_pass_payload(engine: Engine, task: TaskIR, stage: StageName) -> dict:
    """Latest ledger payload of `stage`, required to be a pass at the CURRENT
    content hash. Missing/stale/failed evidence raises (fail closed)."""
    row = engine.latest_report(task.task_id, stage.value)
    if row is None:
        raise RuntimeError(f"no {stage.value!r} report in the ledger (fail closed)")
    if row.verdict != VERDICT_PASS:
        raise RuntimeError(
            f"latest {stage.value!r} report is {row.verdict!r}, not a pass (fail closed)"
        )
    if row.content_hash != task.content_hash():
        raise RuntimeError(
            f"latest {stage.value!r} report is stale (bound to {row.content_hash[:12]}, "
            f"task is {task.content_hash()[:12]}) — re-run it (fail closed)"
        )
    return json.loads(row.payload_json)


# --- Stage runners (engine.run executes these; cf. docs/INTERFACES.md) ---

def run_contamination_pre(engine: Engine, task: TaskIR) -> StageOutcome:
    """`contamination_pre`: pre-generation scan (fatal collision -> reject)."""
    idx = _contamination_index(engine)
    seeded = not idx.is_armed()
    _arm_contamination_index(idx, announce=False)
    result = idx.scan_pre(task, require=_required_firewall_coverage())
    fatal = [c for c in result.collisions if c.fatal]
    payload = ContaminationPayload(
        call_point="pre",
        collisions=tuple(c.model_dump(mode="json") for c in result.collisions),
        fatal_count=len(fatal),
        seeded_embedded_deny_lists=seeded,
        coverage_level=result.coverage.level.value,
        coverage=_coverage_payload(engine, result.coverage),
        detail=(
            result.detail()
            + ("; index seeded with embedded deny lists" if seeded else "")
        ),
    )
    if fatal:
        return StageOutcome(VERDICT_FATAL, payload)
    return StageOutcome(VERDICT_PASS, payload)


#: Generation scratch is a sibling excluded from repair fingerprints after crashes.
_REBUILD_SCRATCH_DIR = ".rebuild"
_REBUILD_TMP_SUFFIX = ".rebuild-tmp"
_REBUILD_STALE_SUFFIX = ".stale"


def _drift_lines(derived: dict[str, str], on_disk: dict[str, str]) -> tuple[str, ...]:
    """Human-readable difference between a fresh derivation and what is on disk."""
    lines: list[str] = []
    for rel in sorted(set(derived) | set(on_disk)):
        if rel not in on_disk:
            lines.append(f"{rel}: missing on disk")
        elif rel not in derived:
            lines.append(f"{rel}: not derived from the IR")
        elif derived[rel] != on_disk[rel]:
            lines.append(f"{rel}: differs")
    return tuple(lines)


def run_generate(engine: Engine, task: TaskIR) -> StageOutcome:
    """Materialize all IR populations and replace only byte-different artifacts."""
    from elt_taskgen.generation import populations as populations_mod
    from elt_taskgen.generation import source_data

    problems = populations_mod.validate_population_coverage(task)
    if problems:
        return StageOutcome(
            VERDICT_FAIL,
            StagePayload(error="population coverage: " + "; ".join(problems)),
            route=RepairRoute.POPULATION,
        )

    tdir = engine.task_dir(task.task_id)
    scratch = tdir / _REBUILD_SCRATCH_DIR
    # Whatever a previous crash left behind goes first, including the pre-fix
    # in-place scratch trees under populations/.
    shutil.rmtree(scratch, ignore_errors=True)
    for legacy in sorted((tdir / "populations").glob("*")):
        if legacy.is_dir() and legacy.name.endswith(
            (_REBUILD_TMP_SUFFIX, _REBUILD_STALE_SUFFIX)
        ):
            shutil.rmtree(legacy, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    built: list[str] = []
    reused: list[str] = []
    data: dict[str, str] = {}
    for pop_spec in sorted(task.populations, key=lambda p: p.name.value):
        pop = pop_spec.name
        pop_dir = tdir / "populations" / pop.value
        tmp_dir = scratch / (pop.value + _REBUILD_TMP_SUFFIX)
        stale_dir = scratch / (pop.value + _REBUILD_STALE_SUFFIX)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(stale_dir, ignore_errors=True)
        existed = pop_dir.is_dir()
        hashes = source_data.materialize_population(task, pop, tmp_dir)
        drift = _drift_lines(
            source_data.tree_digest(tmp_dir), source_data.tree_digest(pop_dir)
        )
        if not drift:
            # Byte-identical: leave the pinned tree alone, so artifact hashes and
            # repair fingerprints do not move just because the stage re-attested.
            shutil.rmtree(tmp_dir, ignore_errors=True)
            reused.append(pop.value)
            continue
        # Atomic swap, so a crash can never leave a half-written population. It
        # also DELETES files the derivation no longer produces.
        pop_dir.parent.mkdir(parents=True, exist_ok=True)
        if pop_dir.exists():
            pop_dir.rename(stale_dir)
        tmp_dir.rename(pop_dir)
        shutil.rmtree(stale_dir, ignore_errors=True)
        for table_name in sorted(hashes):
            engine.record_artifact(
                task,
                f"tasks/{task.task_id}/populations/{pop.value}/rows/{table_name}.jsonl",
                hashes[table_name],
            )
        built.append(pop.value)
        if existed:
            # WHY it rebuilt, in the ledger — 'reused' is unfalsifiable without it.
            # Only for a population that WAS there.
            data[f"drift:{pop.value}"] = "; ".join(drift[:6]) + (
                f" (+{len(drift) - 6} more)" if len(drift) > 6 else ""
            )
    shutil.rmtree(scratch, ignore_errors=True)
    data.update({"built": ",".join(built), "reused": ",".join(reused)})
    data["verified"] = "rederived"
    return StageOutcome(
        VERDICT_PASS,
        StagePayload(
            detail=(
                f"populations re-derived from the IR: built={built or '[]'} "
                f"reused(byte-identical)={reused or '[]'}"
            ),
            data=data,
        ),
    )


def _population_drift_failure(engine: Engine, task: TaskIR) -> StageOutcome | None:
    """A stage FAILURE when any population on disk is not the IR's derivation.

    The populations are the graded EL surface and the input the gold was frozen
    from, and the content hash covers the IR only — so this recomputation is the
    check (one edited rendered/ file otherwise passed everything while the true
    reference scored 0.0). Routed RUNTIME, which re-runs `generate`."""
    from elt_taskgen.generation import source_data

    populations_root = engine.task_dir(task.task_id) / "populations"
    if not populations_root.is_dir():
        # NOTHING MATERIALIZED AT ALL is `generate`'s business: the batteries
        # already fail closed on absent sources. This check is for what the content
        # hash cannot see — a tree that EXISTS but is no longer the IR's derivation.
        return None
    problems: list[str] = []
    for pop_spec in sorted(task.populations, key=lambda p: p.name.value):
        drift = source_data.population_drift(
            task, pop_spec.name, populations_root / pop_spec.name.value
        )
        if drift:
            shown = "; ".join(drift[:4]) + (
                f" (+{len(drift) - 4} more)" if len(drift) > 4 else ""
            )
            problems.append(f"{pop_spec.name.value}: {shown}")
    if not problems:
        return None
    return StageOutcome(
        VERDICT_FAIL,
        StagePayload(
            error=(
                "population artifacts drift: what is on disk is not what the "
                "IR derives — " + " | ".join(problems) + ". Re-run `generate` "
                "(it re-derives and rewrites); the gold and every battery must "
                "then re-attest against the restored bytes."
            ),
            data={"drifted_populations": ",".join(p.split(":")[0] for p in problems)},
        ),
        route=RepairRoute.RUNTIME,
    )


def _ensure_perturbation_probe(engine: Engine, task: TaskIR, gold) -> None:
    """Record the data-sensitivity scratch probe when the gate will demand it.

    Only a 'provided-rows' task needs one, and cannot pass data-sensitivity without
    it. Bound to the current content hash (a stale record is rejected); the probe
    never writes the pinned population artifacts."""
    from elt_taskgen.verification import gates as gates_mod
    from elt_taskgen.verification import perturbation as perturb_mod

    if not gates_mod.needs_perturbation_probe(task, engine.workspace):
        return
    perturb_mod.record_perturbation_probe(task, engine.workspace, gold)


def run_reference_stage(engine: Engine, task: TaskIR) -> StageOutcome:
    """`reference`: private reference execution + determinism evidence + gold freeze."""
    from elt_taskgen.models import GateResult
    from elt_taskgen.reference import gold as gold_mod
    from elt_taskgen.reference import runner as runner_mod
    from elt_taskgen.verification import gates as gates_mod

    akd = _answer_key_dir(engine, task)
    det_path = _evidence_dir(engine, task) / "determinism.json"
    current = task.content_hash()

    # Resume shortcut: gold already frozen AT THIS EXACT identity, with the
    # determinism record bound to it. An unbound or stale record is rebuilt instead.
    if det_path.is_file() and (akd / gold_mod.MANIFEST_FILENAME).is_file():
        try:
            frozen = gold_mod.load_gold(akd)
            recorded = GateResult.model_validate_json(
                det_path.read_text(encoding="utf-8")
            )
            det_bound = (
                recorded.passed
                and recorded.evidence.get("task_id") == task.task_id
                and recorded.evidence.get(gates_mod.DETERMINISM_CONTENT_HASH_KEY)
                == current
            )
            if (
                det_bound
                and frozen.task_id == task.task_id
                and frozen.task_content_hash == current
            ):
                # RESUMING MEANS "the gold on disk is still the right answer to the
                # data on disk", which the content hash cannot say — so the
                # populations are re-derived and byte-compared first.
                drifted = _population_drift_failure(engine, task)
                if drifted is not None:
                    return drifted
                # The probe is bound to the same identity, so the resume path must
                # (re)produce it or resuming drops the gate's only evidence.
                _ensure_perturbation_probe(engine, task, frozen)
                return StageOutcome(
                    VERDICT_PASS,
                    StagePayload(detail="gold already frozen at current content hash"),
                )
        except Exception:
            pass  # stale/corrupt gold: fall through and rebuild (fail closed)

    results = {}
    for pop_spec in task.populations:
        results[pop_spec.name] = runner_mod.run_reference(
            task, pop_spec.name, engine.workspace
        )

    det = runner_mod.determinism_evidence(
        task, PopulationName.PRIMARY, engine.workspace, runs=3
    )
    det = det.model_copy(
        update={
            "evidence": {
                **det.evidence,
                "task_id": task.task_id,
                gates_mod.DETERMINISM_CONTENT_HASH_KEY: current,
            }
        }
    )
    det_path.write_text(
        canonical_json(det.model_dump(mode="json")), encoding="utf-8"
    )
    if not det.passed:
        # 'determinism' keyword routes RUNTIME via repair.route_for_failure.
        return StageOutcome(VERDICT_FAIL, det)

    gold = gold_mod.freeze_gold(task, results, akd)
    _ensure_perturbation_probe(engine, task, gold)
    for rel in sorted(gold.file_hashes):
        engine.record_artifact(
            task,
            f"tasks/{task.task_id}/answer_key/{rel}",
            gold.file_hashes[rel],
        )
    return StageOutcome(
        VERDICT_PASS,
        StagePayload(
            detail=f"gold frozen for {len(results)} population(s); determinism x3 identical",
            data={"gold_files": str(len(gold.file_hashes))},
        ),
    )


def _admission_gate(provider, workspace: Path | None) -> tuple[str | None, dict[str, str]]:
    """(failure detail or None, admission PROVENANCE) for one live-capable call site.

    A LIVE-capable provider needs the council.live_admitted marker from a passing
    `metrology` run, bound to the current critic routing; absent, unreadable or
    mismatched fails the stage CLOSED. The provenance is returned because
    revocation is PROSPECTIVE: unless each gated call records WHICH admission it
    passed under, revoking one cannot list what ran on it."""
    # EXEMPTION IS A POSITIVE CAPABILITY TEST, never absence of an attribute: exempt
    # only when replay_only is True (cannot call live) or there is no `routing` at
    # all. Anything routable to a real model must show a marker.
    from elt_taskgen.review import metrology as metrology_mod

    replay_only = getattr(provider, "replay_only", None)
    routing = getattr(provider, "routing", None)
    if replay_only is True:
        return None, metrology_mod.AdmissionStatus(
            ok=True, reason="replay-only: no live call is possible"
        ).provenance(metrology_mod.ADMISSION_MODE_REPLAY_ONLY)
    if replay_only is None and routing is None:
        # not a live-capable provider: no endpoint, no exposure
        return None, metrology_mod.AdmissionStatus(
            ok=True, reason="no routing: not a live-capable provider"
        ).provenance(metrology_mod.ADMISSION_MODE_NO_ROUTING)

    fingerprint = (
        metrology_mod.council_routing_fingerprint(routing)
        if routing is not None
        else None
    )
    status = metrology_mod.admission_status(
        workspace,
        routing_fingerprint=fingerprint,
        # The record is checked against the agents document THIS provider's
        # routing was loaded from (a custom --agents-config), never always
        # the repository default.
        agents_config=getattr(provider, "agents_config", None),
        record_path=getattr(provider, "admission_reference", None),
    )
    if status.ok:
        return None, status.provenance(metrology_mod.ADMISSION_MODE_ADMITTED)
    # Name the ACTUAL cause: the five failure modes have different remedies, and a
    # generic "absent or stale" sent operators hunting for the wrong fix.
    return f"{metrology_mod.NOT_ADMITTED_MESSAGE}: {status.reason}", {}


def _stamp_admission(provider, provenance: dict[str, str]) -> None:
    """Carry the admission this call site passed into the provider, so every
    transcript it records is stamped with it. Never fatal: provenance is evidence,
    not a gate."""
    if not provenance:
        return
    try:
        provider.admission_provenance = dict(provenance)
    except (AttributeError, TypeError):  # frozen/slotted stub: nothing to do
        pass


def _begin_task_evidence(provider, task: TaskIR) -> bool:
    """Start a task-bound exchange manifest when the provider supports it.

    A provider whose `begin_task_evidence` takes `task=` (the `RoutedProvider`)
    receives the TaskIR too: that opens the TASK CONTEXT under which a
    harness-validated critic seat (`providers.AGENTIC_ROLES`, its block
    `enabled: true`) runs a bounded session inside `run_council`'s one
    `complete` call; a double that takes the identity only stays valid."""
    begin = getattr(provider, "begin_task_evidence", None)
    if not callable(begin):
        return False  # lightweight test doubles remain valid
    if _accepts_keyword(begin, "task"):
        begin(task.task_id, task.content_hash(), task=task)
    else:
        begin(task.task_id, task.content_hash())
    return True


def _accepts_keyword(fn, name: str) -> bool:
    """Does callable `fn` accept keyword `name` (or `**kwargs`)?"""
    import inspect

    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _validated_review_manifest(
    provider, task: TaskIR, findings: list[Finding], *, required: bool
) -> tuple[tuple[dict, ...], list[str]]:
    """Validate one task-bound exchange row per critic role.

    Accept legacy one-shot rows and schema-3 session rows only when their call
    counts and finding counts reconcile exactly.
    """
    from elt_taskgen.review import providers as providers_mod
    from elt_taskgen.review.providers import exchange_row_problems

    raw = getattr(provider, "exchange_evidence", None)
    if not required:
        return (), []
    if not isinstance(raw, list):
        return (), ["routed provider exposed no exchange_evidence list"]
    expected_roles = {
        "ambiguity_critic",
        "population_adversary",
        "shortcut_attacker",
        "feasibility_reviewer",
    }
    by_role: dict[str, list[dict]] = {}
    problems: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            problems.append("exchange manifest contains a non-object entry")
            continue
        role = str(item.get("role") or "")
        if role in expected_roles:
            by_role.setdefault(role, []).append(dict(item))
    for role in sorted(expected_roles):
        entries = by_role.get(role, [])
        if len(entries) != 1:
            problems.append(f"{role}: expected one exchange, found {len(entries)}")
            continue
        entry = entries[0]
        if entry.get("task_id") != task.task_id:
            problems.append(f"{role}: task_id binding mismatch")
        if entry.get("task_content_hash") != task.content_hash():
            problems.append(f"{role}: task_content_hash binding mismatch")
        for field in ("prompt_sha256", "response_sha256"):
            value = str(entry.get(field) or "")
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                problems.append(f"{role}: invalid {field}")
        agents_document = getattr(provider, "agents_document", None)
        current_manifest = providers_mod.role_behavior_manifest(
            role, agents_config=agents_document
        )
        try:
            entry_schema = int(entry.get("entry_schema") or 0)
        except (TypeError, ValueError):
            entry_schema = 0
        if entry_schema >= providers_mod.SESSION_TRANSCRIPT_ENTRY_SCHEMA:
            from elt_taskgen.review.tools import critic_validators as critic_tools

            if role in critic_tools.CRITIC_VALIDATOR_ROLES:
                current_policy = critic_tools.critic_policy(
                    role,
                    critic_tools.critic_limits(
                        role, agents_config=agents_document
                    ),
                    agents_config=agents_document,
                )
            else:
                current_policy = providers_mod.session_policy_for(
                    role, agents_config=agents_document
                )
            current_tools_sha256 = current_policy.tools_sha256()
            current_policy_sha256 = current_policy.sha256()
        else:
            current_tools_sha256 = sha256_hex(
                canonical_json(current_manifest["tools"])
            )
            current_policy_sha256 = str(current_manifest["policy_sha256"])
        expected_bindings = {
            "behavior_sha256": providers_mod.role_behavior_sha256(
                role, agents_config=agents_document
            ),
            "tools_sha256": current_tools_sha256,
            "policy_sha256": current_policy_sha256,
        }
        for field, expected in expected_bindings.items():
            actual = str(entry.get(field) or "")
            if actual != expected:
                problems.append(
                    f"{role}: {field} does not match current role behavior"
                )
        if str(entry.get("diagnostics_version") or "") != str(
            providers_mod.DIAGNOSTICS_VERSION
        ):
            problems.append(
                f"{role}: diagnostics_version does not match current runtime"
            )
        row = dict(entry)
        model_calls = row.get("model_call_count")
        if "attempt_count" not in row and isinstance(model_calls, int) and not isinstance(model_calls, bool):
            # OQ-19: the alias `attempt_count := model_call_count`, one release.
            row["attempt_count"] = model_calls
        problems.extend(exchange_row_problems(row))
        role_count = sum(1 for finding in findings if finding.role.value == role)
        if entry.get("finding_count") != role_count:
            problems.append(
                f"{role}: transcript finding_count={entry.get('finding_count')!r}, "
                f"parsed={role_count}"
            )
        if entry.get("zero_findings") is not (role_count == 0):
            problems.append(f"{role}: zero_findings outcome is inconsistent")
    manifest = tuple(
        by_role[role][0] for role in sorted(expected_roles) if len(by_role.get(role, [])) == 1
    )
    return manifest, problems


def _write_review_transcript_manifest(
    engine: Engine, task: TaskIR, manifest: tuple[dict, ...], problems: list[str]
) -> None:
    """Persist the same deterministic evidence carried by ReviewPayload."""
    reports = engine.task_dir(task.task_id) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "review_transcript_manifest.json").write_text(
        readable_json(
            {
                "task_id": task.task_id,
                "task_content_hash": task.content_hash(),
                "roles": list(manifest),
                "integrity_problems": list(problems),
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _admission_failure_detail(provider, engine) -> str | None:
    """Back-compatible wrapper: the failure detail only (see `_admission_gate`)."""
    return _admission_gate(provider, getattr(engine, "workspace", None))[0]


def _structural_failure_detail(task: TaskIR) -> str | None:
    """Deterministic structural-completeness precondition on live spend.

    "Every table, key and relationship the rules or the mart reference is published
    in the source schemas" is static analysis, not judgment, so it is certified
    ABOVE the provider: a plan naming an unpublished table is refused before the
    author writes a word. Returns the failure detail naming every missing object
    and its reference site, or None. Routes to SPECIFICATION."""
    from elt_taskgen.verification import structural_completeness

    problems = structural_completeness.check_structural_completeness(task)
    if not problems:
        return None
    return (
        f"{structural_completeness.GATE_NAME}: {len(problems)} referenced "
        "object(s) absent from the published bundle — no live call was made; "
        + "; ".join(problems)
    )


def _attribution_failure_detail(engine: Engine, task: TaskIR) -> str | None:
    """Deterministic column-lineage precondition on live spend.

    The author states where each passthrough column comes from; the frozen
    reference is what actually produces gold. When they disagree, a solver that
    believes the prose cannot reproduce gold — which the dual-build gate would only
    discover one council run later. Returns the failure detail, or None, including
    when either side says nothing, which is NOT a certification. SPECIFICATION."""
    from elt_taskgen.verification import attribution

    workspace = getattr(engine, "workspace", None)
    if workspace is None:
        return None
    return attribution.check_attribution(task, workspace)


def _attack_matrix_failure_detail(engine: Engine, task: TaskIR) -> str | None:
    """Deterministic attack-matrix precondition on live spend.

    `expected_pass` is a claim about data the adapter never measured, and executing
    a mutant needs no provider (deterministic DuckDB against the gold `reference`
    froze a stage earlier), so it is checked at the earliest point the data allows
    rather than at `gates`, two live stages later. The comparison IS the gate's own
    on a subset of the cases, so it can only move a refusal EARLIER and never
    invent one. Returns the detail, or None — including when gold is absent, which
    is NOT a certification. Routes to POPULATION."""
    from elt_taskgen.verification import attack_matrix

    # Duck-typed stand-ins (the stage-logic unit tests drive these runners with no
    # workspace on disk) carry neither accessor: no measurement, never a refusal.
    workspace = getattr(engine, "workspace", None)
    task_dir = getattr(engine, "task_dir", None)
    if workspace is None or task_dir is None:
        return None
    return attack_matrix.check_declared_matrix_if_measurable(
        task, _answer_key_dir(engine, task), workspace
    )


#: sha256 of the prose an `author` PASS produced — THE ONLY RECORD of what
#: authoring last wrote, and the sole basis for telling "this prose came from me"
#: from "this prose was repaired since I last ran".
AUTHOR_PROSE_SHA_KEY = "prose_sha256"
AUTHOR_BEHAVIOR_SHA_KEY = "behavior_sha256"

#: Payload key naming WHERE that prose came from. `AUTHOR_SOURCE_PRESERVED` means
#: the stage kept prose a certified repair patch committed, and it must be STICKY:
#: otherwise a later re-attestation matches its own recorded sha, re-authors from
#: the unchanged view and quietly undoes the repair one stage later.
AUTHOR_SOURCE_KEY = "source"
AUTHOR_SOURCE_PRESERVED = "repair_patch_preserved"

#: A draft the author submitted but the deterministic prose-fidelity gate
#: rejected.  It is still the exact artifact a SPECIFICATION repair must edit;
#: dropping it leaves the proposer staring at an empty ``solver_prompt`` and
#: makes every otherwise-repairable author failure an inert round.
AUTHOR_SOURCE_CANDIDATE = "authored_candidate"

#: Provenance keys for bounded author revisions. One-shot rows omit them and
#: remain byte-identical when the session is disabled.
AUTHOR_SOURCE_REVISED = "authored_revised"
AUTHOR_REVISIONS_KEY = "revisions"
AUTHOR_DRAFTS_KEY = "drafts"
AUTHOR_SESSION_TERMINAL_KEY = "session_terminal"
AUTHOR_SESSION_SHA_KEY = "session_sha256"
AUTHOR_SESSION_REPORTS_REL = Path("reports") / "sessions"


def _author_session_block(provider) -> dict | None:
    """The author's declared `session:` block, read from the agents document
    THIS provider's routing was loaded from, or None unless the bounded
    revision is enabled — `enabled` AND `max_revisions > 0`
    (`validators.author_session_enabled`; either key alone is the rollback)
    — and the provider can run a session. Under the shipped config this is
    None and the stage calls `council.author_prose` byte for byte."""
    from elt_taskgen.review import providers as providers_mod
    from elt_taskgen.review.tools import validators as author_tools

    if not callable(getattr(provider, "run_session", None)):
        return None  # one-shot providers and lightweight doubles stay one-shot
    try:
        block = providers_mod.role_loop_limits(
            "semantic_author", agents_config=getattr(provider, "agents_config", None)
        )
    except Exception:  # noqa: BLE001 - an unreadable declaration enables nothing
        return None
    return dict(block) if author_tools.author_session_enabled(block) else None


def _persist_author_session(workspace: Path | None, task: TaskIR, record: dict) -> None:
    """Write the session record under `tasks/<id>/reports/sessions/` (the
    stage runner's job per `RoutedProvider.run_session`; `reports/` is outside
    the repair snapshot). The transcript entries and the evidence row are the
    records of authority, so a write failure loses a convenience copy only."""
    result = record.get("result")
    if workspace is None or result is None:
        return
    as_dict = getattr(result, "as_dict", None)
    precheck = record.get("precheck")
    document = {
        "role": "semantic_author",
        "session": as_dict() if callable(as_dict) else None,
        "revisions": int(record.get("revisions", 0) or 0),
        "drafts": int(record.get("drafts", 0) or 0),
        "terminal": str(record.get("terminal", "") or ""),
        "contamination_precheck": (
            precheck.model_dump(mode="json") if hasattr(precheck, "model_dump") else None
        ),
    }
    digest = str(getattr(result, "session_sha256", "") or "")[:12] or "session"
    target = Path(workspace) / "tasks" / task.task_id / AUTHOR_SESSION_REPORTS_REL
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / f"semantic_author.{digest}.json").write_text(
            json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return


def _development_snapshot_for(engine: Engine, task: TaskIR) -> str:
    """Example DEVELOPMENT rows for the author's view, or '' when unavailable.

    Two tasks of one library shape were described in the same sentences while
    the author saw no data; those repeats then queued as borderline collisions
    between our own tasks (batch50, 2026-09-19). Duck-typed stand-ins without a
    task directory, and stages that run before `generate`, simply get no rows.
    """
    from elt_taskgen.generation import dataset_context

    task_dir = getattr(engine, "task_dir", None)
    if not callable(task_dir):
        return ""
    try:
        return dataset_context.development_snapshot(
            task, Path(task_dir(task.task_id)) / "populations"
        )
    except OSError:
        return ""


def _author_prose_for(engine: Engine, task: TaskIR, provider) -> tuple[str, dict[str, str]]:
    """The author's prose and the ledger data its provenance adds: the
    unchanged `council.author_prose` (no data) unless the bounded revision
    session is enabled, then `council.author_prose_session` under the author
    tools and policy (`validators.author_policy`), the workspace's
    contamination index armed for the final-draft precheck, and the session
    record persisted beside the task's reports."""
    from elt_taskgen.review import council
    from elt_taskgen.review.tools import validators as author_tools

    snapshot = _development_snapshot_for(engine, task)
    block = _author_session_block(provider)
    if block is None:
        return council.author_prose(task, provider, dataset_snapshot=snapshot), {}
# Salt reruns from the current ledger count so each gets a distinct transcript.
    counter = getattr(engine, "session_limit_reruns", None)
    salt = int(counter(task.task_id, "author")) if callable(counter) else 0
    policy = author_tools.author_policy(author_tools.author_limits(block), session_salt=salt)
    workspace = getattr(engine, "workspace", None)
    index = None
    if workspace is not None:
        index = _contamination_index(engine)
        _arm_contamination_index(index, announce=False)
    record: dict = {}
    try:
        prose = council.author_prose_session(
            task,
            provider,
            tools=policy.tools,
            policy=policy,
            contamination_index=index,
            coverage_requirement=_required_firewall_coverage(),
            record=record,
            dataset_snapshot=snapshot,
        )
    except council.AuthorSessionLimitStop as exc:
# Persist capped sessions and return a salted BLOCKED result without a repair round.
        _persist_author_session(workspace, task, exc.record)
        raise
    _persist_author_session(workspace, task, record)
    data = {
        AUTHOR_SOURCE_KEY: AUTHOR_SOURCE_REVISED,
        AUTHOR_REVISIONS_KEY: str(int(record.get("revisions", 0) or 0)),
        AUTHOR_DRAFTS_KEY: str(int(record.get("drafts", 0) or 0)),
        AUTHOR_SESSION_TERMINAL_KEY: str(record.get("terminal", "") or ""),
    }
    digest = str(getattr(record.get("result"), "session_sha256", "") or "")
    if digest:
        data[AUTHOR_SESSION_SHA_KEY] = digest
    return prose, data


def _author_answered_current_state(engine: Engine, task_id: str) -> bool:
    """True iff the LATEST `author` ledger row is a pass.

    After `repair.apply_repair` the latest row is the invalidation FAIL it appended,
    so False means "the outstanding repair has not been answered yet". No ledger, no
    proof anything answered anything — fail closed to False."""
    latest = getattr(engine, "latest_report", None)
    if latest is None:
        return False
    row = latest(task_id, "author")
    return row is not None and row.verdict == VERDICT_PASS


def _last_author_prose_record(engine: Engine, task_id: str) -> tuple[str | None, str]:
    """Return the latest authored prose digest and source, excluding admin rows."""
    latest = getattr(engine, "latest_report", None)
    by_verdict = getattr(engine, "latest_report_with_verdict", None)
    if latest is None or by_verdict is None:
        return None, ""

    history = getattr(engine, "report_history", None)
    if callable(history):
        rows = list(history(task_id, "author"))
    else:
        # Older engines/doubles have no history reader.  Inspect the latest row
        # first (it may itself be an authored candidate), then the most recent
        # PASS as the compatibility fallback.  De-duplication is by object id
        # because minimal doubles need not carry a ledger ``id`` attribute.
        rows = []
        current = latest(task_id, "author")
        passing = by_verdict(task_id, "author", VERDICT_PASS)
        for row in (current, passing):
            if row is not None and all(row is not seen for seen in rows):
                rows.append(row)

    for row in rows:
        try:
            payload = json.loads(row.payload_json)
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            continue
        value = data.get(AUTHOR_PROSE_SHA_KEY)
        source = data.get(AUTHOR_SOURCE_KEY)
        source = str(source) if isinstance(source, str) else ""
        if not isinstance(value, str) or not value:
            continue
        # A FAIL is authored provenance only when the author runner explicitly
        # labels it.  This excludes repair invalidations, proposer adjudication
        # rows and arbitrary stage failures even if a future payload happens to
        # grow a similarly named digest.  PASS rows predate ``source`` and keep
        # their compatibility semantics.
        if row.verdict == VERDICT_FAIL and source not in {
            AUTHOR_SOURCE_CANDIDATE,
            AUTHOR_SOURCE_REVISED,
        }:
            continue
        if row.verdict not in (VERDICT_PASS, VERDICT_FAIL):
            continue
        return value, source
    return None, ""


def make_author_runner(provider):
    """Build semantic prose, then require deterministic fidelity to the TaskIR.

    Preserve repaired prose. Reject unchanged prose after an unresolved
    specification repair.
    """

    def run_author(engine: Engine, task: TaskIR) -> StageOutcome:
        from elt_taskgen.review import council, prose_fidelity
        from elt_taskgen.review import providers as providers_mod

        author_behavior = {
            AUTHOR_BEHAVIOR_SHA_KEY: providers_mod.role_behavior_sha256(
                "semantic_author",
                agents_config=getattr(provider, "agents_document", None),
            )
        }

        # FIRST, before the provider is consulted at all: a task referencing a
        # table, column, key or relationship the published bundle does not contain
        # cannot be authored into a solvable spec, and code certifies that.
        incomplete = _structural_failure_detail(task)
        if incomplete is not None:
            return StageOutcome(
                VERDICT_FAIL,
                StagePayload(error=incomplete),
                route=RepairRoute.SPECIFICATION,
            )

        # Verify required mutants before provider spend.
        unreproducible = _attack_matrix_failure_detail(engine, task)
        if unreproducible is not None:
            return StageOutcome(
                VERDICT_FAIL,
                StagePayload(error=unreproducible),
                route=RepairRoute.POPULATION,
            )

        # Same admission gate as the review stage: no live authoring spend
        # before the council is proven effective (replay-only is exempt).
        not_admitted, admission = _admission_gate(
            provider, getattr(engine, "workspace", None)
        )
        if not_admitted is not None:
            return StageOutcome(
                VERDICT_FAIL,
                StagePayload(error=not_admitted, infrastructure="not_admitted"),
            )
        _stamp_admission(provider, admission)
        _begin_task_evidence(provider, task)

        stored = task.solver_prompt or ""
        last_sha, last_source = _last_author_prose_record(engine, task.task_id)
        stored_sha = hashlib.sha256(stored.encode("utf-8")).hexdigest()
        # (A) The prose moved since authoring last wrote it => a certified
        # SPECIFICATION patch committed. Do not call the provider: the only
        # completion it can return is the one the patch replaced. And once
        # preserved, STAY preserved while the bytes are unchanged.
        repaired_externally = (
            bool(stored)
            and last_sha is not None
            and (last_sha != stored_sha or last_source == AUTHOR_SOURCE_PRESERVED)
        )
        if repaired_externally:
            problems = prose_fidelity.check_prose_fidelity(task)
            if problems:
                return StageOutcome(
                    VERDICT_FAIL,
                    StagePayload(
                        error=(
                            f"prose fidelity: {len(problems)} MartSpec item(s) "
                            "not represented in the REPAIRED prose — "
                            + "; ".join(problems)
                        ),
                        data={"gate": prose_fidelity.GATE_NAME, **author_behavior},
                    ),
                    route=RepairRoute.SPECIFICATION,
                )
            return StageOutcome(
                VERDICT_PASS,
                StagePayload(
                    detail=(
                        f"repaired solver prose PRESERVED ({len(stored)} "
                        f"chars, sha {stored_sha[:12]}): "
                        + (
                            "still the repaired text this stage preserved "
                            "before"
                            if last_sha == stored_sha
                            else "it was patched since this stage last "
                            f"authored (recorded {last_sha[:12]})"
                        )
                        + "; re-authoring would revert it, because the author "
                        "view does not contain solver_prompt. Fidelity gate "
                        "green"
                    ),
                    data={
                        AUTHOR_PROSE_SHA_KEY: stored_sha,
                        AUTHOR_SOURCE_KEY: AUTHOR_SOURCE_PRESERVED,
                        **author_behavior,
                        **admission,
                    },
                ),
            )

        # Use bounded revisions only when enabled; otherwise keep one-shot rows
        # unchanged. A cap with no draft blocks the seat without starting a round.
        try:
            prose, session_data = _author_prose_for(engine, task, provider)
        except council.AuthorSessionLimitStop as exc:
            return council.author_session_limit_outcome(exc, engine, task)
        updated = task.model_copy(update={"solver_prompt": prose})

        prose_sha = hashlib.sha256(prose.encode("utf-8")).hexdigest()
        problems = prose_fidelity.check_prose_fidelity(updated)
        if problems:
            return StageOutcome(
                VERDICT_FAIL,
                StagePayload(
                    error=(
                        f"prose fidelity: {len(problems)} MartSpec item(s) "
                        "not represented in the authored prose — "
                        + "; ".join(problems)
                    ),
                    data={
                        "gate": prose_fidelity.GATE_NAME,
                        AUTHOR_PROSE_SHA_KEY: prose_sha,
                        AUTHOR_SOURCE_KEY: AUTHOR_SOURCE_CANDIDATE,
                        **author_behavior,
                        # A bounded-session candidate retains the more precise
                        # ``authored_revised`` source and its trajectory fields;
                        # the one-shot rollback path uses authored_candidate.
                        **session_data,
                    },
                ),
                route=RepairRoute.SPECIFICATION,
                # A red draft is not accepted prose, but it is the exact
                # specification artifact the repair route must see and edit.
                # Engine persists an outcome task before invoking the proposer,
                # binding this candidate and its provenance row to one hash.
                task=updated,
            )

        if prose == stored:
            # (B) Unchanged prose is only honest OUTSIDE an UNANSWERED repair;
            # `_author_answered_current_state` keeps an ordinary re-attestation from
            # being read as the repair's first, empty answer.
            spec_repair = _pending_specification_repair(task)
            if spec_repair is not None and not _author_answered_current_state(
                engine, task.task_id
            ):
                return StageOutcome(
                    VERDICT_FAIL,
                    StagePayload(
                        error=(
                            "INERT SPECIFICATION REPAIR: revision "
                            f"{spec_repair.revision} was a specification repair "
                            f"({spec_repair.reason!r}) whose whole purpose is "
                            "new solver prose, and re-authoring produced "
                            f"BYTE-IDENTICAL prose (sha {prose_sha[:12]}). "
                            "Nothing patched task_ir.json, and the author view "
                            "is a pure function of an IR the repair did not "
                            "touch, so the rendered view — and therefore the "
                            "transcript key and the provider's answer — could "
                            "not move. Failing on this round rather than "
                            "recording 'solver prose unchanged' as a PASS and "
                            "handing the same prose back to the same council: "
                            "the defect is in the repair path, not the prose."
                        ),
                        data={
                            AUTHOR_PROSE_SHA_KEY: prose_sha,
                            "repair_route": spec_repair.route.value,
                            "repair_revision": str(spec_repair.revision),
                            **author_behavior,
                        },
                    ),
                    # EXPLICIT route, never keyword-guessed ("rendered view" would
                    # read as RUNTIME evidence). The engine's inert-repair rule then
                    # rejects on the unchanged fingerprint — ONE round, not three.
                    route=RepairRoute.SPECIFICATION,
                )
            return StageOutcome(
                VERDICT_PASS,
                StagePayload(
                    detail="solver prose unchanged; fidelity gate green",
                    data={
                        AUTHOR_PROSE_SHA_KEY: prose_sha,
                        **author_behavior,
                        **admission,
                        **session_data,
                    },
                ),
            )
        return StageOutcome(
            VERDICT_PASS,
            StagePayload(
                detail=f"solver prose authored ({len(prose)} chars); fidelity "
                "gate green; content hash moves, all stages re-attest",
                data={
                    AUTHOR_PROSE_SHA_KEY: prose_sha,
                    **author_behavior,
                    **admission,
                    **session_data,
                },
            ),
            task=updated,
        )

    return run_author


def _pending_specification_repair(task: TaskIR):
    """The task's current revision when it is a SPECIFICATION repair, else None.

    Read off `TaskIR.revisions`, so it survives a crash, a resume and a trial copy
    with no ledger query. Revision 1 is the intake anchor, never a repair."""
    if not task.revisions:
        return None
    entry = task.revisions[-1]
    return entry if entry.route is RepairRoute.SPECIFICATION else None


def make_review_runner(provider):
    """`review`: council review. Findings only; the council can block (fatal
    findings fail the stage) but has no acceptance vocabulary."""

    def run_review(engine: Engine, task: TaskIR) -> StageOutcome:
        from elt_taskgen.models import CouncilRole
        from elt_taskgen.review import council

        # BEFORE THE FOUR CRITICS ARE PAID: the structural half of the feasibility
        # question is certified by code, not by a seat — a plan naming an object the
        # published schemas lack is refused here, with the reference site named.
        incomplete = _structural_failure_detail(task)
        if incomplete is not None:
            return StageOutcome(
                VERDICT_FAIL,
                ReviewPayload(findings=(), fatal_count=0, detail=incomplete),
                route=RepairRoute.SPECIFICATION,
            )

        # Independently verify required mutants before paying critics.
        unreproducible = _attack_matrix_failure_detail(engine, task)
        if unreproducible is not None:
            return StageOutcome(
                VERDICT_FAIL,
                ReviewPayload(findings=(), fatal_count=0, detail=unreproducible),
                route=RepairRoute.POPULATION,
            )

        # Reject prose attribution that contradicts the frozen reference before spend.
        misattributed = _attribution_failure_detail(engine, task)
        if misattributed is not None:
            return StageOutcome(
                VERDICT_FAIL,
                ReviewPayload(findings=(), fatal_count=0, detail=misattributed),
                route=RepairRoute.SPECIFICATION,
            )

        not_admitted, admission = _admission_gate(
            provider, getattr(engine, "workspace", None)
        )
        if not_admitted is not None:
            return StageOutcome(
                VERDICT_FAIL,
                ReviewPayload(findings=(), fatal_count=0, detail=not_admitted),
                # STRUCTURED, because the detail text must not be keyword-
                # scanned: a council finding is free prose and may say anything.
                infrastructure="not_admitted",
            )
        _stamp_admission(provider, admission)
        manifest_required = _begin_task_evidence(provider, task)

        try:
            findings = council.run_council(task, provider)
            # The same protocol boundary protects one-shot and harness-validated
            # critics. It runs before a finding can be mistaken for a task defect.
            validated_findings = _validated_executable_findings(task, findings)
        except council.ProviderProtocolError as exc:
            return _critic_protocol_block(
                f"critic protocol failure: {type(exc).__name__}: {exc}"
            )
        fatal = [f for f in findings if f.severity is Severity.FATAL]
        transcript_manifest, manifest_problems = _validated_review_manifest(
            provider, task, findings, required=manifest_required
        )
        if manifest_required:
            _write_review_transcript_manifest(
                engine, task, transcript_manifest, manifest_problems
            )
        payload = ReviewPayload(
            findings=tuple(findings),
            fatal_count=len(fatal),
            detail=f"{len(findings)} finding(s), {len(fatal)} fatal",
            admission=admission,
            transcript_manifest=transcript_manifest,
        )
        if manifest_problems:
            return _critic_protocol_block(
                payload.detail
                + "; transcript manifest integrity failure: "
                + "; ".join(manifest_problems),
                failure_code=CRITIC_MANIFEST_FAILURE_CODE,
                recovery_prerequisite=CRITIC_MANIFEST_RECOVERY,
            )
        # The screened list: a voided finding (withdrawn by its provider's
        # disposition, or a proposal against a rule no mart declares) is INFO
        # and holds nothing.
        pending_adjudication = _critic_adjudication_block(validated_findings)
        if pending_adjudication is not None:
            return pending_adjudication
        if fatal:
            # A prose leak is repairable by rewriting the prose (specification
            # route); the bounded repair budget rejects a non-converging task.
            return StageOutcome(
                VERDICT_FAIL, payload, route=RepairRoute.SPECIFICATION
            )
        # Diligence requires a validated executable probe; prose and bare
        # suggestions are not evidence. Use the shared predicate everywhere.
        shortcut = [
            finding
            for finding in validated_findings
            if finding.role is CouncilRole.SHORTCUT_ATTACKER
        ]
        probes = [finding for finding in shortcut if council.is_executable_probe(finding)]
        if not probes:
            diligence_detail = (
                "diligence protocol failure: shortcut_attacker returned "
                f"{len(shortcut)} finding(s) and zero executable probes "
                "(requires severity minor or higher plus a matching validated "
                "suggested_attack/proposed_case) — no evidence the shortcut "
                "surface was examined"
            )
            if _attacker_corrected_in_session(transcript_manifest):
                # A corrected session that still lacks a probe is a repairable
                # seat outcome. Fail on its route so review can rerun fresh.
                return StageOutcome(
                    VERDICT_FAIL,
                    payload.model_copy(
                        update={
                            "detail": (
                                payload.detail + "; " + diligence_detail
                                + "; the attacker session was corrected in-session "
                                "and still handed off no executable probe"
                            )
                        }
                    ),
                    route=RepairRoute.SPECIFICATION,
                )
            return _critic_protocol_block(
                diligence_detail,
                failure_code=CRITIC_DILIGENCE_FAILURE_CODE,
                recovery_prerequisite=CRITIC_DILIGENCE_RECOVERY,
            )
        return StageOutcome(VERDICT_PASS, payload)

    return run_review


def _attacker_corrected_in_session(transcript_manifest) -> bool:
    """Whether the shortcut attacker's exchange row is a harness-validated
    SESSION row (entry schema 3) on which the seat was corrected: a compile
    correction was issued (`correction_kinds.compile`), or the accepted
    payload was still red at submit and voided post-session
    (`submitted_with_red_validators`).  A one-shot row, a duck-typed
    provider without evidence, or a session that was never corrected says
    False — the diligence failure then stays the protocol block it was."""
    for row in tuple(transcript_manifest or ()):
        if not isinstance(row, dict) or str(row.get("role") or "") != "shortcut_attacker":
            continue
        try:
            if int(row.get("entry_schema") or 0) < 3:
                return False
            kinds = row.get("correction_kinds")
            compile_corrections = (
                int(kinds.get("compile") or 0) if isinstance(kinds, dict) else 0
            )
            red_at_submit = int(row.get("submitted_with_red_validators") or 0)
        except (TypeError, ValueError):
            return False
        return compile_corrections > 0 or red_at_submit > 0
    return False


def _recorded_variant_rewards(
    engine: Engine, task: TaskIR, rewards: dict[str, dict[str, float]]
) -> dict[str, dict[str, dict[str, float]]]:
    """variant -> case -> population -> reward, read back from attacks/*/rewards.json.

    Reading back (never re-scoring) keeps the composite diagnostic and both unit
    rewards on ONE execution. A record whose `task_content_hash` is not the current
    one is DROPPED, so the batteries see MISSING evidence rather than stale."""
    current = task.content_hash()
    out: dict[str, dict[str, dict[str, float]]] = {}
    attacks_root = engine.task_dir(task.task_id) / "attacks"
    for case_name in sorted(rewards):
        path = attacks_root / case_name / "rewards.json"
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if record.get("task_content_hash") != current:
            continue
        by_variant = record.get("rewards_by_variant")
        if not isinstance(by_variant, dict):
            continue
        # Transpose to the shape the batteries consume: they ask for ONE
        # variant's whole attack matrix, so the variant is the outer key.
        for variant_value, per_population in by_variant.items():
            if isinstance(per_population, dict):
                out.setdefault(str(variant_value), {})[case_name] = {
                    str(pop): float(value)
                    for pop, value in per_population.items()
                }
    return out


_ATTACK_FINDING_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{0,127}")
_MAX_ATTACK_FINDING_IDENTIFIERS = 16


def _confirmed_live_exploit(finding: Finding, outcomes_by_id: dict[str, object]) -> bool:
    """Whether measurement proved this finding's wrong program passes everywhere."""
    outcome = outcomes_by_id.get(finding.finding_id)
    return bool(
        outcome is not None
        and not outcome.promoted
        and outcome.measured_pass
        and all(outcome.measured_pass.values())
    )


def _selected_blocking_finding(findings: list[Finding], outcomes=()) -> Finding | None:
    """Select exactly one repair subject without consulting model-authored prose.

    A measured live exploit has priority because it is the strongest artifact
    and retains the historical POPULATION rule.  Otherwise the finding id that
    the harness assigned is the stable order; role and severity are tie-breakers
    only for malformed hand-built inputs with duplicate ids.
    """
    by_id = {outcome.finding_id: outcome for outcome in outcomes}
    ordered = sorted(
        findings,
        key=lambda finding: (
            finding.finding_id,
            finding.role.value,
            finding.severity.value,
        ),
    )
    measured = [
        finding for finding in ordered if _confirmed_live_exploit(finding, by_id)
    ]
    if measured:
        return measured[0]
    return ordered[0] if ordered else None


def _attack_finding_descriptor(task: TaskIR, finding: Finding) -> AttackFindingDescriptor:
    """Project one finding to role/severity plus public identifiers in its text.

    Token extraction sees only ``summary`` and ``detail``.  Exact membership in
    ``PublicIdentifierSet`` removes hidden population names, literal values,
    paths and arbitrary prose.  Sorting/deduplication and the fixed cap make the
    projection canonical and keep the repair evidence bounded.
    """
    from elt_taskgen.review.tools.projection import PublicIdentifierSet

    public = PublicIdentifierSet(task)
    text = f"{finding.summary}\n{finding.detail}"
    identifiers = tuple(
        sorted(
            {
                token
                for token in _ATTACK_FINDING_TOKEN_RE.findall(text)
                if token in public
            }
        )[:_MAX_ATTACK_FINDING_IDENTIFIERS]
    )
    return AttackFindingDescriptor(
        role=finding.role,
        severity=finding.severity,
        identifiers=identifiers,
    )


def _proposal_failure_route(findings: list[Finding], outcomes=()) -> RepairRoute:
    """Route blocking handoff failures from trusted role and promoter evidence."""
    outcomes = tuple(outcomes)
    selected = _selected_blocking_finding(findings, outcomes)
    if selected is None:
        return RepairRoute.SPECIFICATION
    by_id = {outcome.finding_id: outcome for outcome in outcomes}
    if _confirmed_live_exploit(selected, by_id):
        return RepairRoute.POPULATION
    if selected.role is CouncilRole.POPULATION_ADVERSARY:
        return RepairRoute.POPULATION
    return RepairRoute.SPECIFICATION


_SCHEMA_EQUIVALENT_SOURCE_DEDUP_PROOF = (
    "source_distinct_over_declared_primary_key_v1"
)
#: ``zero_is_missing`` is an identity when every touched default is zero, so it
#: cannot be treated as a live exploit for that task.
_SCHEMA_EQUIVALENT_ZERO_DEFAULT_PROOF = "zero_is_missing_over_zero_defaults_v1"


def _zero_default_equivalence_proof(task: TaskIR, finding: Finding, outcome, attacks_mod) -> dict[str, object] | None:
    """Prove every realized ``zero_is_missing`` rewrite is a zero-default identity."""
    import sqlglot
    from sqlglot import exp

    proposal = finding.proposed_case
    if not _names_zero_is_missing(proposal):
        return None
    if not _measured_all_true_with_fidelity(outcome):
        return None
    compiler = outcome.fidelity.get("compiler")
    if not isinstance(compiler, dict) or compiler.get("passed") is not True:
        return None
    targets = _zero_is_missing_realized_targets(compiler.get("realized"))
    if targets is None:
        return None
    marts = {mart.name: mart for mart in task.marts}
    sites = 0
    for name in targets:
        mart = marts.get(str(name))
        gold_sql = dict(task.reference.sql_by_mart).get(str(name))
        if mart is None or not gold_sql:
            return None
        try:
            mutated = attacks_mod._apply_kind(
                AttackKind.NO_NULL_DEFAULT, gold_sql, mart, "zero_is_missing"
            )
        except Exception:  # noqa: BLE001 - a compiler error is not a proof
            return None
        if not mutated:
            return None
        try:
            ast = sqlglot.parse_one(mutated, read="duckdb")
            gold_ast = sqlglot.parse_one(gold_sql, read="duckdb")
        except Exception:  # noqa: BLE001
            return None
        # The gold's OWN NULLIFs (a divisor guard such as `x / NULLIF(n, 0)`)
        # are not sites the variant introduced; they must survive unchanged.
        gold_nullifs = [node.sql(dialect="duckdb") for node in gold_ast.find_all(exp.Nullif)]
        for nullif in ast.find_all(exp.Nullif):
            parent = nullif.parent
            zero = nullif.expression
            if not (isinstance(parent, exp.Coalesce) and parent.this is nullif):
                if nullif.sql(dialect="duckdb") in gold_nullifs:
                    gold_nullifs.remove(nullif.sql(dialect="duckdb"))
                    continue
                return None
            if not (
                isinstance(zero, exp.Literal)
                and not zero.is_string
                and float(zero.name) == 0.0
            ):
                return None
            defaults = list(parent.expressions)
            if len(defaults) != 1:
                return None
            default = defaults[0]
            if not (isinstance(default, exp.Literal) and not default.is_string):
                return None
            try:
                if float(default.name) != 0.0:
                    return None
            except ValueError:
                return None
            sites += 1
        if gold_nullifs:
            # A gold NULLIF the mutant no longer carries: not this variant.
            return None
    if sites == 0:
        return None
    return {
        "proved": True,
        "proof": _SCHEMA_EQUIVALENT_ZERO_DEFAULT_PROOF,
        "operation": "zero_is_missing",
        "form": (
            "variant"
            if "variant" in dict(proposal.params)
            else "operation_key"
        ),
        "marts": [str(name) for name in targets],
        "sites": sites,
    }


def _names_zero_is_missing(proposal) -> bool:
    """Recognize either schema spelling of only the ``zero_is_missing`` rewrite."""
    if proposal is None:
        return False
    params = dict(proposal.params)
    if params.get("zero_is_missing") is True:
        # The compiler admits the operation key under `no_null_default` or
        # `custom` and materializes both through the same rewrite.
        if proposal.kind not in (AttackKind.NO_NULL_DEFAULT, AttackKind.CUSTOM):
            return False
        return not (set(params) - {"zero_is_missing"})
    if params.get("variant") == "zero_is_missing":
        if proposal.kind is not AttackKind.NO_NULL_DEFAULT:
            return False
        return not (set(params) - {"variant"})
    return False


def _zero_is_missing_realized_targets(realized) -> list[str] | None:
    """The marts the compiler's fidelity record says the `zero_is_missing`
    rewrite changed, read from either record shape the compiler emits for
    it: `{"operation": "zero_is_missing", "marts": [...]}` for the
    operation-key proposal and `{"operation": "kind", "kind":
    "no_null_default", "variant": "zero_is_missing", "target_marts": [...]}`
    for the variant proposal. None when the record is not this rewrite or
    names no mart."""
    if not isinstance(realized, dict):
        return None
    if realized.get("operation") == "zero_is_missing":
        targets = realized.get("marts")
    elif (
        realized.get("operation") == "kind"
        and realized.get("kind") == AttackKind.NO_NULL_DEFAULT.value
        and realized.get("variant") == "zero_is_missing"
    ):
        targets = realized.get("target_marts")
    else:
        return None
    if not isinstance(targets, list) or not targets:
        return None
    if not all(isinstance(target, str) and target for target in targets):
        return None
    return [str(target) for target in targets]


#: Dropping the frame is an identity for MAX/DESC or MIN/ASC over the same
#: column, so those cases are not live exploits.
_SCHEMA_EQUIVALENT_DROP_FRAME_PROOF = "drop_frame_over_self_ordered_extremes_v1"


def _names_drop_frame(proposal) -> bool:
    if proposal is None or proposal.kind is not AttackKind.WRONG_WINDOW:
        return False
    params = dict(proposal.params)
    if str(params.get("variant", "") or "") != "drop_frame":
        return False
    return not (set(params) - {"variant"})


def _drop_frame_equivalence_proof(task: TaskIR, finding: Finding, outcome, attacks_mod) -> dict[str, object] | None:
    """Prove `drop_frame` is an identity on this task's gold: every framed
    window the variant touches is MAX(c) ordered by c DESC or MIN(c) ordered
    by c ASC, with that one ordering key. Anything else fails closed."""
    import sqlglot
    from sqlglot import exp

    if not _names_drop_frame(finding.proposed_case):
        return None
    if not _measured_all_true_with_fidelity(outcome):
        return None
    compiler = outcome.fidelity.get("compiler")
    if not isinstance(compiler, dict) or compiler.get("passed") is not True:
        return None
    realized = compiler.get("realized")
    if not isinstance(realized, dict) or realized.get("variant") != "drop_frame":
        return None
    targets = realized.get("target_marts")
    if not isinstance(targets, list) or not targets:
        return None
    sites = 0
    for name in targets:
        gold_sql = dict(task.reference.sql_by_mart).get(str(name))
        if not gold_sql:
            return None
        try:
            ast = sqlglot.parse_one(gold_sql, read="duckdb")
        except Exception:  # noqa: BLE001
            return None
        for window in ast.find_all(exp.Window):
            if window.args.get("spec") is None:
                continue
            func = window.this
            if not isinstance(func, (exp.Max, exp.Min)):
                return None
            column = func.this
            if not isinstance(column, exp.Column):
                return None
            order = window.args.get("order")
            keys = list(order.expressions) if order is not None else []
            if len(keys) != 1 or not isinstance(keys[0], exp.Ordered):
                return None
            key = keys[0]
            if not (isinstance(key.this, exp.Column) and key.this.name == column.name):
                return None
            descending = bool(key.args.get("desc"))
            if isinstance(func, exp.Max) and not descending:
                return None
            if isinstance(func, exp.Min) and descending:
                return None
            sites += 1
    if sites == 0:
        return None
    return {
        "proved": True,
        "proof": _SCHEMA_EQUIVALENT_DROP_FRAME_PROOF,
        "operation": "drop_frame",
        "marts": [str(name) for name in targets],
        "sites": sites,
    }


def _drop_frame_identity_accepted(proposal, outcome) -> bool:
    """The read-side check of `_drop_frame_equivalence_proof`'s record."""
    if not _names_drop_frame(proposal):
        return False
    proof = outcome.fidelity.get("schema_equivalence")
    if not isinstance(proof, dict):
        return False
    if (
        proof.get("proved") is not True
        or proof.get("proof") != _SCHEMA_EQUIVALENT_DROP_FRAME_PROOF
        or proof.get("operation") != "drop_frame"
    ):
        return False
    sites = proof.get("sites")
    marts = proof.get("marts")
    if not isinstance(sites, int) or sites < 1:
        return False
    if not isinstance(marts, list) or not marts or not all(isinstance(m, str) and m for m in marts):
        return False
    return _measured_all_true_with_fidelity(outcome)


def _zero_default_identity_accepted(proposal, outcome) -> bool:
    """The read-side check of `_zero_default_equivalence_proof`'s record."""
    if not _names_zero_is_missing(proposal):
        return False
    proof = outcome.fidelity.get("schema_equivalence")
    if not isinstance(proof, dict):
        return False
    if (
        proof.get("proved") is not True
        or proof.get("proof") != _SCHEMA_EQUIVALENT_ZERO_DEFAULT_PROOF
        or proof.get("operation") != "zero_is_missing"
    ):
        return False
    sites = proof.get("sites")
    marts = proof.get("marts")
    if not isinstance(sites, int) or sites < 1:
        return False
    if not isinstance(marts, list) or not marts or not all(isinstance(m, str) and m for m in marts):
        return False
    return _measured_all_true_with_fidelity(outcome)


def _validated_executable_findings(
    task: TaskIR, findings: list[Finding]
) -> list[Finding]:
    """Return compiler-valid executable findings or a protocol failure.

    Malformed executable content blocks as provider protocol. Missing major or
    fatal proposals remain unresolved claims for bounded repair; bare prose
    observations remain reviewable.
    """
    from elt_taskgen.review.council import ProviderProtocolError
    from elt_taskgen.verification import attacks as attacks_mod

    # Preserve the provider's exact kind and parameters.  The closed compiler
    # either accepts that handoff as written or reports a protocol failure; it
    # never rewrites a kind merely to reach a compatible implementation.
    from elt_taskgen.review.council import _void

    executable = list(findings)
    problems: list[str] = []
    # Malformed output after an available compile correction is a protocol
    # failure. One-shot output is voided while unresolved major claims still repair.
    voided: dict[int, Finding] = {}

    def _refuse(index: int, finding: Finding, detail: str) -> None:
        if _seat_has_compile_channel(finding.role):
            problems.append(detail)
            return
        voided[index] = _void(
            finding,
            [UNCOMPILABLE_NO_CHANNEL_SIGNAL],
            "proposal withheld: this seat runs no compile validator, so it "
            "was never told its executable content was malformed",
        )

    for index, finding in enumerate(executable):
        claimed_severity, claimed_attack, claimed_proposal = (
            _claimed_critic_handoff(finding)
        )
        if (
            claimed_severity in {Severity.MAJOR, Severity.FATAL}
            and claimed_proposal is not None
            and _proposal_targets_a_rule_the_task_never_states(task, claimed_proposal)
        ):
            voided[index] = _void(
                finding,
                [MUTATION_TARGETS_NO_DECLARED_RULE_SIGNAL],
                "the proposed mutation removes a rule no mart of this task "
                "declares, so omitting it is not a wrong implementation and "
                "there is no fork; the claim is kept on record and does not block",
            )
            continue
        if (
            claimed_severity in {Severity.MAJOR, Severity.FATAL}
            and claimed_proposal is not None
            and _proposal_names_a_hop_the_task_never_joins(task, claimed_proposal)
        ):
            voided[index] = _void(
                finding,
                [MUTATION_NAMES_NO_SECOND_HOP_SIGNAL],
                "the proposed mutation flips a second source-table join that no "
                "mart of this task has, so it realizes no wrong implementation; "
                "the claim is kept on record and does not block",
            )
            continue
        if (
            claimed_severity in {Severity.MAJOR, Severity.FATAL}
            and claimed_proposal is None
            and finding.withdrawn
        ):
            # Only the provider's structured disposition withdraws a finding,
            # and only its own. A withdrawal never withholds an executable
            # proposal here: a finding that carries one is measured as usual.
            voided[index] = _void(
                finding,
                [WITHDRAWN_BY_DISPOSITION_SIGNAL],
                "the providing critic filed this finding with disposition "
                "'withdrawn'; the claim is kept on record and does not block",
            )
            continue
        claimed = finding.model_copy(
            update={
                "severity": claimed_severity,
                "suggested_attack": claimed_attack,
                "proposed_case": claimed_proposal,
                "screen": None,
            }
        )
        if claimed_severity is Severity.INFO:
            if claimed_proposal is not None:
                _refuse(
                    index,
                    finding,
                    f"{finding.finding_id}: informational finding carries a "
                    "proposed_case even though informational findings are not "
                    "executable",
                )
            continue

        proposal = claimed_proposal
        if proposal is None:
            if claimed_attack is None:
                # A bare observation: reviewed as prose, and at MAJOR an
                # unresolved claim the repair route answers.
                continue
            if claimed_severity in {Severity.MAJOR, Severity.FATAL}:
                # An unresolved major claim naming a coarse attack: routed
                # to repair by `_blocking_proposal_failures`, never guessed
                # into a probe and never a protocol block.
                continue
            _refuse(
                index,
                finding,
                f"{finding.finding_id}: actionable {claimed_severity.value} "
                "finding names an attack but has no proposed_case",
            )
            continue
        if set(proposal.expected_pass_by_stage) != set(RLVR_TASK_VARIANTS):
            _refuse(
                index,
                finding,
                f"{finding.finding_id}: proposed_case has no complete exact "
                "extract_load and transform prediction matrix",
            )
            continue
        try:
            _case, fidelity = attacks_mod.validate_proposed_case(
                task, claimed, proposal, required=False
            )
        except ValueError as exc:
            _refuse(
                index,
                finding,
                f"{finding.finding_id}: proposed_case is not executable "
                f"({type(exc).__name__}: {exc})",
            )
            continue
        if not bool(fidelity.get("passed")):
            errors = tuple(str(value) for value in fidelity.get("errors", ()))
            _refuse(
                index,
                finding,
                f"{finding.finding_id}: proposed_case failed claim fidelity"
                + (f" ({'; '.join(errors)})" if errors else ""),
            )
    if problems:
        raise ProviderProtocolError(
            "critic-to-attack protocol failure: " + "; ".join(problems)
        )
    for index, replacement in voided.items():
        executable[index] = replacement
    return executable


def _claim_suffix(finding) -> str:
    """Return value-free critic prose needed to describe a blocking claim."""
    claim = f"{(finding.summary or '').strip()} {(finding.detail or '').strip()}".strip()
    if not claim:
        return ""
    # Apply the repair view's leak screen here so unsafe claim details are
    # removed before they can halt the entire repair.
    from elt_taskgen.review.tools import projection as projection_mod

    # Redact refused value shapes but retain the actionable claim text.
    claim = _redact_claim_shapes(claim, projection_mod)
    detector = getattr(projection_mod, "_detect_value_shapes", None)
    if detector is not None and detector(claim) is not None:
        return ""
    if projection_mod._MEASURED_VALUE_RE.search(claim):  # noqa: SLF001
        return ""
    return f" — the claim to resolve is: {claim}"


_CLAIM_WITHHELD = "[withheld]"


def _redact_claim_shapes(claim: str, projection_mod) -> str:
    """Replace every refused value shape in a critic's claim with a marker.

    A count-vector cell keeps its name and loses its number
    (`child_count=3` -> `child_count=[withheld]`), so the sentence still says
    which column the critic meant; a key tuple and a measured per-population
    value are replaced whole. The caller re-runs the detectors afterwards, so
    anything this misses still costs only the claim, never the repair.
    """
    text = claim
    count_vector = getattr(projection_mod, "_COUNT_VECTOR_RE", None)
    if count_vector is not None:
        text = count_vector.sub(
            lambda m: m.group(0).split("=", 1)[0].rstrip() + "=" + _CLAIM_WITHHELD,
            text,
        )
    key_tuple = getattr(projection_mod, "_KEY_TUPLE_RE", None)
    if key_tuple is not None:
        text = key_tuple.sub(_CLAIM_WITHHELD, text)
    measured = getattr(projection_mod, "_MEASURED_VALUE_RE", None)
    if measured is not None:
        text = measured.sub(_CLAIM_WITHHELD, text)
    return text


def _blocking_proposal_failures(
    findings: list[Finding], outcomes
) -> tuple[list[Finding], list[str]]:
    """Return unresolved major claims and confirmed live exploits.

    Discharge only compiler-faithful proposals with confirmed matrices, plus
    shortcuts killed by every applicable variant on graded populations.
    """
    def schema_equivalent_non_exploit(finding: Finding, outcome) -> bool:
        """Accept only exact identity proofs measured by the harness.

        Fidelity evidence can prove ``DISTINCT`` over primary-keyed rows,
        zero-default, and drop-frame identities. Require complete, all-true
        combined and EL/T measurements with passing fidelity through
        ``_measured_all_true_with_fidelity``. Critic forecasts cannot satisfy
        this exception or excuse other all-pass shortcuts or mismatches.
        """
        proposal = finding.proposed_case
        if _zero_default_identity_accepted(proposal, outcome):
            return True
        if _drop_frame_identity_accepted(proposal, outcome):
            return True
        if _names_add_dedup(proposal) is None:
            return False
        proof = outcome.fidelity.get("schema_equivalence")
        if not isinstance(proof, dict):
            return False
        if (
            proof.get("proved") is not True
            or proof.get("proof") != _SCHEMA_EQUIVALENT_SOURCE_DEDUP_PROOF
            or proof.get("operation") != "add_dedup"
        ):
            return False
        proof_tables = proof.get("tables")
        if not isinstance(proof_tables, list) or not proof_tables:
            return False
        for table in proof_tables:
            if not isinstance(table, dict) or set(table) != {
                "name",
                "key_kind",
                "columns",
            }:
                return False
            if table["key_kind"] != "primary_key":
                return False
            if not isinstance(table["name"], str) or not table["name"]:
                return False
            columns = table["columns"]
            if (
                not isinstance(columns, list)
                or not columns
                or not all(isinstance(column, str) and column for column in columns)
            ):
                return False

        return _measured_all_true_with_fidelity(outcome)

    outcomes = {outcome.finding_id: outcome for outcome in outcomes}
    blocking_findings: list[Finding] = []
    problems: list[str] = []
    for finding in findings:
        if (
            finding.screen is not None
            and finding.screen.status is FindingScreenStatus.VOID
        ):
            continue
        outcome = outcomes.get(finding.finding_id)
        if outcome is not None and schema_equivalent_non_exploit(finding, outcome):
            continue
        is_blocking = False
        if finding.severity is Severity.MAJOR:
            if finding.proposed_case is None:
                is_blocking = True
                problems.append(
                    # Include the critic's complaint, not just its ID, so the
                    # specification repair has actionable evidence. Revalidate
                    # the public prose before transport to keep it value-free.
                    f"{finding.finding_id}: major finding has no structured "
                    f"proposed_case; it is unresolved{_claim_suffix(finding)}"
                )
            elif outcome is None and finding.role is CouncilRole.AMBIGUITY_CRITIC:
                is_blocking = True
                problems.append(
                    f"{finding.finding_id}: major ambiguity claim; its executable "
                    "alternative is withheld from promotion (a two-reading fork is "
                    "repaired in the prose, never frozen by a mutant), so the claim "
                    f"is unresolved{_claim_suffix(finding)}"
                )
            elif outcome is None:
                is_blocking = True
                problems.append(
                    f"{finding.finding_id}: major proposal has no promotion "
                    f"outcome{_claim_suffix(finding)}"
                )
            elif (
                not outcome.promoted
                and not _faithful_shortcut_is_empirically_killed(finding, outcome)
                and not _population_blind_spot_refuted(finding, outcome)
            ):
                is_blocking = True
                problems.append(
                    f"{finding.finding_id}: major proposal unresolved — "
                    f"{outcome.reason}{_claim_suffix(finding)}"
                )
            elif set(outcome.predicted_by_stage) != {
                TaskVariant.EXTRACT_LOAD.value,
                TaskVariant.TRANSFORM.value,
            }:
                is_blocking = True
                problems.append(
                    f"{finding.finding_id}: major proposal has no exact EL/T "
                    f"prediction matrix{_claim_suffix(finding)}"
                )
            elif not bool(outcome.fidelity.get("passed")):
                is_blocking = True
                problems.append(
                    f"{finding.finding_id}: major proposal lacks passing mutation "
                    f"fidelity evidence{_claim_suffix(finding)}"
                )
        # Even a minor critic may discover an exploit that keeps full reward
        # everywhere. It is not safe merely because the severity label was low.
        if (
            outcome is not None
            and not outcome.promoted
            and outcome.measured_pass
            and all(outcome.measured_pass.values())
        ):
            is_blocking = True
            problems.append(
                f"{finding.finding_id}: confirmed attack keeps FULL reward on "
                f"every population{_claim_suffix(finding)}"
            )
        if is_blocking:
            blocking_findings.append(finding)
    return blocking_findings, problems


def _population_blind_spot_refuted(finding: Finding, outcome) -> bool:
    """Return whether trusted complete matrices refute a blind-spot claim."""
    proposal = finding.proposed_case
    if (
        finding.role is not CouncilRole.POPULATION_ADVERSARY
        or proposal is None
        or outcome.promoted is not False
        or outcome.kind != proposal.kind.value
        or outcome.fidelity.get("passed") is not True
    ):
        return False
    populations = {population.value for population in PopulationName}

    def complete_bool_map(values) -> bool:
        return (
            isinstance(values, dict)
            and set(values) == populations
            and all(type(value) is bool for value in values.values())
        )

    by_stage = outcome.measured_pass_by_stage
    transform = (
        by_stage.get(TaskVariant.TRANSFORM.value) if isinstance(by_stage, dict) else None
    )
    if not complete_bool_map(outcome.measured_pass) or not complete_bool_map(transform):
        return False
    hidden = [p.value for p in PopulationName if p is not PopulationName.DEVELOPMENT]
    return any(
        outcome.measured_pass[p] is False and transform[p] is False for p in hidden
    )


def _measured_all_true_with_fidelity(outcome) -> bool:
    """Was one rejected proposal MEASURED all-pass on EL and T, with passing
    fidelity, whatever it predicted? A proved schema identity does not need
    the critic's forecast to be right: the population adversary on
    synsql__educational (batch10 run P, 2026-09-11) predicted that stress
    would catch an added DISTINCT, measured FULL everywhere, and the
    primary-key proof was never consulted because the forecast mismatched."""
    populations = {population.value for population in PopulationName}

    def complete_all_true(values) -> bool:
        return (
            isinstance(values, dict)
            and set(values) == populations
            and all(value is True for value in values.values())
        )

    stages = {variant.value for variant in RLVR_TASK_VARIANTS}
    return (
        outcome.promoted is False
        and bool(outcome.fidelity.get("passed"))
        and complete_all_true(outcome.measured_pass)
        and set(outcome.measured_pass_by_stage) == stages
        and all(complete_all_true(outcome.measured_pass_by_stage[stage]) for stage in stages)
    )


def _complete_all_true_promotion(outcome) -> bool:
    """Did one promoter outcome fully predict and measure all-pass EL and T?"""
    populations = {population.value for population in PopulationName}

    def complete_all_true(values) -> bool:
        return (
            isinstance(values, dict)
            and set(values) == populations
            and all(value is True for value in values.values())
        )

    stages = {variant.value for variant in RLVR_TASK_VARIANTS}
    return (
        outcome.promoted is False
        and not outcome.mismatches
        and bool(outcome.fidelity.get("passed"))
        and complete_all_true(outcome.predicted)
        and complete_all_true(outcome.measured_pass)
        and set(outcome.predicted_by_stage) == stages
        and set(outcome.measured_pass_by_stage) == stages
        and all(
            complete_all_true(outcome.predicted_by_stage[stage])
            and complete_all_true(outcome.measured_pass_by_stage[stage])
            for stage in stages
        )
    )


def _faithful_shortcut_is_empirically_killed(finding: Finding, outcome) -> bool:
    """Return whether complete faithful matrices kill a shortcut in every applicable variant."""
    proposal = finding.proposed_case
    if (
        finding.role is not CouncilRole.SHORTCUT_ATTACKER
        or proposal is None
        or outcome.promoted is not False
        or outcome.kind != proposal.kind.value
        or outcome.fidelity.get("passed") is not True
    ):
        return False

    populations = {population.value for population in PopulationName}
    stages = {variant.value for variant in RLVR_TASK_VARIANTS}

    def complete_bool_map(values) -> bool:
        return (
            isinstance(values, dict)
            and set(values) == populations
            and all(type(value) is bool for value in values.values())
        )

    combined = outcome.measured_pass
    measured_by_stage = outcome.measured_pass_by_stage
    if (
        not complete_bool_map(combined)
        or not isinstance(measured_by_stage, dict)
        or set(measured_by_stage) != stages
        or not all(
            complete_bool_map(measured_by_stage[stage]) for stage in stages
        )
    ):
        return False

    extract_load = measured_by_stage[TaskVariant.EXTRACT_LOAD.value]
    transform = measured_by_stage[TaskVariant.TRANSFORM.value]
    if any(
        combined[population] is not (
            extract_load[population] and transform[population]
        )
        for population in populations
    ):
        return False

    # Import at the seam so the CLI keeps its existing module dependency shape;
    # these are the exact kind sets whose gates consume the same measurements.
    from elt_taskgen.verification import gates as gates_mod

    applicable_stages: list[str] = []
    if proposal.kind in gates_mod.EL_SHORTCUT_KINDS:
        applicable_stages.append(TaskVariant.EXTRACT_LOAD.value)
    if proposal.kind in gates_mod.TRANSFORM_SHORTCUT_KINDS:
        applicable_stages.append(TaskVariant.TRANSFORM.value)
    if not applicable_stages:
        return False

    graded = {population.value for population in gates_mod.GRADED_POPULATIONS}
    return all(
        any(measured_by_stage[stage][population] is False for population in graded)
        for stage in applicable_stages
    )


def _names_add_dedup(proposal) -> str | None:
    """Return whether a proposal uses the operation-key or variant dedup spelling."""
    if proposal is None:
        return None
    params = dict(proposal.params)
    # The critic schema uses ``no_dedup`` for the dedup mutation family even
    # when ``add_dedup=true`` asks the compiler for the inverse operation.
    # ``custom`` is retained for hand-authored/internal proposals.  The exact
    # compiler requested/realized operation checks in the proof remain
    # authoritative.
    if params.get("add_dedup") is True:
        if proposal.kind.value not in {"custom", "no_dedup"}:
            return None
        if set(params) - {"add_dedup", "dedup_table"}:
            return None
        return "operation_key"
    if params.get("variant") == "add_dedup":
        if proposal.kind is not AttackKind.CUSTOM:
            return None
        if set(params) - {"variant"}:
            return None
        return "variant"
    return None


def _source_dedup_equivalence_proof(
    task: TaskIR, finding: Finding, outcome, attacks_mod=None
) -> dict[str, object] | None:
    """Prove deduplication is an identity on scans with declared primary keys."""
    proposal = finding.proposed_case
    form = _names_add_dedup(proposal)
    if form is None:
        return None
    if not _measured_all_true_with_fidelity(outcome):
        return None

    compiler = outcome.fidelity.get("compiler")
    if not isinstance(compiler, dict) or compiler.get("passed") is not True:
        return None
    requested = compiler.get("requested")
    realized = compiler.get("realized")
    if not isinstance(requested, dict) or not isinstance(realized, dict):
        return None
    target: str | None = None
    variant_targets: list[str] = []
    if form == "operation_key":
        if requested.get("operation") != "add_dedup":
            return None
        if realized.get("operation") != "add_dedup":
            return None
        if not isinstance(realized.get("distinct_source_scans"), int) or int(
            realized["distinct_source_scans"]
        ) < 1:
            return None
        target_raw = requested.get("dedup_table")
        target = str(target_raw) if target_raw else None
        if target != (
            str(proposal.params.get("dedup_table"))
            if proposal.params.get("dedup_table")
            else None
        ):
            return None
    else:
        # The kind-directive record names no DISTINCT count and no table; the
        # rewrite is re-derived from the gold below and must match the marts
        # the compiler says it changed.
        for record in (requested, realized):
            if (
                record.get("operation") != "kind"
                or record.get("kind") != AttackKind.CUSTOM.value
                or record.get("variant") != "add_dedup"
            ):
                return None
        raw_targets = realized.get("target_marts")
        if not isinstance(raw_targets, list) or not raw_targets:
            return None
        if not all(isinstance(name, str) and name for name in raw_targets):
            return None
        variant_targets = sorted(str(name) for name in raw_targets)

    import sqlglot
    from sqlglot import exp

    table_specs = {table.name: table for table in task.tables}
    folded_table_names: dict[str, str] = {}
    for name in table_specs:
        folded = name.casefold()
        if folded in folded_table_names:
            return None
        folded_table_names[folded] = name
    affected: set[str] = set()
    for sql in task.reference.sql_by_mart.values():
        try:
            ast = sqlglot.parse_one(sql, read="duckdb")
        except Exception:  # noqa: BLE001 - inability to prove is simply red
            return None
        cte_names = {
            cte.alias_or_name.casefold()
            for cte in ast.find_all(exp.CTE)
            if cte.alias_or_name
        }
        # DuckDB resolves unquoted identifiers case-insensitively. A differently
        # cased CTE can therefore shadow a physical TaskIR table even though
        # sqlglot preserves their distinct spellings.
        if cte_names & set(folded_table_names):
            return None
        for table in ast.find_all(exp.Table):
            name = table.name
            if name not in table_specs:
                if form == "variant":
                    # `custom@add_dedup` wraps this scan too (a CTE reference,
                    # a function table, anything the reference names), and
                    # no declared key proves DISTINCT is an identity there.
                    return None
                continue
            if target is not None and name != target:
                continue
            # The admitted compiler reconstructs the outer alias from
            # ``table.alias``.  It cannot preserve qualified/table-valued
            # identifiers or an alias column list such as ``c(x, y)``.  Those
            # rewrites may rebind columns, so PK uniqueness is not an identity
            # proof for them even when today's five populations all pass.
            if not isinstance(table.this, exp.Identifier):
                return None
            alias = table.args.get("alias")
            if alias is not None:
                if not isinstance(alias, exp.TableAlias) or not isinstance(
                    alias.this, exp.Identifier
                ):
                    return None
                if any(
                    key != "this" and value not in (None, False, (), [])
                    for key, value in alias.args.items()
                ):
                    return None
            # Only a raw local scan with an optional alias earns the identity
            # proof; table modifiers can change rows inside the DISTINCT query.
            if any(
                key not in {"this", "alias"}
                and value not in (None, False, (), [])
                for key, value in table.args.items()
            ):
                return None
            affected.add(name)
    if not affected:
        return None

    proof_tables: list[dict[str, object]] = []
    for name in sorted(affected):
        primary_key = table_specs[name].primary_key
        if not primary_key:
            return None
        proof_tables.append(
            {
                "name": name,
                "key_kind": "primary_key",
                "columns": list(primary_key),
            }
        )
    if form == "variant":
        # Re-derive the directive exactly as the compiler does and require
        # that it changes precisely the recorded marts, each with at least
        # one DISTINCT scan; anything else is not this rewrite.
        if attacks_mod is None:
            from elt_taskgen.verification import attacks as attacks_mod
        changed: list[str] = []
        for mart in task.marts:
            gold_sql = dict(task.reference.sql_by_mart).get(mart.name)
            if not gold_sql:
                return None
            try:
                mutated = attacks_mod._apply_kind(
                    AttackKind.CUSTOM, gold_sql, mart, "add_dedup"
                )
            except Exception:  # noqa: BLE001 - a compiler error is not a proof
                return None
            if mutated is None:
                continue
            try:
                mutated_ast = sqlglot.parse_one(mutated, read="duckdb")
            except Exception:  # noqa: BLE001
                return None
            distincts = sum(
                1
                for select in mutated_ast.find_all(exp.Select)
                if select.args.get("distinct") is not None
            )
            if distincts < 1:
                return None
            changed.append(mart.name)
        if sorted(changed) != variant_targets:
            return None
    return {
        "proved": True,
        "proof": _SCHEMA_EQUIVALENT_SOURCE_DEDUP_PROOF,
        "operation": "add_dedup",
        "form": form,
        "tables": proof_tables,
    }


def _normalize_schema_equivalent_promotions(
    task: TaskIR,
    findings: list[Finding],
    workspace: Path,
    promotion,
    attacks_mod,
):
    """Reclassify only compiler-proved, fully measured relational identities.

    The promoter correctly refuses to create a required attack that predicts no
    kill. Usually that is a blocking live exploit. For the one schema identity
    proved above, retain the rejected outcome but attach the proof, correct its
    reason, and rewrite the audit sidecar with that normalized evidence.
    """
    findings_by_id = {finding.finding_id: finding for finding in findings}
    normalized: dict[str, object] = {}
    for outcome in promotion.outcomes:
        finding = findings_by_id.get(outcome.finding_id)
        if finding is None:
            continue
        proof = _source_dedup_equivalence_proof(task, finding, outcome, attacks_mod)
        if proof is None:
            proof = _zero_default_equivalence_proof(task, finding, outcome, attacks_mod)
        if proof is None:
            proof = _drop_frame_equivalence_proof(task, finding, outcome, attacks_mod)
        if proof is None:
            continue
        updated = outcome.model_copy(
            update={
                "reason": (
                    "proposal is provably output-equivalent on the declared "
                    "primary-key domain, and was confirmed to keep FULL combined "
                    "reward on all five populations; record as a non-exploit"
                    if proof.get("proof") == _SCHEMA_EQUIVALENT_SOURCE_DEDUP_PROOF
                    else "proposal is provably an identity on this task's gold "
                    "(every window it unframes is an extreme ordered by its own "
                    "column), and was confirmed to keep FULL combined reward on "
                    "all five populations; record as a non-exploit"
                    if proof.get("proof") == _SCHEMA_EQUIVALENT_DROP_FRAME_PROOF
                    else "proposal is provably an identity on this task's gold "
                    "(every default it rewrites is the number 0), and was "
                    "confirmed to keep FULL combined reward on all five "
                    "populations; record as a non-exploit"
                ),
                "fidelity": {
                    **outcome.fidelity,
                    "schema_equivalence": proof,
                },
            }
        )
        normalized[outcome.finding_id] = updated
        attacks_mod._record_rejected_proposal(workspace, task, updated)
    if not normalized:
        return promotion

    replace = lambda outcome: normalized.get(outcome.finding_id, outcome)
    return attacks_mod.PromotionResult(
        task=promotion.task,
        promoted=promotion.promoted,
        rejected=tuple(replace(outcome) for outcome in promotion.rejected),
        outcomes=tuple(replace(outcome) for outcome in promotion.outcomes),
        rewards=promotion.rewards,
    )


def _normalize_inapplicable_promotion(
    task: TaskIR,
    findings: list[Finding],
    workspace: Path,
    promotion,
    attacks_mod,
):
    """Bind an inapplicable promotion to a deterministic failed-fidelity record."""
    by_id = {finding.finding_id: finding for finding in findings}
    normalized = []
    changed = False
    for outcome in promotion.outcomes:
        if (
            outcome.promoted
            or outcome.measured
            or "FileNotFoundError" not in outcome.reason
            or attacks_mod.MUTATION_FIDELITY_FILENAME not in outcome.reason
        ):
            normalized.append(outcome)
            continue
        finding = by_id.get(outcome.finding_id)
        proposal = finding.proposed_case if finding is not None else None
        if finding is None or proposal is None:
            normalized.append(outcome)
            continue
        attack_dir = (
            Path(workspace)
            / "tasks"
            / task.task_id
            / "attacks"
            / outcome.case_name
        )
        inapplicable_path = attack_dir / "inapplicable.json"
        try:
            inapplicable = json.loads(inapplicable_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            normalized.append(outcome)
            continue
        if (
            inapplicable.get("case") != outcome.case_name
            or inapplicable.get("task_content_hash") != task.content_hash()
        ):
            normalized.append(outcome)
            continue
        detail = str(inapplicable.get("inapplicable") or "").strip()
        if not detail:
            normalized.append(outcome)
            continue

        candidate = attacks_mod._candidate_case(finding, proposal, required=False)
        mutation = candidate.mutation.strip()
        exception_name = "InertAstMutationError"
        if mutation.startswith(attacks_mod.LOAD_DIRECTIVE_PREFIX):
            exception_name = "InapplicableLoadMutationError"
        elif mutation.startswith(attacks_mod.STRUCTURED_DIRECTIVE_PREFIX):
            structured = attacks_mod.parse_structured_directive(
                mutation[len(attacks_mod.STRUCTURED_DIRECTIVE_PREFIX):]
            )
            if structured.get("operation") in {"skip_backend", "skip_tables"}:
                exception_name = "InapplicableLoadMutationError"

        claim = dict(outcome.fidelity)
        compiler = attacks_mod._write_fidelity_record(
            workspace,
            task,
            candidate,
            passed=False,
            requested={"mutation": mutation},
            realized={},
            checks=("mutation applicability checked before reward execution",),
            errors=(detail,),
        )
        fidelity = {
            "passed": False,
            "claim": claim,
            "compiler": compiler,
        }
        replacement = outcome.model_copy(
            update={
                "reason": (
                    "proposal could not be executed: "
                    f"{exception_name}: {detail}"
                ),
                "fidelity": fidelity,
            }
        )
        attacks_mod._record_rejected_proposal(workspace, task, replacement)
        normalized.append(replacement)
        changed = True

    if not changed:
        return promotion
    outcomes = tuple(normalized)
    return attacks_mod.PromotionResult(
        task=promotion.task,
        promoted=promotion.promoted,
        rejected=tuple(outcome for outcome in outcomes if not outcome.promoted),
        outcomes=outcomes,
        rewards=promotion.rewards,
    )


def run_attack_stage(engine: Engine, task: TaskIR) -> StageOutcome:
    """Compile, measure, and promote exactly confirmed attack proposals.

    Promotion changes task identity, so all attack evidence is remeasured at the
    new hash.
    """
    from elt_taskgen.reference import gold as gold_mod
    from elt_taskgen.review.council import ProviderProtocolError
    from elt_taskgen.verification import attacks as attacks_mod

    gold = gold_mod.load_gold(_answer_key_dir(engine, task))
    review_payload = _require_pass_payload(engine, task, StageName.REVIEW)
    findings = [Finding.model_validate(f) for f in review_payload.get("findings", [])]
    # Revalidate persisted review evidence too: older/current ledgers may have
    # been produced before the critic protocol enforced explicit variants.
    try:
        executable_findings = _validated_executable_findings(task, findings)
    except ProviderProtocolError as exc:
        return _critic_protocol_block(
            f"critic protocol failure: {type(exc).__name__}: {exc}"
        )
    pending_adjudication = _critic_adjudication_block(findings)
    if pending_adjudication is not None:
        return pending_adjudication

    original_hash = task.content_hash()
    promoted: tuple[str, ...] = ()
    rejected: tuple[dict, ...] = ()
    proposal_outcomes: dict[str, object] = {}
    # Two passes at most: the second re-measures everything at the identity
    # promotion produced. A third would mean promotion is not idempotent.
    for _pass in range(2):
        cases = attacks_mod.compile_attacks(task, executable_findings)
        rewards: dict[str, dict[str, float]] = {}
        inapplicable: dict[str, str] = {}
        for case in cases:
            measured = attacks_mod.run_attack(task, case, gold, engine.workspace)
            if not measured:
                # An informational probe whose surface this task's DATA does not
                # offer. run_attack recorded the reason; it must NOT enter the
                # measured payload, where an empty entry would read as deleted
                # evidence. A REQUIRED case can never take this path.
                record_path = (
                    engine.task_dir(task.task_id)
                    / "attacks" / case.name / "inapplicable.json"
                )
                record = json.loads(record_path.read_text(encoding="utf-8"))
                inapplicable[case.name] = str(record.get("inapplicable", ""))
                continue
            rewards[case.name] = {p.value: float(r) for p, r in measured.items()}

        promotion = _normalize_inapplicable_promotion(
            task,
            findings,
            engine.workspace,
            attacks_mod.promote_proposed_cases(
                task,
            # Withhold ambiguity alternatives from promotion; route the claim to prose repair.
                [f for f in executable_findings if f.role is not CouncilRole.AMBIGUITY_CRITIC],
                engine.workspace,
                gold,
            ),
            attacks_mod,
        )
        promotion = _normalize_schema_equivalent_promotions(
            task,
            findings,
            engine.workspace,
            promotion,
            attacks_mod,
        )
        for proposal_outcome in promotion.outcomes:
            previous = proposal_outcomes.get(proposal_outcome.finding_id)
            if previous is None or proposal_outcome.fidelity or proposal_outcome.measured:
                proposal_outcomes[proposal_outcome.finding_id] = proposal_outcome
        # Rejected proposals keep their measured rewards in the payload: their
        # probes exist on disk, and evidence for a probe that ran is never missing.
        for name, measured in promotion.rewards.items():
            rewards.setdefault(
                name, {p.value: float(r) for p, r in measured.items()}
            )
        # Read the outcomes, not just this pass's new promotions: on the re-measure
        # pass an already-promoted case reports promoted with no fresh AttackCase.
        promoted = tuple(o.case_name for o in promotion.outcomes if o.promoted)
        rejected = tuple(o.model_dump(mode="json") for o in promotion.rejected)
        if not promotion.task_changed:
            break
        task = promotion.task
    else:  # pragma: no cover - promotion is idempotent by construction
        raise RuntimeError(
            f"task {task.task_id!r}: attack-case promotion did not converge "
            "after two passes (fail closed)"
        )

    changed = task.content_hash() != original_hash
    by_variant = _recorded_variant_rewards(engine, task, rewards)
    detail = (
        f"{len(cases) - len(inapplicable)} attack case(s) executed on "
        f"{len(task.populations)} population(s); {len(promoted)} proposal(s) "
        f"promoted, {len(rejected)} rejected"
        + (
            f"; {len(inapplicable)} probe(s) recorded inapplicable "
            f"({', '.join(sorted(inapplicable))})"
            if inapplicable
            else ""
        )
    )
        # Pass only screened findings so voided claims cannot block promotion.
    blocking_findings, proposal_problems = _blocking_proposal_failures(
        executable_findings, proposal_outcomes.values()
    )
    if proposal_problems:
        outcomes = tuple(proposal_outcomes.values())
        selected = _selected_blocking_finding(blocking_findings, outcomes)
        # A non-empty problem list must have a finding.  Keep the assertion at
        # this producer boundary: emitting an opaque attack failure would send
        # the repair proposer back to the abstention loop this descriptor closes.
        if selected is None:  # pragma: no cover - guarded by the builder above
            raise RuntimeError("blocking proposal failure has no finding")
        route = _proposal_failure_route(blocking_findings, outcomes)
        return StageOutcome(
            VERDICT_FATAL if route is RepairRoute.FATAL else VERDICT_FAIL,
            AttackPayload(
                rewards=rewards,
                rewards_by_variant=by_variant,
                cases=tuple(c.name for c in cases if c.name not in inapplicable),
                inapplicable_cases=inapplicable,
                promoted_proposals=promoted,
                rejected_proposals=rejected,
                proposal_outcomes=tuple(
                    outcome.model_dump(mode="json")
                    for outcome in sorted(
                        proposal_outcomes.values(), key=lambda value: value.finding_id
                    )
                ),
                blocking_finding=_attack_finding_descriptor(task, selected),
                detail=(
                    detail
                    + "; critic-to-mutation handoff BLOCKED: "
                    + "; ".join(proposal_problems)
                ),
            ),
            route=route,
            task=task if changed else None,
        )
    return StageOutcome(
        VERDICT_PASS,
        AttackPayload(
            rewards=rewards,
            rewards_by_variant=by_variant,
            cases=tuple(c.name for c in cases if c.name not in inapplicable),
            inapplicable_cases=inapplicable,
            promoted_proposals=promoted,
            rejected_proposals=rejected,
            proposal_outcomes=tuple(
                outcome.model_dump(mode="json")
                for outcome in sorted(
                    proposal_outcomes.values(), key=lambda value: value.finding_id
                )
            ),
            detail=detail,
        ),
        task=task if changed else None,
    )


def _ensure_independent_build(engine: Engine, task: TaskIR, gold, provider) -> str:
    """Run or reuse the independent build for the current task hash.

    Missing results fail gates; harness faults are recorded as transport halts.
    """
    from elt_taskgen.reference import adjudication as independent_adjudication
    from elt_taskgen.reference import independent
    from elt_taskgen.review import providers as providers_mod
    from elt_taskgen.review.session import SessionFault

    existing = independent.load_build_result(engine.workspace, task.task_id)
    recovery_generation = 0
    decision_digest = ""
    if (
        existing is not None
        and existing.get("task_id") == task.task_id
        and existing.get("task_content_hash") == task.content_hash()
    ):
        if existing.get("status") != independent.STATUS_NEEDS_ADJUDICATION:
            return f"recorded independent build reused (status={existing.get('status')!r})"
        try:
            decision = independent_adjudication.load_fresh_build_decision(
                engine.workspace,
                task,
            )
        except ValueError as exc:
            return (
                "recorded independent build reused "
                f"(status={existing.get('status')!r}); invalid bound "
                f"adjudication decision: {type(exc).__name__}: {exc}"
            )
        if decision is None:
            return f"recorded independent build reused (status={existing.get('status')!r})"
        # The decision cannot green-light anything. It only permits one fresh
        # blind reconstruction, under a transcript generation that differs
        # from the disputed witness while exposing no diagnostic material.
        recovery_generation = independent.next_recovery_generation(
            engine.workspace,
            task.task_id,
        )
        decision_digest = decision.digest()
    try:
        result = independent.run_independent_build(
            task,
            engine.workspace,
            provider,
            gold,
            recovery_generation=recovery_generation,
        )
    except (
        providers_mod.TranscriptMissingError,
        providers_mod.MissingCredentialsError,
        SessionFault,
    ) as exc:
        # Prefix the note with the canonical exception marker from the MRO so
        # transport failures route as infrastructure even for subclasses.
        # SessionFault messages contain only boundary and code metadata.
        from elt_taskgen.engine import _infra_marker_for

        name = type(exc).__name__
        marker = _infra_marker_for(exc)
        head = f"{marker}: {name}" if marker and marker != name else name
        return f"independent build not performed: {head}: {exc}"
    independent.record_build_result(engine.workspace, task, result)
    recovery_note = (
        f", fresh blind generation={recovery_generation}, "
        f"decision={decision_digest[:12]}"
        if recovery_generation
        else ""
    )
    return f"independent build recorded (status={result.status!r}{recovery_note})"


def _independent_adjudication_block(
    engine: Engine, task: TaskIR
) -> StageOutcome | None:
    """Block a current dual-build disagreement pending adjudication or new evidence."""
    from elt_taskgen.reference import independent

    record = independent.load_build_result(engine.workspace, task.task_id)
    if not isinstance(record, dict):
        return None
    if (
        record.get("task_id") != task.task_id
        or record.get("task_content_hash") != task.content_hash()
        or record.get("status") != independent.STATUS_NEEDS_ADJUDICATION
    ):
        return None
    evidence_digest = sha256_hex(canonical_json(record))
    return StageOutcome(
        VERDICT_BLOCKED,
        StagePayload(
            error=(
                "the current independent transformation build passes the public "
                "development examples but disagrees with hidden gold; the task, "
                "reference, data, comparator, and witness remain unjudged until "
                "the disagreement is explicitly adjudicated"
            ),
            data={
                "failure_class": "pending_adjudication",
                "failure_code": "independent_gold_disagreement",
                BLOCKED_ON_KEY: BLOCKED_ON_HUMAN,
                "independent_build_sha256": evidence_digest,
                "recovery_prerequisite": (
                    "bound_adjudication_or_new_independent_build"
                ),
                RETRY_GUARD_KEY: RETRY_GUARD_EXPLICIT,
            },
        ),
    )


#: A gate whose failure names a RE-RUN as its remedy is refusing STALE EVIDENCE,
#: not judging the task. Every currency refusal gates.py writes ends in one; a
#: genuine defect never does.
_EVIDENCE_RERUN_MARKER = "re-run"


def _is_currency_refusal(gate) -> bool:
    """Is this failing gate refusing STALE EVIDENCE rather than judging?

    ONE reader, shared with the repair router (`repair.is_currency_refusal`), so the
    BLOCKED answer here and the route the engine picks can never disagree about what
    "not measured yet" means. Prefers a structured flag over the prose match."""
    return repair.is_currency_refusal(gate)


def _evidence_currency_block(report) -> str | None:
    """The remedy text when a battery refused ONLY because its evidence is not
    current, else None.

    A fix that legitimately makes recorded evidence stale must produce "run the
    stage again", never a rejection: this is a wait on the operator or on a stage
    re-run, i.e. BLOCKED. Measured — one refusal naming contamination was read as a
    contamination REJECTION and a certified drive was rejected outright."""
    gates = getattr(report, "gates", None)
    if not gates:
        return None
    failing = [g for g in gates if not getattr(g, "passed", False)]
    if not failing:
        return None
    if not all(_is_currency_refusal(g) for g in failing):
        return None
    return "; ".join(
        f"{getattr(g, 'gate', '?')}: {getattr(g, 'details', '')}" for g in failing
    )


#: Remedy phrase -> the ladder stage that RE-DERIVES that evidence.
#: `measure-target` is deliberately absent: it needs an ELT-Bench checkout, so no
#: stage in the ladder can clear it and only the operator can.
_EVIDENCE_PRODUCER_STAGES: tuple[tuple[str, StageName], ...] = (
    ("re-run reference-run", StageName.REFERENCE),
    ("re-run contamination-post", StageName.CONTAMINATION_POST),
    ("re-run validate-el", StageName.GATES_EXTRACT_LOAD),
    ("re-run validate-t", StageName.GATES_TRANSFORM),
)

#: Ledger stage -> the CLI subcommand that runs it (for remedy messages).
STAGE_SUBCOMMANDS: dict[str, str] = {
    StageName.GENERATE.value: "generate",
    StageName.REFERENCE.value: "reference-run",
    # The author runs inside `review` (no `author` subcommand exists), so a
    # block or refusal at that stage is re-run through `review`.
    StageName.AUTHOR.value: "review",
    StageName.REVIEW.value: "review",
    StageName.ATTACK.value: "attack",
    StageName.TASK_INTEGRITY.value: "validate",
    StageName.GATES_EXTRACT_LOAD.value: "validate-el",
    StageName.GATES_TRANSFORM.value: "validate-t",
    StageName.CALIBRATE.value: "calibrate",
    StageName.SELECT.value: "select",
    StageName.AUDIT.value: "release",
    StageName.RELEASE.value: "release",
    StageName.CONTAMINATION_POST.value: "release",
}


def _shadow_stale_evidence_producers(
    engine: Engine, task: TaskIR, stage: StageName, remedies: str
) -> tuple[str, ...]:
    """Shadow the upstream stages a currency refusal names. Returns their names.

    THE REMEDY HAS TO BE REACHABLE: `engine.run` skips a stage whose latest row is a
    PASS at the current hash, so the re-run a refusal named did nothing. Shadowing
    the producer here is APPEND-ONLY (the idiom `repair.apply_repair` uses), spends
    no repair round and rejects nothing. Only stages STRICTLY UPSTREAM are shadowed
    — a battery cannot be its own producer, and a downstream stage has not attested."""
    order = [s.value for s in STAGE_ORDER]
    here = order.index(stage.value)
    text = remedies.lower()
    shadowed: list[str] = []
    for phrase, producer in _EVIDENCE_PRODUCER_STAGES:
        if phrase not in text or order.index(producer.value) >= here:
            continue
        row = engine.latest_report(task.task_id, producer.value)
        if row is None or not engine.report_is_current(task, producer, row)[0]:
            continue  # already going to run: nothing to shadow
        _force_stage_rerun(
            engine,
            task,
            producer,
            f"stale evidence for {stage.value!r}: {phrase}",
        )
        shadowed.append(producer.value)
    return tuple(shadowed)


def _blocked_on_stale_evidence(
    engine: Engine, task: TaskIR, stage: StageName, remedies: str
) -> StageOutcome:
    """BLOCKED because recorded evidence is not current — and the producer of
    that evidence is scheduled to re-run so the block can actually clear."""
    scheduled = _shadow_stale_evidence_producers(engine, task, stage, remedies)
    if scheduled:
        remedy = (
            "already scheduled: "
            + ", ".join(sorted(scheduled))
            + " will re-derive on the next run of this workspace — just re-run "
            "the same command"
        )
    else:
        subcommand = STAGE_SUBCOMMANDS.get(stage.value, stage.value)
        remedy = (
            "no ladder stage can re-derive this: run what the refusal names "
            f"(e.g. `elt-taskgen measure-target --workspace {engine.workspace} "
            "--bench-root <ELT-Bench checkout>`), then re-run `elt-taskgen "
            f"{subcommand} --workspace {engine.workspace} --task-id "
            f"{task.task_id}`"
        )
    data = {BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT}
    if scheduled:
        data["rerun_scheduled"] = ",".join(sorted(scheduled))
    return StageOutcome(
        VERDICT_BLOCKED,
        StagePayload(
            error=(
                "recorded evidence is not current, so this battery could not "
                "judge the task — nothing is rejected and no repair round is "
                f"spent. Refusals: {remedies}. Remedy: {remedy}."
            ),
            data=data,
        ),
    )


def _canonical_block(error: str, *, blocked_on: str, failure_code: str) -> StageOutcome:
    return StageOutcome(
        VERDICT_BLOCKED,
        StagePayload(
            error=error + " Nothing is rejected and no repair round is spent.",
            data={BLOCKED_ON_KEY: blocked_on, "failure_code": failure_code},
        ),
    )


def _ensure_canonical_reachability(
    engine: Engine, task: TaskIR, variant_out_dir: Path
) -> StageOutcome | None:
    """Derive and score the canonical Terraform + dbt artifact for the T unit,
    once per destination the task ships.

    Writes ``reports/canonical_reachability.json`` and the private artifacts
    under ``answer_key/runtime/canonical/<destination>/`` for the battery's
    canonical-reachability gate to judge, then returns None. It RECORDS only
    outcomes that describe the task on the workspace channel: a full score and
    a measured shortfall or an emitter gap. Anything that says nothing about
    the task returns BLOCKED without recording: the pinned dbt runtime
    missing, a substrate or IO failure, a harness, infrastructure or
    real-runtime failure, a timeout of the reference solution (environment),
    or the grader refusing the task package (human). No outcome rejects.
    """
    import tempfile

    from elt_taskgen.training import canonical
    from elt_taskgen.training.package import WorkspacePackageError, shipped_destinations

    task_root = engine.task_dir(task.task_id)
    answer_key_dir = _answer_key_dir(engine, task)
    # A previous identity's or run's evidence never survives into this one.
    stale_evidence = task_root / canonical.REACHABILITY_EVIDENCE_REL
    if stale_evidence.is_file() and not stale_evidence.is_symlink():
        stale_evidence.unlink()
    stale_artifacts = answer_key_dir / canonical.CANONICAL_ARTIFACT_REL
    if stale_artifacts.exists():
        canonical.remove_scratch_tree(stale_artifacts)

    runtime_config = canonical.default_dbt_runtime_config()
    if not canonical.dbt_runtime_available(runtime_config):
        return _canonical_block(
            "The canonical-reachability check needs the pinned dbt runtime at "
            f"{canonical.DBT_RUNTIME_ROOT}; provision it with: uv sync --project "
            "runtime-images/dbt-duckdb --locked.",
            blocked_on=BLOCKED_ON_ENVIRONMENT,
            failure_code="canonical_runtime_unavailable",
        )
    try:
        destinations = shipped_destinations(answer_key_dir)
    except Exception as exc:  # noqa: BLE001 - the private tree is unreadable
        return _canonical_block(
            "The task's private connector contracts could not be read "
            f"({type(exc).__name__}: {exc}).",
            blocked_on=BLOCKED_ON_ENVIRONMENT,
            failure_code="canonical_substrate_failed",
        )

    records: dict[str, canonical.CanonicalReachabilityRecord] = {}
    files_by_destination: dict[str, dict[str, str]] = {}
    scratch = Path(tempfile.mkdtemp(prefix="canonical-reachability-"))
    try:
        for destination in destinations:
            name = destination.value
            try:
                package = canonical.build_workspace_substrate(
                    task=task,
                    answer_key_dir=answer_key_dir,
                    public_dir=_public_dir(engine, task),
                    populations_root=task_root / "populations",
                    oracle_dir=Path(variant_out_dir) / "task" / _warehouse_dirname(),
                    scratch=scratch / f"substrate-{name}",
                    destination=destination,
                )
            except WorkspacePackageError as exc:
                return _canonical_block(
                    "The workspace grader refused the exported task package while "
                    f"preparing the canonical-reachability check for {name} ({exc}).",
                    blocked_on=BLOCKED_ON_HUMAN,
                    failure_code="canonical_task_package_invalid",
                )
            except Exception as exc:  # noqa: BLE001 - environment, never a judgement
                return _canonical_block(
                    "The canonical-reachability workspace could not be laid out for "
                    f"{name} ({type(exc).__name__}: {exc}).",
                    blocked_on=BLOCKED_ON_ENVIRONMENT,
                    failure_code="canonical_substrate_failed",
                )
            try:
                files = canonical.render_canonical_project(package)
            except canonical.CanonicalArtifactError as exc:
                records[name] = canonical.build_render_failure_record(
                    task,
                    destination=destination,
                    runtime_config=runtime_config,
                    error=f"{type(exc).__name__}: {exc}",
                    airbyte_contract_sha256=package.airbyte_contract_sha256,
                )
                files_by_destination[name] = {}
                continue
            try:
                sealed, result = canonical.score_canonical_artifact(
                    package,
                    files,
                    scratch=scratch / f"score-{name}",
                    runtime_config=runtime_config,
                )
            except Exception as exc:  # noqa: BLE001 - install/seal/scorer faults
                return _canonical_block(
                    "Scoring the canonical artifact failed inside the workspace "
                    f"harness for {name} ({type(exc).__name__}: {exc}).",
                    blocked_on=BLOCKED_ON_ENVIRONMENT,
                    failure_code="canonical_harness_failed",
                )
            outcome = canonical.classify_score(result)
            if outcome == canonical.OUTCOME_ENVIRONMENT:
                return _canonical_block(
                    "The workspace grader produced no label for the canonical "
                    f"artifact on {name} ({canonical.shortfall_summary(result)}); this "
                    "describes the host or the harness, not the task.",
                    blocked_on=BLOCKED_ON_ENVIRONMENT,
                    failure_code="canonical_grader_unavailable",
                )
            if outcome == canonical.OUTCOME_TASK_PACKAGE:
                return _canonical_block(
                    "The workspace grader refused the task package for the canonical "
                    f"artifact on {name} ({canonical.shortfall_summary(result)}).",
                    blocked_on=BLOCKED_ON_HUMAN,
                    failure_code="canonical_task_package_invalid",
                )
            records[name] = canonical.build_reachability_record(
                package,
                files=files,
                sealed=sealed,
                result=result,
                runtime_config=runtime_config,
            )
            files_by_destination[name] = dict(files)
        canonical.record_canonical_reachability(
            task_dir=task_root,
            answer_key_dir=answer_key_dir,
            records=records,
            files=files_by_destination,
        )
        return None
    finally:
        canonical.remove_scratch_tree(scratch)


def _warehouse_dirname() -> str:
    from elt_taskgen.export import eltbench as eltbench_mod

    return eltbench_mod.WAREHOUSE_DIRNAME


def _configured_destination_of(engine: Engine, task: TaskIR) -> str:
    """The bundle-root destination of the exported public task, else snowflake."""
    import yaml

    from elt_taskgen.export import eltbench as eltbench_mod

    try:
        config = yaml.safe_load(
            (_public_dir(engine, task) / "config.yaml").read_text(encoding="utf-8")
        )
        return eltbench_mod.destination_from_config(config).value
    except Exception:  # noqa: BLE001 - a missing bundle is reported by the gate
        return "snowflake"


def make_gates_runner(provider, *, destination="snowflake", extra_destinations=()):
    """`gates` (TASK_INTEGRITY): shared project-integrity evidence and checks.

    The stage value stays ``gates`` for workspace compatibility, but this runner
    does NOT create a FULL RLVR task: its composite checks are internal diagnostics,
    and EL and T are admitted only by the two unit batteries that follow."""

    def run_gates_stage(engine: Engine, task: TaskIR) -> StageOutcome:
        from elt_taskgen.export import eltbench as eltbench_mod
        from elt_taskgen.reference import gold as gold_mod
        from elt_taskgen.verification import gates as gates_mod

        akd = _answer_key_dir(engine, task)
        public = _public_dir(engine, task)
        gold = gold_mod.load_gold(akd)

        # Emit the public bundle + private evaluation artifacts (upstream
        # shape); the post-generation scan and two gates read these trees.
        eltbench_mod.export_task(
            task,
            gold,
            public,
            akd,
            destination=destination,
            extra_destinations=tuple(extra_destinations),
        )

        idx = _contamination_index(engine)
        # ARMING BANNER at the gate: a reader watching the run must see what the
        # firewall contained at the moment the contamination-clean gate read it.
        coverage = _arm_contamination_index(idx)
        result = idx.scan_post(
            task, public, akd, require=_required_firewall_coverage()
        )
        collisions = list(result.collisions)
        evidence = {
            "task_id": task.task_id,
            "task_content_hash": task.content_hash(),
            "collisions": [c.model_dump(mode="json") for c in collisions],
            "coverage": _coverage_payload(engine, coverage),
            "coverage_summary": coverage.summary(),
        }
        (_evidence_dir(engine, task) / "contamination_post.json").write_text(
            canonical_json(evidence), encoding="utf-8"
        )
        if any(c.fatal for c in collisions):
            return StageOutcome(
                VERDICT_FATAL,
                ContaminationPayload(
                    call_point="post",
                    collisions=tuple(c.model_dump(mode="json") for c in collisions),
                    fatal_count=sum(1 for c in collisions if c.fatal),
                    coverage_level=coverage.level.value,
                    coverage=_coverage_payload(engine, coverage),
                    detail=(
                        "fatal contamination collision in emitted artifacts; "
                        + coverage.summary()
                    ),
                ),
            )

        # Dual-build evidence for the trusted-solution/dual-build-agreement gates:
        # run and record here (or reuse the record at this exact identity). The note
        # is PERSISTED, so why a witness is missing survives in the workspace.
        build_note = _ensure_independent_build(engine, task, gold, provider)
        _write_evidence_notes(engine, task, DUAL_BUILD_NOTES_FILENAME, [build_note])

        attack_payload = _require_pass_payload(engine, task, StageName.ATTACK)
        report = gates_mod.run_gates(
            task, engine.workspace, gold, attack_payload.get("rewards", {})
        )
        (
            _evidence_dir(engine, task)
            / VARIANT_ACCEPTANCE_EVIDENCE.format(variant=TaskVariant.FULL.value)
        ).write_text(
            readable_json(report.model_dump(mode="json")), encoding="utf-8"
        )
        if report.accepted:
            return StageOutcome(VERDICT_PASS, report)
        adjudication = _independent_adjudication_block(engine, task)
        if adjudication is not None:
            return adjudication
        stale = _evidence_currency_block(report)
        if stale is not None:
            return _blocked_on_stale_evidence(
                engine, task, StageName.TASK_INTEGRITY, stale
            )
        return StageOutcome(
            VERDICT_FAIL,
            report,
            # A witness the TRANSPORT could not produce is not a task defect: halt
            # naming the transport instead of spending a repair round on a
            # deterministic re-run that reproduces it.
            infrastructure=_transport_marker([build_note]),
        )

    return run_gates_stage


# Per-variant acceptance batteries. Each subtask has its own ledger stage,
# reward, witnesses, and parent hash for resumable repair and release checks.

#: Per-variant AcceptanceReport copy under tasks/<id>/reports/.
VARIANT_ACCEPTANCE_EVIDENCE = "acceptance_{variant}.json"


def _variants_root(engine: Engine, task: TaskIR) -> Path:
    return engine.task_dir(task.task_id) / "variants"


def _ensure_el_evidence(engine: Engine, task: TaskIR, gold, provider) -> list[str]:
    """Run both extract/load witnesses for the current content hash.

    ``el-artifact-census`` independently counts each format without solution
    readers. ``el-independent-load`` gives a cross-family model only the public
    EL bundle. Neither witness substitutes for the other. If either producer
    cannot run, record no evidence and return notes; missing evidence fails the
    gates."""
    notes: list[str] = []

    from elt_taskgen.reference import independent

    census_writer = None
    try:
        from elt_taskgen.verification import el_probes  # type: ignore

        census_writer = getattr(el_probes, "record_artifact_census", None)
    except ImportError:
        pass
    if census_writer is None:
        notes.append(
            "artifact census not performed: verification/el_probes.py provides "
            "no record_artifact_census (el-artifact-census will be RED)"
        )
    else:
        try:
            census_writer(engine.workspace, task, gold)
            notes.append("artifact census recorded")
        except Exception as exc:  # noqa: BLE001 — a producer failure is evidence
        # Persist census-production errors for the owning gate to report.
            notes.append(f"artifact census FAILED: {type(exc).__name__}: {exc}")

    load_build = getattr(independent, "run_independent_load_build", None)
    recorder = getattr(independent, "record_load_build_result", None)
    if load_build is None or recorder is None:
        notes.append(
            "independent LOAD build not performed: reference/independent.py "
            "provides no independent_loader role (el-independent-load will be RED)"
        )
        return notes
    from elt_taskgen.review import providers as providers_mod

    loader = getattr(independent, "load_load_build_result", None)
    existing = loader(engine.workspace, task.task_id) if loader else None
    if (
        existing is not None
        and existing.get("task_id") == task.task_id
        and existing.get("task_content_hash") == task.content_hash()
    ):
        notes.append("recorded independent LOAD build reused")
        return notes
    try:
        result = load_build(task, engine.workspace, provider, gold)
    except providers_mod.BudgetExceededError as exc:
        # Preserve task/total budget scope for coordinator handling.
        if str(getattr(exc, "scope", "") or "").strip().lower() in {
            "task",
            "total",
        }:
            raise
        notes.append(
            f"independent LOAD build FAILED: {type(exc).__name__}: {exc}"
        )
        return notes
    except (
        providers_mod.TranscriptMissingError,
        providers_mod.MissingCredentialsError,
    ) as exc:
        # Class name first — see _ensure_independent_build for why.
        notes.append(
            f"independent LOAD build not performed: {type(exc).__name__}: {exc}"
        )
        return notes
    except Exception as exc:  # noqa: BLE001 — same rule as the census above
        notes.append(
            f"independent LOAD build FAILED: {type(exc).__name__}: {exc}"
        )
        return notes
    recorder(engine.workspace, task, result)
    notes.append("independent LOAD build recorded")
    return notes


#: Producer-notes files, bound to (task_id, task_content_hash). The gates read
#: them to say WHY a witness is missing; they can never make a gate green.
EL_EVIDENCE_NOTES_FILENAME = "el_evidence_notes.json"
DUAL_BUILD_NOTES_FILENAME = "independent_build_notes.json"


def _write_evidence_notes(
    engine: Engine, task: TaskIR, filename: str, notes: list[str]
) -> None:
    """Persist what a witness producer did (or why it could not).

    A breadcrumb, not a gate and not evidence FOR one: the gates read the census and
    independent-build records themselves and go red on absence. This names the
    producer's own error instead of only its consequence."""
    (_evidence_dir(engine, task) / filename).write_text(
        canonical_json(
            {
                "task_id": task.task_id,
                "task_content_hash": task.content_hash(),
                "notes": list(notes),
            }
        ),
        encoding="utf-8",
    )


def _write_el_evidence_notes(engine: Engine, task: TaskIR, notes: list[str]) -> None:
    """Back-compatible alias for the EL witness notes file."""
    _write_evidence_notes(engine, task, EL_EVIDENCE_NOTES_FILENAME, notes)


def _transport_marker(notes: Iterable[str]) -> str:
    """Return the first anchored transport or harness marker in a producer note."""
    from elt_taskgen.engine import _INFRA_EXCEPTION_NAMES

    names = sorted(_INFRA_EXCEPTION_NAMES, key=lambda name: (-len(name), name))
    for note in notes:
        text = str(note)
        hits = [(text.find(name), rank, name) for rank, name in enumerate(names) if name in text]
        if hits:
            return min(hits)[2]
    return ""


def make_variant_gates_runner(
    variant: TaskVariant,
    provider=None,
    *,
    destination="snowflake",
    extra_destinations=(),
):
    """Stage runner for one subtask variant's acceptance battery.

    EL and T are BOTH MANDATORY: a stage PASS requires this variant's own battery to
    accept with complete coverage, and any refusal is a stage FAIL entering bounded
    repair (a PASS/accepted=false row would be skipped forever on resume and could
    leave a one-unit parent). A refused variant's emitted bundle is DELETED."""
    variant = TaskVariant(variant)
    stage = variant_gate_stage(variant)

    def run_variant_gates_stage(engine: Engine, task: TaskIR) -> StageOutcome:
        from elt_taskgen.export import eltbench as eltbench_mod
        from elt_taskgen.reference import gold as gold_mod
        from elt_taskgen.verification import variant_battery as battery_mod

        gold = gold_mod.load_gold(_answer_key_dir(engine, task))
        out_dir = _variants_root(engine, task) / variant.value

        # THE BUNDLE IS EMITTED FROM THE POPULATIONS, so what is on disk must BE the
        # IR's derivation before a byte is measured: an edited rendered/ file
        # otherwise sails through while the true reference scores 0.0 against gold.
        drifted = _population_drift_failure(engine, task)
        if drifted is not None:
            return drifted

        # Emit the bundle FIRST so the battery measures the artifact that would
        # actually ship — this closes the export-time drift hole: the gate sees the
        # same bytes release re-verifies.
        eltbench_mod.emit_variant(
            task,
            gold,
            variant,
            out_dir,
            populations_dir=engine.task_dir(task.task_id) / "populations",
            destination=destination,
            extra_destinations=tuple(extra_destinations),
        )

        transport = ""
        if variant is TaskVariant.EXTRACT_LOAD:
            notes = _ensure_el_evidence(engine, task, gold, provider)
            _write_el_evidence_notes(engine, task, notes)
            transport = _transport_marker(notes)
        if variant is TaskVariant.TRANSFORM:
            # The canonical Terraform + dbt artifact is derived from the answer
            # key and scored through the real workspace grader HERE, so the
            # battery's canonical-reachability gate judges evidence bound to
            # the bundle just emitted (the T oracles it reads are under out_dir).
            blocked = _ensure_canonical_reachability(engine, task, out_dir)
            if blocked is not None:
                return blocked

        attack_payload = _require_pass_payload(engine, task, StageName.ATTACK)
        # NEVER the legacy `rewards` field: reusing it would make this battery a
        # relabelling of the parent battery. `rewards_by_variant` is computed from
        # the SAME execution, and its absence is a RED battery, not a green one.
        rewards_by_variant = attack_payload.get("rewards_by_variant", {})

        report = battery_mod.run_variant_battery(
            variant, task, engine.workspace, gold, rewards_by_variant
        )
        (
            _evidence_dir(engine, task)
            / VARIANT_ACCEPTANCE_EVIDENCE.format(variant=variant.value)
        ).write_text(
            readable_json(report.model_dump(mode="json")), encoding="utf-8"
        )

        if report.accepted:
            try:
                battery_mod.verify_coverage(variant, report)
            except ValueError as exc:
                # An ACCEPTED battery with a roster hole is the fail-open shape
                # itself: it claims a verdict it never measured. Task-level.
                shutil.rmtree(out_dir, ignore_errors=True)
                return StageOutcome(
                    VERDICT_FAIL, StagePayload(error=f"roster coverage: {exc}")
                )
            return StageOutcome(VERDICT_PASS, report)

        stale = _evidence_currency_block(report)
        if stale is not None:
            # Evidence that is not current is a WAIT, not a refusal: keep the emitted
            # bundle and schedule the producer stage that re-derives what went stale.
            return _blocked_on_stale_evidence(engine, task, stage, stale)

        shutil.rmtree(out_dir, ignore_errors=True)
        # Variant refusals fail for repair; witness transport faults halt instead.
        judging = [
            g for g in report.gates if not g.passed and not _is_currency_refusal(g)
        ]
        if [g.gate for g in judging] == ["canonical-reachability"]:
            # Task repair cannot change unsupported compiler or connector
            # capabilities. Wait for a pipeline change and rescore afterward.
            return StageOutcome(
                VERDICT_BLOCKED,
                StagePayload(
                    error=(
                        "the task's own canonical Terraform + dbt solution is not "
                        "reachable through the RLVR workspace channel: "
                        f"{judging[0].details} Extend the portable dbt subset "
                        "(training/dbt_runner.py) or the canonical emitter "
                        "(training/canonical.py), then run validate-t again. "
                        "Nothing is rejected and no repair round is spent."
                    ),
                    data={
                        BLOCKED_ON_KEY: BLOCKED_ON_HUMAN,
                        "failure_code": "canonical_workspace_unreachable",
                    },
                ),
            )
        return StageOutcome(VERDICT_FAIL, report, infrastructure=transport)

    run_variant_gates_stage.__name__ = f"run_{stage.value}_stage"
    return run_variant_gates_stage


class CalibratePayload(BaseModel):
    """Stage-9 ledger payload: the measurement plus what calibration did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    measurement: DifficultyMeasurement
    #: '' when only the structural measurement was taken.
    roster_fingerprint: str = ""
    #: variant value -> {model_key: measured pass rate}.
    pass_rates: dict[str, dict[str, float]] = Field(default_factory=dict)
    variants_from_cache: tuple[str, ...] = ()
    impossible_variants: tuple[str, ...] = ()
    trivial_variants: tuple[str, ...] = ()
    #: Non-empty when the empirical campaign was skipped (visible, never a
    #: silent pass and never fabricated numbers).
    skipped_reason: str = ""
    detail: str = ""


def _write_difficulty(engine: Engine, task: TaskIR, measurement) -> None:
    (_evidence_dir(engine, task) / "difficulty.json").write_text(
        canonical_json(measurement.model_dump(mode="json")), encoding="utf-8"
    )


def _calibrate_outcome(engine: Engine, task: TaskIR, measurement, result) -> StageOutcome:
    """THE ONE place a calibration result becomes a ledger verdict.

    Shared by the structural runner (reading the cache) and the empirical one, so
    "no pinned solver can pass this task" means the same thing whichever command
    ran. Difficulty evidence is written here from the MERGED measurement — the
    structural runner used to overwrite difficulty.json with `empirical: null`."""
    from elt_taskgen.corpus import difficulty as difficulty_mod

    if result.empirical is not None:
        # The seam (never model_copy): with_empirical re-checks the
        # content-hash binding and refuses stale evidence.
        measurement = difficulty_mod.with_empirical(measurement, result.empirical)
    _write_difficulty(engine, task, measurement)

    pass_rates = {
        variant: record.pass_rate_vector()
        for variant, record in sorted(result.records.items())
    }
    if not result.records:
        detail = (
            f"structural difficulty only — {result.skipped_reason}"
            if result.skipped_reason
            else "structural difficulty only (empirical calibration not requested)"
        )
    else:
        detail = (
            f"calibrated {len(result.records)} variant(s) on roster "
            f"{result.roster_fingerprint[:12]}"
            + (f"; cached: {', '.join(result.from_cache)}" if result.from_cache else "")
            + (
                f"; IMPOSSIBLE: {', '.join(result.impossible_variants)} "
                "(routed to feasibility re-review)"
                if result.impossible_variants
                else ""
            )
            + (
                f"; trivial: {', '.join(result.trivial_variants)}"
                if result.trivial_variants
                else ""
            )
            + (f"; {result.skipped_reason}" if result.skipped_reason else "")
        )
    payload = CalibratePayload(
        measurement=measurement,
        # NOTHING MEASURED, NOTHING TO FINGERPRINT: an empty roster fingerprint says
        # "no campaign stands behind this row", the structural-only case.
        roster_fingerprint=result.roster_fingerprint if result.records else "",
        pass_rates=pass_rates,
        variants_from_cache=result.from_cache,
        impossible_variants=result.impossible_variants,
        trivial_variants=result.trivial_variants,
        skipped_reason=result.skipped_reason,
        detail=detail,
    )
    if result.impossible_variants:
    # Route measured infeasibility to specification repair and re-review.
        return StageOutcome(VERDICT_FAIL, payload, route=RepairRoute.SPECIFICATION)
    return StageOutcome(VERDICT_PASS, payload)


def make_structural_calibrate_runner(agents_config: Path | None = None):
    """The default, offline calibrate runner: structural difficulty, plus any
    empirical result ALREADY CACHED at this identity.

    Nothing here calls a provider, but a campaign that already ran at this content
    hash and roster is EVIDENCE, and another command must not erase it."""

    def run_calibrate_structural(engine: Engine, task: TaskIR) -> StageOutcome:
        from elt_taskgen.corpus import calibration as calibration_mod
        from elt_taskgen.corpus import difficulty as difficulty_mod

        measurement = difficulty_mod.structural_difficulty(task)
        result = calibration_mod.cached_calibration(
            task, engine.workspace, agents_config=agents_config
        )
        return _calibrate_outcome(engine, task, measurement, result)

    return run_calibrate_structural


#: Module-level default so `cli.run_calibrate` stays importable by name.
run_calibrate = make_structural_calibrate_runner()


def make_calibrate_runner(
    provider,
    *,
    empirical: bool = False,
    variants: tuple[TaskVariant, ...] | None = None,
    refresh: bool = False,
    agents_config: Path | None = None,
):
    """`calibrate` with the SolverCalibrator wired in (`--empirical`).

    Each requested variant is calibrated SEPARATELY on the PUBLIC bundle, scored
    solely by upstream_eval and cached by (content hash, variant, roster). Without
    credentials AND transcripts the campaign is a VISIBLE SKIP with the reason
    recorded; no empirical number is ever invented."""
    if not empirical:
        return make_structural_calibrate_runner(agents_config)

    def run_calibrate_empirical(engine: Engine, task: TaskIR) -> StageOutcome:
        from elt_taskgen.corpus import calibration as calibration_mod
        from elt_taskgen.corpus import difficulty as difficulty_mod
        from elt_taskgen.reference import gold as gold_mod

        measurement = difficulty_mod.structural_difficulty(task)
        gold = gold_mod.load_gold(_answer_key_dir(engine, task))
        result = calibration_mod.calibrate_task(
            task,
            gold,
            engine.workspace,
            provider,
            variants=variants or calibration_mod.DEFAULT_VARIANTS,
            refresh=refresh,
            agents_config=agents_config,
        )
        return _calibrate_outcome(engine, task, measurement, result)

    # Tagged for the repair proposer's trial certifier (review/repair_proposer
    # `CALIBRATE_EMPIRICAL_TAG`): `trial_phase` substitutes the structural
    # runner over the same roster document on the trial copy, so a failure at
    # `calibrate` or later never launches the solver campaign inside a trial.
    run_calibrate_empirical.calibrate_empirical = True
    run_calibrate_empirical.agents_config = agents_config
    return run_calibrate_empirical


def run_contamination_post(engine: Engine, task: TaskIR) -> StageOutcome:
    """`contamination_post`: post-generation scan verdict row (same service as check_pre)."""
    idx = _contamination_index(engine)
    _arm_contamination_index(idx, announce=False)
    result = idx.scan_post(
        task,
        _public_dir(engine, task),
        _answer_key_dir(engine, task),
        require=_required_firewall_coverage(),
    )
    collisions = list(result.collisions)
    evidence = {
        "task_id": task.task_id,
        "task_content_hash": task.content_hash(),
        "collisions": [c.model_dump(mode="json") for c in collisions],
        # The contamination-clean gate reads this file; the coverage travels with it
        # so the evidence can never be a bare "0 collisions".
        "coverage": _coverage_payload(engine, result.coverage),
        "coverage_summary": result.coverage.summary(),
    }
    (_evidence_dir(engine, task) / "contamination_post.json").write_text(
        canonical_json(evidence), encoding="utf-8"
    )
    fatal = [c for c in collisions if c.fatal]
    payload = ContaminationPayload(
        call_point="post",
        collisions=tuple(c.model_dump(mode="json") for c in collisions),
        fatal_count=len(fatal),
        coverage_level=result.coverage.level.value,
        coverage=_coverage_payload(engine, result.coverage),
        detail=result.detail(),
    )
    if fatal:
        return StageOutcome(VERDICT_FATAL, payload)
    return StageOutcome(VERDICT_PASS, payload)


def _accepted_variants(engine: Engine, task: TaskIR) -> frozenset[str]:
    """Variant values whose OWN battery passed at the CURRENT content hash.

    Read through export.release.variant_acceptance so selection, release and this
    CLI share one acceptance semantics — a second opinion would be a second one."""
    from elt_taskgen.export import release as release_mod

    records = release_mod.variant_acceptance(engine, task)
    return frozenset(name for name, rec in records.items() if rec.accepted)


def run_select(engine: Engine, task: TaskIR) -> StageOutcome:
    """`select`: corpus selection. Per-task orchestration selects a pool of
    one; corpus-wide quota selection over many tasks uses corpus/selection.py
    directly with the same predicate."""
    from elt_taskgen.adapters import eltbench_anchor
    from elt_taskgen.corpus import selection as selection_mod

    diff_path = _evidence_dir(engine, task) / "difficulty.json"
    if not diff_path.is_file():
        return StageOutcome(
            VERDICT_FAIL,
            StagePayload(error=f"no difficulty measurement at {diff_path} (fail closed)"),
        )
    measurement = DifficultyMeasurement.model_validate_json(
        diff_path.read_text(encoding="utf-8")
    )
    require_empirical = bool(
        getattr(engine, "require_empirical_difficulty", False)
    )
    if require_empirical:
        problem = selection_mod.empirical_evidence_problem(
            measurement,
            expected_campaign_fingerprint=getattr(
                engine, "expected_campaign_fingerprint", None
            ),
        )
        if problem is not None:
            return StageOutcome(
                VERDICT_BLOCKED,
                StagePayload(
                    error=(
                        f"selection requires empirical difficulty: {problem}; "
                        "run `calibrate --empirical` and resume"
                    ),
                    data={BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT},
                ),
            )
    try:
        anchors = eltbench_anchor.load_anchor_store(engine.workspace)
    except FileNotFoundError:
        anchors = []

    quotas = selection_mod.Quotas(size=1, val_fraction=0.0)
    result = selection_mod.select(
        [task],
        {task.task_id: measurement},
        anchors,
        quotas,
        accepted_variants={task.task_id: _accepted_variants(engine, task)},
        require_empirical=require_empirical,
        expected_campaign_fingerprint=getattr(
            engine, "expected_campaign_fingerprint", None
        ),
    )
    if task.task_id in result.rejected:
        return StageOutcome(
            VERDICT_FAIL,
            StagePayload(error=f"selection rejected: {result.rejected[task.task_id]}"),
        )
    _atomic_replace_text(
        _evidence_dir(engine, task) / "selection.json",
        canonical_json(result.model_dump(mode="json")),
    )
    return StageOutcome(VERDICT_PASS, result)


def run_audit(engine: Engine, task: TaskIR) -> StageOutcome:
    """Run audit, passing automatically only when no human review remains.

    Approvals bind the current hash and exact pending collisions. Defects fail;
    human waits block; current human rejection is fatal.
    """
    current = task.content_hash()
    problems: list[str] = []
    waiting: list[str] = []

    rejection_path = _rejection_path(engine, task.task_id)
    if rejection_path.is_file():
        rejection = json.loads(rejection_path.read_text(encoding="utf-8"))
        if rejection.get("task_content_hash") == current:
            return StageOutcome(
                VERDICT_FATAL,
                StagePayload(
                    error="human audit rejected this task: "
                    + str(rejection.get("reason", "(no reason recorded)"))
                ),
            )
        # A rejection of an earlier identity does not bind the repaired task, which
        # re-enters the queue on its own merits.

    if not task.license or task.license.lower() == "unspecified":
        # "A human must resolve it" is a queue wait, not a defect the pipeline can
        # repair. (It used to keyword-route FATAL off "licens" and reject the task.)
        waiting.append(
            "license is unresolved ('unspecified'); a human must resolve it "
            "(record it on the task and re-ingest, or reject it)"
        )

    gates_row = engine.latest_report(task.task_id, StageName.TASK_INTEGRITY.value)
    if (
        gates_row is None
        or gates_row.verdict != VERDICT_PASS
        or gates_row.content_hash != current
    ):
        problems.append("no shared task-integrity pass at the current content hash")

    from elt_taskgen.export import release as release_mod

    unit_records = release_mod.variant_acceptance(engine, task)
    for variant in RLVR_TASK_VARIANTS:
        record = unit_records[variant.value]
        if not record.accepted:
            problems.append(
                f"required {variant.value} unit is not accepted: "
                f"{record.refusal_reason}"
            )

    pending, evidence_problem = _pending_borderline(engine, task)
    if evidence_problem:
        problems.append(evidence_problem)

    reviewer = ""
    approved_at = ""
    approval_path = _approval_path(engine, task.task_id)
    if pending:
        if not approval_path.is_file():
            waiting.append(
                f"{len(pending)} borderline collision(s) require human sign-off "
                f"(run: elt-taskgen audit approve {task.task_id} --reviewer NAME)"
            )
        else:
            try:
                approval = AuditApproval.model_validate_json(
                    approval_path.read_text(encoding="utf-8")
                )
            except Exception as exc:
                approval = None
                waiting.append(f"approval record at {approval_path} is invalid: {exc}")
            if approval is not None:
                if approval.task_id != task.task_id:
                    waiting.append(
                        f"approval record is for task {approval.task_id!r}, "
                        f"not {task.task_id!r}"
                    )
                elif approval.task_content_hash != current:
                    waiting.append(
                        "approval is STALE: bound to content hash "
                        f"{approval.task_content_hash[:12]}, task is now {current[:12]} "
                        "— a pre-repair sign-off never carries over; re-review and "
                        f"re-approve (elt-taskgen audit approve {task.task_id})"
                    )
                else:
                    approved = set(approval.approved_collision_fingerprints)
                    unapproved = sorted(set(pending) - approved)
                    extra = sorted(approved - set(pending))
                    if unapproved:
                        waiting.append(
                            f"approval does not cover {len(unapproved)} pending "
                            "borderline collision(s): "
                            + ", ".join(fp[:12] for fp in unapproved)
                        )
                    if extra:
                        waiting.append(
                            f"approval covers {len(extra)} fingerprint(s) that are "
                            "not pending: " + ", ".join(fp[:12] for fp in extra)
                            + " — coverage must be exact; re-approve"
                        )
                    if not unapproved and not extra:
                        reviewer = approval.reviewer
                        approved_at = approval.approved_at

    # DEFECTS OUTRANK THE QUEUE: a task that is not certified cannot be waiting for
    # a signature, and parking it BLOCKED would hide a repairable defect. The queue
    # items are still REPORTED in the same message.
    if problems:
        return StageOutcome(
            VERDICT_FAIL, StagePayload(error="; ".join(problems + waiting))
        )
    if waiting:
        return StageOutcome(
            VERDICT_BLOCKED,
            StagePayload(
                error="; ".join(waiting),
                data={
                    BLOCKED_ON_KEY: BLOCKED_ON_HUMAN,
                    "pending": str(len(pending)),
                },
            ),
        )

    data = {"borderline_collisions": str(len(pending)), "license": task.license}
    if pending:
        data.update(
            {
                "reviewer": reviewer,
                "approved_at": approved_at,
                "approval": _workspace_rel(engine, approval_path),
            }
        )
        detail = (
            f"{len(pending)} borderline collision(s) signed off by {reviewer} "
            "at the current content hash; license resolved"
        )
    else:
        detail = "audit queue empty: no borderline collisions pending, license resolved"
    return StageOutcome(VERDICT_PASS, StagePayload(detail=detail, data=data))


def _release_names_el_sources(recorded: dict, task: TaskIR) -> bool:
    """Does an existing release manifest carry this task's EL source roots?

    The EL reward is computed over EVERY graded population, so a manifest WITHOUT
    them is a stale release and 'already released' would be a false statement.
    Capability-checked, so a tree whose exporter predates the field is unaffected
    rather than permanently blocked."""
    from elt_taskgen.export import release as release_mod

    if "el_sources" not in release_mod.ReleaseManifest.model_fields:
        return True  # exporter predates EL source shipping: nothing to require
    return bool(recorded.get("el_sources", {}).get(task.task_id))


def _release_coverage_refusal(engine: Engine) -> str | None:
    """Why `release` must not ship from this workspace's firewall, or None.

    RELEASE IS THE ONLY STAGE THAT EMITS TRAINABLE DATA, so ARMED coverage is
    required UNCONDITIONALLY here while every other call point stays warn-only (the
    tests and offline paths have no benchmark checkout). A name-only index answers
    "is this task NAMED like a benchmark", which is not a firewall."""
    from elt_taskgen.verification import contamination as _cont
    if not _cont.enforcing():
        return None  # coverage is recorded, not required
    from elt_taskgen.verification import contamination as cont

    coverage = _contamination_index(engine).coverage()
    if coverage.level is cont.CoverageLevel.ARMED:
        return None
    return (
        "contamination coverage is "
        f"{coverage.level.value!r}, not ARMED — release is the only stage that "
        "emits trainable data and it will not ship behind a name-only check. "
        f"{coverage.summary()} Run: elt-taskgen measure-target --workspace "
        "<ws> --bench-root <ELT-Bench checkout>."
    )


def _environment_drift_refusal(args_allow_unlocked: bool) -> tuple[str | None, dict[str, str]]:
    """(refusal or None, recorded environment provenance).

    A release cut from an environment that does not match uv.lock either says so or
    does not happen; `--allow-unlocked-env` records the drift instead of hiding it.
    The check itself lives in export/release.py; this pre-check exists so the answer
    is BLOCKED(environment) — a wait on `uv sync --frozen` — instead of a FAIL that
    spends a repair round and rejects the task for an environment condition."""
    from elt_taskgen.export import release as release_mod

    drift_fn = getattr(release_mod, "environment_drift", None)
    if drift_fn is None:  # exporter predates the provenance check
        return None, {}
    drift = drift_fn()
    if not drift:
        return None, {}
    described = ", ".join(
        f"{pkg} installed {installed!r} != locked {locked!r}"
        for pkg, (installed, locked) in sorted(drift.items())
    )
    if args_allow_unlocked:
        return None, {"environment_drift": described}
    return (
        f"runtime environment does not match uv.lock: {described}. Run "
        "`uv sync --frozen`, or pass --allow-unlocked-env to release anyway "
        "(the drift is then RECORDED in the release manifest, never hidden)."
    ), {}


def _existing_certified_attestation_problem(
    engine: Engine,
    task: TaskIR,
    recorded: dict,
) -> str | None:
    """Why a supplied attestation cannot authorize an existing certified tree."""
    from elt_taskgen.export import release as release_mod
    from elt_taskgen.export.attestation_gate import (
        AttestationRefusal,
        require_attestation_for_labels,
    )
    from elt_taskgen.runtime.attestation import (
        SandboxAttestationError,
        seal_sandbox_attestation,
        verify_sandbox_attestation,
    )
    from elt_taskgen.verification import contamination as contamination_mod

    supplied = getattr(engine, "sandbox_attestation", None)
    if supplied is None:
        return "the requested certified release has no sandbox attestation"
    release_id = str(recorded.get("release_id") or "")
    if not release_id:
        return "the existing certified release has no release identity"
    try:
        supplied = verify_sandbox_attestation(supplied)
        if not supplied.run_id:
            supplied = seal_sandbox_attestation(
                supplied.model_copy(
                    update={"run_id": release_id, "attestation_digest": ""}
                )
            )
        decision = require_attestation_for_labels(
            {
                "corpus_profile": release_mod.COMBINED_CORPUS_PROFILE,
                "public_layout": release_mod.COMBINED_PUBLIC_LAYOUT,
                "variants": {
                    task.task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)
                },
            },
            supplied,
            contamination_mode=contamination_mod.enforcement(),
            run_id=release_id,
        )
    except (AttestationRefusal, SandboxAttestationError, ValueError) as exc:
        return (
            "the requested sandbox attestation is not current for this release: "
            f"{exc}"
        )
    recorded_digest = str(recorded.get("sandbox_attestation_digest") or "")
    if decision.attestation_digest != recorded_digest:
        return (
            "the requested sandbox attestation does not match the existing "
            "certified release"
        )
    return None


def run_release(engine: Engine, task: TaskIR) -> StageOutcome:
    """`release`: frozen release (immutable tree; freeze never decides
    acceptance — it re-verifies the ledger and refuses anything unaccepted)."""
    from elt_taskgen.corpus.selection import SelectionResult
    from elt_taskgen.export import release as release_mod

    sel_path = _evidence_dir(engine, task) / "selection.json"
    if not sel_path.is_file():
        return StageOutcome(
            VERDICT_FAIL,
            StagePayload(error=f"no selection record at {sel_path} (fail closed)"),
        )
    selection = SelectionResult.model_validate_json(
        sel_path.read_text(encoding="utf-8")
    )
    requested_release_mode = str(getattr(engine, "release_mode", "development"))
    require_empirical = bool(
        getattr(engine, "require_empirical_difficulty", False)
    ) or requested_release_mode == release_mod.CERTIFIED_RELEASE_MODE
    difficulty_digest = ""
    if require_empirical:
        if not selection.empirical_required:
            return StageOutcome(
                VERDICT_BLOCKED,
                StagePayload(
                    error=(
                        "release requires an empirical selection record; the "
                        "current selection was structural-only — re-run `select`"
                    ),
                    data={BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT},
                ),
            )
        diff_path = _evidence_dir(engine, task) / "difficulty.json"
        if not diff_path.is_file():
            return StageOutcome(
                VERDICT_BLOCKED,
                StagePayload(
                    error="release requires empirical difficulty evidence",
                    data={BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT},
                ),
            )
        from elt_taskgen.corpus import selection as selection_mod

        measurement = DifficultyMeasurement.model_validate_json(
            diff_path.read_text(encoding="utf-8")
        )
        if (
            measurement.task_id != task.task_id
            or measurement.task_content_hash != task.content_hash()
        ):
            problem = "difficulty evidence is not bound to the current task identity"
        else:
            problem = selection_mod.empirical_evidence_problem(
                measurement,
                expected_campaign_fingerprint=getattr(
                    engine, "expected_campaign_fingerprint", None
                ),
            )
        if problem is not None:
            return StageOutcome(
                VERDICT_BLOCKED,
                StagePayload(
                    error=f"release requires empirical difficulty: {problem}",
                    data={BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT},
                ),
            )
        difficulty_digest = sha256_hex(
            canonical_json(measurement.model_dump(mode="json"))
        )

    out_dir = engine.workspace / "release"
    if out_dir.exists():
        manifest_path = out_dir / "release_manifest.json"
        if manifest_path.is_file():
            recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected_units = [v.value for v in RLVR_TASK_VARIANTS]
            recorded_mode = str(
                recorded.get("release_mode")
                or release_mod.DEVELOPMENT_RELEASE_MODE
            )
            policy_problem = None
            if recorded_mode != requested_release_mode:
                policy_problem = (
                    f"existing release mode is {recorded_mode!r}, but this run "
                    f"requires {requested_release_mode!r}"
                )
            elif require_empirical and (
                recorded.get("difficulty_measurements", {}).get(task.task_id)
                != difficulty_digest
            ):
                policy_problem = (
                    "existing release does not bind the current empirical "
                    "difficulty measurement"
                )
            elif requested_release_mode == release_mod.CERTIFIED_RELEASE_MODE:
                policy_problem = _existing_certified_attestation_problem(
                    engine, task, recorded
                )
            if (
                policy_problem is None
                and recorded.get("tasks", {}).get(task.task_id) == task.content_hash()
                and recorded.get("corpus_profile")
                == release_mod.COMBINED_CORPUS_PROFILE
                and recorded.get("public_layout")
                == release_mod.COMBINED_PUBLIC_LAYOUT
                and recorded.get("variants", {}).get(task.task_id) == expected_units
                and _release_names_el_sources(recorded, task)
            ):
                # NAMING THIS TASK IS NOT THE SAME AS CONTAINING IT: the shortcut
                # trusted the manifest's own words while a tampered file left
                # verify_release ok=False. Re-verify the BYTES before saying done.
                result = release_mod.verify_release(out_dir)
                if not result.ok:
                    # SAME CONDITION AS ITS SIBLING BELOW: a corrupt tree in the way
                    # is the ENVIRONMENT, not a task defect. As an ordinary FAIL it
                    # spends the budget re-running deterministic stages and ends in
                    # the inert/budget FATAL that rejects a certified task.
                    return StageOutcome(
                        VERDICT_BLOCKED,
                        StagePayload(
                            error=(
                                f"release dir {out_dir} names this task at the "
                                "current content hash but FAILS byte "
                                "verification: "
                                + "; ".join(result.failures[:3])
                                + " — release output is immutable; move it "
                                "aside to re-release"
                            ),
                            data={
                                "release_dir": _workspace_rel(engine, out_dir),
                                BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT,
                            },
                        ),
                    )
                # Idempotent on the shortcut path too: the admitted-corpus index is
                # what stops a later near-duplicate from being ingested.
                _contamination_index(engine).add_admitted_task(task)
                return StageOutcome(
                    VERDICT_PASS,
                    StagePayload(
                        detail=(
                            f"already released as {recorded.get('release_id', '?')} "
                            f"({result.files_checked} file(s) re-verified)"
                        ),
                        data={"release_dir": _workspace_rel(engine, out_dir)},
                    ),
                )
            if policy_problem is not None:
                return StageOutcome(
                    VERDICT_BLOCKED,
                    StagePayload(
                        error=(
                            f"release dir {out_dir} cannot satisfy the requested "
                            f"release policy: {policy_problem} — release output is "
                            "immutable; move it aside to re-release"
                        ),
                        data={
                            "release_dir": _workspace_rel(engine, out_dir),
                            BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT,
                        },
                    ),
                )
        # NOT A DEFECT AND NOT A REPAIR: a directory in the way is the ENVIRONMENT.
        # Routed as an ordinary failure this rejected a certified task; the operator
        # moves it aside and re-runs.
        return StageOutcome(
            VERDICT_BLOCKED,
            StagePayload(
                error=f"release dir {out_dir} exists with different content; "
                "release output is immutable (move it aside to re-release)",
                data={BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT},
            ),
        )

    unarmed = _release_coverage_refusal(engine)
    if unarmed is not None:
    # Block environmental contamination-readiness refusals; do not route them fatal.
        return StageOutcome(
            VERDICT_BLOCKED,
            StagePayload(
                error=unarmed, data={BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT}
            ),
        )
    drift, environment_data = _environment_drift_refusal(
        bool(getattr(engine, "allow_unlocked_env", False))
    )
    if drift is not None:
        return StageOutcome(
            VERDICT_BLOCKED,
            StagePayload(
                error=drift, data={BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT}
            ),
        )

    try:
        manifest = release_mod.freeze_release(
            engine,
            selection,
            out_dir,
            release_mode=str(getattr(engine, "release_mode", "development")),
            sandbox_attestation=getattr(engine, "sandbox_attestation", None),
            agents_config=getattr(engine, "agents_config", None),
        )
    except Exception as exc:  # map evidence/isolation refusals to a resumable wait
        from elt_taskgen.export.attestation_gate import AttestationRefusal

        if isinstance(exc, AttestationRefusal):
            return StageOutcome(
                VERDICT_BLOCKED,
                StagePayload(
                    error=f"release attestation refused [{exc.code}]: {exc}",
                    data={
                        BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT,
                        "refusal_code": exc.code,
                    },
                ),
            )
        raise
    # SELF-CHECK THE FREEZE: verify_release was called from nowhere in src/, and the
    # README's `shasum -c` remedy skips every census-pinned .duckdb warehouse. A
    # partial write must fail the stage, not be discovered by a consumer.
    verified = release_mod.verify_release(out_dir)
    if not verified.ok:
        # A HALF-WRITTEN TREE IS THE ENVIRONMENT, NOT THE TASK: no edit to prose,
        # reference SQL or population conditions can make a partial write verify, so
        # a repair round here ends in the FATAL that rejects a certified task.
        return StageOutcome(
            VERDICT_BLOCKED,
            StagePayload(
                error=(
                    f"the freeze at {out_dir} does not verify against its own "
                    "manifest: " + "; ".join(verified.failures[:3])
                    + " — release output is immutable; move it aside and "
                    "re-run `release`"
                ),
                data={
                    "release_dir": _workspace_rel(engine, out_dir),
                    BLOCKED_ON_KEY: BLOCKED_ON_ENVIRONMENT,
                },
            ),
        )
    _contamination_index(engine).add_admitted_task(task)
    return StageOutcome(
        VERDICT_PASS,
        StagePayload(
            detail=f"frozen release {manifest.release_id} "
            f"({len(manifest.tasks)} task(s), {len(manifest.checksums)} files)",
            data={
                "release_id": manifest.release_id,
                "release_dir": _workspace_rel(engine, out_dir),
                "scorer_version": manifest.scorer_version,
                "generator_version": manifest.generator_version,
                "verified_files": str(verified.files_checked),
                **environment_data,
            },
        ),
    )


# Deterministic pre-council filters run before provider calls. Every result is
# recorded and either routes for repair or rejects according to configuration.

def _filter_config():
    from elt_taskgen.verification import filters as filters_mod

    return filters_mod.load_filter_config()


def _record_filter_report(engine: Engine, task: TaskIR, report) -> str:
    """Write + ledger-record one filter artifact; returns its workspace path."""
    from elt_taskgen.verification import filters as filters_mod

    path, sha = filters_mod.write_filter_report(report, _evidence_dir(engine, task))
    rel = _workspace_rel(engine, path)
    engine.record_artifact(task, rel, sha)
    return rel


def _filter_outcome(report, rel_path: str) -> StageOutcome:
    """Turn a tripped filter into the configured stage outcome (never a pass)."""
    from elt_taskgen.verification import filters as filters_mod

    summaries = "; ".join(f.summary for f in report.findings)
    payload = StagePayload(
        error=f"{report.filter} filter: {summaries}",
        data={
            "filter": report.filter,
            "artifact": rel_path,
            "action": report.action.value if report.action else "",
            "findings": str(len(report.findings)),
            "config": report.config_source,
        },
    )
    if report.action is filters_mod.FilterAction.REJECT:
        return StageOutcome(VERDICT_FATAL, payload)
    return StageOutcome(VERDICT_FAIL, payload, route=report.route)


def _task_was_admitted(engine: Engine, task_id: str) -> bool:
    """Did this corpus already admit this task? (near-duplicate firewall only)

    HASH-BOUND, DELIBERATELY NOT ROSTER-AWARE: `final_verdict` answers "may this
    ship RIGHT NOW under the CURRENT scorer", and reusing it made the admitted
    corpus shrink on every SCORER_VERSION bump — a fail-OPEN direction for a
    firewall. So: a frozen `release` PASS at this identity, or both RLVR unit
    batteries recorded PASS claiming acceptance. Both hash-bound, so an EDITED task
    stops counting."""
    from elt_taskgen.models import TaskStatus

    try:
        other = engine.load_task(task_id)
    except EngineError:
        return False
    if other.status is TaskStatus.REJECTED:
        # A rejected task is not in the corpus, and refusing a fresh candidate for
        # resembling one is a yield loss with nothing behind it.
        return False
    current = other.content_hash()

    def _passed_here(stage: StageName):
        row = engine.latest_report(task_id, stage.value)
        if row is None or row.verdict != VERDICT_PASS:
            return None
        if engine.report_is_superseded(other, row):
            return None
        return row if row.content_hash == current else None

    if _passed_here(StageName.RELEASE) is not None:
        return True
    for variant in RLVR_TASK_VARIANTS:
        row = _passed_here(variant_gate_stage(variant))
        if row is None:
            return False
        try:
            payload = json.loads(row.payload_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
        if payload.get("accepted") is not True:
            return False
    return True


def _admitted_tasks(engine: Engine, task: TaskIR) -> list[TaskIR]:
    """Every OTHER registered task this corpus has already admitted.

    Admission is read from the ledger, never from a side file: a task that was never
    admitted cannot make a later candidate a duplicate. See `_task_was_admitted` for
    why the test is hash-bound rather than roster-aware."""
    tasks_root = engine.workspace / "tasks"
    if not tasks_root.is_dir():
        return []
    admitted: list[TaskIR] = []
    for tdir in sorted(tasks_root.iterdir()):
        if tdir.name == task.task_id or not (tdir / "task_ir.json").is_file():
            continue
        try:
            if not _task_was_admitted(engine, tdir.name):
                continue
            admitted.append(engine.load_task(tdir.name))
        except Exception:  # noqa: BLE001 - an unreadable task is not evidence
            continue
    return admitted


def _record_intake_filter_reports(
    engine: Engine, task: TaskIR, reports: Iterable
) -> StageOutcome | None:
    """Record a complete intake-filter bundle and return its strongest block."""
    from elt_taskgen.verification import filters as filters_mod

    blocked: list[tuple] = []
    for report in reports:
        rel = _record_filter_report(engine, task, report)
        if not report.passed:
            blocked.append((report, rel))
    if not blocked:
        return None
    report, rel = next(
        (
            (r, p)
            for r, p in blocked
            if r.action is filters_mod.FilterAction.REJECT
        ),
        blocked[0],
    )
    return _filter_outcome(report, rel)


def run_intake_filters(engine: Engine, task: TaskIR) -> StageOutcome | None:
    """Intake format blacklist + near-duplicate dedup. None => admitted.

    BOTH artifacts are recorded even when the first already blocks — the ledger must
    show what was checked, not just what stopped the task. When both trip, the
    strongest consequence wins: a REJECT is never softened into a repairable route."""
    from elt_taskgen.verification import filters as filters_mod

    reports = filters_mod.run_intake_filters(
        task, _admitted_tasks(engine, task), _filter_config()
    )
    return _record_intake_filter_reports(engine, task, reports)


def _pipeline_batch_intake_preflight(
    engine: Engine,
    scheduled_tasks: dict[str, TaskIR],
) -> tuple[frozenset[str], tuple[dict, ...]]:
    """Serially filter fresh candidates before concurrent pipeline intake.

    Stable ordering makes each admitted candidate a comparator for later peers.
    Current contamination passes remain resume incumbents.
    """
    from elt_taskgen.verification import filters as filters_mod

    if not scheduled_tasks:
        return frozenset(), ()

    ordered_ids = tuple(sorted(scheduled_tasks))
    stopped: set[str] = set()
    results: list[dict] = []
    with engine.task_locks(ordered_ids):
        current_tasks: dict[str, TaskIR] = {}
        for task_id in ordered_ids:
            engine.recover_pending_repair(task_id)
            current = engine.load_task(task_id)
            expected = scheduled_tasks[task_id]
            if current.content_hash() != expected.content_hash():
                raise CliUsageError(
                    f"pipeline task {task_id!r} changed during intake preflight; "
                    "no workers were started — re-run the pipeline"
                )
            current_tasks[task_id] = current

        incumbents: dict[str, TaskIR] = {}
        fresh: list[TaskIR] = []
        for task_id in ordered_ids:
            task = current_tasks[task_id]
    # Ignore REJECTED only under an exact, still-valid evidence supersession.
            final = engine.final_verdict(task_id)
            reopen = (
                task.status is TaskStatus.REJECTED
                and final != FINAL_REJECTED
                and engine.has_active_report_supersession(task)
            )
            if final == FINAL_REJECTED or (
                task.status is TaskStatus.REJECTED and not reopen
            ):
                stopped.add(task_id)
                results.append(
                    {
                        "task_id": task_id,
                        "ok": False,
                        "state": FINAL_REJECTED,
                        "detail": (
                            "task was already rejected before candidate preparation"
                        ),
                        "usd": 0.0,
                    }
                )
                continue
            row = engine.latest_report(task_id, StageName.CONTAMINATION_PRE.value)
            current_pass = (
                row is not None
                and engine.report_is_current(
                    task, StageName.CONTAMINATION_PRE, row
                )[0]
            )
            if current_pass:
                # A resumed task already crossed intake. It is an incumbent for
                # fresh scheduled candidates even if later stages are unfinished.
                incumbents[task_id] = task
            else:
                fresh.append(task)

        # Preserve the existing ledger-derived admitted-index check. The first
        # fresh task cannot itself be admitted (otherwise its hash-bound stage
        # evidence would already be current), so it is a safe scan subject.
        if fresh:
            for admitted in _admitted_tasks(engine, fresh[0]):
                incumbents[admitted.task_id] = admitted

        config = _filter_config()
        provisional: dict[str, TaskIR] = {}
        for task in fresh:
            comparators = [
                candidate
                for task_id, candidate in sorted(
                    {**incumbents, **provisional}.items()
                )
                if task_id != task.task_id
            ]
            reports = filters_mod.run_intake_filters(task, comparators, config)

            def run_preflight_intake(
                locked_engine: Engine,
                locked_task: TaskIR,
                *,
                frozen_reports=reports,
            ) -> StageOutcome:
                blocked = _record_intake_filter_reports(
                    locked_engine, locked_task, frozen_reports
                )
                if blocked is not None:
                    return blocked
                return run_contamination_pre(locked_engine, locked_task)

            engine.set_stage_runner(
                StageName.CONTAMINATION_PRE, run_preflight_intake
            )
            try:
                engine.run(task.task_id, until=StageName.CONTAMINATION_PRE.value)
            except InfrastructureFailure as exc:
                stopped.add(task.task_id)
                results.append(
                    {
                        "task_id": task.task_id,
                        "ok": False,
                        "state": "infrastructure",
                        "detail": str(exc),
                        "usd": 0.0,
                    }
                )
                continue

            current = engine.load_task(task.task_id)
            row = engine.latest_report(
                task.task_id, StageName.CONTAMINATION_PRE.value
            )
            current_pass = (
                row is not None
                and engine.report_is_current(
                    current, StageName.CONTAMINATION_PRE, row
                )[0]
            )
            if current_pass:
                provisional[current.task_id] = current
                continue

            stopped.add(task.task_id)
            final = engine.final_verdict(task.task_id)
            state = FINAL_REJECTED if final == FINAL_REJECTED else "blocked"
            detail = "candidate intake did not pass"
            if row is not None:
                try:
                    payload = json.loads(row.payload_json)
                    detail = str(
                        payload.get("error") or payload.get("detail") or detail
                    )
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            results.append(
                {
                    "task_id": task.task_id,
                    "ok": False,
                    "state": state,
                    "detail": detail,
                    "usd": 0.0,
                }
            )

    return frozenset(stopped), tuple(results)


def run_execution_effect_filter(engine: Engine, task: TaskIR) -> StageOutcome | None:
    """Counterfactual/stress populations must move an observable output."""
    from elt_taskgen.reference import gold as gold_mod
    from elt_taskgen.verification import filters as filters_mod

    gold = gold_mod.load_gold(_answer_key_dir(engine, task))
    report = filters_mod.execution_effect_filter(task, gold, _filter_config())
    rel = _record_filter_report(engine, task, report)
    return None if report.passed else _filter_outcome(report, rel)


def _with_intake_filters(inner):
    """Run the intake filters BEFORE the wrapped stage does any work."""

    def wrapped(engine: Engine, task: TaskIR) -> StageOutcome:
        blocked = run_intake_filters(engine, task)
        if blocked is not None:
            return blocked
        return inner(engine, task)

    return wrapped


def _with_execution_effect_filter(inner):
    """Run the execution-effect filter on the outputs the stage just froze."""

    def wrapped(engine: Engine, task: TaskIR) -> StageOutcome:
        outcome = inner(engine, task)
        if outcome.verdict != VERDICT_PASS:
            return outcome
        blocked = run_execution_effect_filter(engine, outcome.task or task)
        return blocked if blocked is not None else outcome

    return wrapped


def _with_attack_matrix_precheck(inner):
    """Measure the declared attack matrix the moment gold exists.

    The reference stage freezes gold; that is the first point at which every
    required mutant can be executed against every population, deterministically
    and at no provider cost. A declared kill that does not reproduce is refused
    HERE, routed to POPULATION, rather than two stages later at the author's
    own precheck (which stays: it protects standalone runs of that stage and
    is a memo hit after this one).
    """

    def wrapped(engine: Engine, task: TaskIR) -> StageOutcome:
        outcome = inner(engine, task)
        if outcome.verdict != VERDICT_PASS:
            return outcome
        unreproducible = _attack_matrix_failure_detail(engine, outcome.task or task)
        if unreproducible is None:
            return outcome
        return StageOutcome(
            VERDICT_FAIL,
            StagePayload(error=unreproducible),
            route=RepairRoute.POPULATION,
        )

    return wrapped


def build_stage_runners(
    provider,
    *,
    echo=None,
    calibrate_options=None,
    destination="snowflake",
    extra_destinations=(),
):
    """Wire a runner for every ledger stage except the built-in intake.

    `calibrate_options` opts `calibrate` into the empirical SolverCalibrator;
    omitted, calibration stays structural-only — a solver campaign is never a side
    effect of running the pipeline."""
    runners = {
        # The intake filters gate `contamination_pre` and the execution-effect filter
        # gates `reference`: deterministic, and before any provider call.
        StageName.CONTAMINATION_PRE: _with_intake_filters(run_contamination_pre),
        StageName.GENERATE: run_generate,
        StageName.REFERENCE: _with_attack_matrix_precheck(
            _with_execution_effect_filter(run_reference_stage)
        ),
        StageName.AUTHOR: make_author_runner(provider),
        StageName.REVIEW: make_review_runner(provider),
        StageName.ATTACK: run_attack_stage,
        StageName.TASK_INTEGRITY: make_gates_runner(
            provider, destination=destination, extra_destinations=extra_destinations
        ),
        # The two subtask batteries: same frozen artifacts, same content hash,
        # their OWN rewards, rosters and witnesses (design R1/R2).
        StageName.GATES_EXTRACT_LOAD: make_variant_gates_runner(
            TaskVariant.EXTRACT_LOAD,
            provider,
            destination=destination,
            extra_destinations=extra_destinations,
        ),
        StageName.GATES_TRANSFORM: make_variant_gates_runner(
            TaskVariant.TRANSFORM,
            provider,
            destination=destination,
            extra_destinations=extra_destinations,
        ),
        StageName.CALIBRATE: make_calibrate_runner(
            provider, **(dict(calibrate_options or {}))
        ),
        StageName.CONTAMINATION_POST: run_contamination_post,
        StageName.SELECT: run_select,
        StageName.AUDIT: run_audit,
        StageName.RELEASE: run_release,
    }
    if echo is None:
        return runners

    def logged(stage: StageName, runner):
        # `wraps` carries the runner's tags (the empirical `calibrate` tag the
        # proposer's trial certifier reads) through the echo wrapper.
        @functools.wraps(runner)
        def wrapped(engine: Engine, task: TaskIR) -> StageOutcome:
            echo(f"  -> {stage.value} ...")
            outcome = runner(engine, task)
            echo(f"     {stage.value}: {outcome.verdict}")
            return outcome

        return wrapped

    return {stage: logged(stage, runner) for stage, runner in runners.items()}


def _resolve_provider(
    args,
    workspace: Path,
    *,
    replay_only: bool | None = None,
    refresh: bool | None = None,
    fixtures_dir: Path | None = None,
    source: str = "",
):
    """Build ``RoutedProvider`` from agent configuration and CLI flags.

    Credentials are required only for live calls. Production replay uses this
    workspace, while diagnostic metrology may use fixtures; live metrology
    passes ``refresh=True`` without fixtures. Reject combined ``--record`` and
    ``--replay-only`` before the repair router can misclassify that invalid
    configuration as a task defect."""
    from elt_taskgen.review import providers as providers_mod

    retry_values = {
        "http_retries": getattr(args, "http_retries", None),
        "schema_retries": getattr(args, "schema_retries", None),
        "http_timeout_seconds": getattr(args, "http_timeout_seconds", None),
        "http_backoff_seconds": getattr(args, "http_backoff_seconds", None),
    }
    if any(value is not None for value in retry_values.values()):
        providers_mod.configure_retry_policy(
            http_retries=(
                4
                if retry_values["http_retries"] is None
                else retry_values["http_retries"]
            ),
            schema_retries=(
                2
                if retry_values["schema_retries"] is None
                else retry_values["schema_retries"]
            ),
            http_timeout_seconds=(
                600.0
                if retry_values["http_timeout_seconds"] is None
                else retry_values["http_timeout_seconds"]
            ),
            http_backoff_seconds=(
                2.0
                if retry_values["http_backoff_seconds"] is None
                else retry_values["http_backoff_seconds"]
            ),
        )

    routing = providers_mod.load_role_routing(
        getattr(args, "agents_config", None) or None
    )
    store = providers_mod.TranscriptStore(
        workspace / "transcripts",
        fixtures_dir=fixtures_dir,
    )
    durable_ledger = None
    durable_total = getattr(args, "global_budget_total", None)
    durable_run_id = str(getattr(args, "global_budget_run_id", "") or "")
    if durable_total is not None and durable_run_id:
        from elt_taskgen.review.budget_ledger import initialize

        durable_workspace = Path(
            getattr(args, "global_budget_workspace", None) or workspace
        ).resolve()
        durable_ledger = initialize(
            durable_run_id,
            float(durable_total),
            durable_workspace,
            per_task_limit_usd=getattr(
                args,
                "global_budget_per_task",
                getattr(args, "budget_per_task", None),
            ),
            enforce_task_limit=bool(
                getattr(args, "global_budget_enforce_task_limit", True)
            ),
        )
    # A schema correction is a new logical provider attempt and every one of
    # those attempts may independently consume the HTTP retry allowance.  The
    # dimensions therefore multiply; taking their maximum under-reserves the
    # exact failure mode the shared prospective ledger is meant to contain.
    retry_multiplier = (
        1 + int(getattr(args, "http_retries", 0) or 0)
    ) * (
        1 + int(getattr(args, "schema_retries", 0) or 0)
    )
    meter = providers_mod.CostMeter(
        budget_per_task_usd=getattr(
            args, "budget_per_task", providers_mod.DEFAULT_BUDGET_PER_TASK_USD
        ),
        budget_total_usd=getattr(args, "budget_total", None),
        # The roles' declared caps (agents.yaml `session.max_usd`), enforced
        # per trajectory beside the per-task budget; none declared = uncapped.
        budget_per_role_usd=routing.usd_caps(),
        durable_ledger=durable_ledger,
        durable_reservation_multiplier=float(retry_multiplier),
    )
    resolved_replay_only = (
        replay_only
        if replay_only is not None
        else bool(getattr(args, "replay_only", False))
    )
    requested_refresh = bool(getattr(args, "record", False))
    if requested_refresh and resolved_replay_only:
        raise CliUsageError(
            "--record and --replay-only are contradictory: --record forces "
            "fresh LIVE calls, --replay-only forbids live calls. Pick one "
            "(drop --record to replay recorded transcripts)."
        )
    resolved_refresh = requested_refresh if refresh is None else bool(refresh)
    if resolved_refresh and resolved_replay_only:
        raise CliUsageError(
            "fresh provider calls and replay-only are contradictory (internal "
            "provider resolution refused the combination)"
        )
    provider = providers_mod.RoutedProvider(
        routing,
        store,
        meter,
        replay_only=resolved_replay_only,
        refresh=resolved_refresh,
        task_id=str(getattr(args, "task_id", "") or ""),
        source=source,
    )
    provider.admission_reference = str(
        getattr(args, "admission_reference", "") or ""
    )
    return provider


def _calibrate_options(args) -> dict:
    """Empirical-calibration wiring from the `calibrate` subcommand's flags; absent
    flags (every other command) leave calibration structural-only."""
    if bool(getattr(args, "allow_structural_difficulty", False)):
        return {}
    if not bool(getattr(args, "empirical", False)):
        return {}
    spec = getattr(args, "variants", None)
    variants = _parse_variants(spec) if spec else None
    return {
        "empirical": True,
        "variants": variants,
        "refresh": bool(getattr(args, "recalibrate", False)),
        "agents_config": getattr(args, "agents_config", None) or None,
    }


def _active_campaign_fingerprint(args) -> str | None:
    """Resolve the full solver-campaign identity for strict empirical policy.

    The same agents document feeds both the calibrator and this policy check.
    Returning ``None`` is reserved for commands that explicitly permit
    structural-only difficulty; a malformed active roster fails before work or
    spend rather than making all existing measurements appear mysteriously stale.
    """

    if bool(getattr(args, "allow_structural_difficulty", False)):
        return None
    if not (
        bool(getattr(args, "empirical", False))
        or bool(getattr(args, "require_empirical", False))
    ):
        return None
    from elt_taskgen.corpus import calibration as calibration_mod

    config = getattr(args, "agents_config", None) or None
    try:
        roster = calibration_mod.load_calibration_roster(config)
    except (OSError, ValueError) as exc:
        raise CliUsageError(f"invalid active calibration campaign: {exc}") from None
    return calibration_mod.roster_fingerprint(roster)


def _make_repair_proposer(args, provider):
    """Build the default bounded repair agent unless explicitly disabled.

    The agent works on a held trial copy and a patch is committed only after the
    unchanged deterministic re-validation path passes.  ``--no-repair-proposer``
    is the offline/cost escape hatch; ``one_shot`` remains available as a
    compatibility mode.
    """
    if not getattr(args, "repair_proposer", False):
        return None
    from elt_taskgen.review.repair_proposer import (
        DEFAULT_REPAIR_PROPOSER_MODE,
        REPAIR_PROPOSER_MODES,
        AgenticRepairProposer,
        RepairProposer,
    )

    mode = str(getattr(args, "repair_proposer_mode", None) or DEFAULT_REPAIR_PROPOSER_MODE)
    if mode not in REPAIR_PROPOSER_MODES:
        raise CliUsageError(
            f"--repair-proposer-mode {mode!r} is not one of {list(REPAIR_PROPOSER_MODES)}"
        )
    cls = AgenticRepairProposer if mode == "bounded" else RepairProposer
    return cls(
        provider,
        max_attempts=getattr(args, "repair_attempts", None),
        config_path=getattr(args, "agents_config", None),
    )


def _make_engine(args, provider=None, *, echo=None) -> Engine:
    workspace = Path(args.workspace).resolve()
    if provider is None:
        provider = _resolve_provider(args, workspace)
    engine = _open_engine(
        workspace,
        max_repair_rounds=getattr(args, "max_repair_rounds", 3),
        stage_runners=build_stage_runners(
            provider,
            echo=echo,
            calibrate_options=_calibrate_options(args),
            destination=getattr(args, "destination", None) or "snowflake",
            extra_destinations=tuple(
                getattr(args, "extra_destinations", None) or ()
            ),
        ),
        repair_proposer=_make_repair_proposer(args, provider),
    )
    # Operator intent the `release` runner needs and the Engine has no opinion
    # about (it is not ledger state): recorded on the instance, never persisted.
    engine.allow_unlocked_env = bool(getattr(args, "allow_unlocked_env", False))
    engine.require_empirical_difficulty = bool(
        getattr(args, "require_empirical", False)
    ) and not bool(getattr(args, "allow_structural_difficulty", False))
    engine.expected_campaign_fingerprint = _active_campaign_fingerprint(args)
    engine.release_mode = str(getattr(args, "release_mode", "development"))
    engine.agents_config = getattr(args, "agents_config", None)
    engine.sandbox_attestation = None
    attestation_path = getattr(args, "sandbox_attestation", None)
    if attestation_path is not None:
        from elt_taskgen.runtime.attestation import (
            SandboxAttestation,
            SandboxAttestationError,
            verify_sandbox_attestation,
        )

        try:
            engine.sandbox_attestation = verify_sandbox_attestation(
                SandboxAttestation.model_validate_json(
                    Path(attestation_path).read_text(encoding="utf-8")
                )
            )
        except (OSError, ValueError, SandboxAttestationError) as exc:
            engine.close()
            raise CliUsageError(f"invalid sandbox attestation: {exc}") from None
    if engine.release_mode == "certified" and engine.sandbox_attestation is None:
        engine.close()
        raise CliUsageError(
            "certified release requires --sandbox-attestation; use "
            "--development-release only for an explicitly unlabelled local cut"
        )
    return engine


# --- Reporting ---

def _battery_detail(stage: StageName, payload: dict) -> str:
    """One line describing a recorded battery — ROSTER-AWARE.

    Printed from `variant_battery.summarize_payload`, not the raw `accepted` bool: a
    battery recorded before a gate was added prints "ACCEPTED" while `final_verdict`
    answers in_progress. The reader must be told the battery is STALE."""
    from elt_taskgen.verification import variant_battery as battery_mod

    variant = None
    for candidate in (TaskVariant.EXTRACT_LOAD, TaskVariant.TRANSFORM, TaskVariant.FULL):
        if variant_gate_stage(candidate) is stage:
            variant = candidate
            break
    passed = sum(1 for g in payload["gates"] if g.get("passed"))
    if variant is None:  # pragma: no cover - every gate stage maps to a variant
        return f"{passed}/{len(payload['gates'])} gates passed"
    summary = battery_mod.summarize_payload(payload, variant)
    applicable = int(summary.get("gates_applicable") or len(payload["gates"]))
    detail = f"{passed}/{applicable} gates passed"
    staleness = battery_mod.roster_staleness(payload, variant)
    if stage is StageName.GATES:
        return detail
    if staleness:
        return f"{detail} -> STALE: {staleness}; re-run"
    if summary.get("accepted"):
        return f"{detail} -> variant ACCEPTED"
    failing = ", ".join(summary.get("failing_gates") or ()) or "none recorded"
    return f"{detail} -> variant REFUSED (failing: {failing})"


def _print_stage_report(engine: Engine, task_id: str) -> None:
    print(f"\nStage ledger for {task_id!r} (latest report per stage):")
    for stage in STAGE_ORDER:
        row = engine.latest_report(task_id, stage.value)
        if row is None:
            print(f"  {stage.value:<20} -")
            continue
        payload = json.loads(row.payload_json)
        detail = str(
            payload.get("detail") or payload.get("details") or payload.get("error") or ""
        )
        if "gates" in payload and isinstance(payload.get("gates"), list):
            # The stage verdict says the battery CONCLUDED, `accepted` says whether
            # the variant ships, the roster says whether either is still current.
            detail = _battery_detail(stage, payload)
        if len(detail) > 76:
            detail = detail[:73] + "..."
        print(f"  {stage.value:<20} {row.verdict:<7} rev={row.revision}  {detail}")


def _measured_rewards(engine: Engine, task_id: str) -> dict[str, dict[str, float]]:
    row = engine.latest_report(task_id, StageName.ATTACK.value)
    if row is None:
        return {}
    return json.loads(row.payload_json).get("rewards", {})


def _trusted_rewards(engine: Engine, task_id: str) -> dict[str, float]:
    row = engine.latest_report(task_id, StageName.GATES.value)
    if row is None:
        return {}
    payload = json.loads(row.payload_json)
    for gate in payload.get("gates", []):
        if gate.get("gate") == "trusted-solution":
            return {
                key.split(":", 1)[1]: float(value)
                for key, value in gate.get("evidence", {}).items()
                if key.startswith("reward:")
            }
    return {}


def _print_attack_matrix(engine: Engine, task: TaskIR) -> bool:
    """Print the measured attack matrix and verify it reproduces every expected_pass
    map exactly (full reward where True, lost where False). True iff reproduced."""
    rewards = _measured_rewards(engine, task.task_id)
    trusted = _trusted_rewards(engine, task.task_id)

    name_w = max(
        [len("correct (reference)")] + [len(c.name) for c in task.attack_cases]
    ) + 2
    header = "case".ljust(name_w) + "".join(p.value.ljust(16) for p in POP_ORDER)
    print("\nAttack matrix (measured reward; '(exp pass/fail)' = spec expectation):")
    print("  " + header)
    print("  " + "-" * len(header))

    ok = True

    def cell(reward: float | None, expect: bool | None) -> str:
        nonlocal ok
        if reward is None:
            if expect is not None:
                ok = False
            return "MISSING".ljust(16)
        text = f"{reward:.2f}"
        if expect is None:
            return text.ljust(16)
        matches = (reward == 1.0) if expect else (reward < 1.0)
        if not matches:
            ok = False
        text += f" ({'pass' if expect else 'fail'}{'' if matches else ' VIOLATED'})"
        return text.ljust(16)

    line = "correct (reference)".ljust(name_w)
    for pop in POP_ORDER:
        line += cell(trusted.get(pop.value), True)
    print("  " + line)

    for case in task.attack_cases:
        measured = rewards.get(case.name, {})
        line = case.name.ljust(name_w)
        for pop in POP_ORDER:
            line += cell(measured.get(pop.value), case.expected_pass.get(pop))
        print("  " + line)
    return ok


# --- Subcommand implementations ---

def _print_demo_spend(provider, *, live: bool) -> None:
    """Per-role tokens and USD from the run's CostMeter.

    Replay runs print $0.0000 with the reason, so the zero is a statement ("nothing
    was called") rather than an unpriced route metering silently."""
    meter = getattr(provider, "meter", None)
    if meter is None:  # pragma: no cover - RoutedProvider always carries one
        return
    if not live and not meter.per_role:
        print("\nSpend: $0.0000 (replay: every exchange served from a transcript)")
        return
    print("\nSpend (live calls, metered per API attempt):")
    for role_name in sorted(meter.per_role):
        row = meter.per_role[role_name]
        cache_read = int(row.get("cache_read_input_tokens", 0))
        cache_write = int(row.get("cache_creation_input_tokens", 0))
        cached = (
            f" cache_read={cache_read} cache_write={cache_write}"
            if cache_read or cache_write
            else ""
        )
        print(
            f"  {role_name:<24} in={int(row['input_tokens']):>7} "
            f"out={int(row['output_tokens']):>7}  ${row['usd']:.4f}"
            f"  attempts={int(row.get('attempts', 0))}"
            f" wall={int(row.get('elapsed_ms', 0))}ms{cached}"
        )
    budget = meter.budget_per_task_usd
    ceiling = f" of ${budget:.2f} budget" if budget is not None else ""
    print(f"  {'TOTAL':<24} {'':>11} {'':>11}  ${meter.total_usd:.4f}{ceiling}")


def cmd_record_transcripts(args) -> int:
    """One-time transcript seeding: run author + council LIVE for one task and
    persist every (role, prompt) exchange for later offline replay.

    The only subcommand that REQUIRES credentials. EXIT CODES ARE METROLOGY'S, NOT
    THE STAGE TABLE'S: 0 recorded, 1 the council is not admitted or credentials are
    missing (no live spend happened), 2 a usage refusal. This command has no task
    and cannot reject one."""
    from elt_taskgen.review import council
    from elt_taskgen.review import providers as providers_mod

    routing = providers_mod.load_role_routing(
        getattr(args, "agents_config", None) or None
    )
    loader_only = bool(getattr(args, "loader_only", False))
    live_roles = [
        "semantic_author",
        "ambiguity_critic",
        "population_adversary",
        "shortcut_attacker",
        "feasibility_reviewer",
        # The cross-family implementer transcript is part of the seed set: the
        # gates stage replays it for the dual-build-agreement gate.
        "independent_implementer",
        # And the cross-family LOADER, replayed by gates_extract_load: without this
        # seed every pool task's EXTRACT_LOAD variant is refused on pure replay.
        "independent_loader",
    ]
    if loader_only:
        live_roles = ["independent_loader"]
    problems = providers_mod.credential_problems(routing, live_roles)
    if problems:
        print("cannot record transcripts — live credentials are required:")
        for problem in problems:
            print(f"  - {problem}")
        print("set the missing keys (e.g. export ANTHROPIC_API_KEY=...) and re-run.")
        return 2

    workspace = Path(args.workspace).resolve()
    task_id = getattr(args, "task_id", None) or demo_fixture.DEMO_TASK_ID
    if task_id == demo_fixture.DEMO_TASK_ID:
        task = demo_fixture.demo_task()
        default_out = providers_mod.default_fixtures_dir()
    else:
        engine = _open_engine(workspace)
        try:
            task = engine.load_task(task_id)
        finally:
            engine.close()
        default_out = workspace / "transcripts"
    out_dir = Path(args.out).resolve() if getattr(args, "out", None) else default_out

    # This IS the seeding step: record into out_dir, no fixtures precedence.
    store = providers_mod.TranscriptStore(out_dir)
    meter = providers_mod.CostMeter(
        budget_per_task_usd=getattr(
            args, "budget_per_task", providers_mod.DEFAULT_BUDGET_PER_TASK_USD
        ),
        budget_total_usd=getattr(args, "budget_total", None),
        budget_per_role_usd=routing.usd_caps(),
    )
    provider = providers_mod.RoutedProvider(
        routing,
        store,
        meter,
        refresh=bool(getattr(args, "force", False)),
        task_id=task.task_id,
        source="record-transcripts",
    )

    # Apply the review admission gate before live author and critic seeding.
    if not loader_only:
        detail, admission = _admission_gate(provider, workspace)
        if detail is not None:
            # 1, not 2: the same "blocked, the council is not admitted" answer
            # metrology itself reports, and no live call was made.
            print(f"cannot record transcripts — {detail}")
            print(
                "run `elt-taskgen metrology --workspace council` and export "
                "ELT_TASKGEN_ADMISSION (or re-run it in this workspace)."
            )
            return 1
        _stamp_admission(provider, admission)

    print(f"recording live transcripts for task {task.task_id!r} into {out_dir}")
    from elt_taskgen.reference import independent

    # Seed one exchange for a one-shot witness, but the full trajectory for an
    # enabled session. Read the flag from the agents document used for routing.
    _agents_doc_path = providers_mod.agents_config_of(routing)

    def _witness_session_enabled(role_name: str) -> bool:
        try:
            block = providers_mod.role_loop_limits(
                role_name, agents_config=_agents_doc_path
            )
        except Exception:  # noqa: BLE001 — an unreadable declaration enables nothing
            return False
        return bool(block.get("enabled", False))

    implementer_session = _witness_session_enabled(independent.ROLE_NAME)
    loader_session = _witness_session_enabled(independent.LOADER_ROLE_NAME)

    findings: list = []
    if not loader_only:
        _begin_task_evidence(provider, task)
        # Seed the exact author protocol the review stage will replay.  A bounded
        # author stores a session/trajectory key, not the historical one-shot
        # exchange key; recording the latter made an enabled-by-default author
        # impossible to use with ``--replay-only``.
        author_engine = _open_engine(workspace)
        try:
            prose, _author_session_data = _author_prose_for(
                author_engine, task, provider
            )
        finally:
            author_engine.close()
        authored = task.model_copy(update={"solver_prompt": prose})
        # Surface prose-fidelity problems AT SEEDING TIME: prose that fails the
        # completeness gate makes every later replay of the author stage fail
        # (a SPECIFICATION repair replays the same transcript). Re-record with --force.
        from elt_taskgen.review import prose_fidelity

        fidelity_problems = prose_fidelity.check_prose_fidelity(authored)
        if fidelity_problems:
            print(
                f"WARNING: recorded prose fails the {prose_fidelity.GATE_NAME} "
                f"gate ({len(fidelity_problems)} problem(s)) — the author stage "
                "will fail on replay; re-record with --force:"
            )
            for problem in fidelity_problems:
                print(f"  - {problem}")
        _begin_task_evidence(provider, authored)
        findings = council.run_council(authored, provider)
        # Seed the cross-family independent-implementer exchange (sample 0): the
        # demo's gates stage replays exactly this prompt when it runs the dual build
        # offline. Execution and scoring happen later, in that stage.
        if implementer_session:
            print(
                "independent_implementer session enabled: its whole trajectory "
                "is seeded from the frozen reference (second pass below); no "
                "one-shot exchange is recorded"
            )
        else:
            provider.complete(
                independent.ROLE_NAME, independent.sample_prompt(authored, 0)
            )
    # Seed the loader only after authoring and gold refreeze because its prompt
    # embeds the emitted public bundle byte-for-byte.
    from elt_taskgen.export import eltbench as eltbench_mod
    from elt_taskgen.reference import gold as gold_mod

    _TWO_PASS_HINT = (
        "run `review --replay-only` then `reference-run` for this task, then "
        "re-run record-transcripts to seed the loader exchange"
    )
    _second_pass_roles = "independent_loader" + (
        " and independent_implementer" if implementer_session else ""
    )
    loader_engine = _open_engine(workspace)
    try:
        stored = loader_engine.load_task(task.task_id)
        answer_key = _answer_key_dir(loader_engine, stored)
        populations = loader_engine.task_dir(stored.task_id) / "populations"
        if not (answer_key.is_dir() and populations.is_dir()):
            print(
                f"NOTE: {_second_pass_roles} exchange not seeded (no frozen "
                f"reference in this workspace yet) — {_TWO_PASS_HINT}."
            )
        elif not stored.solver_prompt.strip():
            print(
                f"NOTE: {_second_pass_roles} exchange not seeded (stored task "
                "has no authored prose yet; the gate-time bundle embeds it, "
                f"so a pre-author seed could never replay) — {_TWO_PASS_HINT}."
            )
        else:
            gold_bundle = None
            try:
                gold_bundle = gold_mod.load_gold(answer_key)
                provider.begin_task_evidence(stored.task_id, stored.content_hash())
            except Exception as exc:  # noqa: BLE001 — see the loader WARNING below
                print(
                    f"WARNING: {_second_pass_roles} seeding FAILED before the "
                    f"frozen reference could be read ({type(exc).__name__}: "
                    f"{exc}); {_TWO_PASS_HINT}."
                )
            if gold_bundle is not None and implementer_session:
                try:
                    # THE WHOLE TRAJECTORY: the same entry point the gates stage
                    # runs, so every session turn is recorded under the key the
                    # stage will look up. Nothing is recorded as build evidence
                    # here — that is the gates stage's, at gate time.
                    build = independent.run_independent_build(
                        stored, workspace, provider, gold_bundle
                    )
                    print(
                        "independent_implementer trajectories seeded "
                        f"({len(build.samples)} session(s), status={build.status!r})"
                    )
                except Exception as exc:  # noqa: BLE001 — same rule as the loader
                    print(
                        "WARNING: independent_implementer trajectory seeding "
                        f"FAILED ({type(exc).__name__}: {exc}) — the dual-build "
                        "gates will halt as could-not-measure on replay; "
                        f"{_TWO_PASS_HINT}."
                    )
            if gold_bundle is not None:
                try:
                    eltbench_mod.emit_variant(
                        stored,
                        gold_bundle,
                        TaskVariant.EXTRACT_LOAD,
                        _variants_root(loader_engine, stored)
                        / TaskVariant.EXTRACT_LOAD.value,
                        populations_dir=populations,
                    )
                    if loader_session:
                        load = independent.run_independent_load_build(
                            stored, workspace, provider, gold_bundle
                        )
                        print(
                            "independent_loader trajectories seeded "
                            f"({len(load.samples)} session(s), status={load.status!r})"
                        )
                    else:
                        provider.complete(
                            independent.LOADER_ROLE_NAME,
                            independent.load_sample_prompt(
                                stored,
                                independent.el_bundle_dir(workspace, stored.task_id),
                                0,
                            ),
                        )
                        print("independent_loader exchange seeded (sample 0)")
                except Exception as exc:  # noqa: BLE001 — the paid council seeds are
                    # already recorded; a loader-side crash must not force a re-record.
                    print(
                        "WARNING: independent_loader seeding FAILED "
                        f"({type(exc).__name__}: {exc}) — el-independent-load "
                        "will be RED and the EXTRACT_LOAD variant refused "
                        f"variant-locally; {_TWO_PASS_HINT}."
                    )
    except EngineError:
        # demo path: not registered here; fixtures carry the transcript.
        if implementer_session or loader_session:
            print(
                f"NOTE: {_second_pass_roles} trajectories not seeded (the task "
                "is not registered in this workspace; a session seed needs the "
                f"frozen reference) — {_TWO_PASS_HINT}."
            )
    finally:
        loader_engine.close()
    recorded = sum(1 for _ in out_dir.glob("*/*.json"))
    print(
        f"done: {recorded} transcript file(s) present, "
        f"{len(findings)} council finding(s), spend ${meter.total_usd:.4f}"
        + ("" if loader_only else f" (prose: {len(prose)} chars)")
    )
    return 0


def cmd_metrology(args) -> int:
    """Council efficacy harness: run the defect-injection benchmark and, on a pass,
    write the council.live_admitted record that admits live council routing. Live
    mode always makes fresh calls; replay-only is diagnostic and can neither admit
    nor revoke. A failing fresh-live run REVOKES any prior admission (fail closed).

    Exit 0 = admitted, 1 = blocked, 2 = could not measure. The CostMeter is printed
    on EVERY exit path, including the budget breach where the number matters most."""
    return _cmd_metrology_inner(args)


def _cmd_metrology_inner(args) -> int:
    from elt_taskgen.review import metrology as metrology_mod
    from elt_taskgen.review import providers as providers_mod

    workspace = Path(args.workspace).resolve()
    diagnostic_replay = bool(getattr(args, "replay_only", False))
    parent_workspace = getattr(args, "parent_budget_workspace", None)
    parent_run_id = str(getattr(args, "parent_budget_run_id", "") or "").strip()
    parent_total = getattr(args, "parent_budget_total", None)
    parent_values = (parent_workspace is not None, bool(parent_run_id), parent_total is not None)
    if any(parent_values):
        if not all(parent_values):
            print(
                "ERROR: --parent-budget-workspace, --parent-budget-run-id, and "
                "--parent-budget-total must be supplied together"
            )
            return 2
        if diagnostic_replay:
            print("ERROR: parent budget binding is only valid for fresh live metrology")
            return 2
        if getattr(args, "budget_total", None) is None:
            print(
                "ERROR: parent-bound metrology requires an explicit --budget-total "
                "aggregate metrology sublimit"
            )
            return 2
        parent_workspace = Path(parent_workspace).resolve()
        parent_database = parent_workspace / "state" / "pipeline_budget.sqlite3"
        if not parent_database.is_file():
            print(
                "ERROR: parent budget ledger does not exist at the exact "
                f"workspace: {parent_database}"
            )
            return 2
        # `_resolve_provider` opens an aggregate-only view of this exact
        # existing run. Candidate calls continue to use their immutable
        # per-candidate cap; metrology is constrained by its own --budget-total
        # and atomically consumes the same parent aggregate headroom.
        args.global_budget_workspace = parent_workspace
        args.global_budget_run_id = parent_run_id
        args.global_budget_total = float(parent_total)
        args.global_budget_per_task = None
        args.global_budget_enforce_task_limit = False
    provider = _resolve_provider(
        args,
        workspace,
        replay_only=diagnostic_replay,
        # A production admission is an observation of the CURRENT providers,
        # not a re-labeling of cached responses. Replay remains available only
        # as an explicit diagnostic and cannot write admission below.
        refresh=False if diagnostic_replay else True,
        fixtures_dir=(
            providers_mod.default_fixtures_dir() if diagnostic_replay else None
        ),
        source=(
            "metrology-diagnostic-replay"
            if diagnostic_replay
            else "metrology-fresh-live"
        ),
    )
    provider.task_id = provider.task_id or "council-metrology"
    workers = int(getattr(args, "workers", 1) or 1)
    if workers > 1:
        # OQ-20: one RoutedProvider per worker over ONE routing, store and
        # meter; the pool orders every worker's evidence by trial index.
        provider = _metrology_worker_pool(provider, workers, providers_mod)
    try:
        return _cmd_metrology_measured(
            args, workspace, provider, metrology_mod, providers_mod
        )
    finally:
        # EVERY exit path: admitted, blocked, and could-not-measure. A run
        # that died on BudgetExceededError still spent the budget it breached.
        _print_demo_spend(
            provider, live=not bool(getattr(args, "replay_only", False))
        )


def _metrology_worker_pool(first, workers: int, providers_mod):
    """`--workers N`: `first` plus N-1 siblings sharing its routing, transcript
    store and cost meter, as one `RoutedProviderPool`."""
    siblings = [first]
    for _ in range(max(0, int(workers) - 1)):
        siblings.append(
            providers_mod.RoutedProvider(
                first.routing,
                first.store,
                first.meter,
                replay_only=bool(first.replay_only),
                refresh=bool(first.refresh),
                task_id=str(first.task_id or ""),
                admission=dict(getattr(first, "admission_provenance", {}) or {}),
                source=str(getattr(first, "source", "") or ""),
            )
        )
    return providers_mod.RoutedProviderPool(siblings)


def _evidence_in_trial_order(rows) -> list:
    """The evidence rows ordered by `(trial_index, role, prompt_sha256)` when
    they carry a trial index (OQ-20: the manifest and dispatch digests are
    then identical at any worker count); rows without one keep their
    order, after the indexed ones."""
    if not isinstance(rows, (list, tuple)):
        return list(rows or []) if rows is not None else []
    indexed = []
    for position, row in enumerate(rows):
        index = row.get("trial_index") if isinstance(row, dict) else None
        ordered = index if isinstance(index, int) and not isinstance(index, bool) else None
        indexed.append(
            (
                0 if ordered is not None else 1,
                ordered if ordered is not None else position,
                str(row.get("role") or "") if isinstance(row, dict) else "",
                str(row.get("prompt_sha256") or "") if isinstance(row, dict) else "",
                position,
                row,
            )
        )
    return [item[-1] for item in sorted(indexed, key=lambda item: item[:5])]


def _canonical_order_schedule(metrology_mod, seed: int):
    """`--diagnostic-order-check`: the SAME trials the seed draws (scored and
    canary), in canonical `sha256(specimen name)` order instead of the
    seeded shuffle — an exchangeability probe (metrology redesign §9)."""
    trials = list(metrology_mod.select_trials(seed)) + list(
        metrology_mod.select_canary_trials(seed)
    )
    return tuple(
        sorted(
            trials,
            key=lambda t: (
                hashlib.sha256(t.specimen.name.encode("utf-8")).hexdigest(),
                int(t.variant),
                int(t.replicate),
            ),
        )
    )


def _metrology_seat_lines(role: str, m, thresholds) -> list[str]:
    """The per-seat EFFICIENCY and INTEGRITY readings (metrology redesign
    §8, §10; SoT T8): canary flags, the private-probe and policy-violation
    counters, the advisory efficiency ratios, ICC and n_eff, pass^k. Read
    with `getattr` so a report written by an earlier harness (or a
    `RoleMetrics` without a field) prints what it has."""
    lines: list[str] = []
    canary_kinds = "  ".join(
        f"{kind}={count}"
        for kind, count in sorted(dict(getattr(m, "canary_hits_by_kind", {}) or {}).items())
    )
    canary_flag = "CANARY HIT" if int(getattr(m, "canary_hits", 0) or 0) else "canary clean"
    lines.append(
        f"      integrity: {canary_flag} ({int(getattr(m, 'canary_hits', 0) or 0)}/"
        f"{int(getattr(m, 'canary_trials', 0) or 0)}"
        + (f"; by kind: {canary_kinds}" if canary_kinds else "")
        + f")  private probes={int(getattr(m, 'private_probe_count', 0) or 0)} "
        f"(<={thresholds.max_private_probes})  policy violations="
        f"{int(getattr(m, 'policy_violation_trials', 0) or 0)} "
        f"(UB {float(getattr(m, 'policy_violation_ub', 0.0) or 0.0):.2f} "
        f"<={thresholds.max_policy_violation_ub})"
    )
    scored = int(getattr(m, "tampered_count", 0) or 0) + int(getattr(m, "clean_count", 0) or 0)

    def ratio(name: str) -> str:
        value = getattr(m, name, None)
        if value is None:
            return "n/a"
        try:
            count = int(value)
        except (TypeError, ValueError):
            return "n/a"
        return f"{count}/{scored}" if scored else str(count)

    lines.append(
        "      efficiency (advisory): "
        f"stuck={ratio('stuck_trials')} (UB <={thresholds.advisory_max_stuck_ratio_ub})  "
        f"limit-stopped={ratio('limit_stopped_trials')} "
        f"(UB <={thresholds.advisory_max_limit_stopped_ratio_ub})  "
        f"format-retry={ratio('format_retry_trials')}  "
        f"compile-correction={ratio('compile_correction_trials')}  "
        f"nudges={int(getattr(m, 'nudge_count', 0) or 0)}  "
        f"validator runs={int(getattr(m, 'validator_run_count', 0) or 0)}  "
        f"wasted-call ratio="
        + (
            f"{float(getattr(m, 'wasted_call_ratio')):.2f}"
            if getattr(m, "wasted_call_ratio", None) is not None
            else "n/a"
        )
        + f" (<={thresholds.advisory_max_wasted_call_ratio})"
    )
    icc = getattr(m, "icc", None)
    n_eff = getattr(m, "n_eff", None)
    pass_k = getattr(m, "pass_k", None)
    lines.append(
        "      clustering: ICC="
        + (f"{float(icc):.2f}" if isinstance(icc, (int, float)) else "n/a")
        + "  n_eff="
        + (f"{float(n_eff):.1f}" if isinstance(n_eff, (int, float)) else "n/a")
        + "  pass^k="
        + (
            " ".join(f"k{k}={float(v):.2f}" for k, v in sorted(dict(pass_k).items()))
            if isinstance(pass_k, dict) and pass_k
            else "n/a"
        )
    )
    return lines


def _cmd_metrology_measured(
    args, workspace, provider, metrology_mod, providers_mod
) -> int:
    from elt_taskgen.review.session import PolicyFault, SessionFault
    from elt_taskgen.review.tools.projection import DiagnosticTripwire
    from elt_taskgen.review.trajectory import ChainError

    agents_config = getattr(args, "agents_config", None) or None
    try:
        thresholds = metrology_mod.load_metrology_thresholds(agents_config)
        # RUN-START TOOLCHAIN ASSERTION (metrology redesign §2; R-F): the
        # fingerprint hashes the PINNED duckdb/sqlglot versions, so a run on a
        # drifted toolchain must not be allowed to earn an admission the
        # digest could not tell apart. Nothing has been spent yet.
        pins = metrology_mod.assert_toolchain_pins(path=agents_config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    except metrology_mod.ToolchainPinError as exc:
        print(f"ERROR [{exc.code}]: {exc}")
        print("No admission was written or revoked: the toolchain was not measured.")
        return 2
    fingerprint = metrology_mod.council_routing_fingerprint(provider.routing)
    tool_surface = metrology_mod.tool_surface_sha256()
    # The mix is SAMPLED from the specimen pool, so the seed is part of the run's
    # identity: printed, recorded, and accepted back via --seed to reproduce it.
    seed = args.seed if getattr(args, "seed", None) is not None else (
        metrology_mod.fresh_seed()
    )
    trials_per_role = (
        metrology_mod.TAMPERED_PER_ROLE * metrology_mod.REPLICATES_TAMPERED
    )
    print(
        f"council metrology  (workspace: {workspace}; harness "
        f"{metrology_mod.HARNESS_VERSION})\n"
        f"thresholds: recall lower bound >={thresholds.min_recall} "
        f"precision>={thresholds.min_precision} "
        f"nitpick upper bound <={thresholds.max_nitpick_rate} "
        f"@{thresholds.confidence:.2f} confidence  "
        f"canary hits <={thresholds.max_canary_hits}  "
        f"private probes <={thresholds.max_private_probes}  "
        f"policy-violation UB <={thresholds.max_policy_violation_ub}\n"
        f"routing={fingerprint[:12]}  tool surface={tool_surface[:12]}  "
        "toolchain=" + ", ".join(f"{k} {v}" for k, v in sorted(pins.items())) + "\n"
        f"seed={seed}  pool={metrology_mod.pool_sha256()[:12]} "
        f"({len(metrology_mod.specimen_pool())} specimens, drawing "
        f"{metrology_mod.CLEAN_PER_RUN} clean x"
        f"{metrology_mod.REPLICATES_CLEAN} + "
        f"{metrology_mod.TAMPERED_PER_ROLE}/role tampered x"
        f"{metrology_mod.REPLICATES_TAMPERED} = {trials_per_role} trials/seat; "
        f"+ {metrology_mod.CANARY_PER_ROLE} canary trials/seat from "
        f"{len(metrology_mod.canary_pool())} canaries)"
    )
    order_check = bool(getattr(args, "diagnostic_order_check", False))
    run_kwargs: dict = {}
    if order_check:
        # DIAGNOSTIC: the same draw in canonical sha256(name) order (never the
        # seeded shuffle), exit 2 whatever it measures — an order-dependence
        # probe, not an observation that can admit or revoke.
        run_kwargs = {"trials": _canonical_order_schedule(metrology_mod, seed), "canary_trials": ()}
        print("DIAGNOSTIC ORDER CHECK: the schedule runs in canonical sha256(name) order")
    try:
        report = metrology_mod.run_metrology(
            provider,
            thresholds=thresholds,
            routing_fingerprint=fingerprint,
            seed=seed,
            **run_kwargs,
        )
    except (
        providers_mod.TranscriptMissingError,
        providers_mod.MissingCredentialsError,
        providers_mod.BudgetExceededError,
        # A protocol violation means the measurement could not be TAKEN — exit 2 —
        # not that the council failed on the merits.
        providers_mod.ProviderProtocolError,
        # A harness fault (a validator crash or deadline, a transport fault,
        # a tripwire, a chain that does not verify, a policy fault raised
        # outside a session's own catch) is infrastructure: could not
        # measure, no verdict, no tombstone (C7).
        SessionFault,
        DiagnosticTripwire,
        PolicyFault,
        ChainError,
    ) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 2
    report_path = metrology_mod.write_report(report, workspace / "reports")
    print(f"report: {report_path}")
    for role in sorted(report.per_role):
        m = report.per_role[role]
        verdict = "pass" if report.role_pass[role] else "BLOCK"
        if m.block_reasons:
            verdict += " [" + ", ".join(m.block_reasons) + "]"
        print(
            f"  {role:<24} recall={m.recall:.2f} ({m.detected_count}/"
            f"{m.tampered_count}, >={m.recall_lb:.2f})  "
            f"precision={m.precision:.2f}  "
            f"nitpick={m.nitpick_rate:.2f} (<={m.nitpick_ub:.2f})  "
            f"canary hits={m.canary_hits}/{m.canary_trials}  -> {verdict}"
        )
        # The MARGIN, per specimen: which planted defects this seat lands always,
        # sometimes and never. A bare ratio leaves the operator guessing.
        margins = "  ".join(
            f"{name}={score:.1f}" for name, score in sorted(m.per_specimen.items())
        )
        if margins:
            print(f"      per-specimen: {margins}")
        for line in _metrology_seat_lines(role, m, thresholds):
            print(line)
    dependence = getattr(metrology_mod, "position_dependence", None)
    if callable(dependence):
        try:
            per_role = dependence(report)
        except Exception as exc:  # noqa: BLE001 - a reading, never a verdict
            per_role = {}
            print(f"position dependence: not computed ({type(exc).__name__})")
        for role in sorted(per_role or {}):
            stats = per_role[role]
            fields = getattr(stats, "__dict__", None) or (stats if isinstance(stats, dict) else {})
            summary = "  ".join(
                f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}"
                for k, v in sorted(dict(fields).items())
            )
            print(f"  position dependence (reported, not gated) {role:<24} {summary}")

    if order_check:
        print(
            "DIAGNOSTIC ONLY (order check): a canonically ordered schedule is "
            "an exchangeability probe; it never writes or revokes council "
            "admission. Run 'elt-taskgen metrology' without "
            "--diagnostic-order-check for a fresh live admission."
        )
        return 2
    if bool(getattr(args, "replay_only", False)):
        print(
            "DIAGNOSTIC ONLY: replayed metrology never writes or revokes "
            "council admission. Run 'elt-taskgen metrology' without "
            "--replay-only for a fresh live admission."
        )
        return 2

    # OQ-20: evidence sorted by trial_index BEFORE hashing, so the manifest
    # and dispatch digests do not depend on how many workers ran the trials.
    evidence = _evidence_in_trial_order(getattr(provider, "exchange_evidence", None))
    try:
        trajectories = metrology_mod.summarize_fresh_live_trajectories(
            evidence,
            expected_by_role=metrology_mod.expected_trajectories_by_role(report),
        )
    except metrology_mod.LiveExchangeEvidenceError as exc:
        print(f"ERROR [{exc.code}]: {exc}")
        print(
            "No admission was written or revoked because this run could not "
            "prove a complete fresh-live measurement."
        )
        return 2
    print(
        "fresh-live evidence: "
        f"{trajectories.trajectory_count}/"
        f"{trajectories.expected_trajectory_count} trajectories, "
        f"model calls={trajectories.model_call_count_total} "
        f"(replayed={trajectories.replayed_model_call_count}), "
        f"tool calls={trajectories.tool_call_count_total} "
        f"(stale results={trajectories.stale_tool_result_count}), "
        f"validator runs={trajectories.validator_run_count_total}, "
        f"nudges={trajectories.nudge_count_total}, "
        f"corrections={trajectories.correction_count_total}, "
        f"limit-stopped={trajectories.limit_stopped_count}, "
        f"policy violations={trajectories.policy_violation_count}, "
        f"manifest={trajectories.trajectory_manifest_sha256[:12]}, "
        f"dispatch={trajectories.dispatch_order_sha256[:12]}"
        + (f", workers={provider.size}" if hasattr(provider, "size") else "")
    )
    # ADVISORY efficiency readings (reported, never gated; blocking from
    # harness "7"): the limit-stopped ratio is the one a one-shot seat can
    # show; the stuck and wasted-call ratios need model-initiated tools.
    scored_trials = sum(
        m.tampered_count + m.clean_count for m in report.per_role.values()
    )
    if scored_trials:
        print(
            "advisory: limit-stopped ratio="
            f"{trajectories.limit_stopped_count / scored_trials:.2f} "
            f"(advisory bar UB <={thresholds.advisory_max_limit_stopped_ratio_ub}); "
            f"run spend=${trajectories.usd_total:.2f}, "
            f"wall={trajectories.wall_ms_total / 1000:.0f}s"
        )
    if report.admitted:
        # The CLI command IS the explicit re-earn: only here may the write follow
        # $ELT_TASKGEN_ADMISSION onto the consulted record.
        marker = metrology_mod.write_admission_marker(
            workspace,
            report,
            exchange_evidence=evidence,
            honor_override=True,
            fingerprint_components=metrology_mod.fingerprint_components(
                provider.routing
            ),
            agents_config=getattr(provider, "agents_config", None),
        )
        print(f"ADMITTED: live council routing enabled ({marker})")
        print(
            "  consult this ONE record from every other workspace with "
            f"{metrology_mod.ADMISSION_ENV}={marker}  "
            "(do NOT copy it — a copy cannot be revoked)"
        )
        return 0
    failed = sorted(r for r in report.role_pass if not report.role_pass[r])
    reasons = {
        reason for r in failed for reason in report.per_role[r].block_reasons
    }
    if {"canary_hit", "private_probe"} <= reasons:
        reason_code = "private_exposure"
    elif "canary_hit" in reasons:
        reason_code = "canary_hit"
    elif "private_probe" in reasons:
        reason_code = "private_probe"
    else:
        reason_code = "blocked"
    tombstone = metrology_mod.revoke_admission(
        workspace,
        reason="a metrology run BLOCKED on "
        + ", ".join(
            f"{r} [{', '.join(report.per_role[r].block_reasons)}]" for r in failed
        ),
        seed=report.seed,
        honor_override=True,
        reason_code=reason_code,
        routing_fingerprint=fingerprint,
        tool_surface=report.tool_surface_sha256,
        trajectory_manifest_sha256=trajectories.trajectory_manifest_sha256,
    )
    print(f"reproduce this run with: --seed {report.seed}")
    if tombstone is not None:
        # A blocked run must WITHDRAW the previous admission everywhere, out loud:
        # deleting one file left other copies still admitting.
        print(f"REVOKED: admission withdrawn at {tombstone}")
    print(
        "BLOCKED: council not admitted — live review stays fail-closed; "
        "improve the failing role(s) and re-run 'elt-taskgen metrology'."
    )
    return 1


def _load_manifest_builder():
    """Import the shipped ``build_dbt_manifest.py`` maintenance tool."""
    import importlib.util
    from elt_taskgen.package_resources import resource_path

    tool_path = resource_path("tools/build_dbt_manifest.py")
    if not tool_path.is_file():
        raise RuntimeError(
            f"packaged manifest builder not found at {tool_path}; use "
            "--manifest with a prebuilt manifest.json instead"
        )
    import sys as _sys

    name = "elt_taskgen_build_dbt_manifest"
    if name in _sys.modules:
        return _sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, tool_path)
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: pydantic resolves this module's forward
    # references (ProjectChoice on ManifestBuild) through sys.modules.
    _sys.modules[name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except BaseException:
        _sys.modules.pop(name, None)
        raise
    return module


def _dbt_source_attribution(args):
    """Return (catalog pool, safe attribution override) before any dbt build.

    A custom ``--root`` is a different source tree. Its bytes may be newer than
    the catalog's provenance manifest, so silently copying that manifest's
    commit into the TaskIR would create false attribution. This preflight runs
    before dbt can perform build or dependency work.
    """
    from elt_taskgen.catalog import load_source_catalog

    config_path = Path(args.sources_config).resolve() if args.sources_config else None
    catalog = load_source_catalog(config_path)
    source = catalog.pool(args.pool)
    provenance = source.provenance(args.package)
    catalog_root = source.root_path().resolve()
    selected_root = Path(args.root).resolve() if args.root else catalog_root

    source_commit = str(getattr(args, "source_commit", None) or "").strip().lower()
    source_upstream = str(getattr(args, "source_upstream", None) or "").strip()
    if source_upstream and not source_commit:
        raise ValueError("--source-upstream requires --source-commit")
    if source_commit and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_commit) is None:
        raise ValueError(
            "--source-commit must be a full 40- or 64-character hexadecimal commit"
        )

    attribution: str | None = None
    if source_commit:
        upstream = source_upstream or provenance.get("upstream", "")
        if not upstream:
            raise ValueError(
                "--source-commit needs --source-upstream because the source "
                "catalog has no upstream URL for this record"
            )
        base = source.attribution or source.pool
        attribution = (
            f"{base}: {args.package} ({upstream} @ {source_commit[:12]})"
        )
    elif selected_root != catalog_root and provenance:
        catalog_commit = provenance.get("commit", "unknown")
        raise ValueError(
            f"--root selects {selected_root}, outside the catalog root "
            f"{catalog_root}; the catalog's {catalog_commit[:12]} provenance "
            "does not attest those bytes. Pass the custom tree's verified "
            "--source-commit (and --source-upstream if the catalog has no URL)."
        )

    return source, attribution


def _dbt_ingest_identity(args, package_project_name: str):
    """PoolSelection for a vendored package: license + attribution + family.

    The catalog is the authority on all three and an EXCLUDED record is refused
    here. ``family`` is the dbt project name, keeping ids like
    ``dbt__servicenow``.
    """
    source, attribution = _dbt_source_attribution(args)
    return source.selection(
        args.package,
        license=args.license,
        attribution=attribution,
        family=package_project_name,
    )


def _print_task_summary(task) -> None:
    from collections import Counter

    spread = Counter(b.backend.value for b in task.backends)
    print(
        f"  {task.task_id}\n"
        f"    family={task.family_id}  license={task.license}\n"
        f"    source tables={len(task.tables)}  marts={len(task.marts)}  "
        f"relationships={len(task.relationships)}\n"
        f"    backends: "
        + ", ".join(f"{b}={spread[b]}" for b in sorted(spread))
        + "\n"
        f"    marts: " + ", ".join(m.name for m in task.marts)
    )


def _report_ingest_collisions(
    task_id: str, collisions: Iterable[_CollisionLike]
) -> int:
    """Print the one ingest refusal format and return its fatal count.

    Adapters own how contamination is detected; the CLI owns how it is presented,
    because shell wrappers depend on the ingest exit-code contract and per-command
    dialects would drift into incompatible labels."""
    found = tuple(collisions)
    for collision in found:
        label = "FATAL" if collision.fatal else "borderline"
        print(
            f"  contamination {label} "
            f"[{collision.kind}/{collision.against}] {collision.detail}"
        )
    fatal_count = sum(1 for collision in found if collision.fatal)
    if fatal_count:
        print(
            f"REFUSED: {fatal_count} fatal contamination collision(s) — "
            f"{task_id} was NOT registered"
        )
    return fatal_count


def cmd_ingest_five(args) -> int:
    """Preflight and register an exact, provenance-pinned five-source roster."""
    from elt_taskgen.ingest_manifest import (
        FiveSourceIngestError,
        ingest_five_sources,
    )

    try:
        result = ingest_five_sources(
            Path(args.manifest),
            workspace=Path(args.workspace),
            reingest=bool(getattr(args, "reingest", False)),
            dry_run=bool(args.dry_run),
        )
    except (FiveSourceIngestError, OSError, ValueError) as exc:
        print(f"ERROR: five-source ingest refused: {exc}")
        return 2

    verb = "would register" if result.dry_run else "registered"
    print(
        f"five-source manifest {result.manifest_sha256[:16]}: "
        f"{verb} {len(result.tasks)} task(s)"
    )
    for task in result.tasks:
        print(f"  {task.origin.value:<10} {task.task_id}  {task.content_hash()[:12]}")
    if result.receipt is not None:
        print(f"completion receipt: {result.receipt}")
    return 0


def cmd_ingest_dbt(args) -> int:
    """Vendored package (or a prebuilt manifest) -> registered TaskIR candidates.

    `--package` builds target/manifest.json when absent (never writing into the
    vendored tree), stamps catalog license/attribution and runs the SAME
    contamination pre-check as pipeline stage 2."""
    from elt_taskgen.adapters import dbt

    workspace = Path(args.workspace).resolve()
    selection = None
    package_name_override: str | None = None

    if args.package:
        # Reject an unbound custom source root before manifest construction can
        # mutate a build directory or fetch dependencies.
        try:
            _dbt_source_attribution(args)
        except (KeyError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
        try:
            builder = _load_manifest_builder()
        except RuntimeError as exc:
            print(f"ERROR: {exc}")
            return 2
        build_root = (
            Path(args.build_root).resolve()
            if args.build_root
            else builder.default_build_root(workspace, args.package)
        )
        try:
            build = builder.build_manifest(
                args.package,
                build_root=build_root,
                pool=args.pool,
                root=Path(args.root).resolve() if args.root else None,
                project=builder.ProjectChoice(args.project),
                force=args.rebuild_manifest,
                run_deps=not args.no_deps,
                dbt_python=args.dbt_python,
                echo=print,
            )
        except builder.ManifestBuildError as exc:
            print(f"ERROR: {exc}")
            return 2
        manifest_path = Path(build.manifest_path)
        package_name_override = build.package_project_name
        print(
            f"manifest: {manifest_path}\n"
            f"  project={build.project_choice.value}  "
            f"{'rebuilt' if build.rebuilt else 'reused'}  "
            f"fingerprint={build.fingerprint[:16]}"
        )
        try:
            selection = _dbt_ingest_identity(args, package_name_override)
        except (KeyError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
    else:
        manifest_path = Path(args.manifest).resolve()

    spec = dbt.load_manifest(manifest_path)
    if package_name_override:
        # The parsed project is integration_tests ('<pkg>_integration_tests');
        # the FAMILY must follow the package, not the CI harness around it.
        spec = spec.model_copy(update={"package_name": package_name_override})
    if selection is not None:
        spec = spec.model_copy(update={"license": selection.license})

    result = dbt.extract_candidates(spec, pool=args.pool)
    for skipped in result.skipped:
        print(f"skipped cut: {skipped.reason}")
    if args.strict and result.skipped:
        print(f"ERROR: --strict and {len(result.skipped)} cut(s) were skipped")
        return 2
    if not result.tasks:
        print(f"ERROR: no usable candidate cut in {manifest_path} (fail closed)")
        return 2

    tasks = []
    for task in result.tasks:
        if selection is not None:
            if task.family_id != selection.family_id:
                print(
                    f"ERROR: adapter family {task.family_id!r} disagrees with the "
                    f"catalog selection {selection.family_id!r} (fail closed)"
                )
                return 2
            task = task.model_copy(
                update={**selection.ir_identity(), "cluster_id": selection.family_id}
            )
        tasks.append(task)

    from elt_taskgen.verification.contamination import ContaminationIndex

    # The SAME service the pipeline's stage-2 runner uses, at ingest time. A dry run
    # opens no engine, so the index is built from the workspace path directly.
    idx = ContaminationIndex(workspace / "state" / "contamination")
    _arm_contamination_index(idx)
    engine = _open_engine(workspace) if not args.dry_run else None
    try:
        registered = 0
        refused = 0
        clean = 0
        for task in tasks:
            collisions = idx.check_pre(task)
            fatal_count = _report_ingest_collisions(task.task_id, collisions)
            if fatal_count:
                refused += 1
                continue
            clean += 1
            _print_task_summary(task)
            if engine is None:
                continue
            engine.register(task, allow_overwrite=bool(getattr(args, "reingest", False)))
            registered += 1
            print("    registered")
    finally:
        if engine is not None:
            engine.close()

    source = args.package or str(manifest_path)
    tail = f" ({refused} refused by the contamination pre-check)" if refused else ""
    if args.dry_run:
        print(
            f"dry run: {clean} ingestible candidate task(s) of {len(tasks)} extracted "
            f"from {source}{tail}, nothing registered"
        )
        return 0 if clean else 2
    print(f"{registered} candidate task(s) ingested from {source}{tail}")
    return 0 if registered else 2


def cmd_ingest_synsql(args) -> int:
    """SynSQL tables.json -> TaskIR, contamination-gated like every other pool.

    The pre call point runs HERE, before the optional 9 GB answer-leak scan and
    before the engine is opened. THE GATE ITSELF LIVES IN adapters/synsql.py: SynSQL
    is also ingested programmatically, so a CLI-only gate would be bypassable by
    construction. This function owns the printing and the exit code only."""
    from elt_taskgen.adapters import synsql
    from elt_taskgen.verification import contamination as cont

    task, trusted_schema_atoms = synsql.to_task_ir_with_schema_atoms(
        args.db_id, Path(args.tables).resolve(), pool=args.pool
    )

    workspace = Path(args.workspace).resolve()
    index_dir = workspace / "state" / "contamination"
    idx = cont.ContaminationIndex(index_dir)
    if not idx.is_armed():
        print("contamination index seeded with the embedded benchmark deny lists")
    _arm_contamination_index(idx)
    try:
        borderline = synsql.assert_uncontaminated(task, index_dir)
    except synsql.SynSQLContaminationError as exc:
        _report_ingest_collisions(task.task_id, exc.collisions)
        return 2
    _report_ingest_collisions(task.task_id, borderline)

    if args.data:
        records = [
            r for r in synsql.iter_records(Path(args.data).resolve())
            if r.db_id == args.db_id
        ]
        synsql.assert_no_answer_leak(
            task,
            records,
            trusted_schema_atoms=trusted_schema_atoms,
        )
        print(f"leak guard: checked against {len(records)} SynSQL record(s), clean")
    engine = _open_engine(workspace)
    try:
        engine.register(task, allow_overwrite=bool(getattr(args, "reingest", False)))
    finally:
        engine.close()
    print(f"registered {task.task_id}  (family {task.family_id})")
    return 0


def _schemapile_filter(args, index):
    """Relational floor for this run: index defaults, overridden per flag."""
    from elt_taskgen.adapters.schemapile import RelationalFilter

    base = index.filter if index is not None else RelationalFilter()
    overrides = {
        field: getattr(args, field)
        for field in (
            "min_tables",
            "max_tables",
            "min_columns",
            "min_foreign_keys",
            "min_linked_table_pairs",
            "min_tables_with_pk",
        )
        if getattr(args, field, None) is not None
    }
    if not overrides:
        return base
    # Re-CONSTRUCT rather than model_copy: a copy skips validation, and an
    # out-of-range threshold from the shell must fail closed, not widen the corpus.
    return RelationalFilter(**{**base.model_dump(), **overrides})


def cmd_ingest_schemapile(args) -> int:
    """SchemaPile record/cluster -> TaskIR (per-record license gate + clusters).

    Selection is answered from the compact index; the 327 MB corpus is touched only
    to fetch the ONE chosen record. `--cluster` ingests that cluster's
    representative — one task per repo-plus-shape component, never one per migration
    file. Exit 0 registered/listed, 2 refused; never 1 (it has no EMPTY-RESULT case)."""
    from elt_taskgen.adapters import schemapile
    from elt_taskgen.catalog import load_source_catalog

    index = schemapile.load_index(Path(args.index).resolve())
    filt = _schemapile_filter(args, index)

    if args.list:
        # Recount against the filter ACTUALLY in force: the index's stored stats were
        # computed with the thresholds it was built with.
        passing = sorted(
            (
                r
                for r in index.records
                if r.permissive
                and r.license
                and not schemapile.filter_problems(r.metrics, filt)
            ),
            key=lambda r: (tuple(-v for v in r.rank_key), r.key),
        )
        best = {}  # cluster_id -> its best passing record (insertion = rank order)
        for record in passing:
            best.setdefault(record.cluster, record)
        print(
            f"index {args.index}: {len(index.records)} record(s), "
            f"{len(index.clusters)} cluster(s), {len(passing)} record(s) clear the "
            f"relational filter, {len(best)} cluster(s) have an ingestible "
            "representative"
        )
        print(
            f"filter: min_tables={filt.min_tables} max_tables={filt.max_tables} "
            f"min_columns={filt.min_columns} min_foreign_keys={filt.min_foreign_keys} "
            f"min_linked_table_pairs={filt.min_linked_table_pairs} "
            f"min_tables_with_pk={filt.min_tables_with_pk}"
        )
        for rep in list(best.values())[: args.limit]:
            cluster = index.cluster(rep.cluster)
            print(
                f"  {cluster.cluster_id}\n"
                f"      tables={rep.metrics.tables} fks={rep.metrics.foreign_keys} "
                f"columns={rep.metrics.columns} pk_tables={rep.metrics.tables_with_pk} "
                f"license={rep.license}\n"
                f"      records_in_cluster={len(cluster.members)} "
                f"repos={len(cluster.repos)} representative={rep.key!r}"
            )
        return 0

    if bool(args.cluster) == bool(args.key):
        print("pass exactly one of --cluster or --key (or use --list)")
        return 2

    catalog = load_source_catalog(
        Path(args.sources_config).resolve() if args.sources_config else None
    )
    source = (
        Path(args.source).resolve()
        if args.source
        else catalog.pool(args.pool).root_path() / schemapile.SOURCE_FILENAME
    )
    task = schemapile.task_from_index(
        index,
        source=source,
        key=args.key,
        cluster=args.cluster,
        pool=args.pool,
        filt=filt,
        catalog=catalog,
    )

    # Contamination is ONE service with TWO call points; the pre call point runs here
    # so a colliding candidate is refused BEFORE it costs a workspace entry.
    from elt_taskgen.verification import contamination as cont

    workspace = Path(args.workspace).resolve()
    idx = cont.ContaminationIndex(workspace / "state" / "contamination")
    if not idx.is_armed():
        print("contamination index was empty — seeded with the embedded deny lists")
    _arm_contamination_index(idx)
    collisions = idx.check_pre(task)
    # Canonical refusal shape and exit code (INGEST EXIT-CODE CONTRACT): a
    # per-command dialect returning 1 made `if [ $? -eq 2 ]` miss a refusal here.
    if _report_ingest_collisions(task.task_id, collisions):
        return 2

    engine = _open_engine(workspace)
    try:
        engine.register(task, allow_overwrite=bool(getattr(args, "reingest", False)))
    finally:
        engine.close()
    print(f"registered {task.task_id}")
    print(f"  family     {task.family_id}  (cluster {task.cluster_id})")
    print(f"  license    {task.license}")
    print(f"  attribution {task.attribution}")
    print(
        f"  schema     {len(task.tables)} table(s), {len(task.relationships)} "
        f"relationship(s), {len(task.marts)} mart(s)"
    )
    return 0


#: Printed by `--help` for the ingest commands so the exit-code contract a wrapper
#: depends on is discoverable from the shell. ONE string: copies would drift apart.
INGEST_EXIT_CODE_EPILOG = """\
exit codes:
  0  registered (or a --list/--dry-run query answered normally)
  1  EMPTY RESULT: the query was fine, there was simply nothing to return
     (only `ingest-wikidbs --list` over an empty scan window). Nothing was
     refused and nothing was written -- widen the search.
  2  REFUSED / fail closed: nothing entered the workspace. Covers contamination
     refusals, bad arguments and unusable inputs.

a contamination refusal is always exit 2 AND prints:
    contamination FATAL [<kind>/<against>] <detail>
  REFUSED: <n> fatal contamination collision(s) -- <task_id> was NOT registered
borderline collisions print the same line with 'borderline' and do not change
the exit code.
"""


#: `ingest-anchor` read like "ingest ELT-Bench as a training source", which it
#: never was. `measure-target` is the primary name; the alias still works.
DEPRECATED_ANCHOR_COMMAND = "ingest-anchor"
ANCHOR_COMMAND = "measure-target"

#: The commands that register a TaskIR and therefore carry `--reingest`.
#: `measure-target` is NOT one: it writes anchors, never a candidate task.
REINGESTING_COMMANDS: tuple[str, ...] = (
    "ingest-five",
    "ingest-dbt",
    "ingest-synsql",
    "ingest-schemapile",
    "ingest-wikidbs",
    "ingest-dlt",
)


def cmd_ingest_anchor(args) -> int:
    """Load the ELT-Bench GOAL/reference store (measurement-only, never trainable).

    Arms the contamination firewall with the pinned benchmark's fingerprints and
    records the target difficulty distribution `select` compares against. The store
    lands under <workspace>/reference/anchors/, deliberately NOT beside tasks/:
    selection.py and release.py both refuse Origin.ELTBENCH_ANCHOR."""
    import warnings

    from elt_taskgen.adapters import eltbench_anchor
    from elt_taskgen.verification import contamination as cont

    if getattr(args, "command", None) == DEPRECATED_ANCHOR_COMMAND:
        print(
            f"NOTE: '{DEPRECATED_ANCHOR_COMMAND}' is a deprecated alias for "
            f"'{ANCHOR_COMMAND}'. ELT-Bench is the GOAL, not a training source.",
            file=sys.stderr,
        )

    workspace = Path(args.workspace).resolve()
    bench_root = Path(args.bench_root).resolve()
    if args.db:
        tasks = [eltbench_anchor.import_anchor_task(bench_root, args.db)]
    else:
        tasks = eltbench_anchor.import_all_anchors(bench_root)

    # Never silently ignore a pre-relocation <ws>/anchors/ store: it is moved into
    # reference/anchors/ and reported (ignoring it disarms the firewall by omission).
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        migrated = eltbench_anchor.migrate_legacy_anchor_store(workspace)
    for w in caught:
        print(f"NOTE: {w.message}", file=sys.stderr)
    if migrated:
        print(f"migrated {len(migrated)} legacy anchor file(s) into reference/anchors/")

    # A PARTIAL ARM IS NOT A FIREWALL: an empty or short checkout armed the index
    # with 0 fingerprints and exited 0, after which every ingest in that workspace
    # passed a check that could not see the benchmark. Fail closed before writing.
    if not args.db and len(tasks) < len(cont.ELTBENCH_FAMILIES):
        print(
            f"REFUSED: pinned checkout at {bench_root} yields {len(tasks)} "
            f"anchor task(s), expected {len(cont.ELTBENCH_FAMILIES)} — the "
            "firewall is NOT armed and nothing was written. Check --bench-root "
            "points at a complete ELT-Bench checkout.",
            file=sys.stderr,
        )
        return 2

    eltbench_anchor.save_anchor_store(tasks, workspace)
    store_dir = eltbench_anchor.anchor_store_dir(workspace)

    idx = cont.ContaminationIndex(workspace / "state" / "contamination")
    _arm_contamination_index(idx, announce=False)
    fingerprints: set[str] = set()
    for task in tasks:
        fingerprints |= cont.task_fingerprints(task)
    # THE BENCHMARK'S OWN EVALUATION SQL, so the `sql:` namespace covers what the
    # ARMING_REMEDY sentence claims. Missing files are not an error: the fingerprints
    # that exist are added and the count is printed.
    sql_added = 0
    sql_root = bench_root / "evaluation" / "sql"
    for db in sorted({t.family_id.split("__")[-1] for t in tasks} | (
        {args.db} if args.db else set()
    )):
        for sql_path in sorted((sql_root / db).glob("*.sql")) if sql_root.is_dir() else []:
            try:
                text = sql_path.read_text(encoding="utf-8")
            except OSError:
                continue
            fingerprint = cont.sql_fingerprint(text)
            if fingerprint:
                fingerprints.add(fingerprint)
                sql_added += 1
    idx.add_benchmark("eltbench", sorted(fingerprints))
    print(
        f"imported {len(tasks)} anchor task(s); contamination index armed with "
        f"{len(fingerprints)} anchor fingerprint(s) "
        f"({sql_added} from evaluation SQL)"
    )
    # This is the ONE command that turns a name check into a firewall; say so in the
    # same numbers every other call point reports.
    coverage = idx.coverage()
    print(f"  {coverage.summary()}")
    print(f"reference store: {store_dir}  (measurement-only; never trainable)")
    if coverage.level is not cont.CoverageLevel.ARMED:
        # Covers the 0-anchor case AND a store that predates the type-blind shape
        # fingerprints: either way this workspace's firewall is only a name check.
        print(
            "REFUSED: the contamination index is still not ARMED after "
            f"import ({coverage.level.value}). {cont.ARMING_REMEDY}",
            file=sys.stderr,
        )
        return 2
    return 0


def cmd_ingest_wikidbs(args) -> int:
    """WikiDBs database dir -> TaskIR (real rows), or a candidate shortlist.

    `--list` never walks the 213 GB pool: it visits a bounded window of ONE part
    directory. Ingest routes through the same contamination pre-check as every other
    pool. This is the ONLY ingest command that returns 1 — `--list` scanned its
    window and found nothing eligible, so widen --scan/--start."""
    from elt_taskgen.adapters import wikidbs
    from elt_taskgen.catalog import load_source_catalog
    from elt_taskgen.verification import contamination as cont

    catalog = load_source_catalog(
        Path(args.sources_config).resolve() if args.sources_config else None
    )
    try:
        pool_row = catalog.pool(args.pool)
    except KeyError as exc:
        print(f"ERROR: {exc}")
        return 2
    root = Path(args.root).resolve() if args.root else pool_row.root_path()
    family_map = Path(args.family_map).resolve() if args.family_map else None

    if args.list:
        try:
            candidates = wikidbs.list_candidates(
                root,
                part=args.part,
                scan=args.scan,
                limit=args.limit,
                start=args.start,
                family_map_path=family_map,
            )
        except wikidbs.WikiDbsIngestError as exc:
            print(f"ERROR: {exc}")
            return 2
        if not candidates:
            print(
                f"no eligible WikiDBs candidate in part-{args.part} "
                f"[{args.start},{args.start + args.scan}) — widen --scan/--start"
            )
            return 1
        print(
            f"{'node':>6}  {'component':>10} {'size':>5}  {'tabs':>4} {'fks':>4} "
            f"{'rows':>6}  directory"
        )
        for c in candidates:
            print(
                f"{c.node_id:6d}  {c.component:>10} {c.component_size:5d}  "
                f"{c.table_count:4d} {c.foreign_key_count:4d} {c.row_count:6d}  "
                f"{c.directory}"
            )
        print(
            "\nat most one candidate per WikiDBGraph component (two from one "
            "component are ONE family). ingest with:\n"
            f"  elt-taskgen ingest-wikidbs --db-dir "
            f"'{root / f'part-{args.part}' / candidates[0].directory}'"
        )
        return 0

    if not args.db_dir:
        print("ERROR: --db-dir is required (or pass --list to see candidates)")
        return 2

    try:
        task = wikidbs.to_task_ir(
            Path(args.db_dir).resolve(),
            pool=args.pool,
            family_map_path=family_map,
            wikidbs_root=None if args.no_verify_nodes else root,
            catalog=catalog,
        )
    except (wikidbs.WikiDbsIngestError, wikidbs.ProvenanceLeakError) as exc:
        print(f"ERROR: {exc}")
        return 2

    workspace = Path(args.workspace).resolve()
    idx = cont.ContaminationIndex(workspace / "state" / "contamination")
    if not idx.is_armed():
        print("contamination index seeded with the embedded benchmark deny lists")
    _arm_contamination_index(idx)
    collisions = idx.check_pre(task)
    if _report_ingest_collisions(task.task_id, collisions):
        return 2

    engine = _open_engine(workspace)
    try:
        engine.register(task, allow_overwrite=bool(getattr(args, "reingest", False)))
    finally:
        engine.close()
    rows = sum(
        len(r) for p in task.populations
        for r in p.literal_rows.values()
        if p.name.value == "primary"
    )
    print(
        f"registered {task.task_id}  (family {task.family_id}, "
        f"{len(task.tables)} tables, {len(task.relationships)} link(s), "
        f"{rows} real rows in the primary population, "
        f"policy {wikidbs.REAL_DATA_POLICY!r}, license {task.license})"
    )
    return 0


def _dlt_manifest_dir(args) -> Path:
    if args.manifest_dir:
        return Path(args.manifest_dir).resolve()
    from elt_taskgen.package_resources import resource_path

    return resource_path("config/dlt_connectors")


def cmd_ingest_dlt(args) -> int:
    """dlt connector manifest(s) -> TaskIR candidate(s), contamination-gated.

    Manifests under `config/dlt_connectors/` are committed, built by
    tools/extract_dlt_manifest.py, which reads the vendored connector with `ast` and
    never imports it. A connector whose resources exist only at runtime is SKIPPED
    with its unresolved reasons printed, never silently guessed at."""
    from elt_taskgen.adapters import dlt as dlt_adapter
    from elt_taskgen.catalog import load_source_catalog
    from elt_taskgen.verification import contamination as cont

    manifest_dir = _dlt_manifest_dir(args)
    try:
        paths = dlt_adapter.list_manifests(manifest_dir)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 2

    if args.list:
        print(f"{manifest_dir}: {len(paths)} manifest(s)")
        for path in paths:
            try:
                m = dlt_adapter.load_connector(path)
            except dlt_adapter.NoStaticResources:
                print(f"  {path.stem:<20} UNINGESTIBLE (no static resources)")
                continue
            loadable = m.loadable()
            print(
                f"  {m.connector:<20} {len(loadable):>3} loadable resource(s), "
                f"{sum(1 for e in loadable if e.primary_key):>2} keyed, "
                f"{sum(1 for e in loadable if e.parent):>2} child, "
                f"{sum(1 for e in loadable if e.cursor):>2} incremental "
                f"(record {m.record or '?'})"
            )
        return 0

    if not args.all and not args.connector:
        print("ERROR: pass --connector <name> or --all (or --list)")
        return 2

    selected: list[Path] = []
    if args.all:
        selected = paths
    else:
        wanted = args.connector
        stem = wanted[4:] if wanted.startswith("dlt_") else wanted
        match = [p for p in paths if p.stem == stem]
        if not match:
            print(
                f"ERROR: no manifest for connector {wanted!r} in {manifest_dir}; "
                f"have {[p.stem for p in paths]}"
            )
            return 2
        selected = match

    catalog = load_source_catalog(
        Path(args.sources_config).resolve() if args.sources_config else None
    )
    try:
        pool_row = catalog.pool(args.pool)
    except KeyError as exc:
        print(f"ERROR: {exc}")
        return 2

    workspace = Path(args.workspace).resolve()
    index_dir = workspace / "state" / "contamination"
    idx = cont.ContaminationIndex(index_dir)
    if not idx.is_armed():
        print("contamination index seeded with the embedded benchmark deny lists")
    _arm_contamination_index(idx)

    registered = 0
    skipped: list[tuple[str, str]] = []
    engine = _open_engine(workspace)
    try:
        for path in selected:
            try:
                manifest = dlt_adapter.load_connector(path)
            except dlt_adapter.NoStaticResources as exc:
                skipped.append((path.stem, str(exc)))
                continue
            except ValidationError as exc:
                # A manifest that does not VALIDATE is a defect in the manifest (or
                # in the extractor), not a connector we decline to ingest. pydantic's
                # ValidationError is a ValueError, so the handler below would have
                # printed it as `SKIPPED <name>` and exited 0.
                print(f"ERROR: {path}: {type(exc).__name__}: {exc}")
                return 2
            except ValueError as exc:
                # A malformed manifest is an ERROR, never a quiet skip.
                print(f"ERROR: {path}: {exc}")
                return 2
            try:
                selection = pool_row.selection(
                    manifest.selector,
                    license=manifest.license or None,
                    attribution=manifest.attribution or None,
                    family=manifest.connector,
                )
                task = dlt_adapter.to_task_ir(
                    manifest, pool=args.pool, selection=selection
                )
                borderline = dlt_adapter.assert_uncontaminated(task, index_dir)
            except dlt_adapter.DltContaminationError as exc:
                # A REFUSAL, not a skip: print the canonical shape and take the exit
                # code with it. Via the generic ValueError below it reported as
                # `SKIPPED` and the command still exited 0 if anything else registered.
                _report_ingest_collisions(task.task_id, exc.collisions)
                return 2
            except ValidationError as exc:
    # Skip adapter refusals, but surface invalid constructed IR as a builder defect.
                print(f"ERROR: {path.stem}: {type(exc).__name__}: {exc}")
                return 2
            except ValueError as exc:
                skipped.append((path.stem, str(exc)))
                continue
            _report_ingest_collisions(task.task_id, borderline)
            if not args.dry_run:
                engine.register(task, allow_overwrite=bool(getattr(args, "reingest", False)))
            registered += 1
            verb = "would register" if args.dry_run else "registered"
            rest = sum(1 for b in task.backends if b.backend.value == "rest")
            files = sum(1 for b in task.backends if b.backend.value == "files")
            print(
                f"{verb} {task.task_id}  (family {task.family_id}, "
                f"{len(task.tables)} source tables [{rest} rest / {files} files], "
                f"{len(task.relationships)} FK edge(s), "
                f"marts {[m.name for m in task.marts]}, license {task.license})"
            )
    finally:
        engine.close()

    for name, reason in skipped:
        print(f"SKIPPED {name}: {reason}")
    if not registered:
        print("no dlt connector was ingested")
        return 2
    checked = "checked" if args.dry_run else "ingested"
    print(f"{registered} dlt connector(s) {checked} from {manifest_dir}")
    return 0


def _force_stage_rerun(engine: Engine, task: TaskIR, stage: StageName, reason: str) -> None:
    """Shadow a stage's current PASS so `engine.run` executes it again.

    APPEND-ONLY: a new FAIL row under MAX(id), exactly the way `repair.apply_repair`
    invalidates a stage; it never mutates or deletes the row it supersedes. Calling
    the runner directly instead bypassed `_handle_failure`, so a measured IMPOSSIBLE
    verdict was appended with no route, no repair round and no rejection — and the
    next command painted a PASS over it."""
    if not engine.task_lock_held(task.task_id):
        raise EngineError(
            "force-stage invalidation must run under the task's execution lock"
        )
    engine.record_report(
        task,
        stage.value,
        VERDICT_FAIL,
        StagePayload(
            detail=f"superseded: {reason}",
            data={"reason": reason, "stage": stage.value},
        ),
    )


def _atomic_replace_text(path: Path, text: str) -> None:
    """Durably publish a small workspace record with temp + rename.

    Corpus and per-task selection records are resumable coordinator state.  A
    process death may leave an unreferenced private temp file, but never a torn
    JSON document at the public path.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _report_blocked(
    engine: Engine, task_id: str, until: StageName | None = None
) -> bool:
    """Print the blocking stage, if the task is waiting on one. True if blocked.

    A stage subcommand answers for ITS stage: a block further down the ladder is real
    but is not this command's verdict. THE REMEDY IS PRINTED AS A COMMAND — "clear
    the block and re-run" was not actionable, because a stage that already passed at
    this hash is skipped, so the line says whether a producer was already shadowed or
    the operator needs `--re-emit`."""
    row = engine.blocked_stage(task_id)
    if row is None:
        return False
    order = [stage.value for stage in STAGE_ORDER]
    if until is not None and order.index(row.stage) > order.index(until.value):
        return False
    try:
        payload = json.loads(row.payload_json)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    data = payload.get("data") or {}
    waiting_on = str(data.get(BLOCKED_ON_KEY, "") or "?")
    print(
        f"BLOCKED at {row.stage} (waiting on: {waiting_on}): "
        f"{payload.get('error') or payload.get('detail') or ''}"
    )
    scheduled = str(data.get("rerun_scheduled", "") or "")
    subcommand = STAGE_SUBCOMMANDS.get(row.stage, row.stage)
    rerun = (
        f"elt-taskgen {subcommand} --workspace {engine.workspace} "
        f"--task-id {task_id}"
    )
    if scheduled:
        print(
            f"nothing was rejected — {scheduled} was shadowed (append-only) "
            f"and will re-derive on the next run: {rerun}"
        )
    else:
        print(
            "nothing was rejected — clear the block, then re-run in this "
            f"workspace: {rerun}. A stage that already PASSED at this content "
            "hash is skipped on resume; add --re-emit to force it."
        )
    return True


def _schedule_required_empirical_evidence(engine: Engine, task: TaskIR) -> str | None:
    """Shadow stale structural-only rows so a strict run measures and reselects.

    Returns an operator-facing refusal when the identity is already behind an
    immutable release.  All invalidation is append-only through
    :func:`_force_stage_rerun`.
    """
    from elt_taskgen.corpus import selection as selection_mod

    diff_path = _evidence_dir(engine, task) / "difficulty.json"
    problem = "difficulty evidence is missing"
    if diff_path.is_file():
        try:
            measurement = DifficultyMeasurement.model_validate_json(
                diff_path.read_text(encoding="utf-8")
            )
            problem = (
                selection_mod.empirical_evidence_problem(
                    measurement,
                    expected_campaign_fingerprint=getattr(
                        engine, "expected_campaign_fingerprint", None
                    ),
                )
                or ""
            )
        except (OSError, ValueError) as exc:
            problem = f"difficulty evidence is unreadable: {exc}"
    selection_path = _evidence_dir(engine, task) / "selection.json"
    selection_is_empirical = False
    if selection_path.is_file():
        try:
            from elt_taskgen.corpus.selection import SelectionResult

            selection_is_empirical = SelectionResult.model_validate_json(
                selection_path.read_text(encoding="utf-8")
            ).empirical_required
        except (OSError, ValueError):
            selection_is_empirical = False
    if (problem or not selection_is_empirical) and _released_at_current_hash(
        engine, task
    ):
        return (
            "refusing to retrofit empirical difficulty behind an immutable "
            "release. Clone the accepted workspace without release/, run "
            "`calibrate --empirical`, and cut a new release identity."
        )
    if problem:
        row = engine.latest_report(task.task_id, StageName.CALIBRATE.value)
        if row is not None and engine.report_is_current(
            task, StageName.CALIBRATE, row
        )[0]:
            _force_stage_rerun(
                engine,
                task,
                StageName.CALIBRATE,
                "empirical difficulty is required: " + problem,
            )
    if problem or not selection_is_empirical:
        row = engine.latest_report(task.task_id, StageName.SELECT.value)
        if row is not None and engine.report_is_current(
            task, StageName.SELECT, row
        )[0]:
            _force_stage_rerun(
                engine,
                task,
                StageName.SELECT,
                "selection must be re-derived under empirical policy",
            )
    return None


def _run_until(args, until: StageName | None) -> int:
    """Run the ladder up to `until` and report the ledger AND the spend.

    Exit code per the STAGE EXIT-CODE CONTRACT: 0 the stage passed, 1 the task is
    rejected, 2 could not measure or decide. The spend total is PRINTED, not
    persisted."""
    workspace = Path(args.workspace).resolve()
    provider = _resolve_provider(args, workspace)
    engine = _make_engine(args, provider, echo=print)
    try:
        require_empirical = bool(
            getattr(engine, "require_empirical_difficulty", False)
        )
        re_emit = until is not None and bool(getattr(args, "re_emit", False))

        def locked_pre_run(locked_engine: Engine, task: TaskIR) -> None:
            if require_empirical:
                refusal = _schedule_required_empirical_evidence(locked_engine, task)
                if refusal is not None:
                    raise _LockedRunRefusal(refusal)
            if re_emit:
                assert until is not None
                # An explicit re-emit is not an ordinary resume. Read and
                # shadow either the PASS or an explicit-recovery BLOCKED row
                # while holding the same lock as the execution it schedules.
                row = locked_engine.latest_report(task.task_id, until.value)
                same_identity_guard = bool(
                    row is not None
                    and row.content_hash == task.content_hash()
                    and blocked_retry_requires_explicit_recovery(row)
                )
                if row is not None and (
                    locked_engine.report_is_current(task, until, row)[0]
                    or same_identity_guard
                ):
                    _force_stage_rerun(
                        locked_engine,
                        task,
                        until,
                        "--re-emit requested at this content hash",
                    )

        try:
            run_kwargs = {}
            if require_empirical or re_emit:
                run_kwargs["pre_run"] = locked_pre_run
            engine.run(
                args.task_id,
                until=until.value if until else None,
                **run_kwargs,
            )
        except _LockedRunRefusal as exc:
            print(str(exc))
            return 2
        except InfrastructureFailure as exc:
            print(f"could not measure: {exc}")
            _print_stage_report(engine, args.task_id)
            return 2
        _print_stage_report(engine, args.task_id)
        verdict = engine.final_verdict(args.task_id)
        print(f"final verdict: {verdict}")
        if _report_blocked(engine, args.task_id, until):
            return 2
        if verdict == FINAL_REJECTED:
            # TASK-LEVEL, not stage-scoped: once the task is rejected run() is a
            # no-op, so a stage subcommand would exit 0 off its own stale pass row.
            print(
                "task is REJECTED; stage subcommands do not run on a rejected "
                "task — fix the candidate and retry in a fresh workspace"
            )
            return 1
        if until is None:
            return 0 if verdict == FINAL_ACCEPTED else 1
        task = engine.load_task(args.task_id)
        row = engine.latest_report(args.task_id, until.value)
        return 0 if engine.report_is_current(task, until, row)[0] else 1
    finally:
        _print_demo_spend(provider, live=not bool(getattr(args, "replay_only", False)))
        engine.close()


def _released_at_current_hash(engine: Engine, task: TaskIR) -> bool:
    """Has this identity already been frozen into a release?"""
    row = engine.latest_report(task.task_id, StageName.RELEASE.value)
    return engine.report_is_current(task, StageName.RELEASE, row)[0]


def cmd_calibrate(args) -> int:
    """Run calibration, explicitly rerunning current evidence when empirical.

    Released identities cannot be recalibrated because repair would invalidate an
    immutable release.
    """
    workspace = Path(args.workspace).resolve()
    provider = _resolve_provider(args, workspace)
    engine = _make_engine(args, provider, echo=print)
    try:
        empirical = bool(getattr(args, "empirical", False))
        refusal = (
            "refusing post-hoc empirical calibration: this identity is "
            "already RELEASED, and a measured infeasibility here would "
            "route a specification repair that invalidates stages "
            "behind an immutable release tree. Move release/ aside and "
            "re-run the pipeline if the task must be re-cut."
        )

        def locked_calibrate_pre_run(
            locked_engine: Engine, task: TaskIR
        ) -> None:
            if not empirical:
                return
            # Both the immutable-release check and any shadow row belong to the
            # same critical section as the empirical execution. No coordinator
            # can freeze this identity between the check and invalidation.
            if _released_at_current_hash(locked_engine, task):
                raise _LockedRunRefusal(refusal)
            row = locked_engine.latest_report(
                task.task_id, StageName.CALIBRATE.value
            )
            if row is None or not locked_engine.report_is_current(
                task, StageName.CALIBRATE, row
            )[0]:
                return
            try:
                payload = json.loads(row.payload_json)
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = {}
            incomplete = (
                bool(payload.get("skipped_reason"))
                or not payload.get("pass_rates")
                or bool(getattr(args, "recalibrate", False))
            )
            if incomplete:
                _force_stage_rerun(
                    locked_engine,
                    task,
                    StageName.CALIBRATE,
                    "empirical calibration requested at this content hash",
                )

        try:
            run_kwargs = {"pre_run": locked_calibrate_pre_run} if empirical else {}
            engine.run(
                args.task_id,
                until=StageName.CALIBRATE.value,
                **run_kwargs,
            )
        except _LockedRunRefusal as exc:
            print(str(exc))
            return 2
        except InfrastructureFailure as exc:
            print(f"could not measure: {exc}")
            _print_stage_report(engine, args.task_id)
            return 2
        _print_stage_report(engine, args.task_id)
        if _report_blocked(engine, args.task_id, StageName.CALIBRATE):
            return 2
        if engine.final_verdict(args.task_id) == FINAL_REJECTED:
            print("final verdict: rejected")
            return 1
        row = engine.latest_report(args.task_id, StageName.CALIBRATE.value)
        return 0 if row is not None and row.verdict == VERDICT_PASS else 1
    finally:
        # calibrate is the one stage subcommand that does NOT go through `_run_until`,
        # and --empirical is the most expensive thing the CLI can do.
        _print_demo_spend(provider, live=not bool(getattr(args, "replay_only", False)))
        engine.close()


_PIPELINE_FAILURE_FIELDS: tuple[str, ...] = (
    "failure_class",
    "failure_code",
    BLOCKED_ON_KEY,
    RETRY_GUARD_KEY,
    "recovery_prerequisite",
)


def _pipeline_blocked_metadata(row) -> tuple[str, dict[str, str]]:
    """Project one engine BLOCKED row into the process-worker protocol.

    The configured coordinator cannot infer why a generic ``state=blocked``
    result occurred.  Preserve the stage's typed, non-judgmental cause across
    the process boundary so protocol failures and pending adjudications do not
    become operational blocks in the run report.  Values may live at the
    StagePayload top level or in its structured ``data`` mapping.
    """

    try:
        payload = json.loads(row.payload_json)
    except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
        return (
            "blocked stage evidence is malformed",
            {
                "failure_class": "infrastructure_failure",
                "failure_code": "blocked_stage_payload_invalid",
                BLOCKED_ON_KEY: "workspace",
            },
        )
    if not isinstance(payload, dict):
        return (
            "blocked stage evidence is not an object",
            {
                "failure_class": "infrastructure_failure",
                "failure_code": "blocked_stage_payload_invalid",
                BLOCKED_ON_KEY: "workspace",
            },
        )
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    detail = str(payload.get("error") or payload.get("detail") or "stage blocked")
    metadata: dict[str, str] = {}
    for key in _PIPELINE_FAILURE_FIELDS:
        raw = payload.get(key)
        if raw in (None, ""):
            raw = data.get(key)
        value = str(raw or "").strip()
        if value:
            metadata[key] = value
    return detail, metadata


def _pipeline_worker(payload: dict) -> dict:
    """Process-isolated task worker used by :func:`cmd_pipeline`.

    Every invocation constructs its own provider, meter, and Engine connection.
    The Engine's per-task lock protects duplicate scheduling; SQLite WAL permits
    distinct tasks to execute concurrently.
    """
    worker_args = argparse.Namespace(**payload)
    workspace = Path(worker_args.workspace).resolve()
    provider = None
    engine = None
    target_stage = StageName(
        str(
            getattr(
                worker_args,
                "until_stage",
                StageName.CONTAMINATION_POST.value,
            )
        )
    )
    configured_spec = None
    if getattr(worker_args, "run_spec", None) is not None:
        from elt_taskgen.pipeline_readiness import GenerationRunSpec

        configured_spec = GenerationRunSpec.model_validate(worker_args.run_spec)
        apply_agent_harness(configured_spec.agent_harness)
    run_id = str(getattr(worker_args, "run_id", "") or "")
    attempt_path: Path | None = None
    attempt_document: dict = {}
    if run_id:
        attempt_dir = (
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "attempts"
            / str(worker_args.task_id)
        )
        attempt_dir.mkdir(parents=True, exist_ok=True)
        # A prior RUNNING record in this task/run cannot still be owned by this
        # newly-created worker. Preserve it as interrupted rather than silently
        # replacing the only evidence that a process died.
        for prior_path in sorted(attempt_dir.glob("*.json")):
            try:
                prior = json.loads(prior_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                continue
            if isinstance(prior, dict) and prior.get("state") == "RUNNING":
                prior["state"] = "INTERRUPTED"
                prior["detail"] = "unfinished attempt observed before resume"
                _atomic_replace_text(
                    prior_path, readable_json(prior) + "\n"
                )
        attempt_id = uuid.uuid4().hex
        attempt_path = attempt_dir / f"{attempt_id}.json"
        attempt_document = {
            "attempt_id": attempt_id,
            "task_id": str(worker_args.task_id),
            "target_stage": target_stage.value,
            "state": "RUNNING",
        }
        _atomic_replace_text(
            attempt_path, readable_json(attempt_document) + "\n"
        )

    def finish(result: dict) -> dict:
        if attempt_path is not None:
            final_attempt = dict(attempt_document)
            final_attempt.update(
                {
                    "state": result["state"],
                    "detail": result.get("detail", ""),
                    "usd": float(result.get("usd", 0.0)),
                }
            )
            infrastructure_scope = str(
                result.get("infrastructure_scope", "") or ""
            )
            if infrastructure_scope:
                final_attempt["infrastructure_scope"] = infrastructure_scope
            for key in _PIPELINE_FAILURE_FIELDS:
                value = str(result.get(key, "") or "").strip()
                if value:
                    final_attempt[key] = value
            _atomic_replace_text(
                attempt_path, readable_json(final_attempt) + "\n"
            )
        return result

    try:
        provider = _resolve_provider(worker_args, workspace)
        engine = _make_engine(worker_args, provider)
        task = engine.load_task(worker_args.task_id)
        if attempt_path is not None:
            attempt_document["task_content_hash"] = task.content_hash()
            _atomic_replace_text(
                attempt_path, readable_json(attempt_document) + "\n"
            )
        require_empirical = bool(
            getattr(engine, "require_empirical_difficulty", False)
        )

        def locked_pipeline_pre_run(
            locked_engine: Engine, locked_task: TaskIR
        ) -> None:
    # Configured resume invalidates passes whose owned artifacts are missing.
            if bool(getattr(worker_args, "enforce_evidence_contract", False)):
                from elt_taskgen.pipeline_readiness import stale_passes

                for check in stale_passes(
                    locked_engine,
                    locked_task,
                    through=target_stage,
                    spec=configured_spec,
                ):
                    _force_stage_rerun(
                        locked_engine,
                        locked_task,
                        check.stage,
                        "configured pipeline evidence check: " + check.reason,
                    )
            if require_empirical:
                refusal = _schedule_required_empirical_evidence(
                    locked_engine, locked_task
                )
                if refusal is not None:
                    raise _LockedRunRefusal(refusal)
            if bool(getattr(worker_args, "recalibrate", False)):
                if _released_at_current_hash(locked_engine, locked_task):
                    raise _LockedRunRefusal(
                        "cannot recalibrate an identity behind an immutable release"
                    )
                for stage in (StageName.CALIBRATE, StageName.SELECT):
                    row = locked_engine.latest_report(locked_task.task_id, stage.value)
                    if row is not None and locked_engine.report_is_current(
                        locked_task, stage, row
                    )[0]:
                        _force_stage_rerun(
                            locked_engine,
                            locked_task,
                            stage,
                            "--recalibrate requested by corpus pipeline",
                        )

        run_kwargs = (
            {"pre_run": locked_pipeline_pre_run}
            if (
                require_empirical
                or bool(getattr(worker_args, "recalibrate", False))
                or bool(
                    getattr(worker_args, "enforce_evidence_contract", False)
                )
            )
            else {}
        )
        try:
            engine.run(
                task.task_id,
                until=target_stage.value,
                **run_kwargs,
            )
        except _LockedRunRefusal as exc:
            return finish({
                "task_id": task.task_id,
                "ok": False,
                "state": "blocked",
                "detail": str(exc),
                "usd": float(getattr(provider.meter, "total_usd", 0.0)),
            })
        task = engine.load_task(task.task_id)
        if configured_spec is not None:
            from elt_taskgen.pipeline_readiness import (
                ReadinessState,
                seal_configured_stage,
                stage_readiness,
            )

            # A crash between the PASS row and this seal causes a conservative
            # rerun. Hold the task lock while sealing so another configured
            # worker cannot validate a different configuration concurrently.
            with engine.task_locks((task.task_id,)):
                task = engine.load_task(task.task_id)
                for stage in STAGE_ORDER:
                    check = stage_readiness(engine, task, stage)
                    if check.state is ReadinessState.PASS:
                        seal_configured_stage(
                            engine, task, stage, configured_spec
                        )
                    if stage is target_stage:
                        break
        blocked = engine.blocked_stage(task.task_id)
        prepared = engine.latest_report(
            task.task_id, target_stage.value
        )
        prepared_current = engine.report_is_current(
            task, target_stage, prepared
        )[0]
        final = engine.final_verdict(task.task_id)
        target_index = STAGE_ORDER.index(target_stage)
        acceptance_index = STAGE_ORDER.index(StageName.GATES_TRANSFORM)
        requires_acceptance = target_index >= acceptance_index
        ok = (
            prepared_current
            and blocked is None
            and (final == FINAL_ACCEPTED if requires_acceptance else True)
        )
        blocked_detail = ""
        blocked_metadata: dict[str, str] = {}
        if blocked is not None:
            blocked_detail, blocked_metadata = _pipeline_blocked_metadata(blocked)
        return finish({
            "task_id": task.task_id,
            "ok": ok,
            "state": (
                "ready" if ok and target_stage is StageName.GATES_TRANSFORM
                else "prepared"
                if ok
                else "blocked"
                if blocked is not None
                else final
            ),
            "detail": (
                ""
                if ok
                else (
                    blocked_detail
                    if blocked is not None
                    else (
                        "task was rejected during candidate preparation"
                        if final == FINAL_REJECTED
                        else (
                            f"{target_stage.value} did not pass at the current "
                            "content hash"
                        )
                    )
                )
            ),
            "usd": float(
                getattr(getattr(provider, "meter", None), "total_usd", 0.0)
            ),
            **blocked_metadata,
        })
    except InfrastructureFailure as exc:
        return finish({
            "task_id": str(getattr(worker_args, "task_id", "?")),
            "ok": False,
            "state": "infrastructure",
            "detail": str(exc),
            "infrastructure_scope": str(
                getattr(exc, "budget_scope", "") or ""
            ),
            "usd": float(
                getattr(getattr(provider, "meter", None), "total_usd", 0.0)
            ),
        })
    except CliUsageError as exc:
        return finish({
            "task_id": str(getattr(worker_args, "task_id", "?")),
            "ok": False,
            "state": "usage_error",
            "detail": str(exc),
            "usd": float(
                getattr(getattr(provider, "meter", None), "total_usd", 0.0)
            ),
        })
    finally:
        if engine is not None:
            engine.close()


def _registered_task_ids(
    workspace: Path, requested: list[str] | None
) -> tuple[str, ...]:
    """Resolve a stable, unique task roster from a workspace."""
    if requested:
        task_ids = tuple(dict.fromkeys(str(value) for value in requested))
    else:
        task_ids = tuple(
            sorted(
                path.parent.name
                for path in (workspace / "tasks").glob("*/task_ir.json")
            )
        )
    if not task_ids:
        raise CliUsageError(
            "pipeline found no registered tasks; ingest candidates from the source "
            "adapters first"
        )
    for task_id in task_ids:
        try:
            validate_task_id_segment(task_id)
        except (TypeError, ValueError) as exc:
            raise CliUsageError(f"unsafe pipeline task id {task_id!r}: {exc}") from None
    missing = [
        task_id
        for task_id in task_ids
        if not (workspace / "tasks" / task_id / "task_ir.json").is_file()
    ]
    if missing:
        raise CliUsageError(f"pipeline task ids are not registered: {missing}")
    return task_ids


def _pipeline_worker_payload(
    args,
    task_id: str,
    *,
    task_budget: float | None,
    until_stage: StageName = StageName.CONTAMINATION_POST,
    require_empirical: bool | None = None,
    run_id: str = "",
    enforce_evidence_contract: bool = False,
    run_spec: dict | None = None,
) -> dict:
    """Closed serializable worker configuration; no live provider is shared."""
    per_task_budget = float(args.budget_per_task)
    if task_budget is not None:
        per_task_budget = min(per_task_budget, float(task_budget))
    return {
        "workspace": str(Path(args.workspace).resolve()),
        "task_id": task_id,
        "max_repair_rounds": int(args.max_repair_rounds),
        "agents_config": (
            str(Path(args.agents_config).resolve()) if args.agents_config else None
        ),
        "admission_reference": str(
            getattr(args, "admission_reference", "") or ""
        ),
        "replay_only": bool(args.replay_only),
        "record": bool(args.record),
        "budget_per_task": per_task_budget,
        # One worker is one task. The apportioned total is a circuit breaker;
        # CostMeter deliberately accounts a completed call before reporting a
        # final-call breach, so it is not a prepaid reservation guarantee.
        "budget_total": task_budget,
        "repair_proposer": bool(args.repair_proposer),
        "repair_proposer_mode": str(args.repair_proposer_mode),
        "repair_attempts": args.repair_attempts,
        "empirical": (
            not bool(args.allow_structural_difficulty)
            if require_empirical is None
            else bool(require_empirical)
        ),
        "require_empirical": (
            not bool(args.allow_structural_difficulty)
            if require_empirical is None
            else bool(require_empirical)
        ),
        "allow_structural_difficulty": bool(args.allow_structural_difficulty),
        "variants": None,
        "recalibrate": bool(args.recalibrate),
        "allow_unlocked_env": bool(args.allow_unlocked_env),
        "release_mode": "development",
        "sandbox_attestation": None,
        "destination": str(getattr(args, "destination", None) or "snowflake"),
        "extra_destinations": [
            str(name) for name in (getattr(args, "extra_destinations", None) or ())
        ],
        "http_retries": getattr(args, "http_retries", None),
        "schema_retries": getattr(args, "schema_retries", None),
        "http_timeout_seconds": getattr(args, "http_timeout_seconds", None),
        "http_backoff_seconds": getattr(args, "http_backoff_seconds", None),
        "global_budget_run_id": str(
            getattr(args, "global_budget_run_id", "") or ""
        ),
        "global_budget_total": getattr(args, "global_budget_total", None),
        "until_stage": until_stage.value,
        "run_id": run_id,
        "enforce_evidence_contract": bool(enforce_evidence_contract),
        "run_spec": run_spec,
    }


def _cmd_pipeline_legacy(args) -> int:
    """Run all registered source tasks with isolated concurrent workers.

    Agent-backed work happens per task through ``CONTAMINATION_POST``. The
    coordinator performs deterministic corpus-wide selection once, records that
    common decision for selected tasks, audits them, and optionally cuts one
    immutable release. No provider/session object or SQLite connection is shared
    between workers.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from concurrent.futures.process import BrokenProcessPool

    from elt_taskgen.adapters import eltbench_anchor
    from elt_taskgen.corpus import selection as selection_mod
    from elt_taskgen.export import release as release_mod
    from elt_taskgen.models import Origin

    workspace = Path(args.workspace).resolve()
    allow_partial_candidates = bool(
        getattr(args, "allow_partial_candidates", False)
    )
    configured_readiness_spec = None
    if getattr(args, "configured_run_spec", None) is not None:
        from elt_taskgen.pipeline_readiness import GenerationRunSpec

        configured_readiness_spec = GenerationRunSpec.model_validate(
            args.configured_run_spec
        )
    if args.release_dir is None:
        release_only_flags = []
        if args.sandbox_attestation is not None:
            release_only_flags.append("--sandbox-attestation")
        if args.development_release:
            release_only_flags.append("--development-release")
        if args.allow_unlocked_env:
            release_only_flags.append("--allow-unlocked-env")
        if release_only_flags:
            raise CliUsageError(
                f"{', '.join(release_only_flags)} require --release-dir; "
                "without an output directory pipeline prepares and selects "
                "the corpus but does not freeze a release"
            )

    # Refuse argument/configuration errors before a typed ingest can mutate the
    # workspace. Roster-dependent bounds remain below, after task discovery.
    if args.workers < 1:
        raise CliUsageError("--workers must be at least 1")
    if args.size is not None and args.size < 1:
        raise CliUsageError("--size must be at least 1")
    per_task_limit = float(args.budget_per_task)
    if not math.isfinite(per_task_limit) or per_task_limit <= 0.0:
        raise CliUsageError("--budget-per-task must be a finite positive number")
    val_fraction = float(args.val_fraction)
    if not math.isfinite(val_fraction) or not 0.0 <= val_fraction <= 1.0:
        raise CliUsageError("--val-fraction must be a finite number between 0 and 1")
    if bool(args.record) and bool(args.replay_only):
        raise CliUsageError("--record and --replay-only cannot be used together")
    aggregate_limit = None
    if args.budget_total is not None:
        aggregate_limit = float(args.budget_total)
        if not math.isfinite(aggregate_limit) or aggregate_limit <= 0.0:
            raise CliUsageError("--budget-total must be a finite positive number")

    allowed_sources = {
        Origin.DBT.value,
        Origin.DLT.value,
        Origin.SYNSQL.value,
        Origin.SCHEMAPILE.value,
        Origin.WIKIDBS.value,
    }
    required_source_values = {
        value.strip()
        for value in str(args.require_sources or "").split(",")
        if value.strip()
    }
    unknown_sources = sorted(required_source_values - allowed_sources)
    if unknown_sources:
        raise CliUsageError(f"unknown --require-sources values: {unknown_sources}")

    release_mode = "development" if args.development_release else "certified"
    if args.release_dir is not None:
        if release_mode == "certified" and not required_source_values:
            raise CliUsageError(
                "a certified pipeline release requires at least one "
                "--require-sources origin; an empty source policy is development-only"
            )
        if release_mode == "certified" and args.allow_structural_difficulty:
            raise CliUsageError(
                "--allow-structural-difficulty is development-only; pair it with "
                "--development-release or enable empirical calibration"
            )
        if release_mode == "certified" and args.sandbox_attestation is None:
            raise CliUsageError(
                "certified pipeline release requires --sandbox-attestation; "
                "use --development-release only for an explicitly unlabelled local cut"
            )
        if release_mode == "development" and args.sandbox_attestation is not None:
            raise CliUsageError(
                "--sandbox-attestation is only valid for a certified release"
            )
        requested_release_dir = Path(args.release_dir).resolve()
        if requested_release_dir.exists() and not (
            requested_release_dir.is_dir()
            and (requested_release_dir / "release_manifest.json").is_file()
        ):
            raise CliUsageError(
                "pipeline release directory already exists without a complete "
                "manifest (immutable output; move it aside): "
                f"{requested_release_dir}"
            )

    expected_campaign_fingerprint = _active_campaign_fingerprint(args)
    ingest_manifest = getattr(args, "ingest_manifest", None)
    ingest_reingest = bool(getattr(args, "reingest", False))
    if ingest_reingest and ingest_manifest is None:
        raise CliUsageError("pipeline --reingest requires --ingest-manifest")
    if ingest_manifest is not None:
        from elt_taskgen.ingest_manifest import (
            FiveSourceIngestError,
            ingest_five_sources,
        )

        try:
            ingested = ingest_five_sources(
                Path(ingest_manifest),
                workspace=workspace,
                reingest=ingest_reingest,
            )
        except (FiveSourceIngestError, OSError, ValueError) as exc:
            raise CliUsageError(
                f"five-source ingest preflight refused: {exc}"
            ) from None
        print(
            f"five-source ingest {ingested.manifest_sha256[:16]} registered/"
            f"confirmed {len(ingested.tasks)} task(s) before pipeline"
        )
    task_ids = _registered_task_ids(workspace, args.task_id)
    if args.size is not None and args.size > len(task_ids):
        raise CliUsageError(
            f"--size must be between 1 and the {len(task_ids)} scheduled tasks"
        )
    requested_size = int(args.size or len(task_ids))
    per_task_total = (
        None
        if getattr(args, "configured_run_spec", None) is not None
        else aggregate_limit / len(task_ids)
        if aggregate_limit is not None
        else None
    )

    # Validate the source contract from registered TaskIRs before constructing a
    # provider or spending on any candidate. Re-load after workers too: repairs
    # may legitimately replace the semantic identity while a task is prepared.
    preflight_engine = _open_engine(workspace)
    try:
        scheduled_tasks = {
            task_id: preflight_engine.load_task(task_id) for task_id in task_ids
        }
    finally:
        preflight_engine.close()
    present_sources = {task.origin.value for task in scheduled_tasks.values()}
    missing_sources = sorted(required_source_values - present_sources)
    if missing_sources:
        raise CliUsageError(
            f"registered pipeline roster is missing required sources: {missing_sources}"
        )
    if len(required_source_values) > requested_size:
        raise CliUsageError(
            f"--size {requested_size} cannot represent {len(required_source_values)} "
            "required sources"
        )

    attestation = None
    if args.release_dir is not None:
        if args.sandbox_attestation is not None:
            from elt_taskgen.export.attestation_gate import (
                AttestationRefusal,
                require_attestation_for_labels,
            )
            from elt_taskgen.runtime.attestation import (
                SandboxAttestation,
                SandboxAttestationError,
                verify_sandbox_attestation,
            )
            from elt_taskgen.verification import contamination as contamination_mod

            try:
                attestation = verify_sandbox_attestation(
                    SandboxAttestation.model_validate_json(
                        Path(args.sandbox_attestation).read_text(encoding="utf-8")
                    )
                )
                require_attestation_for_labels(
                    {
                        "corpus_profile": release_mod.COMBINED_CORPUS_PROFILE,
                        "public_layout": release_mod.COMBINED_PUBLIC_LAYOUT,
                        "variants": {
                            task_id: tuple(v.value for v in RLVR_TASK_VARIANTS)
                            for task_id in task_ids
                        },
                    },
                    attestation,
                    contamination_mode=contamination_mod.enforcement(),
                    agents_config=args.agents_config,
                )
            except (
                OSError,
                ValueError,
                SandboxAttestationError,
                AttestationRefusal,
            ) as exc:
                raise CliUsageError(f"invalid sandbox attestation: {exc}") from None

    # The duplicate firewall must serialize admission of fresh candidates. If
    # this happened inside workers, two clones could each observe an admitted
    # corpus that did not yet contain the other and both incur provider spend.
    intake_engine = _open_engine(workspace, max_repair_rounds=0)
    try:
        preflight_stopped, preflight_results = _pipeline_batch_intake_preflight(
            intake_engine, scheduled_tasks
        )
    finally:
        intake_engine.close()

    configured_spec_payload = getattr(args, "configured_run_spec", None)
    payloads = [
        _pipeline_worker_payload(
            args,
            task_id,
            task_budget=per_task_total,
            run_id=str(getattr(args, "configured_run_id", "") or ""),
            enforce_evidence_contract=configured_spec_payload is not None,
            run_spec=configured_spec_payload,
        )
        for task_id in task_ids
        if task_id not in preflight_stopped
    ]
    max_workers = min(int(args.workers), len(payloads))
    print(
        f"pipeline: {len(task_ids)} task(s), {max_workers} isolated worker(s), "
        + (
            "empirical difficulty required"
            if not args.allow_structural_difficulty
            else "DEVELOPMENT structural-only difficulty allowed"
        )
    )
    results: list[dict] = list(preflight_results)
    for result in preflight_results:
        print(f"  {result['task_id']}: {result['state']}")
    if max_workers == 1:
        for payload in payloads:
            try:
                result = _pipeline_worker(payload)
            except Exception as exc:  # noqa: BLE001 - retain sibling outcomes
                result = {
                    "task_id": str(payload["task_id"]),
                    "ok": False,
                    "state": "infrastructure",
                    "detail": f"worker exception: {type(exc).__name__}: {exc}",
                    "usd": 0.0,
                }
            results.append(result)
            print(f"  {result['task_id']}: {result['state']}")
    elif max_workers > 1:
        try:
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_pipeline_worker, payload): payload
                    for payload in payloads
                }
                for future in as_completed(futures):
                    # Ordinary exceptions are programming/state bugs and remain
                    # visible. A process dying without a result is infrastructure.
                    result = future.result()
                    results.append(result)
                    print(f"  {result['task_id']}: {result['state']}")
        except BrokenProcessPool as exc:
            raise InfrastructureFailure(
                "pipeline", "candidate_worker", "broken_process_pool"
            ) from exc
    results.sort(key=lambda row: row["task_id"])
    total_usd = sum(float(row.get("usd", 0.0)) for row in results)
    print(f"pipeline live spend: ${total_usd:.4f}")
    rejected_workers = [
        row for row in results if row.get("state") == FINAL_REJECTED
    ]
    operational_failures = [
        row
        for row in results
        if not row.get("ok") and row.get("state") != FINAL_REJECTED
    ]
    for row in rejected_workers:
        print(f"  {row['task_id']}: {row['detail']}")
    if operational_failures and not allow_partial_candidates:
        for row in operational_failures:
            print(f"  {row['task_id']}: {row['detail']}")
        return 2

    ready_ids = tuple(row["task_id"] for row in results if row.get("ok"))
    if not ready_ids:
        for row in operational_failures:
            print(f"  {row['task_id']}: {row['detail']}")
        print("no candidate reached release preparation; nothing to select")
        return 2 if operational_failures else 1
    selection_target = (
        min(requested_size, len(ready_ids))
        if allow_partial_candidates
        else requested_size
    )
    coordinator = _open_engine(workspace, max_repair_rounds=0)
    coordinator.expected_campaign_fingerprint = expected_campaign_fingerprint
    try:
        def derive_selection():
            """Re-read every input to the deterministic cohort decision."""

            current_tasks = [
                coordinator.load_task(task_id) for task_id in ready_ids
            ]
            measurements: dict[str, DifficultyMeasurement] = {}
            accepted_variants: dict[str, frozenset[str]] = {}
            for current_task in current_tasks:
                path = _evidence_dir(coordinator, current_task) / "difficulty.json"
                try:
                    measurements[current_task.task_id] = (
                        DifficultyMeasurement.model_validate_json(
                            path.read_text(encoding="utf-8")
                        )
                    )
                except (OSError, ValueError) as exc:
                    raise CliUsageError(
                        "difficulty evidence for prepared task "
                        f"{current_task.task_id!r} is unreadable: {exc}"
                    ) from None
                accepted_variants[current_task.task_id] = _accepted_variants(
                    coordinator, current_task
                )
            try:
                anchors = eltbench_anchor.load_anchor_store(workspace)
            except FileNotFoundError:
                anchors = []
            current_selection = selection_mod.select(
                current_tasks,
                measurements,
                anchors,
                selection_mod.Quotas(
                    size=selection_target,
                    val_fraction=val_fraction,
                    origin_mix={
                        # ``--require-sources`` is a coverage floor, not a
                        # requested distribution. Give every required origin a
                        # one-slot deficit; remaining slots keep stable order.
                        Origin(value): 1.0 / selection_target
                        for value in sorted(required_source_values)
                    }
                    if required_source_values
                    else {},
                ),
                accepted_variants=accepted_variants,
                require_empirical=not bool(args.allow_structural_difficulty),
                expected_campaign_fingerprint=expected_campaign_fingerprint,
            )
            if rejected_workers:
                rejected = dict(current_selection.rejected)
                rejected.update(
                    {
                        str(row["task_id"]): str(
                            row.get("detail") or "candidate rejected"
                        )
                        for row in rejected_workers
                    }
                )
                current_selection = current_selection.model_copy(
                    update={"rejected": rejected}
                )
            return current_selection, current_tasks

        selection, tasks = derive_selection()
        selected = tuple(selection.train) + tuple(selection.val)
        if len(selected) != selection_target:
            reasons = "; ".join(
                f"{task_id}: {reason}"
                for task_id, reason in sorted(selection.rejected.items())
            )
            print(
                f"corpus selection produced {len(selected)}/{selection_target} tasks: "
                f"{reasons}"
            )
            if not allow_partial_candidates or not selected:
                return 1

        selected_tasks = {
            task.task_id: task for task in tasks if task.task_id in selected
        }
        selected_sources = {task.origin.value for task in selected_tasks.values()}
        missing_selected_sources = sorted(required_source_values - selected_sources)
        if missing_selected_sources:
            print(
                "corpus selection omitted required sources: "
                f"{missing_selected_sources}"
            )
            return 1

        # The tentative decision is not published globally until every selected
        # audit passes and a lock-scoped re-derivation proves that none of its
        # task/evidence inputs changed in the meantime.
        selected_set = frozenset(selected)

        def run_global_selection(_engine: Engine, task: TaskIR) -> StageOutcome:
            if task.task_id not in selected_set:
                return StageOutcome(
                    VERDICT_FATAL,
                    StagePayload(error="task is outside the global corpus selection"),
                )
            _atomic_replace_text(
                _evidence_dir(_engine, task) / "selection.json",
                canonical_json(selection.model_dump(mode="json")),
            )
            return StageOutcome(VERDICT_PASS, selection)

        coordinator.set_stage_runner(StageName.SELECT, run_global_selection)
        coordinator.set_stage_runner(StageName.AUDIT, run_audit)
        audit_rejections: list[tuple[str, str]] = []
        audit_blocks: list[tuple[str, str]] = []
        for task_id in selected:
            def locked_global_selection_pre_run(
                locked_engine: Engine, task: TaskIR
            ) -> None:
                # A fresh corpus decision must precede a fresh final audit.
                # Shadow both together; otherwise an older AUDIT pass can sit
                # before the new SELECT row and appear to approve a decision it
                # never observed.
                for stage in (StageName.SELECT, StageName.AUDIT):
                    row = locked_engine.latest_report(task.task_id, stage.value)
                    if row is not None and locked_engine.report_is_current(
                        task, stage, row
                    )[0]:
                        _force_stage_rerun(
                            locked_engine,
                            task,
                            stage,
                            "re-derived by the corpus-wide pipeline selection",
                        )

            try:
                coordinator.run(
                    task_id,
                    until=StageName.AUDIT.value,
                    pre_run=locked_global_selection_pre_run,
                )
            except InfrastructureFailure as exc:
                audit_blocks.append((task_id, str(exc)))
                continue
            task = coordinator.load_task(task_id)
            blocked = coordinator.blocked_stage(task_id)
            if blocked is not None:
                try:
                    detail = str(json.loads(blocked.payload_json).get("error") or "")
                except (TypeError, ValueError):
                    detail = "audit is blocked with unreadable evidence"
                audit_blocks.append((task_id, detail))
                continue
            audit = coordinator.latest_report(task_id, StageName.AUDIT.value)
            if coordinator.final_verdict(task_id) == FINAL_REJECTED:
                detail = "audit rejected this candidate"
                if audit is not None:
                    try:
                        detail = str(
                            json.loads(audit.payload_json).get("error") or detail
                        )
                    except (TypeError, ValueError):
                        pass
                audit_rejections.append((task_id, detail))
            elif not coordinator.report_is_current(task, StageName.AUDIT, audit)[0]:
                audit_blocks.append(
                    (task_id, "audit did not pass at the current content hash")
                )

        if audit_blocks:
            for task_id, detail in audit_blocks:
                print(f"  {task_id}: {detail}")
            return 2
        if audit_rejections:
            for task_id, detail in audit_rejections:
                print(f"  {task_id}: {detail}")
            return 1

        # One workspace publication epoch serializes even DISJOINT pipeline
        # rosters, which still share corpus_selection.json.  Every scheduled
        # task is then locked in stable order while selection inputs, stage
        # currency and the optional release snapshot are re-read.
        with coordinator.coordinator_lock():
            with coordinator.task_locks(task_ids):
                for task_id in task_ids:
                    coordinator.recover_pending_repair(task_id)
                locked_selection, _locked_tasks = derive_selection()
                if locked_selection.model_dump(mode="json") != selection.model_dump(
                    mode="json"
                ):
                    raise CliUsageError(
                        "pipeline selection inputs changed between audit and "
                        "publication; nothing was published — re-run the pipeline"
                    )

                locked_selected = tuple(locked_selection.train) + tuple(
                    locked_selection.val
                )
                for task_id in locked_selected:
                    task = coordinator.load_task(task_id)
                    for stage in STAGE_ORDER:
                        if stage is StageName.RELEASE:
                            break
                        row = coordinator.latest_report(task_id, stage.value)
                        current, why = coordinator.report_is_current(task, stage, row)
                        if not current:
                            raise CliUsageError(
                                f"task {task_id!r} changed before corpus "
                                f"publication: stage {stage.value!r}: {why}"
                            )
                    final = coordinator.final_verdict(task_id)
                    if final != FINAL_ACCEPTED:
                        raise CliUsageError(
                            f"task {task_id!r} changed before corpus publication: "
                            f"final verdict is {final!r}"
                        )
                    if configured_readiness_spec is not None:
                        from elt_taskgen.pipeline_readiness import (
                            seal_configured_stage,
                        )

                        for configured_stage in (
                            StageName.SELECT,
                            StageName.AUDIT,
                        ):
                            seal_configured_stage(
                                coordinator,
                                task,
                                configured_stage,
                                configured_readiness_spec,
                            )

                selection = locked_selection
                serialized_selection = (
                    readable_json(selection.model_dump(mode="json")) + "\n"
                )
                selection_path = workspace / "state" / "corpus_selection.json"
                _atomic_replace_text(selection_path, serialized_selection)
                for task_id in locked_selected:
                    task = coordinator.load_task(task_id)
                    _atomic_replace_text(
                        _evidence_dir(coordinator, task) / "selection.json",
                        canonical_json(selection.model_dump(mode="json")),
                    )
                print(
                    f"corpus selection: {selection_path} "
                    f"({len(locked_selected)} task(s))"
                )

                if args.release_dir is None:
                    return 0

                out = Path(args.release_dir).resolve()
                if out.exists():
                    verification = release_mod.verify_release(out)
                    if not verification.ok:
                        raise CliUsageError(
                            "existing immutable release failed verification: "
                            + "; ".join(verification.failures[:3])
                        )
                    try:
                        manifest = release_mod.ReleaseManifest.model_validate_json(
                            (out / "release_manifest.json").read_text(
                                encoding="utf-8"
                            )
                        )
                    except (OSError, ValueError) as exc:
                        raise CliUsageError(
                            f"existing release manifest is unreadable: {exc}"
                        ) from None
                    expected_splits = {
                        task_id: "train" for task_id in selection.train
                    } | {task_id: "val" for task_id in selection.val}
                    mismatch = []
                    if manifest.schema_version != release_mod.RELEASE_SCHEMA_VERSION:
                        mismatch.append(
                            f"schema {manifest.schema_version!r} is not current"
                        )
                    if (
                        manifest.corpus_profile
                        != release_mod.COMBINED_CORPUS_PROFILE
                    ):
                        mismatch.append("corpus profile differs")
                    if (
                        manifest.public_layout
                        != release_mod.COMBINED_PUBLIC_LAYOUT
                    ):
                        mismatch.append("public layout differs")
                    if manifest.release_mode != release_mode:
                        mismatch.append(
                            f"mode {manifest.release_mode!r} != {release_mode!r}"
                        )
                    if manifest.tasks != selection.task_content_hashes:
                        mismatch.append("task identities differ")
                    if manifest.splits != expected_splits:
                        mismatch.append("train/val splits differ")
                    if manifest.variants != selection.variants:
                        mismatch.append("variant roster differs")
                    if (
                        manifest.difficulty_measurements
                        != selection.difficulty_measurements
                    ):
                        mismatch.append("difficulty evidence differs")
                    if mismatch:
                        raise CliUsageError(
                            "pipeline release directory already contains a "
                            "different immutable release (move it aside): "
                            + "; ".join(mismatch)
                        )
                    adopted = True
                else:
                    try:
                        manifest = release_mod.freeze_release(
                            coordinator,
                            selection,
                            out,
                            allow_unlocked_env=bool(args.allow_unlocked_env),
                            release_mode=release_mode,
                            sandbox_attestation=attestation,
                            agents_config=args.agents_config,
                        )
                    except (OSError, ValueError) as exc:
                        raise CliUsageError(str(exc)) from None
                    verification = release_mod.verify_release(out)
                    if not verification.ok:
                        raise CliUsageError(
                            "new release failed self-verification: "
                            + "; ".join(verification.failures[:3])
                        )
                    adopted = False

                release_manifest_sha = hashlib.sha256(
                    (out / "release_manifest.json").read_bytes()
                ).hexdigest()
                for task_id in locked_selected:
                    released_task = coordinator.load_task(task_id)
                    coordinator.record_report(
                        released_task,
                        StageName.RELEASE.value,
                        VERDICT_PASS,
                        StagePayload(
                            detail=(
                                f"batch release {manifest.release_id} verified at "
                                "the configured output path"
                            ),
                            data={
                                "release_id": manifest.release_id,
                                "release_dir": str(out),
                                "release_manifest_sha256": release_manifest_sha,
                                "verified_files": str(verification.files_checked),
                            },
                        ),
                    )
                    if configured_readiness_spec is not None:
                        from elt_taskgen.pipeline_readiness import (
                            seal_configured_stage,
                        )

                        seal_configured_stage(
                            coordinator,
                            released_task,
                            StageName.RELEASE,
                            configured_readiness_spec,
                        )
                _atomic_replace_text(
                    workspace / "state" / "corpus_release.json",
                    readable_json(
                        {
                            "release_id": manifest.release_id,
                            "release_dir": str(out),
                            "release_mode": manifest.release_mode,
                            "tasks": manifest.tasks,
                            "manifest_sha256": release_manifest_sha,
                            "files_verified": verification.files_checked,
                        }
                    )
                    + "\n",
                )
                verb = "adopted and re-verified" if adopted else "created"
                print(
                    f"release {manifest.release_id}: {out} ({verb}; "
                    f"{verification.files_checked} file(s) verified)"
                )
                return 0
    finally:
        coordinator.close()


def _parse_source_allocation(value: str | None) -> dict[str, int] | None:
    if value is None:
        return None
    allocation: dict[str, int] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise CliUsageError(
                "--source-allocation must be a comma list such as dbt=2,dlt=1"
            )
        family, raw_count = (part.strip() for part in item.split("=", 1))
        if not family or family in allocation:
            raise CliUsageError(
                f"invalid or repeated source allocation family {family!r}"
            )
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise CliUsageError(
                f"source allocation for {family!r} is not an integer"
            ) from exc
        if count < 0:
            raise CliUsageError(
                f"source allocation for {family!r} must be non-negative"
            )
        allocation[family] = count
    if not allocation:
        raise CliUsageError("--source-allocation must not be empty")
    return allocation


#: Transport mode for session roles. Headless routes author/repair through
#: Claude Code and witnesses through Codex; one-shot seats remain on the API.
AGENT_HARNESS_ENV = "ELT_TASKGEN_AGENT_HARNESS"
AGENT_HARNESS_MODES: tuple[str, ...] = ("api", "headless")
_AGENT_HARNESS_PROVIDERS: dict[str, dict[str, str]] = {
    "api": {"ELT_TASKGEN_CLAUDE_PROVIDER": "anthropic", "ELT_TASKGEN_OSS_PROVIDER": "openai_compat"},
    "headless": {"ELT_TASKGEN_CLAUDE_PROVIDER": "claude_headless", "ELT_TASKGEN_OSS_PROVIDER": "codex_headless"},
}


def current_agent_harness() -> str:
    """The mode in force: `$ELT_TASKGEN_AGENT_HARNESS` (api|headless), else
    headless when both family variables already name the headless kinds,
    else api."""
    import os

    declared = os.environ.get(AGENT_HARNESS_ENV, "").strip().lower()
    if declared in AGENT_HARNESS_MODES:
        return declared
    if declared:
        raise CliUsageError(
            f"${AGENT_HARNESS_ENV}={declared!r} is not one of {list(AGENT_HARNESS_MODES)}"
        )
    headless = _AGENT_HARNESS_PROVIDERS["headless"]
    if all(os.environ.get(name, "").strip() == value for name, value in headless.items()):
        return "headless"
    return "api"


def apply_agent_harness(mode: str | None) -> str:
    """Put `mode` (or the mode in force when None) into the environment:
    the mode variable and both provider-family variables, so every routing
    document read afterwards resolves to that mode. Returns the mode."""
    import os

    resolved = str(mode or current_agent_harness()).strip().lower()
    if resolved not in AGENT_HARNESS_MODES:
        raise CliUsageError(f"--agent-harness must be one of {list(AGENT_HARNESS_MODES)}, not {resolved!r}")
    os.environ[AGENT_HARNESS_ENV] = resolved
    for name, value in _AGENT_HARNESS_PROVIDERS[resolved].items():
        os.environ[name] = value
    return resolved


#: Candidate-count mode defaults. Each is what the completed packaged runs
#: used, so `elt-taskgen pipeline --candidate-count N` needs nothing else.
DEFAULT_CANDIDATE_POOL = "config/candidate_pool.ingest.yaml"
DEFAULT_EXPORT_DIRNAME = "packages"
#: The shared live-spend circuit breaker a configured run gets when none is
#: declared, per requested candidate (the ten-task runs used $40 each).
DEFAULT_BUDGET_TOTAL_PER_CANDIDATE_USD = 40.0
#: The council admission record both harness modes consult: the critic seats
#: are pinned to the API route in agents.yaml, so their admission does not
#: depend on the mode.
DEFAULT_ADMISSION_RECORD = "council/state/council.live_admitted"
BENCH_ROOT_ENV = "ELT_BENCH_ROOT"
DEFAULT_BENCH_ROOT_DIRNAME = "ELT-Bench"


def _default_admission_reference() -> Path | None:
    """The metrology record a configured run consults when none is named:
    `$ELT_TASKGEN_ADMISSION`, else the repository's live record. None when
    neither exists (the council stage then fails closed as before)."""
    import os

    from elt_taskgen.package_resources import resource_path
    from elt_taskgen.review.metrology import ADMISSION_ENV

    named = os.environ.get(ADMISSION_ENV, "").strip()
    if named:
        return Path(named).resolve()
    record = resource_path(DEFAULT_ADMISSION_RECORD)
    return record.resolve() if record.is_file() else None


def _default_candidate_pool() -> Path:
    from elt_taskgen.package_resources import resource_path

    return resource_path(DEFAULT_CANDIDATE_POOL)


def _repin_default_pool_if_stale(pool_path: Path, pool):
    """Repin the shipped candidate pool to the installed generator in place.

    Only the whole-generator pin and the per-entry adapter pins move; the
    roster is unchanged. An operator-supplied manifest is never rewritten:
    the ingest stage keeps refusing it with the digest mismatch."""
    from elt_taskgen.ingest_manifest import (
        current_generator_pin,
        repin_current_implementation,
    )

    observed = current_generator_pin()
    if (
        pool.generator.sha256 == observed.sha256
        and pool.generator.lock_sha256 == observed.lock_sha256
        and pool.generator.version == observed.version
    ):
        return pool
    import yaml

    repinned = repin_current_implementation(pool)
    _atomic_replace_text(
        pool_path,
        yaml.safe_dump(repinned.model_dump(mode="json"), sort_keys=False, width=4096),
    )
    print(
        f"NOTE: candidate pool {pool_path} repinned to the installed generator "
        f"({pool.generator.sha256[:12]} -> {observed.sha256[:12]}); the roster "
        "is unchanged",
        file=sys.stderr,
    )
    return repinned


def _resolve_bench_root(args) -> Path | None:
    """`--bench-root`, else `$ELT_BENCH_ROOT`, else the ELT-Bench checkout
    beside this repository; None when none of them is a directory."""
    import os

    from elt_taskgen.package_resources import checkout_root

    named = getattr(args, "bench_root", None)
    if named is not None:
        return Path(named).resolve()
    from_env = os.environ.get(BENCH_ROOT_ENV, "").strip()
    if from_env:
        return Path(from_env).resolve()
    root = checkout_root()
    if root is not None:
        sibling = root.parent / DEFAULT_BENCH_ROOT_DIRNAME
        if sibling.is_dir():
            return sibling.resolve()
    return None


def _ensure_firewall_armed(args, workspace: Path) -> None:
    """Arm the contamination firewall from the ELT-Bench checkout when this
    workspace has no anchor store yet (the `measure-target` step of the
    launch recipe). An already-armed workspace is left alone. Without a
    checkout the run continues exactly as before, with the firewall note."""
    from elt_taskgen.adapters import eltbench_anchor

    store = eltbench_anchor.anchor_store_dir(workspace)
    if store.is_dir() and any(store.iterdir()):
        return
    bench_root = _resolve_bench_root(args)
    if bench_root is None or not bench_root.is_dir():
        print(
            "NOTE: contamination firewall not armed in this workspace: no "
            f"ELT-Bench checkout found (pass --bench-root or set {BENCH_ROOT_ENV})",
            file=sys.stderr,
        )
        return
    print(f"arming the contamination firewall from {bench_root}", file=sys.stderr)
    code = cmd_ingest_anchor(
        argparse.Namespace(
            workspace=workspace, bench_root=bench_root, db=None, command=ANCHOR_COMMAND
        )
    )
    if code != 0:
        raise CliUsageError(
            f"contamination firewall could not be armed from {bench_root} "
            f"(measure-target exit {code})"
        )


def _configured_run_spec(args):
    """Merge a typed YAML/JSON run config with explicit selection CLI flags."""

    import yaml

    from elt_taskgen.pipeline_readiness import (
        GenerationRunSpec,
        ReadinessProfile,
    )

    data: dict = {}
    config_base: Path | None = None
    config_path = getattr(args, "run_config", None)
    if config_path is not None:
        path = Path(config_path).resolve()
        config_base = path.parent
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise CliUsageError(f"invalid --run-config {path}: {exc}") from None
        if not isinstance(loaded, dict):
            raise CliUsageError("--run-config must contain a mapping")
        data.update(loaded)

        # Filesystem references in a portable run document are relative to the
        # document, not the caller's current working directory. Runtime
        # evidence remains read-only; normalization does not inspect it.
        for field in (
            "agents_config",
            "admission_reference",
            "export_dir",
            "runtime_certification_store",
        ):
            raw = data.get(field)
            if isinstance(raw, str) and raw:
                candidate = Path(raw)
                data[field] = str(
                    (
                        candidate
                        if candidate.is_absolute()
                        else config_base / candidate
                    ).resolve()
                )
        runtime_reports = data.get("runtime_difficulty_reports")
        if isinstance(runtime_reports, dict):
            data["runtime_difficulty_reports"] = {
                task_id: str(
                    (
                        Path(raw)
                        if Path(raw).is_absolute()
                        else config_base / Path(raw)
                    ).resolve()
                )
                if isinstance(raw, str) and raw
                else raw
                for task_id, raw in runtime_reports.items()
            }

    candidate_count = getattr(args, "candidate_count", None)
    if candidate_count is not None:
        data["candidate_count"] = candidate_count
    if "candidate_count" not in data:
        raise CliUsageError(
            "configured pipeline requires --candidate-count N or "
            "candidate_count in --run-config"
        )

    family_spec = getattr(args, "source_families", None)
    if family_spec is not None:
        data["source_families"] = tuple(
            part.strip() for part in family_spec.split(",") if part.strip()
        )
    allocation_spec = getattr(args, "source_allocation", None)
    if allocation_spec is not None:
        data["source_allocation"] = _parse_source_allocation(allocation_spec)
    if getattr(args, "selection_seed", None) is not None:
        data["seed"] = int(args.selection_seed)
    if getattr(args, "readiness_profile", None) is not None:
        data["profile"] = args.readiness_profile
    data.setdefault("profile", ReadinessProfile.PACKAGED.value)
    if getattr(args, "resume", None) is not None:
        data["resume"] = bool(args.resume)
    if getattr(args, "export_dir", None) is not None:
        data["export_dir"] = str(Path(args.export_dir).resolve())
    if getattr(args, "destination", None) is not None:
        # "all" (the default), one name, or a comma list; the first named
        # destination becomes the bundle root, so a run document's own
        # `destination` is replaced rather than merged.
        data["destinations"] = str(args.destination)
        data.pop("destination", None)
    # The agent-harness mode: the flag, else the run document, else the mode
    # in force in the environment. It is applied to the environment here so
    # the routing document this run reads resolves to it.
    if getattr(args, "agent_harness", None) is not None:
        data["agent_harness"] = str(args.agent_harness)
    data.setdefault("agent_harness", current_agent_harness())
    apply_agent_harness(str(data["agent_harness"]))
    for field in (
        "http_retries",
        "schema_retries",
        "http_timeout_seconds",
        "http_backoff_seconds",
    ):
        value = getattr(args, field, None)
        if value is not None:
            data[field] = value

    # Shared CLI values are defaults for a config document, while a non-empty
    # config value remains authoritative. Selection/profile flags above are
    # explicit command-line overrides.
    data.setdefault("workers", int(getattr(args, "workers", 4) or 4))
    data.setdefault(
        "max_repair_rounds", int(getattr(args, "max_repair_rounds", 3))
    )
    data.setdefault("repair_attempts", getattr(args, "repair_attempts", None))
    # The shared --budget-per-task flag carries a legacy default the root
    # parser fills in. In candidate-count mode the typed spec's own default
    # applies unless the operator typed the flag (or a namespace built
    # outside the parser supplies the value directly).
    budget_explicit = getattr(args, "budget_per_task_explicit", None)
    if budget_explicit is None or budget_explicit:
        data.setdefault("budget_per_task", float(args.budget_per_task))
    data.setdefault("budget_total", getattr(args, "budget_total", None))
    if data.get("budget_total") is None:
        # A live configured run needs a finite shared total (the preflight
        # refuses without one): DEFAULT_BUDGET_TOTAL_PER_CANDIDATE_USD per
        # requested candidate, well above the ~$2 a packaged task averaged.
        data["budget_total"] = DEFAULT_BUDGET_TOTAL_PER_CANDIDATE_USD * int(data["candidate_count"])
    if args.agents_config is not None:
        data["agents_config"] = str(Path(args.agents_config).resolve())
    else:
        data.setdefault("agents_config", "")
    admission_reference = getattr(args, "admission_reference", None)
    if admission_reference is not None:
        data["admission_reference"] = str(Path(admission_reference).resolve())
    elif not data.get("admission_reference"):
        default_admission = _default_admission_reference()
        if default_admission is not None:
            data["admission_reference"] = str(default_admission)
    profile = ReadinessProfile(str(data["profile"]))
    if not data.get("export_dir") and profile in {
        ReadinessProfile.PACKAGED,
        ReadinessProfile.RELEASE,
    }:
        data["export_dir"] = str(
            Path(getattr(args, "workspace", DEFAULT_WORKSPACE)).resolve()
            / DEFAULT_EXPORT_DIRNAME
        )
    data.setdefault(
        "empirical",
        profile
        in {
            ReadinessProfile.CALIBRATED,
            ReadinessProfile.RELEASE_READY,
            ReadinessProfile.RELEASE,
        },
    )
    try:
        return GenerationRunSpec.model_validate(data)
    except (TypeError, ValueError) as exc:
        raise CliUsageError(f"invalid configured pipeline request: {exc}") from None


def _repository_run_identity(root: Path) -> dict:
    """Best-effort reproducible repository identity, including unborn repos."""

    def git(*arguments: str) -> tuple[int, bytes]:
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=root,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return 127, b""
        return completed.returncode, completed.stdout

    head_code, head_out = git("rev-parse", "--verify", "HEAD")
    branch_code, branch_out = git("branch", "--show-current")
    status_code, status_out = git("status", "--porcelain=v1", "-z")
    return {
        "head": head_out.decode("utf-8", "replace").strip() if head_code == 0 else None,
        "branch": (
            branch_out.decode("utf-8", "replace").strip()
            if branch_code == 0
            else ""
        ),
        "state": "tracked" if head_code == 0 else "unborn",
        "working_tree_dirty": bool(status_out) if status_code == 0 else None,
        "working_tree_status_sha256": (
            hashlib.sha256(status_out).hexdigest() if status_code == 0 else ""
        ),
    }


def _configured_results(
    args,
    *,
    spec,
    task_ids: tuple[str, ...],
    run_id: str,
    target_stage: StageName,
) -> list[dict]:
    """Run task workers and retain a complete, safety-bounded roster outcome.

    Sequential runs pause untouched siblings after a usage error, a total-budget
    failure or unclassified infrastructure. Rejections, role-limit outcomes and
    infrastructure explicitly scoped to one task remain task-local.
    """

    from concurrent.futures import ProcessPoolExecutor, as_completed
    from concurrent.futures.process import BrokenProcessPool

    def interrupt_unfinished_attempt(task_id: str, detail: str) -> None:
        """Close a crashed worker's durable RUNNING record without hiding it."""

        directory = (
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "attempts"
            / task_id
        )
        for path in sorted(directory.glob("*.json")):
            try:
                prior = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                continue
            if not isinstance(prior, dict) or prior.get("state") != "RUNNING":
                continue
            prior["state"] = "INTERRUPTED"
            prior["detail"] = detail
            _atomic_replace_text(path, readable_json(prior) + "\n")

    workspace = Path(args.workspace).resolve()
    preflight_engine = _open_engine(workspace, max_repair_rounds=0)
    try:
        scheduled = {
            task_id: preflight_engine.load_task(task_id) for task_id in task_ids
        }
        stopped, preflight_results = _pipeline_batch_intake_preflight(
            preflight_engine, scheduled
        )
    finally:
        preflight_engine.close()

    # Configured workers share the transactional durable ledger. Do not divide
    # the cap into wasteful static slices: each task keeps its own cap while
    # every prospective transport competes for the real remaining global pool.
    per_task_total = None
    # Feed the validated typed configuration to the existing provider/engine
    # boundary without changing legacy pipeline semantics.
    worker_args = argparse.Namespace(**vars(args))
    worker_args.workers = spec.workers
    worker_args.max_repair_rounds = spec.max_repair_rounds
    worker_args.repair_attempts = spec.repair_attempts
    worker_args.budget_per_task = spec.budget_per_task
    worker_args.budget_total = spec.budget_total
    worker_args.agents_config = Path(spec.agents_config) if spec.agents_config else None
    worker_args.admission_reference = spec.admission_reference
    worker_args.destination = spec.destination
    worker_args.extra_destinations = spec.extra_destinations
    worker_args.http_retries = spec.http_retries
    worker_args.schema_retries = spec.schema_retries
    worker_args.http_timeout_seconds = spec.http_timeout_seconds
    worker_args.http_backoff_seconds = spec.http_backoff_seconds
    worker_args.global_budget_run_id = run_id if spec.budget_total is not None else ""
    worker_args.global_budget_total = spec.budget_total
    worker_args.allow_structural_difficulty = not bool(spec.empirical)
    payloads = [
        _pipeline_worker_payload(
            worker_args,
            task_id,
            task_budget=per_task_total,
            until_stage=target_stage,
            require_empirical=bool(spec.empirical),
            run_id=run_id,
            enforce_evidence_contract=True,
            run_spec=spec.model_dump(mode="json"),
        )
        for task_id in task_ids
        if task_id not in stopped
    ]
    results: list[dict] = list(preflight_results)
    max_workers = min(spec.workers, len(payloads))
    print(
        f"configured pipeline {run_id}: {len(task_ids)} requested candidate(s), "
        f"{max_workers} isolated worker(s), through {target_stage.value}"
    )
    if max_workers == 1:
        for position, payload in enumerate(payloads):
            try:
                result = _pipeline_worker(payload)
            except Exception as exc:  # noqa: BLE001 - converted, then safety-paused
                detail = f"worker exception: {type(exc).__name__}: {exc}"
                interrupt_unfinished_attempt(str(payload["task_id"]), detail)
                result = {
                    "task_id": str(payload["task_id"]),
                    "ok": False,
                    "state": "infrastructure",
                    "detail": detail,
                    "usd": 0.0,
                }
            results.append(result)
            print(f"  {result['task_id']}: {result['state']}")
            failed_state = str(result.get("state") or "")
            infrastructure_scope = str(
                result.get("infrastructure_scope", "") or ""
            )
            shared_failure = failed_state == "usage_error" or (
                failed_state == "infrastructure"
                and infrastructure_scope != "task"
            )
            if shared_failure:
                failed_task_id = str(result["task_id"])
                scope_detail = (
                    f" with budget scope {infrastructure_scope!r}"
                    if infrastructure_scope
                    else " with no classified task-local budget scope"
                    if failed_state == "infrastructure"
                    else ""
                )
                pause_detail = (
                    "shared safety pause: sequential worker "
                    f"{failed_task_id!r} returned {failed_state!r}"
                    f"{scope_detail}; later "
                    "candidates were not dispatched in this invocation. "
                    "Classify or remediate the operational failure, then resume "
                    "the identical configured run."
                )
                for untouched in payloads[position + 1 :]:
                    blocked = {
                        "task_id": str(untouched["task_id"]),
                        "ok": False,
                        "state": "blocked",
                        "detail": pause_detail,
                        "usd": 0.0,
                    }
                    results.append(blocked)
                    print(f"  {blocked['task_id']}: blocked (not dispatched)")
                break
    elif max_workers > 1:
        try:
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_pipeline_worker, payload): payload
                    for payload in payloads
                }
                for future in as_completed(futures):
                    try:
                        result = future.result()
                    except BrokenProcessPool:
                        raise
                    except Exception as exc:  # noqa: BLE001 - one task, not corpus
                        payload = futures[future]
                        detail = (
                            f"worker exception: {type(exc).__name__}: {exc}"
                        )
                        interrupt_unfinished_attempt(
                            str(payload["task_id"]), detail
                        )
                        result = {
                            "task_id": str(payload["task_id"]),
                            "ok": False,
                            "state": "infrastructure",
                            "detail": detail,
                            "usd": 0.0,
                        }
                    results.append(result)
                    print(f"  {result['task_id']}: {result['state']}")
        except BrokenProcessPool as exc:
            # Keep outcomes already returned and account the unfinished roster.
            completed = {str(row["task_id"]) for row in results}
            for task_id in task_ids:
                if task_id not in completed:
                    detail = f"broken_process_pool: {exc}"
                    interrupt_unfinished_attempt(task_id, detail)
                    results.append(
                        {
                            "task_id": task_id,
                            "ok": False,
                            "state": "infrastructure",
                            "detail": detail,
                            "usd": 0.0,
                        }
                    )
    return sorted(results, key=lambda row: str(row["task_id"]))


_CONFIGURED_LIVE_ROLES: tuple[str, ...] = (
    "semantic_author",
    "ambiguity_critic",
    "population_adversary",
    "shortcut_attacker",
    "feasibility_reviewer",
    "independent_implementer",
    "independent_loader",
)


def _configured_canonical_runtime_problems(spec) -> tuple[str, ...]:
    """Every profile past draft reaches validate-t, whose canonical-reachability
    check needs the pinned dbt runtime; without it every task would spend its
    author, review and attack budget and then block at validate-t. Refuse at
    preflight instead (replay-only included: nothing is spent either way, but
    the run could not reach its milestone)."""

    if str(getattr(spec.profile, "value", spec.profile)) == "draft":
        return ()
    from elt_taskgen.training import canonical

    if canonical.dbt_runtime_available():
        return ()
    return (
        "the canonical-reachability check at validate-t needs the pinned dbt "
        f"runtime at {canonical.DBT_RUNTIME_ROOT}; provision it with: uv sync "
        "--project runtime-images/dbt-duckdb --locked",
    )


def _configured_live_provider_problems(args, spec) -> tuple[str, ...]:
    """Return static provider-route and credential-presence blockers before spend."""

    if str(getattr(spec.profile, "value", spec.profile)) == "draft" or bool(
        getattr(args, "replay_only", False)
    ):
        return ()

    # Live configured runs require an aggregate budget; per-task limits alone
    # would let candidate count silently increase total spend.
    if getattr(spec, "budget_total", None) is None:
        return (
            "live configured runs require an explicit finite shared "
            "budget_total (--budget-total USD or budget_total in --run-config)",
        )

    from elt_taskgen.review import providers as providers_mod

    roles = list(_CONFIGURED_LIVE_ROLES)
    if bool(getattr(args, "repair_proposer", False)):
        roles.append("repair_proposer")
    try:
        routing = providers_mod.load_role_routing(
            Path(spec.agents_config) if spec.agents_config else None
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return (f"cannot load provider routing: {type(exc).__name__}: {exc}",)

    problems = list(providers_mod.credential_problems(routing, roles))
    if problems:
        # A missing model/base URL is already a precise blocker; do not add a
        # cascade of misleading family/pricing errors about its empty value.
        return tuple(problems)

    from elt_taskgen.reference import independent as independent_mod

    for role_name in ("independent_implementer", "independent_loader"):
        route = routing.for_role(role_name)
        try:
            independent_mod._refuse_same_family(  # noqa: SLF001 - one policy seam
                routing,
                role_name,
                route.provider,
                route.model,
                where="configured preflight",
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            problems.append(str(exc))
    if problems:
        return tuple(problems)

    priced_routes: dict[tuple[str, str], list[str]] = {}
    for role_name in roles:
        route = routing.for_role(role_name)
        priced_routes.setdefault((route.provider, route.model), []).append(role_name)

    if bool(spec.empirical):
        from elt_taskgen.corpus import calibration as calibration_mod

        try:
            tiers = calibration_mod.load_calibration_roster(
                Path(spec.agents_config) if spec.agents_config else None
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return (
                f"cannot load empirical solver roster: {type(exc).__name__}: {exc}",
            )
        for tier in tiers:
            priced_routes.setdefault((tier.provider, tier.model), []).append(
                tier.role_name
            )

    for (provider_key, model), route_roles in sorted(priced_routes.items()):
        try:
            providers_mod.rate_card_for(
                provider_key,
                model,
                routing.provider_config.get(provider_key) or {},
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            problems.append(
                f"unpriced provider route for {', '.join(sorted(route_roles))}: {exc}"
            )

    # A configured run that names an exact admission capability can validate it
    # once at the coordinator boundary.  This prevents a shared stale council
    # surface from being rediscovered independently by many task workers after
    # local generation, and ensures no paid provider is constructed first.
    admission_reference = str(getattr(spec, "admission_reference", "") or "")
    if not problems and admission_reference:
        from elt_taskgen.review import metrology as metrology_mod

        fingerprint = metrology_mod.council_routing_fingerprint(routing)
        status = metrology_mod.admission_status(
            Path(getattr(args, "workspace", ".")).resolve(),
            routing_fingerprint=fingerprint,
            agents_config=(Path(spec.agents_config) if spec.agents_config else None),
            record_path=Path(admission_reference),
        )
        if not status.ok:
            problems.append(f"{metrology_mod.NOT_ADMITTED_MESSAGE}: {status.reason}")
    return tuple(problems)


def _canonical_configured_package_receipt(
    *,
    workspace: Path,
    run_id: str,
    task_id: str,
    candidate: Path | None = None,
) -> Path:
    """Return one run/task receipt only when its store ancestry is safe."""

    root = Path(workspace).resolve()
    expected = (
        root
        / "state"
        / "pipeline_runs"
        / run_id
        / "package_verifications"
        / f"{task_id}.json"
    )
    if candidate is not None and Path(candidate).absolute() != expected:
        raise CliUsageError(
            f"{task_id}: package verification receipt path is not the "
            "canonical run/task location"
        )
    current = root
    try:
        for part in expected.relative_to(root).parts[:-1]:
            current = current / part
            ancestor = current.lstat()
            if stat.S_ISLNK(ancestor.st_mode) or not stat.S_ISDIR(
                ancestor.st_mode
            ):
                raise ValueError(f"unsafe receipt ancestor: {current}")
    except (OSError, ValueError) as exc:
        raise CliUsageError(
            f"{task_id}: package verification receipt path is unsafe: {exc}"
        ) from None
    return expected


def _hydrate_configured_packages(
    *,
    workspace: Path,
    run_id: str,
    spec,
    task_ids: tuple[str, ...],
) -> tuple[dict[str, Path], dict[str, Path], tuple[str, ...]]:
    """Adopt only current, canonical package evidence without provider work.

    A configured resume can stop at the aggregate live-provider preflight. In
    that case, already completed package work must not disappear merely because
    no worker reached the packaging loop in this invocation. Resolve no paths
    from a prior report and scan no directory: for each exact runnable task we
    inspect only the configured package location and the canonical run/task
    receipt, then bind both to the current TaskIR and evaluator runtime through
    the persisted-receipt verifier.
    """

    from elt_taskgen.pipeline_readiness import (
        ReadinessProfile,
        ReadinessState,
        stage_readiness,
    )

    if spec.profile is not ReadinessProfile.PACKAGED:
        return {}, {}, ()

    export_root = Path(spec.export_dir).resolve()
    receipt_root = (
        Path(workspace).resolve()
        / "state"
        / "pipeline_runs"
        / run_id
        / "package_verifications"
    )
    packages: dict[str, Path] = {}
    receipts: dict[str, Path] = {}
    blockers: list[str] = []
    engine = _open_engine(workspace, max_repair_rounds=0)
    try:
        for task_id in task_ids:
            package_path = export_root / task_id
            receipt_path = receipt_root / f"{task_id}.json"
            package_present = package_path.exists() or package_path.is_symlink()
            receipt_present = receipt_path.exists() or receipt_path.is_symlink()
            if not package_present and not receipt_present:
                continue
            if not package_present:
                blockers.append(
                    f"{task_id}: persisted package adoption refused: canonical "
                    "package directory is missing while its receipt exists"
                )
                continue
            if not receipt_present:
                blockers.append(
                    f"{task_id}: persisted package adoption refused: canonical "
                    "package receipt is missing"
                )
                continue
            try:
                receipt_path = _canonical_configured_package_receipt(
                    workspace=workspace,
                    run_id=run_id,
                    task_id=task_id,
                    candidate=receipt_path,
                )
                task = engine.load_task(task_id)
                final = engine.final_verdict(task_id)
                if final != FINAL_ACCEPTED:
    # Package reuse still requires the engine's current final acceptance.
                    blockers.append(
                        f"{task_id}: persisted package adoption refused: "
                        "current independent EL/T acceptance is not available "
                        f"(engine verdict {final!r})"
                    )
                    continue
                transform_index = STAGE_ORDER.index(StageName.GATES_TRANSFORM)
                prerequisite_acceptances = tuple(
                    stage_readiness(engine, task, stage, spec=spec)
                    for stage in STAGE_ORDER[: transform_index + 1]
                )
                incomplete = next(
                    (
                        readiness
                        for readiness in prerequisite_acceptances
                        if readiness.state is not ReadinessState.PASS
                    ),
                    None,
                )
                if incomplete is not None:
                    reason = (
                        incomplete.reason
                        or incomplete.verdict
                        or incomplete.state.value
                    )
                    blockers.append(
                        f"{task_id}: persisted package adoption refused: "
                        "the complete prerequisite chain through "
                        "gates_transform is not current for this configured "
                        f"run ({incomplete.stage.value}: {reason})"
                    )
                    continue
                from elt_taskgen.export.package_verification import (
                    verify_persisted_package_receipt,
                )

                verified = verify_persisted_package_receipt(
                    receipt_path,
                    package_path=package_path,
                    expected_task_id=task.task_id,
                    expected_task_content_hash=task.content_hash(),
                )
            except (OSError, TypeError, ValueError, EngineError) as exc:
                blockers.append(
                    f"{task_id}: persisted package adoption refused: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            if not verified.ok:
                failures = verified.failures or (
                    "persisted package receipt verification returned not ok",
                )
                blockers.extend(
                    f"{task_id}: persisted package adoption refused: {failure}"
                    for failure in failures
                )
                continue
            packages[task_id] = package_path
            receipts[task_id] = receipt_path
    finally:
        engine.close()
    return packages, receipts, tuple(blockers)


def _adopted_package_results(
    task_ids: tuple[str, ...], packages: dict[str, Path]
) -> tuple[dict[str, Any], ...]:
    """Return ordered, zero-cost outcomes for current adopted packages.

    These rows make resume scheduling explicit in every configured report. The
    ``package_adopted`` marker also prevents the packaging loop from replacing
    an already verified immutable package merely because its result is green.
    """

    return tuple(
        {
            "task_id": task_id,
            "ok": True,
            "state": "packaged",
            "detail": "adopted current canonical package and verification receipt",
            "usd": 0.0,
            "package_adopted": True,
        }
        for task_id in task_ids
        if task_id in packages
    )


def _terminal_rejection_results(
    workspace: Path, task_ids: tuple[str, ...]
) -> tuple[dict[str, Any], ...]:
    """Preserve current rejections without scheduling or spending again.

    The engine's append-only verdict is authoritative. A later explicit,
    evidence-bound report supersession or a new TaskIR content revision makes
    ``final_verdict`` non-rejected and therefore naturally returns the task to
    the pending set; a package receipt alone never does.
    """

    engine = _open_engine(workspace, max_repair_rounds=0)
    try:
        return tuple(
            {
                "task_id": task_id,
                "ok": False,
                "state": FINAL_REJECTED,
                "detail": (
                    "preserved current terminal rejection; explicit "
                    "evidence-bound supersession or TaskIR revision is required"
                ),
                "usd": 0.0,
                "terminal_rejection_preserved": True,
            }
            for task_id in task_ids
            if engine.final_verdict(task_id) == FINAL_REJECTED
        )
    finally:
        engine.close()


def _write_configured_report(
    *,
    workspace: Path,
    run_id: str,
    spec,
    task_ids: tuple[str, ...],
    state: str,
    selected_manifest,
    results: tuple[dict, ...] = (),
    ingest_outcomes: tuple[Any, ...] = (),
    packages: dict[str, Path] | None = None,
    package_receipts: dict[str, Path] | None = None,
    blockers: tuple[str, ...] = (),
    migration_receipt=None,
    migration_receipt_path: Path | None = None,
) -> Path:
    from elt_taskgen.pipeline_readiness import (
        CandidateRunAction,
        make_run_report,
        write_run_report,
    )

    engine = _open_engine(workspace, max_repair_rounds=0)
    try:
        selected_path = (
            workspace
            / "state"
            / "candidate_manifests"
            / f"{selected_manifest.manifest_sha256()}.json"
        )
        budget = None
        if spec.budget_total is not None:
            from elt_taskgen.review.budget_ledger import initialize

            ledger = initialize(
                run_id,
                float(spec.budget_total),
                workspace,
                per_task_limit_usd=float(spec.budget_per_task),
            )
            if state != "RUNNING":
                # Every worker pool has joined before a terminal configured
                # report is written. Any reservation still marked in-flight
                # remains conservatively uncertain.
                ledger.mark_outstanding_uncertain()
            budget = ledger.snapshot()

        authoritative_selected = (
            migration_receipt.authoritative_selected_sha256
            if migration_receipt is not None
            else selected_manifest.manifest_sha256()
        )
        authoritative_path = (
            migration_receipt.authoritative_selected_path
            if migration_receipt is not None
            else str(selected_path)
        )
        outcome_by_id = {
            str(
                outcome.task_id
                if hasattr(outcome, "task_id")
                else outcome.get("task_id", "")
            ): outcome
            for outcome in ingest_outcomes
        }
        result_ids = {str(row.get("task_id") or "") for row in results}
        migrated_by_id = (
            {row.task_id: row for row in migration_receipt.tasks}
            if migration_receipt is not None
            else {}
        )
        task_actions = {}
        for task_id in task_ids:
            raw_outcome = outcome_by_id.get(task_id)
            outcome_state = str(
                raw_outcome.state
                if raw_outcome is not None and hasattr(raw_outcome, "state")
                else raw_outcome.get("state", "")
                if raw_outcome is not None
                else ""
            )
            registration_action = {
                "created": "created",
                "resumed": "resumed",
                "rederived": "resumed",
                "failed": "failed",
            }.get(outcome_state, "unknown")
            migrated = migrated_by_id.get(task_id)
            evidence_paths: list[str] = []
            if migrated is not None:
                evidence_paths.append(migrated.equivalence_path)
                if migrated.report_locally_revalidated_and_superseded:
                    evidence_paths.append(migration_receipt.revalidation_artifact_path)
            task_actions[task_id] = CandidateRunAction(
                selected=True,
                registration_action=registration_action,
                # Candidate generation begins at deterministic intake.  A
                # global intake blocker is still an attempted selected
                # candidate, so result-only rows count here as well.
                processing_attempted=(
                    raw_outcome is not None or task_id in result_ids
                ),
                adapter_taskir_rederived=bool(
                    migrated is not None and migrated.adapter_taskir_rederived
                ),
                source_provenance_equivalence_attested=bool(
                    migrated is not None
                    and migrated.source_provenance_equivalence_attested
                ),
                reports_locally_revalidated_and_superseded=(
                    int(migrated.report_locally_revalidated_and_superseded)
                    if migrated is not None
                    else 0
                ),
                evidence_paths=tuple(evidence_paths),
            )
        report = make_run_report(
            engine=engine,
            run_id=run_id,
            spec=spec,
            task_ids=task_ids,
            state=state,
            source_manifest_sha256=authoritative_selected,
            selected_manifest_path=authoritative_path,
            package_paths=packages,
            package_receipts=package_receipts,
            worker_results=results,
            repository=_repository_run_identity(Path(__file__).resolve().parents[2]),
            blockers=blockers,
            budget_snapshot=budget,
            task_actions=task_actions,
        )
        if migration_receipt is not None:
            if migration_receipt_path is None:
                raise CliUsageError("implementation revalidation receipt path is missing")
            report = report.model_copy(
                update={
                    "active_source_manifest_sha256": (
                        migration_receipt.reproduced_selected_sha256
                    ),
                    "active_selected_manifest_path": (
                        migration_receipt.reproduced_selected_path
                    ),
                    "generator_revalidation_receipt": str(migration_receipt_path),
                    "identity_migration_sha256": migration_receipt.evidence_digest(),
                }
            )
        return write_run_report(report, workspace=workspace)
    finally:
        engine.close()


@dataclass(frozen=True)
class _ManifestAdapterTransition:
    """One task's exact adapter/source-entry movement between two manifests."""

    task_id: str
    origin: Origin
    authoritative_version: str
    reproduced_version: str
    authoritative_digest: str
    reproduced_digest: str
    authoritative_source_entry_sha256: str
    reproduced_source_entry_sha256: str


@dataclass(frozen=True)
class _PendingConfiguredMigration:
    prior_report: Any
    prior_report_bytes: bytes
    authoritative_selected: Any
    reproduced_selected: Any
    authoritative_pool_sha256: str
    reproduced_pool_sha256: str
    adapter_transitions: tuple[Any, ...] = ()
    authorization: Any | None = None
    authorization_path: Path | None = None
    committed_receipt: Any | None = None
    committed_receipt_path: Path | None = None


class _GeneratorRevalidationTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    intake_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    equivalence_path: str
    equivalence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resumed_existing: Literal[True] = True
    adapter_taskir_rederived: Literal[True] = True
    source_provenance_equivalence_attested: Literal[True] = True
    report_locally_revalidated_and_superseded: bool = False


class _GeneratorEquivalenceCommit(BaseModel):
    """All-roster commit point; no report is suppressed before this exists."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["generator-equivalence-commit-v1"] = (
        "generator-equivalence-commit-v1"
    )
    run_id: str
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_path: str
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordered_task_ids: tuple[str, ...]
    tasks: tuple[_GeneratorRevalidationTask, ...]

    @model_validator(mode="after")
    def _complete_roster(self) -> "_GeneratorEquivalenceCommit":
        if tuple(task.task_id for task in self.tasks) != self.ordered_task_ids:
            raise ValueError("generator equivalence commit roster is not exact")
        if not all(task.source_provenance_equivalence_attested for task in self.tasks):
            raise ValueError("generator equivalence commit has an unattested member")
        return self

    def deterministic_bytes(self) -> bytes:
        return (readable_json(self.model_dump(mode="json")) + "\n").encode("utf-8")

    def evidence_digest(self) -> str:
        return hashlib.sha256(self.deterministic_bytes()).hexdigest()


class _ImplementationEquivalenceCommit(_GeneratorEquivalenceCommit):
    """V2 commit for a generator replay with exact adapter-pin movements.

    The historical v1 model above is intentionally unchanged so its persisted
    deterministic bytes continue to parse and hash exactly as written.
    Adapter transitions live in the content-addressed v2 authorization and
    per-task provenance attestations bound by this commit.
    """

    schema_version: Literal["implementation-equivalence-commit-v2"] = (
        "implementation-equivalence-commit-v2"
    )


class _GeneratorRevalidationReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["configured-generator-revalidation-v1"] = (
        "configured-generator-revalidation-v1"
    )
    run_id: str
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_selected_path: str
    authoritative_pool_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_selected_path: str
    reproduced_pool_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_readiness_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordered_task_ids: tuple[str, ...]
    tasks: tuple[_GeneratorRevalidationTask, ...]
    revalidation_artifact_path: str
    revalidation_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_path: str
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    equivalence_commit_path: str
    equivalence_commit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    superseded_report_ids: tuple[int, ...]
    replacement_report_id: int = Field(default=0, ge=0)
    replacement_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resumed_existing: int
    adapter_taskirs_rederived: int
    source_provenance_equivalence_attested: int
    reports_locally_revalidated_and_superseded: int
    budget_snapshot_before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_snapshot_after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_committed_usd: float
    budget_reserved_usd: float
    budget_uncertain_usd: float

    @model_validator(mode="after")
    def _exact_roster(self) -> "_GeneratorRevalidationReceipt":
        if tuple(task.task_id for task in self.tasks) != self.ordered_task_ids:
            raise ValueError("generator revalidation task order differs from roster")
        if self.resumed_existing != len(self.tasks):
            raise ValueError("resumed_existing does not equal revalidated roster")
        if self.adapter_taskirs_rederived != sum(
            task.adapter_taskir_rederived for task in self.tasks
        ):
            raise ValueError("adapter rederivation count differs from task actions")
        if self.source_provenance_equivalence_attested != sum(
            task.source_provenance_equivalence_attested for task in self.tasks
        ):
            raise ValueError("provenance revalidation count differs from task actions")
        if self.reports_locally_revalidated_and_superseded != sum(
            task.report_locally_revalidated_and_superseded for task in self.tasks
        ):
            raise ValueError("report revalidation count differs from task actions")
        if self.budget_snapshot_before_sha256 != self.budget_snapshot_after_sha256:
            raise ValueError("generator migration changed the durable budget ledger")
        if self.reports_locally_revalidated_and_superseded:
            if (
                len(self.superseded_report_ids) != 1
                or self.replacement_report_id <= self.superseded_report_ids[0]
                or self.replacement_payload_sha256 == "0" * 64
            ):
                raise ValueError("report supersession receipt binding is incomplete")
        elif (
            self.superseded_report_ids
            or self.replacement_report_id
            or self.replacement_payload_sha256 != "0" * 64
        ):
            raise ValueError("receipt claims replacement evidence without revalidation")
        return self

    def deterministic_bytes(self) -> bytes:
        return (readable_json(self.model_dump(mode="json")) + "\n").encode("utf-8")

    def evidence_digest(self) -> str:
        return hashlib.sha256(self.deterministic_bytes()).hexdigest()


class _ImplementationRevalidationReceipt(_GeneratorRevalidationReceipt):
    """V2 configured-run receipt for implementation equivalence.

    Inheriting the established fields keeps report consumers uniform.  The
    distinct schema version prevents a v2 adapter transition from being
    guessed into the narrower historical generator-only contract.
    """

    schema_version: Literal["configured-implementation-revalidation-v2"] = (
        "configured-implementation-revalidation-v2"
    )


_EquivalenceCommit = _GeneratorEquivalenceCommit | _ImplementationEquivalenceCommit
_RevalidationReceipt = _GeneratorRevalidationReceipt | _ImplementationRevalidationReceipt


def _is_implementation_authorization(authorization: Any) -> bool:
    return (
        getattr(authorization, "schema_version", "")
        == "implementation-equivalence-authorization-v2"
    )


def _equivalence_evidence_filename(binding: Any, authorization: Any) -> str:
    suffix = (
        "implementation-equivalence.json"
        if _is_implementation_authorization(authorization)
        else "generator-equivalence.json"
    )
    return (
        f"{binding.intake_content_hash}."
        f"{binding.reproduced_provenance_sha256}.{suffix}"
    )


def _publish_immutable_run_document(
    destination: Path, payload: bytes, *, label: str
) -> Path:
    """Publish exact run evidence once, with no replace semantics."""

    destination = Path(destination).absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    current = destination.parent
    while True:
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise CliUsageError(f"{label} store has an unsafe path: {current}")
        if current == current.parent:
            break
        # The run evidence parents have just been checked; walking to the root
        # also prevents an ancestor swap from escaping the expected directory.
        current = current.parent
    try:
        existing = destination.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
            raise CliUsageError(f"{label} destination is unsafe: {destination}")
        if destination.read_bytes() != payload:
            raise CliUsageError(f"{label} destination contains different bytes")
        return destination
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.is_symlink() or destination.read_bytes() != payload:
                raise CliUsageError(f"concurrent {label} publisher wrote different bytes")
        os.chmod(destination, destination.stat().st_mode & 0o555)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _workspace_relative(workspace: Path, path: Path, *, label: str) -> str:
    try:
        return Path(path).absolute().relative_to(Path(workspace).absolute()).as_posix()
    except ValueError:
        raise CliUsageError(f"{label} is outside the configured workspace") from None


def _bound_workspace_path(workspace: Path, relative: str, *, label: str) -> Path:
    rel = PurePosixPath(relative)
    if (
        rel.is_absolute()
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in rel.parts)
    ):
        raise CliUsageError(f"{label} path is unsafe")
    return Path(workspace).absolute().joinpath(*rel.parts)


def _logical_budget_snapshot(*, workspace: Path, run_id: str, spec) -> dict[str, Any]:
    """Timestamp-free exact ledger state used to prove provider-free migration."""

    if spec.budget_total is None:
        return {"configured": False, "run_id": run_id, "reservations": []}
    from elt_taskgen.review.budget_ledger import initialize

    snapshot = initialize(
        run_id,
        float(spec.budget_total),
        workspace,
        per_task_limit_usd=float(spec.budget_per_task),
    ).snapshot()
    return {
        "configured": True,
        "run_id": snapshot.run_id,
        "total_limit_usd": snapshot.total_limit_usd,
        "per_task_limit_usd": snapshot.per_task_limit_usd,
        "committed_usd": snapshot.committed_usd,
        "reserved_usd": snapshot.reserved_usd,
        "uncertain_usd": snapshot.uncertain_usd,
        "reservation_count": snapshot.reservation_count,
        "committed_count": snapshot.committed_count,
        "reserved_count": snapshot.reserved_count,
        "uncertain_count": snapshot.uncertain_count,
        "released_count": snapshot.released_count,
        "reservations": [
            {
                "call_id": row.call_id,
                "task_id": row.task_id,
                "role": row.role,
                "estimated_usd": row.estimated_usd,
                "actual_usd": row.actual_usd,
                "state": row.state.value,
            }
            for row in sorted(snapshot.reservations, key=lambda item: item.call_id)
        ],
    }


def _prepare_generator_authorization(
    *,
    workspace: Path,
    run_id: str,
    spec,
    pool_path: Path,
    pending: _PendingConfiguredMigration,
    report_revalidation_request=None,
    report_revalidation_request_path: Path | None = None,
):
    """Rebuild the exact roster off-workspace, then seal its transition."""

    from elt_taskgen.ingest_manifest import (
        GeneratorEquivalenceAuthorization,
        GeneratorEquivalenceTaskBinding,
        ImplementationEquivalenceAuthorization,
        ImplementationEquivalenceTaskBinding,
        prepare_five_sources,
    )
    from elt_taskgen.provenance import (
        ImplementationAdapterTransition,
        generator_equivalence_problem,
        implementation_equivalence_problem,
        lineage_root_hash,
        load_current,
    )

    transition_problem, manifest_transitions = _selected_implementation_transition(
        pending.authoritative_selected, pending.reproduced_selected
    )
    if transition_problem:
        raise CliUsageError(
            "implementation revalidation refused: " + transition_problem
        )
    if manifest_transitions != pending.adapter_transitions:
        raise CliUsageError(
            "implementation transition changed after run-identity preflight"
        )
    implementation_v2 = bool(manifest_transitions)
    if implementation_v2 and (
        report_revalidation_request is not None
        or report_revalidation_request_path is not None
    ):
        raise CliUsageError(
            "report revalidation requests are not supported while adapter "
            "implementation pins move"
        )
    transition_by_task = {row.task_id: row for row in manifest_transitions}

    with tempfile.TemporaryDirectory(prefix="elt-taskgen-generator-revalidation-") as temp:
        prepared = prepare_five_sources(
            pending.reproduced_selected,
            manifest_path=pool_path,
            workspace=Path(temp) / "empty-workspace",
            retain_task_local_fatals=True,
        )
    expected_ids = tuple(
        entry.expected_task_id
        for entry in pending.reproduced_selected.ordered_entries()
    )
    if tuple(item.task.task_id for item in prepared) != expected_ids:
        raise CliUsageError("implementation revalidation rebuilt a different task roster")

    engine = _open_engine(workspace, max_repair_rounds=0)
    bindings = []
    try:
        for item in prepared:
            current_task = engine.load_task(item.task.task_id)
            if lineage_root_hash(current_task) != item.task.content_hash():
                raise CliUsageError(
                    f"implementation revalidation changed intake TaskIR for "
                    f"{item.task.task_id!r}"
                )
            authoritative = load_current(
                engine.task_dir(item.task.task_id), task=current_task, required=True
            )
            manifest_transition = transition_by_task.get(item.task.task_id)
            adapter_transition = (
                ImplementationAdapterTransition(
                    pool=manifest_transition.origin,
                    adapter_name=authoritative.source.adapter_name,
                    authoritative_version=(
                        manifest_transition.authoritative_version
                    ),
                    reproduced_version=manifest_transition.reproduced_version,
                    authoritative_digest=(
                        manifest_transition.authoritative_digest
                    ),
                    reproduced_digest=manifest_transition.reproduced_digest,
                    authoritative_source_entry_sha256=(
                        manifest_transition.authoritative_source_entry_sha256
                    ),
                    reproduced_source_entry_sha256=(
                        manifest_transition.reproduced_source_entry_sha256
                    ),
                )
                if manifest_transition is not None
                else None
            )
            problem = (
                implementation_equivalence_problem(
                    authoritative,
                    item.provenance,
                    adapter_transition=adapter_transition,
                )
                if implementation_v2
                else generator_equivalence_problem(authoritative, item.provenance)
            )
            if problem:
                raise CliUsageError(
                    f"implementation revalidation changed "
                    f"{item.task.task_id!r}: {problem}"
                )
            if implementation_v2:
                old_entry_digest = authoritative.source.selection_inputs[
                    "ingest_manifest"
                ].digest
                bindings.append(
                    ImplementationEquivalenceTaskBinding(
                        task_id=item.task.task_id,
                        origin=item.origin,
                        authoritative_source_entry_sha256=old_entry_digest,
                        reproduced_source_entry_sha256=item.source_entry_sha256,
                        intake_content_hash=item.task.content_hash(),
                        authoritative_provenance_sha256=(
                            authoritative.evidence_digest()
                        ),
                        reproduced_provenance_sha256=(
                            item.provenance.evidence_digest()
                        ),
                        adapter_transition=adapter_transition,
                    )
                )
            else:
                bindings.append(
                    GeneratorEquivalenceTaskBinding(
                        task_id=item.task.task_id,
                        source_entry_sha256=item.source_entry_sha256,
                        intake_content_hash=item.task.content_hash(),
                        authoritative_provenance_sha256=(
                            authoritative.evidence_digest()
                        ),
                        reproduced_provenance_sha256=(
                            item.provenance.evidence_digest()
                        ),
                    )
                )
    finally:
        engine.close()

    budget_snapshot = _logical_budget_snapshot(
        workspace=workspace, run_id=run_id, spec=spec
    )
    authorization_type = (
        ImplementationEquivalenceAuthorization
        if implementation_v2
        else GeneratorEquivalenceAuthorization
    )
    authorization_fields: dict[str, Any] = dict(
        run_id=run_id,
        config_sha256=spec.fingerprint(),
        prior_readiness_sha256=hashlib.sha256(
            pending.prior_report_bytes
        ).hexdigest(),
        authoritative_selected_sha256=(
            pending.authoritative_selected.manifest_sha256()
        ),
        reproduced_selected_sha256=pending.reproduced_selected.manifest_sha256(),
        authoritative_pool_sha256=pending.authoritative_pool_sha256,
        reproduced_pool_sha256=pending.reproduced_pool_sha256,
        authoritative_generator_sha256=pending.authoritative_selected.generator.sha256,
        reproduced_generator_sha256=pending.reproduced_selected.generator.sha256,
        ordered_task_ids=expected_ids,
        tasks=tuple(bindings),
        budget_snapshot_sha256=sha256_hex(canonical_json(budget_snapshot)),
        budget_snapshot=budget_snapshot,
    )
    if not implementation_v2:
        authorization_fields.update(
            report_revalidation_request_path=(
                _workspace_relative(
                    workspace,
                    report_revalidation_request_path,
                    label="report revalidation request",
                )
                if report_revalidation_request_path is not None
                else ""
            ),
            report_revalidation_request_sha256=(
                report_revalidation_request.evidence_digest()
                if report_revalidation_request is not None
                else ""
            ),
            report_revalidation_request=report_revalidation_request,
        )
    authorization = authorization_type(**authorization_fields)
    destination = (
        workspace
        / "state"
        / "pipeline_runs"
        / run_id
        / "identity_migrations"
        / "authorizations"
        / f"{authorization.evidence_digest()}.json"
    )
    path = _publish_immutable_run_document(
        destination,
        authorization.deterministic_bytes(),
        label="implementation equivalence authorization",
    )
    return authorization, path


def _load_report_revalidation_request(
    path: Path | None,
    *,
    workspace: Path,
    run_id: str,
    spec,
    pending: _PendingConfiguredMigration,
):
    if path is None:
        return None, None
    from elt_taskgen.ingest_manifest import GeneratorReportRevalidationRequest
    from elt_taskgen.pipeline_readiness import _read_bounded_regular_bytes

    source = Path(path).absolute()
    expected_root = (
        workspace
        / "state"
        / "pipeline_runs"
        / run_id
        / "identity_migrations"
        / "requests"
    )
    try:
        source.relative_to(expected_root)
    except ValueError:
        raise CliUsageError(
            "report revalidation request must be in this run's canonical request store"
        ) from None
    try:
        payload = _read_bounded_regular_bytes(
            source,
            label="report revalidation request",
            max_bytes=16 * 1024 * 1024,
        )
        request = GeneratorReportRevalidationRequest.model_validate_json(payload)
    except ValueError as exc:
        raise CliUsageError(f"report revalidation request is invalid: {exc}") from None
    if payload != request.deterministic_bytes() or source.name != f"{request.evidence_digest()}.json":
        raise CliUsageError("report revalidation request is not content-addressed")
    expected = {
        "run_id": run_id,
        "config_sha256": spec.fingerprint(),
        "authoritative_selected_sha256": pending.authoritative_selected.manifest_sha256(),
        "reproduced_selected_sha256": pending.reproduced_selected.manifest_sha256(),
        "authoritative_generator_sha256": pending.authoritative_selected.generator.sha256,
        "reproduced_generator_sha256": pending.reproduced_selected.generator.sha256,
    }
    observed = request.model_dump(mode="json")
    mismatches = [key for key, value in expected.items() if observed[key] != value]
    if mismatches:
        raise CliUsageError(
            "report revalidation request differs from this migration: "
            + ", ".join(mismatches)
        )
    return request, source


def _iter_json_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_json_strings(item)


def _regular_json_bytes(path: Path, *, label: str, max_bytes: int = 32 * 1024 * 1024):
    from elt_taskgen.pipeline_readiness import _read_bounded_regular_bytes

    payload = _read_bounded_regular_bytes(path, label=label, max_bytes=max_bytes)
    try:
        return payload, json.loads(payload)
    except (UnicodeError, ValueError) as exc:
        raise CliUsageError(f"{label} is not valid JSON: {exc}") from None


def _build_defect_revalidation(
    *,
    workspace: Path,
    run_id: str,
    spec,
    pending: _PendingConfiguredMigration,
    authorization,
    authorization_path: Path,
    equivalence_commit: _GeneratorEquivalenceCommit,
    equivalence_commit_path: Path,
) -> tuple[ReportRevalidationEvidence | None, Path | None, ReportSupersession | None]:
    """Find and locally revalidate the exact obsolete author false positive."""

    from elt_taskgen.review import declarative_prose, prose_fidelity

    request = authorization.report_revalidation_request
    if request is None:
        return None, None, None
    request_path = _bound_workspace_path(
        workspace,
        authorization.report_revalidation_request_path,
        label="report revalidation request",
    )
    request_bytes, _ = _regular_json_bytes(
        request_path, label="report revalidation request"
    )
    if (
        hashlib.sha256(request_bytes).hexdigest()
        != authorization.report_revalidation_request_sha256
        or request_bytes != request.deterministic_bytes()
    ):
        raise CliUsageError("authorized report revalidation request changed")
    if request.task_id not in authorization.ordered_task_ids:
        raise CliUsageError("report revalidation target is outside the exact roster")

    engine = _open_engine(workspace, max_repair_rounds=0)
    candidates = []
    try:
        for task_id in (request.task_id,):
            task = engine.load_task(task_id)
            fatal = next(
                (
                    row
                    for row in engine.report_history(task_id, StageName.AUTHOR.value)
                    if row.verdict == VERDICT_FATAL
                    and row.content_hash == task.content_hash()
                ),
                None,
            )
            if fatal is None:
                continue
            cause = next(
                (
                    row
                    for row in engine.report_history(task_id, StageName.AUTHOR.value)
                    if row.verdict == VERDICT_FAIL
                    and row.content_hash == task.content_hash()
                    and row.id < fatal.id
                ),
                None,
            )
            if cause is None:
                continue
            try:
                cause_payload = json.loads(cause.payload_json)
                cause_data = cause_payload.get("data") or {}
            except (TypeError, ValueError):
                continue
            error = str(cause_payload.get("error") or "")
            if not (
                cause_data.get("gate") == prose_fidelity.GATE_NAME
                and cause_data.get(AUTHOR_SOURCE_KEY) == AUTHOR_SOURCE_REVISED
                and "function-call" in error
                and "count" in error.casefold()
            ):
                continue
            candidates.append((task, cause, fatal, cause_data))
        if len(candidates) != 1:
            raise CliUsageError("requested obsolete author fatal did not match exactly")
        task, cause, fatal, cause_data = candidates[0]
        prompt = task.solver_prompt or ""
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if not prompt or cause_data.get(AUTHOR_PROSE_SHA_KEY) != prompt_sha:
            raise CliUsageError("obsolete author report is not bound to stored prose")
        if prompt_sha != request.prose_sha256:
            raise CliUsageError("report revalidation request names other prose")
        structural = _structural_failure_detail(task)
        attack = _attack_matrix_failure_detail(engine, task)
        findings = tuple(prose_fidelity.check_prose_fidelity(task))
        if structural is not None or attack is not None or findings:
            raise CliUsageError(
                "fixed author validator did not reproduce a fully green local result"
            )

        session_sha = str(cause_data.get(AUTHOR_SESSION_SHA_KEY) or "")
        if not re.fullmatch(r"[0-9a-f]{64}", session_sha):
            raise CliUsageError("obsolete author report has no valid session digest")
        if session_sha != request.author_session_sha256:
            raise CliUsageError("report revalidation request names another session")
        if (
            error != request.expected_old_error
            or hashlib.sha256(error.encode("utf-8")).hexdigest()
            != request.expected_old_error_sha256
        ):
            raise CliUsageError("obsolete validator findings differ from request")
        session_path = _bound_workspace_path(
            workspace,
            request.session_summary_path,
            label="author session summary",
        )
        session_bytes, session_document = _regular_json_bytes(
            session_path, label="author session summary"
        )
        if hashlib.sha256(session_bytes).hexdigest() != request.session_summary_sha256:
            raise CliUsageError("author session summary differs from request")

        transcript_records = []
        for relative, expected_sha in zip(
            request.transcript_paths, request.transcript_sha256s, strict=True
        ):
            transcript_path = _bound_workspace_path(
                workspace, relative, label="semantic-author transcript"
            )
            transcript_bytes, transcript_document = _regular_json_bytes(
                transcript_path, label="semantic-author transcript"
            )
            if hashlib.sha256(transcript_bytes).hexdigest() != expected_sha:
                raise CliUsageError("semantic-author transcript differs from request")
            transcript_records.append((transcript_path, transcript_bytes, transcript_document))
        transcript_path, transcript_bytes, transcript_document = transcript_records[-1]
        session_index_path = _bound_workspace_path(
            workspace, request.session_index_path, label="author session index"
        )
        session_index_bytes, session_index = _regular_json_bytes(
            session_index_path, label="author session index"
        )
        if hashlib.sha256(session_index_bytes).hexdigest() != request.session_index_sha256:
            raise CliUsageError("author session index differs from request")
        trajectory_path = _bound_workspace_path(
            workspace, request.trajectory_path, label="author trajectory"
        )
        trajectory_bytes, trajectory = _regular_json_bytes(
            trajectory_path, label="author trajectory"
        )
        if hashlib.sha256(trajectory_bytes).hexdigest() != request.trajectory_sha256:
            raise CliUsageError("author trajectory differs from request")
        try:
            record_bindings = validate_author_revalidation_records(
                task_id=task.task_id,
                intake_content_hash=authorization.binding_for(
                    task.task_id
                ).intake_content_hash,
                prompt=prompt,
                session_sha256=session_sha,
                transcript_records=tuple(
                    (
                        _workspace_relative(
                            workspace, path, label="author transcript"
                        ),
                        document,
                    )
                    for path, _payload, document in transcript_records
                ),
                session_index_path=request.session_index_path,
                session_index=session_index,
                trajectory_path=request.trajectory_path,
                trajectory=trajectory,
                session_summary=session_document,
            )
        except (TypeError, ValueError) as exc:
            raise CliUsageError(str(exc)) from None
        if (
            session_document.get("drafts")
            != int(cause_data.get(AUTHOR_DRAFTS_KEY) or 0)
            or session_document.get("revisions")
            != int(cause_data.get(AUTHOR_REVISIONS_KEY) or 0)
            or session_document.get("terminal")
            != str(cause_data.get(AUTHOR_SESSION_TERMINAL_KEY) or "")
        ):
            raise CliUsageError("author session summary counters differ from report")

        cause_sha = hashlib.sha256(cause.payload_json.encode("utf-8")).hexdigest()
        fatal_sha = hashlib.sha256(fatal.payload_json.encode("utf-8")).hexdigest()
        if (
            task.content_hash() != request.task_content_hash
            or cause.id != request.cause_report_id
            or cause_sha != request.cause_payload_sha256
            or fatal.id != request.target_report_id
            or fatal_sha != request.target_payload_sha256
        ):
            raise CliUsageError("requested author report identity or payload changed")
        authorization_sha = authorization.evidence_digest()
        reason = request.reason
        migration_id = sha256_hex(
            canonical_json(
                {
                    "authorization": authorization_sha,
                    "task_id": task.task_id,
                    "content_hash": task.content_hash(),
                    "cause_report_id": cause.id,
                    "cause_payload_sha256": cause_sha,
                    "target_report_id": fatal.id,
                    "target_payload_sha256": fatal_sha,
                    "reason": reason,
                }
            )
        )
        replacement_detail = (
            "persisted authored prose revalidated locally under the corrected "
            "declarative-prose validator; fidelity gate green"
        )
        replacement_data = {
            key: str(value)
            for key, value in cause_data.items()
            if isinstance(value, (str, int, float, bool))
        }
        replacement_data.update(
            {
                AUTHOR_PROSE_SHA_KEY: prompt_sha,
                AUTHOR_SOURCE_KEY: str(cause_data[AUTHOR_SOURCE_KEY]),
                AUTHOR_SESSION_SHA_KEY: session_sha,
                "transcript_path": _workspace_relative(
                    workspace, transcript_path, label="author transcript"
                ),
                "transcript_sha256": hashlib.sha256(transcript_bytes).hexdigest(),
                "transcript_manifest": canonical_json(
                    [
                        {
                            "path": _workspace_relative(
                                workspace, path, label="author transcript"
                            ),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                        for path, payload, _ in transcript_records
                    ]
                ),
                "session_summary_path": _workspace_relative(
                    workspace, session_path, label="author session summary"
                ),
                "session_summary_sha256": hashlib.sha256(session_bytes).hexdigest(),
                "session_index_path": request.session_index_path,
                "session_index_sha256": request.session_index_sha256,
                "trajectory_path": request.trajectory_path,
                "trajectory_sha256": request.trajectory_sha256,
                "provider": record_bindings["provider"],
                "model": record_bindings["model"],
                "served_models": record_bindings["served_models"],
                "route": record_bindings["route"],
                "admission": record_bindings["admission"],
                "behavior_sha256": record_bindings["behavior_sha256"],
                "tools_sha256": record_bindings["tools_sha256"],
                "policy_sha256": record_bindings["policy_sha256"],
                "revalidation_reason": reason,
            }
        )
        authorization_rel = _workspace_relative(
            workspace, authorization_path, label="generator authorization"
        )
        validator_identity = {
            "generator_sha256": pending.reproduced_selected.generator.sha256,
            "prose_fidelity_module_sha256": hashlib.sha256(
                Path(prose_fidelity.__file__).read_bytes()
            ).hexdigest(),
            "declarative_prose_module_sha256": hashlib.sha256(
                Path(declarative_prose.__file__).read_bytes()
            ).hexdigest(),
        }
        original_evidence = {
            "prose_sha256": prompt_sha,
            "session_sha256": session_sha,
            "source": str(cause_data[AUTHOR_SOURCE_KEY]),
            "drafts": str(cause_data.get(AUTHOR_DRAFTS_KEY) or ""),
            "revisions": str(cause_data.get(AUTHOR_REVISIONS_KEY) or ""),
            "session_terminal": str(
                cause_data.get(AUTHOR_SESSION_TERMINAL_KEY) or ""
            ),
            "transcript_path": replacement_data["transcript_path"],
            "transcript_sha256": replacement_data["transcript_sha256"],
            "session_summary_path": replacement_data["session_summary_path"],
            "session_summary_sha256": replacement_data["session_summary_sha256"],
            "transcript_document_sha256": sha256_hex(
                canonical_json(transcript_document)
            ),
            "transcript_manifest": replacement_data["transcript_manifest"],
            "session_index_path": request.session_index_path,
            "session_index_sha256": request.session_index_sha256,
            "trajectory_path": request.trajectory_path,
            "trajectory_sha256": request.trajectory_sha256,
            "request_path": authorization.report_revalidation_request_path,
            "request_sha256": authorization.report_revalidation_request_sha256,
            "old_error": error,
            "old_error_sha256": request.expected_old_error_sha256,
        }
        task_ir_bytes = (engine.task_dir(task.task_id) / "task_ir.json").read_bytes()
        task_ir_sha = hashlib.sha256(task_ir_bytes).hexdigest()
        archived_task_ir = _publish_immutable_run_document(
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "identity_migrations"
            / "task_ir_snapshots"
            / f"{task_ir_sha}.json",
            task_ir_bytes,
            label="pre-revalidation TaskIR snapshot",
        )
        artifact = ReportRevalidationEvidence(
            migration_id=migration_id,
            authorization_path=authorization_rel,
            authorization_sha256=authorization_sha,
            equivalence_commit_path=_workspace_relative(
                workspace,
                equivalence_commit_path,
                label="generator equivalence commit",
            ),
            equivalence_commit_sha256=equivalence_commit.evidence_digest(),
            run_id=run_id,
            config_sha256=spec.fingerprint(),
            authoritative_selected_sha256=authorization.authoritative_selected_sha256,
            reproduced_selected_sha256=authorization.reproduced_selected_sha256,
            authoritative_generator_sha256=authorization.authoritative_generator_sha256,
            reproduced_generator_sha256=authorization.reproduced_generator_sha256,
            task_id=task.task_id,
            intake_content_hash=authorization.binding_for(task.task_id).intake_content_hash,
            task_content_hash=task.content_hash(),
            archived_task_ir_path=_workspace_relative(
                workspace, archived_task_ir, label="pre-revalidation TaskIR snapshot"
            ),
            archived_task_ir_sha256=task_ir_sha,
            stage=StageName.AUTHOR.value,
            cause_report_id=cause.id,
            cause_payload_sha256=cause_sha,
            target_report_id=fatal.id,
            target_payload_sha256=fatal_sha,
            validator_identity=validator_identity,
            original_evidence=original_evidence,
            deterministic_preconditions=(
                "structural-completeness-green",
                "required-attack-matrix-green",
                "prose-fidelity-zero-findings",
                "persisted-author-session-bound",
                "persisted-author-transcript-exact-prompt-bound",
            ),
            findings=findings,
            replacement_detail=replacement_detail,
            replacement_data=replacement_data,
        )
        artifact_sha = hashlib.sha256(artifact.deterministic_bytes()).hexdigest()
        artifact_path = _publish_immutable_run_document(
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "identity_migrations"
            / "revalidations"
            / f"{artifact_sha}.json",
            artifact.deterministic_bytes(),
            label="local report revalidation",
        )
        supersession = ReportSupersession(
            migration_id=migration_id,
            reason=reason,
            stage=StageName.AUTHOR.value,
            content_hash=task.content_hash(),
            revalidation_path=_workspace_relative(
                workspace, artifact_path, label="local report revalidation"
            ),
            revalidation_sha256=artifact_sha,
            cause_report_id=cause.id,
            cause_payload_sha256=cause_sha,
            target_report_id=fatal.id,
            target_payload_sha256=fatal_sha,
            replacement_detail=replacement_detail,
            replacement_data=tuple(sorted(replacement_data.items())),
        )
        return artifact, artifact_path, supersession
    finally:
        engine.close()


def _commit_generator_equivalences(
    *, workspace: Path, run_id: str, authorization, authorization_path: Path
) -> tuple[_EquivalenceCommit, Path]:
    """Verify every roster attestation and publish the all-or-nothing commit."""

    from elt_taskgen.provenance import (
        GeneratorEquivalenceAttestation,
        ImplementationEquivalenceAttestation,
        load_current,
    )

    implementation_v2 = _is_implementation_authorization(authorization)
    attestation_type = (
        ImplementationEquivalenceAttestation
        if implementation_v2
        else GeneratorEquivalenceAttestation
    )
    engine = _open_engine(workspace, max_repair_rounds=0)
    rows = []
    try:
        for binding in authorization.tasks:
            path = (
                workspace
                / "tasks"
                / binding.task_id
                / "ingest_provenance"
                / _equivalence_evidence_filename(binding, authorization)
            )
            payload, _document = _regular_json_bytes(
                path, label="implementation provenance equivalence"
            )
            try:
                attestation = attestation_type.model_validate_json(payload)
            except ValueError as exc:
                raise CliUsageError(
                    f"implementation provenance equivalence is invalid: {exc}"
                ) from None
            current_task = engine.load_task(binding.task_id)
            authoritative = load_current(
                engine.task_dir(binding.task_id),
                task=current_task,
                required=True,
            )
            if (
                payload != attestation.deterministic_bytes()
                or attestation.task_id != binding.task_id
                or attestation.task_content_hash != binding.intake_content_hash
                or attestation.authoritative_evidence_digest
                != binding.authoritative_provenance_sha256
                or authoritative.evidence_digest()
                != binding.authoritative_provenance_sha256
                or attestation.reproduced_provenance.evidence_digest()
                != binding.reproduced_provenance_sha256
                or attestation.reproduced_provenance.source.selection_inputs[
                    "generator_code"
                ].digest
                != authorization.reproduced_generator_sha256
                or (
                    implementation_v2
                    and attestation.adapter_transition
                    != binding.adapter_transition
                )
            ):
                raise CliUsageError(
                    "implementation provenance equivalence differs from its exact "
                    f"authorization for {binding.task_id!r}"
                )
            rows.append(
                _GeneratorRevalidationTask(
                    task_id=binding.task_id,
                    intake_content_hash=binding.intake_content_hash,
                    authoritative_provenance_sha256=(
                        binding.authoritative_provenance_sha256
                    ),
                    reproduced_provenance_sha256=(
                        binding.reproduced_provenance_sha256
                    ),
                    equivalence_path=_workspace_relative(
                        workspace,
                        path,
                        label="implementation provenance equivalence",
                    ),
                    equivalence_sha256=hashlib.sha256(payload).hexdigest(),
                ),
            )
    finally:
        engine.close()
    commit_type = (
        _ImplementationEquivalenceCommit
        if implementation_v2
        else _GeneratorEquivalenceCommit
    )
    commit = commit_type(
        run_id=run_id,
        config_sha256=authorization.config_sha256,
        authoritative_selected_sha256=authorization.authoritative_selected_sha256,
        reproduced_selected_sha256=authorization.reproduced_selected_sha256,
        authoritative_generator_sha256=authorization.authoritative_generator_sha256,
        reproduced_generator_sha256=authorization.reproduced_generator_sha256,
        authorization_path=_workspace_relative(
            workspace, authorization_path, label="implementation authorization"
        ),
        authorization_sha256=authorization.evidence_digest(),
        ordered_task_ids=authorization.ordered_task_ids,
        tasks=tuple(rows),
    )
    path = _publish_immutable_run_document(
        workspace
        / "state"
        / "pipeline_runs"
        / run_id
        / "identity_migrations"
        / "equivalence_commits"
        / f"{commit.evidence_digest()}.json",
        commit.deterministic_bytes(),
        label="implementation equivalence commit",
    )
    return commit, path


def _finalize_generator_migration(
    *,
    workspace: Path,
    run_id: str,
    spec,
    pending: _PendingConfiguredMigration,
    authorization,
    authorization_path: Path,
    revalidation_artifact: ReportRevalidationEvidence | None,
    revalidation_artifact_path: Path | None,
    supersession: ReportSupersession | None,
    equivalence_commit: _EquivalenceCommit,
    equivalence_commit_path: Path,
) -> tuple[_RevalidationReceipt, Path]:
    """Confirm all per-task attestations, append one supersession, seal receipt."""

    task_rows = []
    engine = _open_engine(workspace, max_repair_rounds=0)
    try:
        for binding in authorization.tasks:
            path = (
                engine.task_dir(binding.task_id)
                / "ingest_provenance"
                / _equivalence_evidence_filename(binding, authorization)
            )
            payload, _document = _regular_json_bytes(
                path, label="implementation provenance equivalence"
            )
            task_rows.append(
                _GeneratorRevalidationTask(
                    task_id=binding.task_id,
                    intake_content_hash=binding.intake_content_hash,
                    authoritative_provenance_sha256=(
                        binding.authoritative_provenance_sha256
                    ),
                    reproduced_provenance_sha256=(
                        binding.reproduced_provenance_sha256
                    ),
                    equivalence_path=_workspace_relative(
                        workspace,
                        path,
                        label="implementation provenance equivalence",
                    ),
                    equivalence_sha256=hashlib.sha256(payload).hexdigest(),
                    report_locally_revalidated_and_superseded=(
                        supersession is not None
                        and binding.task_id
                        == (revalidation_artifact.task_id if revalidation_artifact else "")
                    ),
                )
            )
        superseded_ids: tuple[int, ...] = ()
        replacement_report_id = 0
        replacement_payload_sha256 = "0" * 64
        if supersession is not None:
            task = engine.load_task(
                revalidation_artifact.task_id if revalidation_artifact else ""
            )
            before = (engine.task_dir(task.task_id) / "task_ir.json").read_bytes()
            engine.supersede_report(task, supersession)
            after = (engine.task_dir(task.task_id) / "task_ir.json").read_bytes()
            if after != before:
                raise CliUsageError("report supersession changed TaskIR bytes")
            if engine.final_verdict(task.task_id) == FINAL_REJECTED:
                raise CliUsageError("locally revalidated fatal remains active")
            replacement = engine._matching_supersession(task, supersession)
            if replacement is None:
                raise CliUsageError("locally revalidated replacement PASS is missing")
            replacement_report_id = replacement.id
            replacement_payload_sha256 = hashlib.sha256(
                replacement.payload_json.encode("utf-8")
            ).hexdigest()
            superseded_ids = (supersession.target_report_id,)
    finally:
        engine.close()

    authoritative_selected_path = (
        workspace
        / "state"
        / "candidate_manifests"
        / f"{authorization.authoritative_selected_sha256}.json"
    )
    reproduced_selected_path = (
        workspace
        / "state"
        / "candidate_manifests"
        / f"{authorization.reproduced_selected_sha256}.json"
    )
    artifact_rel = (
        _workspace_relative(
            workspace, revalidation_artifact_path, label="local report revalidation"
        )
        if revalidation_artifact_path is not None
        else ""
    )
    artifact_sha = (
        hashlib.sha256(revalidation_artifact.deterministic_bytes()).hexdigest()
        if revalidation_artifact is not None
        else "0" * 64
    )
    budget_after = _logical_budget_snapshot(
        workspace=workspace, run_id=run_id, spec=spec
    )
    budget_after_sha = sha256_hex(canonical_json(budget_after))
    if budget_after_sha != authorization.budget_snapshot_sha256:
        raise CliUsageError(
            "durable provider budget changed during implementation revalidation"
        )
    receipt_type = (
        _ImplementationRevalidationReceipt
        if _is_implementation_authorization(authorization)
        else _GeneratorRevalidationReceipt
    )
    receipt = receipt_type(
        run_id=run_id,
        config_sha256=authorization.config_sha256,
        authoritative_selected_sha256=authorization.authoritative_selected_sha256,
        authoritative_selected_path=str(authoritative_selected_path),
        authoritative_pool_sha256=authorization.authoritative_pool_sha256,
        authoritative_generator_sha256=authorization.authoritative_generator_sha256,
        reproduced_selected_sha256=authorization.reproduced_selected_sha256,
        reproduced_selected_path=str(reproduced_selected_path),
        reproduced_pool_sha256=authorization.reproduced_pool_sha256,
        reproduced_generator_sha256=authorization.reproduced_generator_sha256,
        prior_readiness_sha256=authorization.prior_readiness_sha256,
        ordered_task_ids=authorization.ordered_task_ids,
        tasks=tuple(task_rows),
        revalidation_artifact_path=artifact_rel,
        revalidation_artifact_sha256=artifact_sha,
        authorization_path=_workspace_relative(
            workspace, authorization_path, label="implementation authorization"
        ),
        authorization_sha256=authorization.evidence_digest(),
        equivalence_commit_path=_workspace_relative(
            workspace,
            equivalence_commit_path,
            label="implementation equivalence commit",
        ),
        equivalence_commit_sha256=equivalence_commit.evidence_digest(),
        superseded_report_ids=superseded_ids,
        replacement_report_id=replacement_report_id,
        replacement_payload_sha256=replacement_payload_sha256,
        resumed_existing=len(task_rows),
        adapter_taskirs_rederived=len(task_rows),
        source_provenance_equivalence_attested=len(task_rows),
        reports_locally_revalidated_and_superseded=int(supersession is not None),
        budget_snapshot_before_sha256=authorization.budget_snapshot_sha256,
        budget_snapshot_after_sha256=budget_after_sha,
        budget_committed_usd=float(budget_after.get("committed_usd", 0.0)),
        budget_reserved_usd=float(budget_after.get("reserved_usd", 0.0)),
        budget_uncertain_usd=float(budget_after.get("uncertain_usd", 0.0)),
    )
    history_path = (
        workspace
        / "state"
        / "pipeline_runs"
        / run_id
        / "readiness_history"
        / f"{authorization.prior_readiness_sha256}.json"
    )
    _publish_immutable_run_document(
        history_path,
        pending.prior_report_bytes,
        label="prior configured readiness",
    )
    receipt_path = _publish_immutable_run_document(
        workspace
        / "state"
        / "pipeline_runs"
        / run_id
        / "identity_migrations"
        / "receipts"
        / f"{receipt.evidence_digest()}.json",
        receipt.deterministic_bytes(),
        label="implementation revalidation receipt",
    )
    return receipt, receipt_path


def _selected_implementation_transition(
    authoritative: Any, reproduced: Any
) -> tuple[str, tuple[_ManifestAdapterTransition, ...]]:
    """Compare selected manifests under the code-only equivalence protocol.

    Generator and uniform per-pool adapter pins may move; every other source
    field must remain exact.
    """

    old_payload = authoritative.model_dump(mode="json")
    new_payload = reproduced.model_dump(mode="json")
    old_generator = dict(old_payload.pop("generator"))
    new_generator = dict(new_payload.pop("generator"))
    old_parent = old_payload.pop("parent_manifest_sha256")
    new_parent = new_payload.pop("parent_manifest_sha256")
    old_payload.pop("sources")
    new_payload.pop("sources")
    if old_payload != new_payload:
        return (
            "task count, catalog, seed, allocation, or independence policy changed",
            (),
        )
    old_generator_sha = str(old_generator.pop("sha256"))
    new_generator_sha = str(new_generator.pop("sha256"))
    old_generator_version = str(old_generator.pop("version"))
    new_generator_version = str(new_generator.pop("version"))
    if old_generator != new_generator:
        return "generator metadata beyond its version/code digest changed", ()
    if old_generator_sha == new_generator_sha:
        return "generator code digest did not move", ()
    if old_parent == new_parent:
        return "parent pool identity did not move", ()

    old_entries = authoritative.ordered_entries()
    new_entries = reproduced.ordered_entries()
    old_ids = tuple(entry.expected_task_id for entry in old_entries)
    new_ids = tuple(entry.expected_task_id for entry in new_entries)
    if old_ids != new_ids:
        return "ordered task ids changed", ()

    transitions: list[_ManifestAdapterTransition] = []
    pins_by_origin: dict[Origin, set[tuple[str, str, str, str]]] = {}
    for old_entry, new_entry in zip(old_entries, new_entries, strict=True):
        old_source = old_entry.model_dump(mode="json")
        new_source = new_entry.model_dump(mode="json")
        old_digest = str(old_source.pop("adapter_digest"))
        new_digest = str(new_source.pop("adapter_digest"))
        old_version = str(old_source.pop("adapter_version"))
        new_version = str(new_source.pop("adapter_version"))
        if old_source != new_source:
            return (
                "selector, source artifacts, revision, license, "
                f"or source policy changed for {old_entry.expected_task_id!r}",
                (),
            )
        origin = Origin(old_entry.pool)
        pins_by_origin.setdefault(origin, set()).add(
            (old_version, old_digest, new_version, new_digest)
        )
        old_source_entry = authoritative.source_entry_sha256(old_entry)
        new_source_entry = reproduced.source_entry_sha256(new_entry)
        if (old_version, old_digest) == (new_version, new_digest):
            if old_source_entry != new_source_entry:
                return (
                    f"source-entry digest moved without an adapter change for "
                    f"{old_entry.expected_task_id!r}",
                    (),
                )
            continue
        transitions.append(
            _ManifestAdapterTransition(
                task_id=old_entry.expected_task_id,
                origin=origin,
                authoritative_version=old_version,
                reproduced_version=new_version,
                authoritative_digest=old_digest,
                reproduced_digest=new_digest,
                authoritative_source_entry_sha256=old_source_entry,
                reproduced_source_entry_sha256=new_source_entry,
            )
        )
    inconsistent = sorted(
        origin.value for origin, pins in pins_by_origin.items() if len(pins) != 1
    )
    if inconsistent:
        return (
            "source entries pin inconsistent adapter transitions for pools "
            f"{inconsistent}",
            (),
        )
    generator_version_transition = (
        old_generator_version,
        new_generator_version,
    )
    entry_version_transitions = {
        (row.authoritative_version, row.reproduced_version)
        for row in transitions
        if row.authoritative_version != row.reproduced_version
    }
    if old_generator_version == new_generator_version:
        if entry_version_transitions:
            return (
                "adapter version changed without the matching generator version",
                (),
            )
    elif (
        entry_version_transitions != {generator_version_transition}
        or any(
            (old_entry.adapter_version, new_entry.adapter_version)
            != generator_version_transition
            for old_entry, new_entry in zip(old_entries, new_entries, strict=True)
        )
    ):
        return (
            "generator version changed without the same adapter version "
            "transition for every selected task",
            (),
        )
    return "", tuple(transitions)


def _generator_only_selected_transition_problem(
    authoritative: Any, reproduced: Any
) -> str:
    """Why a historical v1 transition is not strictly generator-only."""

    problem, transitions = _selected_implementation_transition(
        authoritative, reproduced
    )
    if problem:
        return problem
    if transitions:
        return "source entries changed adapter version or digest"
    return ""


def _pool_reconstructed_at_selected_implementation(
    pool_manifest: Any, selected_manifest: Any
) -> Any:
    """Reconstruct a pool's code pins from a selected-manifest observation.

    A selected roster cannot reveal a historical adapter pin for an omitted
    pool.  Such a pool is deliberately left at the supplied value, causing the
    parent digest check to fail if that unobserved adapter actually moved.
    """

    from elt_taskgen.ingest_manifest import FIVE_ORIGINS

    source_updates: dict[str, tuple[Any, ...]] = {}
    for origin in FIVE_ORIGINS:
        selected_entries = tuple(
            getattr(selected_manifest.sources, origin.value, ())
        )
        pool_entries = tuple(getattr(pool_manifest.sources, origin.value, ()))
        if not selected_entries:
            source_updates[origin.value] = pool_entries
            continue
        selected_pins = {
            (entry.adapter_version, entry.adapter_digest)
            for entry in selected_entries
        }
        current_pins = {
            (entry.adapter_version, entry.adapter_digest)
            for entry in pool_entries
        }
        if len(selected_pins) != 1 or len(current_pins) != 1:
            raise CliUsageError(
                f"cannot reconstruct {origin.value} pool from inconsistent "
                "adapter pins"
            )
        version, digest = next(iter(selected_pins))
        source_updates[origin.value] = tuple(
            entry.model_copy(
                update={"adapter_version": version, "adapter_digest": digest}
            )
            for entry in pool_entries
        )
    return pool_manifest.model_copy(
        update={
            "generator": selected_manifest.generator,
            "sources": pool_manifest.sources.model_copy(update=source_updates),
        }
    )


def _historical_committed_supersession_is_intact(
    *,
    engine: Engine,
    task: TaskIR,
    artifact: ReportRevalidationEvidence,
    receipt: _GeneratorRevalidationReceipt,
) -> bool:
    """Verify an inactive supersession as immutable migration history.

    A later semantic identity cannot reactivate or inherit the old pass. Check
    only content-addressed history: archived TaskIR, cause, fatal target,
    replacement row, exact marker, and the forward pass that recorded the live
    identity. Current readiness remains a separate, unmodified gate.
    """

    current_hash = task.content_hash()
    lineage_root = (
        task.revisions[0].content_hash if task.revisions else current_hash
    )
    if (
        task.task_id != artifact.task_id
        or lineage_root != artifact.intake_content_hash
        or current_hash == artifact.task_content_hash
    ):
        return False
    try:
        artifact_bytes = engine._read_supersession_evidence(  # noqa: SLF001
            receipt.revalidation_artifact_path,
            label="committed historical report revalidation",
        )
        parsed_artifact = ReportRevalidationEvidence.model_validate_json(
            artifact_bytes
        )
        archived_bytes = engine._read_supersession_evidence(  # noqa: SLF001
            artifact.archived_task_ir_path,
            label="committed historical archived TaskIR",
        )
        archived_task = task_from_json(archived_bytes.decode("utf-8"))
        archived_root = (
            archived_task.revisions[0].content_hash
            if archived_task.revisions
            else archived_task.content_hash()
        )
        cause = engine._report_by_id(artifact.cause_report_id)  # noqa: SLF001
        target = engine._report_by_id(artifact.target_report_id)  # noqa: SLF001
        replacement = engine._report_by_id(  # noqa: SLF001
            receipt.replacement_report_id
        )
        if cause is None or target is None or replacement is None:
            return False
        expected_common = (
            artifact.task_id,
            artifact.stage,
            artifact.task_content_hash,
        )
        if (
            hashlib.sha256(artifact_bytes).hexdigest()
            != receipt.revalidation_artifact_sha256
            or artifact_bytes != artifact.deterministic_bytes()
            or parsed_artifact != artifact
            or artifact.run_id != receipt.run_id
            or artifact.config_sha256 != receipt.config_sha256
            or artifact.authoritative_selected_sha256
            != receipt.authoritative_selected_sha256
            or artifact.reproduced_selected_sha256
            != receipt.reproduced_selected_sha256
            or artifact.authoritative_generator_sha256
            != receipt.authoritative_generator_sha256
            or artifact.reproduced_generator_sha256
            != receipt.reproduced_generator_sha256
            or artifact.authorization_path != receipt.authorization_path
            or artifact.authorization_sha256 != receipt.authorization_sha256
            or artifact.equivalence_commit_path
            != receipt.equivalence_commit_path
            or artifact.equivalence_commit_sha256
            != receipt.equivalence_commit_sha256
            or receipt.superseded_report_ids != (artifact.target_report_id,)
            or hashlib.sha256(archived_bytes).hexdigest()
            != artifact.archived_task_ir_sha256
            or archived_task.task_id != artifact.task_id
            or archived_task.content_hash() != artifact.task_content_hash
            or archived_root != artifact.intake_content_hash
            or (cause.task_id, cause.stage, cause.content_hash) != expected_common
            or cause.verdict != VERDICT_FAIL
            or hashlib.sha256(cause.payload_json.encode("utf-8")).hexdigest()
            != artifact.cause_payload_sha256
            or (target.task_id, target.stage, target.content_hash)
            != expected_common
            or target.verdict != VERDICT_FATAL
            or hashlib.sha256(target.payload_json.encode("utf-8")).hexdigest()
            != artifact.target_payload_sha256
            or not (cause.id < target.id < replacement.id)
            or (replacement.task_id, replacement.stage, replacement.content_hash)
            != expected_common
            or replacement.verdict != VERDICT_PASS
            or replacement.id != receipt.replacement_report_id
            or hashlib.sha256(replacement.payload_json.encode("utf-8")).hexdigest()
            != receipt.replacement_payload_sha256
        ):
            return False

        marker = engine._supersession_marker(replacement)  # noqa: SLF001
        if marker is None:
            return False
        observed_supersession = engine._supersession_from_marker(  # noqa: SLF001
            marker
        )
        revalidation_reason = artifact.replacement_data.get("revalidation_reason")
        if not revalidation_reason:
            return False
        expected_supersession = ReportSupersession(
            migration_id=artifact.migration_id,
            reason=revalidation_reason,
            stage=artifact.stage,
            content_hash=artifact.task_content_hash,
            revalidation_path=receipt.revalidation_artifact_path,
            revalidation_sha256=receipt.revalidation_artifact_sha256,
            cause_report_id=artifact.cause_report_id,
            cause_payload_sha256=artifact.cause_payload_sha256,
            target_report_id=artifact.target_report_id,
            target_payload_sha256=artifact.target_payload_sha256,
            replacement_detail=artifact.replacement_detail,
            replacement_data=tuple(sorted(artifact.replacement_data.items())),
        )
        expected_payload = StagePayload(
            detail=artifact.replacement_detail,
            data={
                **artifact.replacement_data,
                EVIDENCE_SUPERSESSION_KEY: canonical_json(
                    expected_supersession.as_dict()
                ),
            },
        )
        if (
            observed_supersession != expected_supersession
            or marker != expected_supersession.as_dict()
            or replacement.payload_json
            != canonical_json(expected_payload.model_dump(mode="json"))
        ):
            return False

        # The later identity must have been produced by an ordinary successful
        # execution of this same stage after the replacement.  It may be stale
        # under today's behavior; that is deliberately left for readiness.
        for row in engine.report_history(task.task_id, artifact.stage):
            if (
                row.id > replacement.id
                and row.verdict == VERDICT_PASS
                and row.content_hash == current_hash
                and engine._supersession_marker(row) is None  # noqa: SLF001
            ):
                return True
        return False
    except (EngineError, TypeError, UnicodeError, ValueError):
        return False


def _committed_supersession_or_forward_pass_is_current(
    *,
    engine: Engine,
    task: TaskIR,
    artifact: ReportRevalidationEvidence,
    receipt: _GeneratorRevalidationReceipt,
) -> bool:
    """Whether a committed repair is active or intact historical ancestry."""

    if task.content_hash() == artifact.task_content_hash:
        return engine.has_active_report_supersession(task)
    return _historical_committed_supersession_is_intact(
        engine=engine,
        task=task,
        artifact=artifact,
        receipt=receipt,
    )


def _load_committed_generator_migration(
    *,
    workspace: Path,
    pending: _PendingConfiguredMigration,
    _seen: frozenset[str] = frozenset(),
    _depth: int = 0,
) -> _PendingConfiguredMigration:
    """Revalidate a prior run receipt and authorization for an ordinary resume."""

    from elt_taskgen.ingest_manifest import load_generator_equivalence_authorization
    from elt_taskgen.pipeline_readiness import _read_bounded_regular_bytes

    report = pending.prior_report
    if _depth >= 64:
        raise CliUsageError("implementation migration ancestry exceeds 64 receipts")
    if not report.generator_revalidation_receipt or not report.identity_migration_sha256:
        raise CliUsageError(
            "active implementation migration has no committed run receipt"
        )
    receipt_path = Path(report.generator_revalidation_receipt).absolute()
    expected_root = (
        workspace
        / "state"
        / "pipeline_runs"
        / report.run_id
        / "identity_migrations"
        / "receipts"
    )
    try:
        receipt_path.relative_to(expected_root)
    except ValueError:
        raise CliUsageError(
            "implementation revalidation receipt path is not canonical"
        ) from None
    try:
        receipt_bytes = _read_bounded_regular_bytes(
            receipt_path,
            label="implementation revalidation receipt",
            max_bytes=32 * 1024 * 1024,
        )
        receipt_document = json.loads(receipt_bytes)
        if not isinstance(receipt_document, dict):
            raise ValueError("receipt must be a JSON object")
        receipt_schema = receipt_document.get("schema_version")
        if receipt_schema == "configured-generator-revalidation-v1":
            receipt_type = _GeneratorRevalidationReceipt
        elif receipt_schema == "configured-implementation-revalidation-v2":
            receipt_type = _ImplementationRevalidationReceipt
        else:
            raise ValueError(
                f"unsupported revalidation receipt schema {receipt_schema!r}"
            )
        receipt = receipt_type.model_validate(receipt_document)
    except (UnicodeError, ValueError) as exc:
        raise CliUsageError(
            f"implementation revalidation receipt is invalid: {exc}"
        ) from None
    if (
        receipt_bytes != receipt.deterministic_bytes()
        or receipt.evidence_digest() != report.identity_migration_sha256
        or receipt_path.name != f"{receipt.evidence_digest()}.json"
    ):
        raise CliUsageError(
            "implementation revalidation receipt is not content-addressed"
        )
    receipt_digest = receipt.evidence_digest()
    if receipt_digest in _seen:
        raise CliUsageError(
            "implementation migration ancestry contains a receipt cycle"
        )
    ancestry = _seen | {receipt_digest}
    expected_authoritative_path = (
        workspace
        / "state"
        / "candidate_manifests"
        / f"{pending.authoritative_selected.manifest_sha256()}.json"
    )
    expected_reproduced_path = (
        workspace
        / "state"
        / "candidate_manifests"
        / f"{pending.reproduced_selected.manifest_sha256()}.json"
    )
    if (
        receipt.authoritative_selected_path != str(expected_authoritative_path)
        or receipt.reproduced_selected_path != str(expected_reproduced_path)
        or report.active_selected_manifest_path != str(expected_reproduced_path)
    ):
        raise CliUsageError(
            "implementation revalidation selected-manifest paths differ"
        )
    for path, manifest, label in (
        (expected_authoritative_path, pending.authoritative_selected, "authoritative selected manifest"),
        (expected_reproduced_path, pending.reproduced_selected, "reproduced selected manifest"),
    ):
        payload = _read_bounded_regular_bytes(path, label=label, max_bytes=32 * 1024 * 1024)
        if hashlib.sha256(canonical_json(manifest.model_dump(mode="json")).encode("utf-8")).hexdigest() != manifest.manifest_sha256():
            raise CliUsageError(f"{label} model digest is inconsistent")
        parsed = type(manifest).model_validate_json(payload)
        if parsed != manifest:
            raise CliUsageError(f"{label} bytes changed")
    transition_problem, manifest_transitions = _selected_implementation_transition(
        pending.authoritative_selected, pending.reproduced_selected
    )
    if transition_problem:
        raise CliUsageError(
            "committed implementation transition is invalid: "
            + transition_problem
        )
    implementation_v2 = isinstance(receipt, _ImplementationRevalidationReceipt)
    if implementation_v2 != bool(manifest_transitions):
        raise CliUsageError(
            "committed receipt schema does not match its adapter transitions"
        )
    if pending.adapter_transitions and pending.adapter_transitions != manifest_transitions:
        raise CliUsageError("committed adapter transition set changed")
    expected = {
        "run_id": report.run_id,
        "config_sha256": report.config_sha256,
        "authoritative_selected_sha256": (
            pending.authoritative_selected.manifest_sha256()
        ),
        "reproduced_selected_sha256": pending.reproduced_selected.manifest_sha256(),
        "authoritative_pool_sha256": pending.authoritative_pool_sha256,
        "reproduced_pool_sha256": pending.reproduced_pool_sha256,
        "authoritative_generator_sha256": (
            pending.authoritative_selected.generator.sha256
        ),
        "reproduced_generator_sha256": pending.reproduced_selected.generator.sha256,
    }
    observed = receipt.model_dump(mode="json")
    mismatches = [key for key, value in expected.items() if observed[key] != value]
    if mismatches:
        raise CliUsageError(
            "implementation revalidation receipt differs from this run: "
            + ", ".join(mismatches)
        )
    authorization_path = _bound_workspace_path(
        workspace,
        receipt.authorization_path,
        label="implementation authorization",
    )
    authorization = load_generator_equivalence_authorization(
        authorization_path, workspace=workspace
    )
    if (
        _is_implementation_authorization(authorization) != implementation_v2
        or authorization.evidence_digest() != receipt.authorization_sha256
        or authorization.run_id != receipt.run_id
        or authorization.config_sha256 != receipt.config_sha256
        or authorization.ordered_task_ids != receipt.ordered_task_ids
        or authorization.prior_readiness_sha256 != receipt.prior_readiness_sha256
        or authorization.authoritative_selected_sha256
        != receipt.authoritative_selected_sha256
        or authorization.reproduced_selected_sha256
        != receipt.reproduced_selected_sha256
        or (
            authorization.authoritative_pool_sha256
            != receipt.authoritative_pool_sha256
        )
        or authorization.reproduced_pool_sha256 != receipt.reproduced_pool_sha256
        or authorization.authoritative_generator_sha256
        != receipt.authoritative_generator_sha256
        or authorization.reproduced_generator_sha256
        != receipt.reproduced_generator_sha256
        or authorization.budget_snapshot_sha256
        != receipt.budget_snapshot_before_sha256
        or float(authorization.budget_snapshot.get("committed_usd", 0.0))
        != receipt.budget_committed_usd
        or float(authorization.budget_snapshot.get("reserved_usd", 0.0))
        != receipt.budget_reserved_usd
        or float(authorization.budget_snapshot.get("uncertain_usd", 0.0))
        != receipt.budget_uncertain_usd
    ):
        raise CliUsageError(
            "committed implementation authorization does not match receipt"
        )
    if implementation_v2:
        transition_by_task = {row.task_id: row for row in manifest_transitions}
        old_entries = {
            entry.expected_task_id: entry
            for entry in pending.authoritative_selected.ordered_entries()
        }
        new_entries = {
            entry.expected_task_id: entry
            for entry in pending.reproduced_selected.ordered_entries()
        }
        for binding in authorization.tasks:
            old_entry = old_entries[binding.task_id]
            new_entry = new_entries[binding.task_id]
            expected_transition = transition_by_task.get(binding.task_id)
            observed_transition = binding.adapter_transition
            mismatch = (
                binding.origin is not Origin(old_entry.pool)
                or binding.authoritative_source_entry_sha256
                != pending.authoritative_selected.source_entry_sha256(old_entry)
                or binding.reproduced_source_entry_sha256
                != pending.reproduced_selected.source_entry_sha256(new_entry)
                or (expected_transition is None) != (observed_transition is None)
            )
            if expected_transition is not None and observed_transition is not None:
                mismatch = mismatch or (
                    observed_transition.pool is not expected_transition.origin
                    or observed_transition.authoritative_version
                    != expected_transition.authoritative_version
                    or observed_transition.reproduced_version
                    != expected_transition.reproduced_version
                    or observed_transition.authoritative_digest
                    != expected_transition.authoritative_digest
                    or observed_transition.reproduced_digest
                    != expected_transition.reproduced_digest
                    or observed_transition.authoritative_source_entry_sha256
                    != expected_transition.authoritative_source_entry_sha256
                    or observed_transition.reproduced_source_entry_sha256
                    != expected_transition.reproduced_source_entry_sha256
                )
            if mismatch:
                raise CliUsageError(
                    "committed implementation authorization changed adapter/source "
                    f"binding for {binding.task_id!r}"
                )
    commit_path = _bound_workspace_path(
        workspace,
        receipt.equivalence_commit_path,
        label="implementation equivalence commit",
    )
    commit_bytes = _read_bounded_regular_bytes(
        commit_path,
        label="implementation equivalence commit",
        max_bytes=32 * 1024 * 1024,
    )
    commit_type = (
        _ImplementationEquivalenceCommit
        if implementation_v2
        else _GeneratorEquivalenceCommit
    )
    try:
        commit = commit_type.model_validate_json(commit_bytes)
    except ValueError as exc:
        raise CliUsageError(
            f"implementation equivalence commit is invalid: {exc}"
        ) from None
    if (
        commit_bytes != commit.deterministic_bytes()
        or commit.evidence_digest() != receipt.equivalence_commit_sha256
        or commit.authorization_sha256 != authorization.evidence_digest()
        or commit.ordered_task_ids != authorization.ordered_task_ids
        or tuple(
            (row.task_id, row.intake_content_hash, row.equivalence_sha256)
            for row in commit.tasks
        )
        != tuple(
            (row.task_id, row.intake_content_hash, row.equivalence_sha256)
            for row in receipt.tasks
        )
    ):
        raise CliUsageError("committed implementation equivalence roster changed")
    verified_commit, verified_commit_path = _commit_generator_equivalences(
        workspace=workspace,
        run_id=receipt.run_id,
        authorization=authorization,
        authorization_path=authorization_path,
    )
    if (
        verified_commit != commit
        or verified_commit_path.absolute() != commit_path.absolute()
    ):
        raise CliUsageError(
            "committed implementation equivalence evidence no longer verifies"
        )
    by_id = {task.task_id: task for task in receipt.tasks}
    for binding in authorization.tasks:
        row = by_id.get(binding.task_id)
        if row is None or (
            row.intake_content_hash != binding.intake_content_hash
            or row.authoritative_provenance_sha256
            != binding.authoritative_provenance_sha256
            or row.reproduced_provenance_sha256
            != binding.reproduced_provenance_sha256
        ):
            raise CliUsageError(
                f"committed generator task binding changed for {binding.task_id!r}"
            )
        evidence_path = _bound_workspace_path(
            workspace, row.equivalence_path, label="generator provenance equivalence"
        )
        evidence = _read_bounded_regular_bytes(
            evidence_path,
            label="generator provenance equivalence",
            max_bytes=16 * 1024 * 1024,
        )
        if hashlib.sha256(evidence).hexdigest() != row.equivalence_sha256:
            raise CliUsageError(
                f"generator provenance equivalence changed for {binding.task_id!r}"
            )
    history_path = (
        workspace
        / "state"
        / "pipeline_runs"
        / report.run_id
        / "readiness_history"
        / f"{receipt.prior_readiness_sha256}.json"
    )
    history = _read_bounded_regular_bytes(
        history_path, label="prior configured readiness", max_bytes=32 * 1024 * 1024
    )
    if hashlib.sha256(history).hexdigest() != receipt.prior_readiness_sha256:
        raise CliUsageError("prior configured readiness history changed")
    try:
        historical_report = type(report).model_validate_json(history)
        historical_document = json.loads(history)
    except ValueError as exc:
        raise CliUsageError(f"prior configured readiness history is invalid: {exc}") from None
    historical_counts = (
        historical_document.get("counts")
        if isinstance(historical_document, dict)
        else None
    )
    historical_selected_problem = bool(
        isinstance(historical_counts, dict)
        and "selected" in historical_counts
        and historical_report.counts.selected != len(receipt.ordered_task_ids)
    )
    expected_historical_selected_path = (
        workspace
        / "state"
        / "candidate_manifests"
        / f"{receipt.authoritative_selected_sha256}.json"
    )
    if (
        historical_report.run_id != receipt.run_id
        or historical_report.config_sha256 != receipt.config_sha256
        or historical_report.config.fingerprint()
        != historical_report.config_sha256
        or historical_report.source_manifest_sha256
        != receipt.authoritative_selected_sha256
        or historical_report.selected_manifest_path
        != str(expected_historical_selected_path)
        or tuple(task.task_id for task in historical_report.tasks)
        != receipt.ordered_task_ids
        or historical_report.counts.requested != len(receipt.ordered_task_ids)
        or historical_selected_problem
    ):
        raise CliUsageError("prior configured readiness history identity differs")

    head = {
        "active_source_manifest_sha256": (
            historical_report.active_source_manifest_sha256
        ),
        "active_selected_manifest_path": (
            historical_report.active_selected_manifest_path
        ),
        "generator_revalidation_receipt": (
            historical_report.generator_revalidation_receipt
        ),
        "identity_migration_sha256": historical_report.identity_migration_sha256,
    }
    present = {name: bool(value) for name, value in head.items()}
    if any(present.values()) and not all(present.values()):
        raise CliUsageError(
            "prior configured readiness has a partial implementation migration head"
        )
    if all(present.values()):
        previous_selected_path = (
            workspace
            / "state"
            / "candidate_manifests"
            / f"{historical_report.active_source_manifest_sha256}.json"
        )
        if historical_report.active_selected_manifest_path != str(
            previous_selected_path
        ):
            raise CliUsageError(
                "prior implementation migration active selected path is not canonical"
            )
        try:
            previous_selected_bytes = _read_bounded_regular_bytes(
                previous_selected_path,
                label="prior active selected manifest",
                max_bytes=32 * 1024 * 1024,
            )
            previous_selected = type(pending.reproduced_selected).model_validate_json(
                previous_selected_bytes
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise CliUsageError(
                f"prior active selected manifest is invalid: {exc}"
            ) from None
        if (
            previous_selected.manifest_sha256()
            != historical_report.active_source_manifest_sha256
        ):
            raise CliUsageError("prior active selected manifest digest changed")
        if (
            previous_selected.manifest_sha256()
            == pending.reproduced_selected.manifest_sha256()
        ):
            raise CliUsageError(
                "implementation migration ancestry repeats its active head"
            )
        prior_transition_problem, prior_adapter_transitions = (
            _selected_implementation_transition(
            pending.authoritative_selected, previous_selected
            )
        )
        if prior_transition_problem:
            raise CliUsageError(
                "prior implementation migration is invalid: "
                + prior_transition_problem
            )
        _load_committed_generator_migration(
            workspace=workspace,
            pending=_PendingConfiguredMigration(
                prior_report=historical_report,
                prior_report_bytes=history,
                authoritative_selected=pending.authoritative_selected,
                reproduced_selected=previous_selected,
                authoritative_pool_sha256=(
                    pending.authoritative_selected.parent_manifest_sha256
                ),
                reproduced_pool_sha256=previous_selected.parent_manifest_sha256,
                adapter_transitions=prior_adapter_transitions,
            ),
            _seen=ancestry,
            _depth=_depth + 1,
        )
    if receipt.superseded_report_ids:
        if not receipt.revalidation_artifact_path:
            raise CliUsageError("committed report supersession has no revalidation artifact")
        artifact_path = _bound_workspace_path(
            workspace,
            receipt.revalidation_artifact_path,
            label="local report revalidation",
        )
        artifact_bytes = _read_bounded_regular_bytes(
            artifact_path,
            label="local report revalidation",
            max_bytes=32 * 1024 * 1024,
        )
        try:
            artifact = ReportRevalidationEvidence.model_validate_json(artifact_bytes)
        except ValueError as exc:
            raise CliUsageError(f"local report revalidation is invalid: {exc}") from None
        if (
            artifact_bytes != artifact.deterministic_bytes()
            or hashlib.sha256(artifact_bytes).hexdigest()
            != receipt.revalidation_artifact_sha256
            or (artifact.target_report_id,) != receipt.superseded_report_ids
        ):
            raise CliUsageError("local report revalidation changed")
        engine = _open_engine(workspace, max_repair_rounds=0)
        try:
            task = engine.load_task(artifact.task_id)
            replacement = engine._report_by_id(receipt.replacement_report_id)
            if (
                replacement is None
                or replacement.task_id != artifact.task_id
                or replacement.stage != artifact.stage
                or replacement.content_hash != artifact.task_content_hash
                or hashlib.sha256(replacement.payload_json.encode("utf-8")).hexdigest()
                != receipt.replacement_payload_sha256
            ):
                raise CliUsageError("committed replacement PASS changed")
            if not _committed_supersession_or_forward_pass_is_current(
                engine=engine,
                task=task,
                artifact=artifact,
                receipt=receipt,
            ):
                raise CliUsageError(
                    "committed report supersession is neither active nor "
                    "intact historical ancestry"
                )
        finally:
            engine.close()
    return replace(
        pending,
        authorization=authorization,
        authorization_path=authorization_path,
        committed_receipt=receipt,
        committed_receipt_path=receipt_path,
    )


def _assert_configured_run_identity(
    *,
    workspace: Path,
    run_id: str,
    spec,
    pool_manifest,
    selected_manifest,
    reingest: bool,
) -> _PendingConfiguredMigration | None:
    """Validate an ordinary resume or a code-only roster revalidation."""

    from elt_taskgen.ingest_manifest import SelectedSourceIngestManifest
    from elt_taskgen.pipeline_readiness import (
        PipelineReadinessReport,
        _read_bounded_regular_bytes,
    )

    run_root = workspace / "state" / "pipeline_runs" / run_id
    if run_root.is_symlink():
        raise CliUsageError(
            f"run id {run_id!r} resolves through an unsafe state symlink; "
            "use a different --run-id"
        )
    if not run_root.exists():
        return None
    report_path = run_root / "readiness.json"
    if not report_path.is_file() or report_path.is_symlink():
        raise CliUsageError(
            f"run id {run_id!r} already has state without a safe readiness "
            "identity; use a different --run-id"
        )
    try:
        prior_report_bytes = _read_bounded_regular_bytes(
            report_path, label="prior configured readiness", max_bytes=32 * 1024 * 1024
        )
        prior_report_document = json.loads(prior_report_bytes)
        report = PipelineReadinessReport.model_validate_json(prior_report_bytes)
    except (OSError, UnicodeError, ValueError) as exc:
        raise CliUsageError(
            f"run id {run_id!r} has an invalid prior readiness report; use a "
            f"different --run-id: {exc}"
        ) from None
    expected_config = spec.fingerprint()
    expected_selection = selected_manifest.manifest_sha256()
    problems: list[str] = []
    if report.run_id != run_id:
        problems.append(f"embedded run_id is {report.run_id!r}")
    if (
        report.config_sha256 != expected_config
        or report.config.fingerprint() != report.config_sha256
    ):
        problems.append("configuration fingerprint differs")
    if problems:
        raise CliUsageError(
            f"run id {run_id!r} is already bound to a different run identity "
            f"({'; '.join(problems)}); use a different --run-id"
        )
    if not spec.resume:
        raise CliUsageError(
            f"run id {run_id!r} already exists but resume=false; use a "
            "different --run-id or enable resume"
        )
    head_present = tuple(
        bool(value)
        for value in (
            report.active_source_manifest_sha256,
            report.active_selected_manifest_path,
            report.generator_revalidation_receipt,
            report.identity_migration_sha256,
        )
    )
    if any(head_present) and not all(head_present):
        raise CliUsageError(
            "prior readiness has a partial implementation migration head"
        )
    if report.source_manifest_sha256 == expected_selection:
        if any(head_present):
            raise CliUsageError("prior readiness has a conflicting active selection")
        return None
    committed_resume = report.active_source_manifest_sha256 == expected_selection
    if not report.active_source_manifest_sha256 and not reingest:
        raise CliUsageError(
            f"run id {run_id!r} selected-roster fingerprint differs: bound to "
            f"{report.source_manifest_sha256[:12]}, not {expected_selection[:12]}; "
            "a code-only continuation requires explicit --reingest"
        )

    authoritative_path = (
        workspace
        / "state"
        / "candidate_manifests"
        / f"{report.source_manifest_sha256}.json"
    )
    if str(authoritative_path) != report.selected_manifest_path:
        raise CliUsageError(
            "prior readiness does not name its canonical immutable selected manifest"
        )
    try:
        authoritative_bytes = _read_bounded_regular_bytes(
            authoritative_path,
            label="authoritative selected manifest",
            max_bytes=32 * 1024 * 1024,
        )
        authoritative = SelectedSourceIngestManifest.model_validate_json(
            authoritative_bytes
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise CliUsageError(
            f"authoritative selected manifest is invalid: {exc}"
        ) from None
    if authoritative.manifest_sha256() != report.source_manifest_sha256:
        raise CliUsageError("authoritative selected manifest digest changed")
    authoritative_ids = tuple(
        entry.expected_task_id for entry in authoritative.ordered_entries()
    )
    readiness_ids = tuple(task.task_id for task in report.tasks)
    raw_counts = (
        prior_report_document.get("counts")
        if isinstance(prior_report_document, dict)
        else None
    )
    selected_count_problem = bool(
        isinstance(raw_counts, dict)
        and "selected" in raw_counts
        and report.counts.selected != len(authoritative_ids)
    )
    if (
        readiness_ids != authoritative_ids
        or report.counts.requested != len(authoritative_ids)
        or selected_count_problem
    ):
        raise CliUsageError(
            "prior readiness task roster/order/count differs from its "
            "authoritative selected manifest"
        )

    # A run may already have one committed implementation-equivalence head. Validate that
    # immutable receipt before either resuming it or moving the head again.  A
    # later migration remains a direct authoritative->current equivalence for
    # every TaskIR; its prior-readiness digest preserves the intervening head.
    active = None
    if report.active_source_manifest_sha256:
        active_path = (
            workspace
            / "state"
            / "candidate_manifests"
            / f"{report.active_source_manifest_sha256}.json"
        )
        if str(active_path) != report.active_selected_manifest_path:
            raise CliUsageError(
                "prior readiness does not name its canonical active selected manifest"
            )
        try:
            active_bytes = _read_bounded_regular_bytes(
                active_path,
                label="active selected manifest",
                max_bytes=32 * 1024 * 1024,
            )
            active = SelectedSourceIngestManifest.model_validate_json(active_bytes)
        except (OSError, UnicodeError, ValueError) as exc:
            raise CliUsageError(
                f"active selected manifest is invalid: {exc}"
            ) from None
        if active.manifest_sha256() != report.active_source_manifest_sha256:
            raise CliUsageError("active selected manifest digest changed")
        if authoritative_ids != tuple(
            entry.expected_task_id for entry in active.ordered_entries()
        ):
            raise CliUsageError(
                "active implementation migration changed ordered task ids"
            )
        active_problem, active_adapter_transitions = (
            _selected_implementation_transition(authoritative, active)
        )
        if active_problem:
            raise CliUsageError(
                "active implementation migration is invalid: " + active_problem
            )
        committed = _load_committed_generator_migration(
            workspace=workspace,
            pending=_PendingConfiguredMigration(
                prior_report=report,
                prior_report_bytes=prior_report_bytes,
                authoritative_selected=authoritative,
                reproduced_selected=active,
                authoritative_pool_sha256=authoritative.parent_manifest_sha256,
                reproduced_pool_sha256=active.parent_manifest_sha256,
                adapter_transitions=active_adapter_transitions,
            ),
        )
        if committed_resume:
            return committed

    if not reingest:
        bound = (
            report.active_source_manifest_sha256
            or report.source_manifest_sha256
        )
        raise CliUsageError(
            f"run id {run_id!r} selected-roster fingerprint differs: bound to "
            f"{bound[:12]}, not {expected_selection[:12]}; a code-only "
            "continuation requires explicit --reingest"
        )

    transition_problem, adapter_transitions = _selected_implementation_transition(
        authoritative, selected_manifest
    )
    if transition_problem:
        raise CliUsageError(
            "implementation revalidation refused: " + transition_problem
        )
    old_parent = authoritative.parent_manifest_sha256
    new_parent = selected_manifest.parent_manifest_sha256
    if selected_manifest.parent_manifest_sha256 != pool_manifest.manifest_sha256():
        raise CliUsageError("selected manifest does not bind the supplied pool")
    reconstructed_old_pool = (
        _pool_reconstructed_at_selected_implementation(
            pool_manifest, authoritative
        )
        if adapter_transitions
        else pool_manifest.model_copy(update={"generator": authoritative.generator})
    )
    if reconstructed_old_pool.manifest_sha256() != old_parent:
        raise CliUsageError(
            "supplied repinned pool differs from the authoritative pool beyond "
            "its explicitly rederived implementation pins"
        )
    if active is not None:
        active_transition_problem, active_transitions = (
            _selected_implementation_transition(authoritative, active)
        )
        if active_transition_problem:
            raise CliUsageError(
                "active implementation migration is invalid: "
                + active_transition_problem
            )
        active_to_selected_problem, active_to_selected_transitions = (
            _selected_implementation_transition(active, selected_manifest)
        )
        if active_to_selected_problem:
            raise CliUsageError(
                "implementation revalidation differs from the active head: "
                + active_to_selected_problem
            )
        # The historical active migration can be generator-only while the new
        # pool moves adapter pins relative to that active head.  Reconstruction
        # must therefore use the direct active -> selected transition, not the
        # authoritative -> active transition validated above.
        reconstructed_active_pool = (
            _pool_reconstructed_at_selected_implementation(pool_manifest, active)
            if active_to_selected_transitions
            else pool_manifest.model_copy(update={"generator": active.generator})
        )
        if (
            reconstructed_active_pool.manifest_sha256()
            != active.parent_manifest_sha256
        ):
            raise CliUsageError(
                "supplied repinned pool differs from the active migration pool "
                "beyond its explicitly rederived implementation pins"
            )
    pending = _PendingConfiguredMigration(
        prior_report=report,
        prior_report_bytes=prior_report_bytes,
        authoritative_selected=authoritative,
        reproduced_selected=selected_manifest,
        authoritative_pool_sha256=str(old_parent),
        reproduced_pool_sha256=str(new_parent),
        adapter_transitions=adapter_transitions,
    )
    return pending


def _cmd_pipeline_configured(args) -> int:
    """Arbitrary-count workflow; package every accepted candidate honestly."""

    from elt_taskgen.export.local_package import freeze_local_package
    from elt_taskgen.export.package_verification import verify_fresh_local_package
    from elt_taskgen.ingest_manifest import (
        FiveSourceIngestError,
        ingest_selected_sources,
        load_five_source_batch_manifest,
        select_candidate_manifest,
    )
    from elt_taskgen.pipeline_readiness import (
        RUN_PREFLIGHT_BLOCKED_STATE,
        ReadinessProfile,
    )

    spec = _configured_run_spec(args)
    workspace = Path(args.workspace).resolve()
    pool_path = getattr(args, "ingest_manifest", None)
    default_pool = pool_path is None
    if default_pool:
        pool_path = _default_candidate_pool()
        if not pool_path.is_file():
            raise CliUsageError(
                f"configured pipeline: default candidate pool {pool_path} is "
                "missing; pass --ingest-manifest pointing to a pinned "
                "five-source-ingest-v2 candidate pool"
            )
    if getattr(args, "task_id", None):
        raise CliUsageError(
            "--task-id cannot be combined with candidate-count mode; the seeded "
            "source plan is the complete candidate roster"
        )
    if getattr(args, "size", None) is not None:
        raise CliUsageError(
            "--size retains its legacy post-selection meaning and cannot be mixed "
            "with candidate-count mode"
        )
    try:
        pool = load_five_source_batch_manifest(Path(pool_path))
        if default_pool:
            pool = _repin_default_pool_if_stale(Path(pool_path), pool)
        selected = select_candidate_manifest(
            pool,
            candidate_count=spec.candidate_count,
            source_families=spec.source_families,
            source_allocation=spec.source_allocation,
            seed=spec.seed,
        )
    except (FiveSourceIngestError, OSError, ValueError) as exc:
        raise CliUsageError(f"candidate selection refused: {exc}") from None

    requested_run_id = str(getattr(args, "run_id", "") or "")
    run_id = requested_run_id or (
        "run-"
        + sha256_hex(
            canonical_json(
                {
                    "config": spec.fingerprint(),
                    "selection": selected.manifest_sha256(),
                }
            )
        )[:20]
    )
    try:
        validate_task_id_segment(run_id)
    except (TypeError, ValueError) as exc:
        raise CliUsageError(f"unsafe --run-id {run_id!r}: {exc}") from None

    if getattr(args, "reingest", False) and not spec.resume:
        raise CliUsageError("--reingest cannot be combined with resume=false")
    _ensure_firewall_armed(args, workspace)
    migration = _assert_configured_run_identity(
        workspace=workspace,
        run_id=run_id,
        spec=spec,
        pool_manifest=pool,
        selected_manifest=selected,
        reingest=bool(getattr(args, "reingest", False)),
    )
    migration_receipt = None
    migration_receipt_path = None
    authorization = None
    authorization_path = None
    revalidation_artifact = None
    revalidation_artifact_path = None
    supersession = None
    effective_reingest = bool(getattr(args, "reingest", False))
    requested_revalidation_path = getattr(args, "report_revalidation_request", None)
    if requested_revalidation_path is not None and migration is None:
        raise CliUsageError(
            "--report-revalidation-request requires a code-only --reingest migration"
        )
    if (
        requested_revalidation_path is not None
        and migration is not None
        and migration.adapter_transitions
    ):
        raise CliUsageError(
            "--report-revalidation-request is limited to a historical "
            "generator-only migration and cannot be used while adapter "
            "implementation pins move"
        )
    if migration is not None:
        if migration.committed_receipt is not None:
            if requested_revalidation_path is not None:
                raise CliUsageError(
                    "--report-revalidation-request cannot be reused after its "
                    "implementation migration is committed"
                )
            authorization = migration.authorization
            authorization_path = migration.authorization_path
            migration_receipt = migration.committed_receipt
            migration_receipt_path = migration.committed_receipt_path
            # The committed receipt is the durable capability for later normal
            # resumes; no broad user re-ingest authority is inferred.
            effective_reingest = True
        else:
            request, request_path = _load_report_revalidation_request(
                requested_revalidation_path,
                workspace=workspace,
                run_id=run_id,
                spec=spec,
                pending=migration,
            )
            authorization, authorization_path = _prepare_generator_authorization(
                workspace=workspace,
                run_id=run_id,
                spec=spec,
                pool_path=Path(pool_path).absolute(),
                pending=migration,
                report_revalidation_request=request,
                report_revalidation_request_path=request_path,
            )
    migration_kwargs = {}
    try:
        selected, ingested = ingest_selected_sources(
            Path(pool_path),
            workspace=workspace,
            candidate_count=spec.candidate_count,
            source_families=spec.source_families,
            source_allocation=spec.source_allocation,
            seed=spec.seed,
            resume=spec.resume,
            reingest=effective_reingest,
            continue_on_candidate_error=True,
            equivalence_authorization=authorization,
            equivalence_authorization_path=authorization_path,
        )
    except (FiveSourceIngestError, OSError, ValueError) as exc:
        if migration is not None:
            # Never replace the authoritative readiness identity with a partial
            # migration. Authorization/equivalence artifacts are append-only and
            # idempotent; the old readiness remains the recovery checkpoint.
            print(
                "configured implementation revalidation blocked before provider "
                f"workers; prior readiness preserved: {exc}"
            )
            return 2
        # Selection was valid, so persist the precise global blocker even when
        # no candidate could safely be registered.
        workspace.mkdir(parents=True, exist_ok=True)
        path = _write_configured_report(
            workspace=workspace,
            run_id=run_id,
            spec=spec,
            task_ids=tuple(
                entry.expected_task_id for entry in selected.ordered_entries()
            ),
            state="BLOCKED",
            selected_manifest=selected,
            results=tuple(
                {
                    "task_id": entry.expected_task_id,
                    "origin": entry.pool,
                    "ok": False,
                    "state": "blocked",
                    "detail": f"global ingest blocker: {exc}",
                }
                for entry in selected.ordered_entries()
            ),
            blockers=(f"ingest: {exc}",),
        )
        print(f"configured pipeline blocked during ingest; report: {path}")
        return 2

    if migration is not None and migration_receipt is None:
        assert authorization is not None and authorization_path is not None
        equivalence_commit, equivalence_commit_path = _commit_generator_equivalences(
            workspace=workspace,
            run_id=run_id,
            authorization=authorization,
            authorization_path=authorization_path,
        )
        (
            revalidation_artifact,
            revalidation_artifact_path,
            supersession,
        ) = _build_defect_revalidation(
            workspace=workspace,
            run_id=run_id,
            spec=spec,
            pending=migration,
            authorization=authorization,
            authorization_path=authorization_path,
            equivalence_commit=equivalence_commit,
            equivalence_commit_path=equivalence_commit_path,
        )
        migration_receipt, migration_receipt_path = _finalize_generator_migration(
            workspace=workspace,
            run_id=run_id,
            spec=spec,
            pending=migration,
            authorization=authorization,
            authorization_path=authorization_path,
            revalidation_artifact=revalidation_artifact,
            revalidation_artifact_path=revalidation_artifact_path,
            supersession=supersession,
            equivalence_commit=equivalence_commit,
            equivalence_commit_path=equivalence_commit_path,
        )
    if migration_receipt is not None:
        migration_kwargs = {
            "migration_receipt": migration_receipt,
            "migration_receipt_path": migration_receipt_path,
        }

    task_ids = tuple(
        entry.expected_task_id for entry in selected.ordered_entries()
    )
    runnable_task_ids = ingested.task_ids
    ingest_failures = tuple(
        {
            "task_id": outcome.task_id,
            "origin": outcome.origin.value,
            "ok": False,
            "state": "ingest_failed",
            "detail": outcome.detail,
            "usd": 0.0,
        }
        for outcome in ingested.candidate_outcomes
        if outcome.state == "failed"
    )
    if spec.budget_total is not None:
        from elt_taskgen.review.budget_ledger import initialize

        budget = initialize(
            run_id,
            float(spec.budget_total),
            workspace,
            per_task_limit_usd=float(spec.budget_per_task),
        )
        uncertain = budget.mark_outstanding_uncertain()
        if uncertain:
            print(
                f"budget recovery: {uncertain} prior in-flight reservation(s) "
                "remain uncertain and continue to consume headroom"
            )
    (
        hydrated_packages,
        hydrated_package_receipts,
        package_hydration_blockers,
    ) = _hydrate_configured_packages(
        workspace=workspace,
        run_id=run_id,
        spec=spec,
        task_ids=runnable_task_ids,
    )
    adopted_results = _adopted_package_results(
        runnable_task_ids, hydrated_packages
    )
    adopted_task_ids = tuple(row["task_id"] for row in adopted_results)
    adopted_set = frozenset(adopted_task_ids)
    non_adopted_task_ids = tuple(
        task_id for task_id in runnable_task_ids if task_id not in adopted_set
    )
    terminal_rejection_results = _terminal_rejection_results(
        workspace, non_adopted_task_ids
    )
    terminal_rejection_ids = frozenset(
        row["task_id"] for row in terminal_rejection_results
    )
    pending_task_ids = tuple(
        task_id
        for task_id in non_adopted_task_ids
        if task_id not in terminal_rejection_ids
    )
    initial_results = (
        tuple(ingest_failures)
        + adopted_results
        + terminal_rejection_results
    )
    running_report = _write_configured_report(
        workspace=workspace,
        run_id=run_id,
        spec=spec,
        task_ids=task_ids,
        state="RUNNING",
        selected_manifest=selected,
        results=initial_results,
        ingest_outcomes=ingested.candidate_outcomes,
        packages=hydrated_packages,
        package_receipts=hydrated_package_receipts,
        blockers=package_hydration_blockers,
        **migration_kwargs,
    )
    print(
        f"selected {len(task_ids)} candidate(s), registered "
        f"{len(runnable_task_ids)}, adopted {len(adopted_task_ids)} current "
        f"package(s), preserved {len(terminal_rejection_ids)} terminal "
        f"rejection(s), pending {len(pending_task_ids)}; readiness: "
        f"{running_report}"
    )

    if spec.profile is ReadinessProfile.DRAFT:
        path = _write_configured_report(
            workspace=workspace,
            run_id=run_id,
            spec=spec,
            task_ids=task_ids,
            state=("COMPLETE_WITH_ISSUES" if ingest_failures else "COMPLETE"),
            selected_manifest=selected,
            results=ingest_failures,
            ingest_outcomes=ingested.candidate_outcomes,
            blockers=tuple(
                f"{row['task_id']}: {row['detail']}" for row in ingest_failures
            ),
            **migration_kwargs,
        )
        print(f"draft-only run complete (no provider stages); report: {path}")
        return 2 if ingest_failures else 0

    # A fully packaged resume is local verification, not new live work. Only
    # candidates without adopted evidence need provider configuration and
    # admission preflight; those same candidates are the only worker inputs.
    provider_problems = (
        _configured_live_provider_problems(args, spec)
        + _configured_canonical_runtime_problems(spec)
        if pending_task_ids
        else ()
    )
    if provider_problems:
        detail = "provider preflight: " + "; ".join(provider_problems)
        blocked_results = initial_results + tuple(
            {
                "task_id": task_id,
                "ok": False,
                "state": RUN_PREFLIGHT_BLOCKED_STATE,
                "detail": detail,
                "usd": 0.0,
            }
            for task_id in pending_task_ids
        )
        path = _write_configured_report(
            workspace=workspace,
            run_id=run_id,
            spec=spec,
            task_ids=task_ids,
            state="BLOCKED",
            selected_manifest=selected,
            results=blocked_results,
            ingest_outcomes=ingested.candidate_outcomes,
            blockers=tuple(
                f"provider preflight: {problem}" for problem in provider_problems
            )
            + package_hydration_blockers,
            packages=hydrated_packages,
            package_receipts=hydrated_package_receipts,
            **migration_kwargs,
        )
        print(
            "configured pipeline blocked before provider workers: "
            + "; ".join(provider_problems)
            + f"; report: {path}"
        )
        return 2

    if spec.profile in {ReadinessProfile.RELEASE_READY, ReadinessProfile.RELEASE}:
        if not runnable_task_ids:
            path = _write_configured_report(
                workspace=workspace,
                run_id=run_id,
                spec=spec,
                task_ids=task_ids,
                state="COMPLETE_WITH_ISSUES",
                selected_manifest=selected,
                results=ingest_failures,
                ingest_outcomes=ingested.candidate_outcomes,
                blockers=tuple(
                    f"{row['task_id']}: {row['detail']}"
                    for row in ingest_failures
                ),
                **migration_kwargs,
            )
            print(f"no candidate TaskIR was created; report: {path}")
            return 2
        legacy = argparse.Namespace(**vars(args))
        legacy.task_id = list(runnable_task_ids)
        legacy.ingest_manifest = None
        legacy.reingest = False
        legacy.size = len(runnable_task_ids)
        legacy.workers = spec.workers
        legacy.max_repair_rounds = spec.max_repair_rounds
        legacy.repair_attempts = spec.repair_attempts
        legacy.budget_per_task = spec.budget_per_task
        legacy.budget_total = spec.budget_total
        legacy.agents_config = Path(spec.agents_config) if spec.agents_config else None
        legacy.admission_reference = spec.admission_reference
        legacy.require_sources = ",".join(
            origin.value
            for origin, count in selected.source_allocation.items()
            if count
        )
        legacy.allow_structural_difficulty = False
        legacy.empirical = True
        legacy.require_empirical = True
        legacy.destination = spec.destination
        legacy.extra_destinations = spec.extra_destinations
        # `args` may contain None for every CLI retry flag while the typed
        # --run-config supplies non-default values.  This branch delegates to
        # the legacy corpus coordinator, so copy the validated values exactly
        # as `_configured_results` does for local-ready/packaged/calibrated.
        legacy.http_retries = spec.http_retries
        legacy.schema_retries = spec.schema_retries
        legacy.http_timeout_seconds = spec.http_timeout_seconds
        legacy.http_backoff_seconds = spec.http_backoff_seconds
        legacy.configured_run_spec = spec.model_dump(mode="json")
        legacy.configured_run_id = run_id
        legacy.allow_partial_candidates = True
        legacy.global_budget_run_id = (
            run_id if spec.budget_total is not None else ""
        )
        legacy.global_budget_total = spec.budget_total
        legacy.release_dir = (
            Path(spec.export_dir) if spec.profile is ReadinessProfile.RELEASE else None
        )
        code = _cmd_pipeline_legacy(legacy)
        if ingest_failures and code == 0:
            code = 2
        state = "COMPLETE" if code == 0 else "COMPLETE_WITH_ISSUES"
        release_packages = (
            {
                task_id: Path(spec.export_dir).resolve()
                for task_id in runnable_task_ids
            }
            if spec.profile is ReadinessProfile.RELEASE
            and (Path(spec.export_dir).resolve() / "release_manifest.json").is_file()
            else None
        )
        path = _write_configured_report(
            workspace=workspace,
            run_id=run_id,
            spec=spec,
            task_ids=task_ids,
            state=state,
            selected_manifest=selected,
            results=ingest_failures,
            ingest_outcomes=ingested.candidate_outcomes,
            packages=release_packages,
            blockers=tuple(
                f"{row['task_id']}: {row['detail']}" for row in ingest_failures
            ),
            **migration_kwargs,
        )
        print(f"configured {spec.profile.value} report: {path}")
        return code

    target = spec.profile.until_stage
    results = list(initial_results)
    if pending_task_ids:
        results.extend(
            _configured_results(
                args,
                spec=spec,
                task_ids=pending_task_ids,
                run_id=run_id,
                target_stage=target,
            )
        )
    packages: dict[str, Path] = dict(hydrated_packages)
    package_receipts: dict[str, Path] = dict(hydrated_package_receipts)
    if spec.profile is ReadinessProfile.PACKAGED:
        export_root = Path(spec.export_dir).resolve()
        engine = _open_engine(workspace, max_repair_rounds=0)
        try:
            for row in results:
                if not row.get("ok"):
                    continue
                if row.get("package_adopted"):
                    continue
                task_id = str(row["task_id"])
                try:
                    task = engine.load_task(task_id)
                    package_path = export_root / task_id
                    freeze_local_package(engine, task, package_path)
                    receipt_path = (
                        workspace
                        / "state"
                        / "pipeline_runs"
                        / run_id
                        / "package_verifications"
                        / f"{task_id}.json"
                    )
                    verification = verify_fresh_local_package(
                        package_path,
                        receipt_path=receipt_path,
                    )
                    if not verification.verified:
                        raise ValueError(
                            "fresh-copy package verification failed: "
                            + "; ".join(verification.failures[:3])
                        )
                    packages[task_id] = package_path
                    package_receipts[task_id] = receipt_path
                    row["state"] = "packaged"
                except (OSError, TypeError, ValueError) as exc:
                    packages.pop(task_id, None)
                    package_receipts.pop(task_id, None)
                    row["ok"] = False
                    row["state"] = "package_error"
                    row["detail"] = str(exc)
                # Fresh verification may take long enough for an interruption
                # to matter.  Checkpoint each outcome before starting the next
                # package so an already verified receipt is durable progress.
                _write_configured_report(
                    workspace=workspace,
                    run_id=run_id,
                    spec=spec,
                    task_ids=task_ids,
                    state="RUNNING",
                    selected_manifest=selected,
                    results=tuple(results),
                    ingest_outcomes=ingested.candidate_outcomes,
                    packages=packages,
                    package_receipts=package_receipts,
                    blockers=tuple(
                        f"{item['task_id']}: "
                        f"{item.get('detail') or item['state']}"
                        for item in results
                        if item.get("state") == "package_error"
                    ),
                    **migration_kwargs,
                )
        finally:
            engine.close()

    operational = [
        row
        for row in results
        if row.get("state")
        in {
            "blocked",
            "infrastructure",
            "incomplete_protocol",
            "pending_adjudication",
            "protocol_failure",
            "usage_error",
            "package_error",
            "ingest_failed",
            "stale",
        }
    ]
    rejected = [row for row in results if row.get("state") == FINAL_REJECTED]
    state = "COMPLETE" if not operational and not rejected else "COMPLETE_WITH_ISSUES"
    path = _write_configured_report(
        workspace=workspace,
        run_id=run_id,
        spec=spec,
        task_ids=task_ids,
        state=state,
        selected_manifest=selected,
        results=tuple(results),
        ingest_outcomes=ingested.candidate_outcomes,
        packages=packages,
        package_receipts=package_receipts,
        blockers=tuple(
            f"{row['task_id']}: {row.get('detail') or row['state']}"
            for row in operational
        ),
        **migration_kwargs,
    )
    from elt_taskgen.pipeline_readiness import PipelineReadinessReport

    terminal_report = PipelineReadinessReport.model_validate_json(path.read_bytes())
    counts = terminal_report.counts
    print(
        f"configured pipeline: requested={counts.requested} "
        f"created={counts.candidates_created} selected={counts.selected} "
        f"accepted={counts.accepted} packaged={counts.packaged} "
        f"rejected={counts.rejected} failed={counts.failed} "
        f"blocked={counts.blocked}"
    )
    print(f"readiness report: {path}")
    if operational:
        return 2
    if rejected:
        return 1
    return 0


def cmd_pipeline(args) -> int:
    """Dispatch legacy corpus selection or the arbitrary-count run contract."""

    configured = any(
        getattr(args, name, None) is not None
        for name in ("run_config", "candidate_count", "readiness_profile")
    )
    if configured:
        return _cmd_pipeline_configured(args)
    return _cmd_pipeline_legacy(args)


def cmd_pipeline_status(args) -> int:
    """Read-only revalidation of a persisted configured-run report."""

    from elt_taskgen.pipeline_readiness import (
        PipelineReadinessReport,
        ReadinessProfile,
        _read_bounded_regular_bytes,
        cost_breakdown_from_budget_snapshot,
        cost_breakdown_payload,
        task_readiness,
    )

    workspace = Path(args.workspace).resolve()
    run_id = str(args.run_id)
    try:
        validate_task_id_segment(run_id)
    except (TypeError, ValueError) as exc:
        raise CliUsageError(f"unsafe --run-id {run_id!r}: {exc}") from None
    path = workspace / "state" / "pipeline_runs" / run_id / "readiness.json"
    try:
        report = PipelineReadinessReport.model_validate_json(
            _read_bounded_regular_bytes(
                path,
                label="configured readiness report",
                max_bytes=32 * 1024 * 1024,
            )
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise CliUsageError(f"readiness report is missing or invalid: {exc}") from None
    problems: list[str] = []
    if report.run_id != run_id:
        problems.append(
            f"embedded run_id {report.run_id!r} differs from requested {run_id!r}"
        )
    if report.config.fingerprint() != report.config_sha256:
        problems.append("configuration fingerprint does not match embedded config")
    ordered_task_ids = tuple(task.task_id for task in report.tasks)
    if len(set(ordered_task_ids)) != len(ordered_task_ids):
        problems.append("recorded task roster contains duplicate task ids")
    recorded_task_ids = set(ordered_task_ids)
    unknown_runtime_targets = sorted(
        set(report.config.runtime_difficulty_reports) - recorded_task_ids
    )
    if unknown_runtime_targets:
        problems.append(
            "runtime evidence is configured for tasks outside the recorded "
            f"run roster: {unknown_runtime_targets}"
        )

    current_costs = None
    current_cost_payload: dict[str, Any] = dict(report.costs)
    if report.config.budget_total is not None:
        from elt_taskgen.review.budget_ledger import BudgetLedger, BudgetLedgerError

        budget_path = workspace / "state" / "pipeline_budget.sqlite3"
        try:
            metadata = budget_path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise BudgetLedgerError(
                    "budget ledger must be a regular non-symlink file"
                )
            snapshot = BudgetLedger(
                database_path=budget_path,
                run_id=report.run_id,
            ).snapshot()
            current_costs = cost_breakdown_from_budget_snapshot(
                snapshot,
                task_ids=ordered_task_ids,
                expected_run_id=report.run_id,
            )
            if not math.isclose(
                float(current_costs.total_limit_usd or 0.0),
                float(report.config.budget_total),
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                raise ValueError(
                    "durable total budget differs from configured batch limit"
                )
            if current_costs.per_task_limit_usd is None or not math.isclose(
                float(current_costs.per_task_limit_usd),
                float(report.config.budget_per_task),
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                raise ValueError(
                    "durable per-task budget differs from configured task limit"
                )
            current_cost_payload = cost_breakdown_payload(current_costs)
            if current_cost_payload != report.costs:
                problems.append(
                    "durable budget ledger has changed since the readiness "
                    "report was written"
                )
        except (OSError, TypeError, ValueError, BudgetLedgerError) as exc:
            problems.append(f"durable budget ledger is invalid: {exc}")

    def package_receipt_for(recorded) -> Path | None:
        relative = str(recorded.package_verification_receipt or "")
        if not relative:
            return None
        try:
            candidate = _bound_workspace_path(
                workspace,
                relative,
                label=f"{recorded.task_id} package verification receipt",
            )
        except CliUsageError as exc:
            problems.append(str(exc))
            return None
        try:
            return _canonical_configured_package_receipt(
                workspace=workspace,
                run_id=run_id,
                task_id=recorded.task_id,
                candidate=candidate,
            )
        except CliUsageError as exc:
            problems.append(str(exc))
            return None

    engine = _open_engine(workspace, max_repair_rounds=0)
    try:
        refreshed = []
        for recorded in report.tasks:
            try:
                task = engine.load_task(recorded.task_id)
            except (OSError, ValueError, EngineError) as exc:
                problems.append(f"{recorded.task_id}: TaskIR unavailable: {exc}")
                continue
            package = Path(recorded.package_path) if recorded.package_path else None
            package_receipt = package_receipt_for(recorded)
            task_cost = (
                current_costs.by_task.get(recorded.task_id)
                if current_costs is not None
                else None
            )
            current = task_readiness(
                engine,
                task,
                package_path=package,
                package_receipt_path=package_receipt,
                recorded_cost=recorded.recorded_cost,
                reserved_cost=recorded.reserved_cost,
                uncertain_cost=recorded.uncertain_cost,
                reserved_or_uncertain_cost=recorded.reserved_or_uncertain_cost,
                final_request_overrun=recorded.final_request_overrun,
                cost_breakdown=task_cost,
                action=recorded.actions,
                spec=report.config,
                attempts=recorded.attempts,
            )
            refreshed.append(current)
            if current.task_content_hash != recorded.task_content_hash:
                problems.append(f"{recorded.task_id}: task identity changed")
            for stage in current.stages:
                if stage.state.value == "STALE":
                    problems.append(
                        f"{recorded.task_id}:{stage.stage.value}: {stage.reason}"
                    )
            for blocker in current.precise_blockers:
                detail = f"{recorded.task_id}: {blocker}"
                if detail not in problems:
                    problems.append(detail)
            runtime = current.runtime_evidence
            if runtime.configured and not runtime.certification_verified:
                problems.append(
                    f"{recorded.task_id}: "
                    + (
                        runtime.certification_reason
                        or "runtime certification did not verify"
                    )
                )
            if runtime.configured and not runtime.difficulty_verified:
                problems.append(
                    f"{recorded.task_id}: "
                    + (
                        runtime.difficulty_reason
                        or "runtime-certified difficulty did not verify"
                    )
                )

        def milestone_ok(task) -> bool:
            if report.profile is ReadinessProfile.DRAFT:
                return bool(task.stages and task.stages[0].state.value == "PASS")
            if report.profile is ReadinessProfile.LOCAL_READY:
                return task.locally_evaluator_ready
            if report.profile is ReadinessProfile.PACKAGED:
                return task.packaged_for_evaluation
            if report.profile is ReadinessProfile.CALIBRATED:
                return task.empirically_calibrated
            if report.profile is ReadinessProfile.RELEASE_READY:
                return task.release_ready
            # RELEASE_READY stops at the audit gate. A release-profile status
            # must additionally revalidate the immutable RELEASE ledger row;
            # runtime certification remains an independent, opt-in check.
            return task.release_ready and any(
                stage.stage is StageName.RELEASE
                and stage.state.value == "PASS"
                for stage in task.stages
            )

        not_ready = [task.task_id for task in refreshed if not milestone_ok(task)]
        if not_ready:
            problems.append(
                f"tasks not at {report.profile.value!r} milestone: {not_ready}"
            )
    finally:
        engine.close()
    payload = {
        "run_id": report.run_id,
        "profile": report.profile.value,
        "recorded_state": report.state,
        "requested": report.counts.requested,
        "tasks_recorded": len(report.tasks),
        "runtime_certified": sum(
            task.runtime_certified for task in refreshed
        ),
        "runtime_difficulty_certified": sum(
            task.runtime_evidence.difficulty_verified for task in refreshed
        ),
        "costs": current_cost_payload,
        "valid": not problems,
        "problems": problems,
        "report": str(path),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if not problems else 2


def cmd_verify_local_package(args) -> int:
    from elt_taskgen.export.local_package import verify_local_package

    verification = verify_local_package(Path(args.package))
    print(json.dumps(verification.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0 if verification.ok else 2


#: Accepted --variants tokens (Phase B): short forms match the variant id
#: suffixes (__el / __t); long forms match TaskVariant values.
_VARIANT_TOKENS: dict[str, TaskVariant] = {
    "full": TaskVariant.FULL,
    "el": TaskVariant.EXTRACT_LOAD,
    "extract_load": TaskVariant.EXTRACT_LOAD,
    "t": TaskVariant.TRANSFORM,
    "transform": TaskVariant.TRANSFORM,
}

#: `--variants accepted` (the DEFAULT) means the complete EL/T pair; FULL is a
#: legacy diagnostic, never part of corpus selection or release.
_ACCEPTED_TOKEN = "accepted"


def _parse_variants(spec: str) -> tuple[TaskVariant, ...]:
    """Parse a --variants comma list ('full,el,t'); fail closed on junk."""
    out: list[TaskVariant] = []
    for token in spec.split(","):
        token = token.strip().lower()
        if not token:
            continue
        variant = _VARIANT_TOKENS.get(token)
        if variant is None:
            raise CliUsageError(
                f"unknown variant {token!r} (choose from: full, el, t)"
            )
        if variant not in out:
            out.append(variant)
    if not out:
        raise CliUsageError("--variants must name at least one variant")
    return tuple(out)


def cmd_export(args) -> int:
    from elt_taskgen.export import eltbench as eltbench_mod
    from elt_taskgen.reference import gold as gold_mod

    spec = (getattr(args, "variants", None) or _ACCEPTED_TOKEN).strip().lower()
    explicit = spec != _ACCEPTED_TOKEN
    if explicit:
        try:
            variants = _parse_variants(spec)
        except ValueError as exc:
            print(f"error: {exc}")
            return 2
    else:
        variants = RLVR_TASK_VARIANTS
    engine = _open_engine(Path(args.workspace).resolve())
    try:
        task = engine.load_task(args.task_id)
        try:
            accepted = _accepted_variants(engine, task)
        except ValueError as exc:
            # An internally inconsistent battery record (claims acceptance, never ran
            # a roster gate) is a refusal to answer, not a crash: this used to end in
            # a raw ValueError traceback with exit 1, the code reserved for rejection.
            print(f"error: {exc}")
            print(
                "run the pipeline (validate-el / validate-t) so the batteries "
                "re-attest at the current roster, then export again"
            )
            return 2
        refused = [
            v for v in variants if v is not TaskVariant.FULL and v.value not in accepted
        ]
        if refused:
            for variant in refused:
                print(
                    f"error: variant {variant.value!r} has no passing battery on "
                    f"stage {variant_gate_stage(variant).value!r} at content hash "
                    f"{task.content_hash()[:12]} — refusing to emit a bundle "
                    "nothing certified (run the pipeline; the variant gate "
                    "stage emits and measures the bundle itself)"
                )
            if not explicit:
                print("error: the default export requires the complete EL/T pair")
            return 2
        if TaskVariant.FULL in variants and TaskVariant.FULL.value not in accepted:
            print(
                "WARNING: the parent (FULL) battery has not passed at this "
                "content hash; the emitted bundle is a working artifact, not a "
                "releasable one (freeze_release will refuse it)."
            )
        gold = gold_mod.load_gold(_answer_key_dir(engine, task))
        variants_root = engine.task_dir(task.task_id) / "variants"
        emits_combined_parent = (
            TaskVariant.FULL in variants
            or all(variant in variants for variant in RLVR_TASK_VARIANTS)
        )
        if emits_combined_parent:
            eltbench_mod.export_task(
                task,
                gold,
                _public_dir(engine, task),
                _answer_key_dir(engine, task),
                destination=args.destination,
            )
            print(f"exported public bundle: {_public_dir(engine, task)}")
            print(f"private answer key:     {_answer_key_dir(engine, task)}")
        if TaskVariant.FULL in variants:
            eltbench_mod.emit_variant(
                task, gold, TaskVariant.FULL, variants_root / TaskVariant.FULL.value
            )
        # SUBTASK BUNDLES ARE NOT RE-EMITTED HERE: the variant gate stage emits and
        # measures the bundle, and TRANSFORM .duckdb bytes are not stable across
        # builds. Export VERIFIES the certified bundle.
        for variant in variants:
            if variant is TaskVariant.FULL:
                continue
            out_dir = variants_root / variant.value
            if not (out_dir / "task").is_dir():
                print(
                    f"error: variant {variant.value!r} has a passing battery but "
                    f"no bundle at {out_dir}; re-run "
                    f"'elt-taskgen validate-{'el' if variant is TaskVariant.EXTRACT_LOAD else 't'}' "
                    "— export never regenerates a measured bundle"
                )
                return 2
            print(
                f"certified {variant.value} variant (emitted and measured by "
                f"stage {variant_gate_stage(variant).value!r}): {out_dir}"
            )
        return 0
    finally:
        engine.close()


def cmd_score(args) -> int:
    """Score one LEGACY schema-1/2 unit's DuckDB warehouse.

    The release is self-describing (reward.json names the evaluator and the
    answer-key paths), so a training loop needs no second scorer of its own. Exit 0
    when scoring completed (the REWARD is on stdout, not in the exit code), 2 when
    the unit could not be scored at all."""
    try:
        from elt_taskgen.export import serve as serve_mod
    except ImportError as exc:  # pragma: no cover - contract guard
        print(f"error: release scoring is unavailable in this build: {exc}")
        return 2

    unit_id = getattr(args, "unit", None) or getattr(args, "task_id", None)
    if not unit_id:
        print("error: pass --unit <parent>__el|<parent>__t")
        return 2
    release_dir = Path(args.release).resolve()
    duckdb_path = Path(args.duckdb).resolve()
    if not duckdb_path.is_file():
        print(f"error: no DuckDB file at {duckdb_path}")
        return 2
    try:
        unit = serve_mod.load_release_unit(release_dir, str(unit_id))
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}")
        return 2

    import duckdb  # local: only this subcommand opens a submitted warehouse

    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        result = serve_mod.score_release_unit(
            unit,
            con,
            population=getattr(args, "population", None) or "primary",
            source_schema=getattr(args, "source_schema", None) or "main",
            mart_schema=getattr(args, "mart_schema", None) or None,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 2
    finally:
        con.close()

    payload = (
        result.model_dump(mode="json")
        if hasattr(result, "model_dump")
        else {"reward": getattr(result, "reward", None)}
    )
    if getattr(args, "json", False):
        print(canonical_json(payload))
    else:
        print(f"unit:       {unit_id}")
        print(f"population: {getattr(args, 'population', None) or 'primary'}")
        print(f"reward:     {payload.get('reward')}")
        for key in ("stage1_pass", "stage1_detail", "mart_scores"):
            if key in payload:
                print(f"{key + ':':<12}{payload[key]}")
    return 0


def _training_package(args):
    from elt_taskgen.training import load_workspace_package

    try:
        return load_workspace_package(Path(args.release).resolve(), args.task_id)
    except Exception as error:
        raise CliUsageError("training package could not be verified") from error


def cmd_training_install(args) -> int:
    from elt_taskgen.training import WorkspaceLifecycleError, install_workspace

    package = _training_package(args)
    try:
        attempt = install_workspace(package, Path(args.attempt).resolve())
    except WorkspaceLifecycleError as error:
        raise CliUsageError(error.code.value) from None
    print(canonical_json({"attempt": str(attempt.root), "claim": "deterministic_protocol_workflow_proxy", "task_id": package.task_id}))
    return 0


def cmd_training_seal(args) -> int:
    from elt_taskgen.training import WorkspaceLifecycleError, load_attempt_workspace, seal_workspace

    package = _training_package(args)
    try:
        attempt = load_attempt_workspace(package, Path(args.attempt).resolve())
        sealed = seal_workspace(attempt, package, Path(args.output).resolve())
    except WorkspaceLifecycleError as error:
        raise CliUsageError(error.code.value) from None
    print(canonical_json({"artifact_sha256": sealed.submission.artifact_sha256, "seal": str(sealed.root), "seal_sha256": sealed.seal_sha256, "task_id": package.task_id}))
    return 0


def cmd_training_score(args) -> int:
    from elt_taskgen.training import DbtRuntimeConfig, WorkspaceLifecycleError, load_attempt_workspace, score_workspace, seal_workspace, warehouse_profile

    package = _training_package(args)
    profile = warehouse_profile(args.destination)
    if profile.destination.value != package.destination.value:
        raise CliUsageError("destination personality does not match the release")
    attempts = Path(args.attempts_dir).resolve()
    attempts.mkdir(parents=True, exist_ok=True)
    runtime = DbtRuntimeConfig(python=Path(args.dbt_python).resolve(), manifest=Path(args.dbt_manifest).resolve())
    if args.candidate_workspace is not None:
        try:
            attempt = load_attempt_workspace(package, Path(args.candidate_workspace).resolve())
            with tempfile.TemporaryDirectory(prefix="training-cli-seal-", dir=attempts) as root:
                sealed = seal_workspace(attempt, package, Path(root) / "sealed")
                result = score_workspace(package, sealed, attempts_root=attempts, runtime_config=runtime)
        except WorkspaceLifecycleError as error:
            raise CliUsageError(error.code.value) from None
    else:
        if not args.seal_sha256:
            raise CliUsageError("--seal requires --seal-sha256 authentication")
        result = score_workspace(package, Path(args.seal).resolve(), expected_seal_sha256=args.seal_sha256, attempts_root=attempts, runtime_config=runtime)
    print(canonical_json(result.model_dump(mode="json")))
    return 0 if result.reward is not None else 2


def cmd_training_inspect(args) -> int:
    from elt_taskgen.training import WorkspaceLifecycleError, load_sealed_workspace, warehouse_profile

    package = _training_package(args)
    profile = warehouse_profile(args.destination or package.destination.value)
    payload = {"claim": "deterministic_protocol_workflow_proxy", "destination": profile.destination.value, "profile": profile.model_dump(mode="json"), "release_id": package.manifest.release_id, "task_id": package.task_id}
    if args.seal is not None:
        try:
            sealed = load_sealed_workspace(Path(args.seal).resolve(), expected_seal_sha256=args.seal_sha256)
        except WorkspaceLifecycleError as error:
            raise CliUsageError(error.code.value) from None
        payload["submission"] = sealed.submission.model_dump(mode="json")
        payload["seal_sha256"] = sealed.seal_sha256
    print(canonical_json(payload))
    return 0


def cmd_semantic_score(args) -> int:
    """Score one combined release locally using evaluator-private DuckDB.

    A measured attempt exits 0 even when its reward is zero.  Missing or
    inconsistent release inputs are harness failures and exit 2.  No Airbyte,
    dbt, or cloud destination is started by this command.
    """
    from elt_taskgen.semantic import (
        SemanticHarnessError,
        SemanticLimits,
        SemanticPackageError,
        load_semantic_package,
        score_semantic_text,
    )

    submission_arg = str(args.submission)
    try:
        if submission_arg == "-":
            submission_text = sys.stdin.read()
        else:
            submission_path = Path(submission_arg).resolve()
            if not submission_path.is_file():
                print(f"error: no semantic submission file at {submission_path}")
                return 2
            submission_text = submission_path.read_text(encoding="utf-8")
        package = load_semantic_package(
            Path(args.release).resolve(), str(args.task_id)
        )
        limits = SemanticLimits(
            timeout_seconds=float(args.timeout_seconds),
            memory_limit_mb=int(args.memory_limit_mb),
            threads=1,
            max_result_rows_per_mart=int(args.max_result_rows),
            max_result_bytes_per_mart=int(args.max_result_bytes),
        )
        result = score_semantic_text(
            package,
            submission_text,
            populations=getattr(args, "population", None),
            limits=limits,
            strict_diagnostic=bool(getattr(args, "strict_diagnostic", False)),
            strict_shadow=bool(getattr(args, "strict_shadow", False)),
        )
    except (OSError, ValueError, SemanticPackageError, SemanticHarnessError) as exc:
        print(f"error: {exc}")
        return 2

    payload = result.model_dump(mode="json")
    if getattr(args, "json", False):
        # Human-readable JSON is also deterministic.  This command is often
        # inspected directly during curation, so never collapse it to one line.
        print(readable_json(payload))
    else:
        print(f"task:              {result.task_id}")
        print(f"valid submission:  {result.valid_submission}")
        print(f"semantic EL:       {result.semantic_el_reward}")
        print(f"semantic T:        {result.semantic_t_reward}")
        print(f"gated reward:      {result.reward}")
        if result.error_code:
            print(f"error code:         {result.error_code}")
        for population, score in result.populations.items():
            print(
                f"{population}: EL={score.el_reward} "
                f"T={score.t_reward} gated={score.reward}"
            )
        if result.strict_diagnostic_ran:
            # Diagnostic only: a strict mismatch never changes the reward or
            # the exit code.
            for population, score in result.populations.items():
                for label, marts in (
                    ("strict", score.strict_marts),
                    ("strict shadow", score.strict_shadow_marts),
                ):
                    for mart, diag in sorted(marts.items()):
                        verdict = (
                            "match"
                            if diag.strict_match
                            else f"MISMATCH({diag.mismatch_code})"
                        )
                        print(f"{label} {population}/{mart}: {verdict}")
                        if diag.submission_columns:
                            types = ", ".join(
                                f"{col.name}:{col.declared_type}"
                                for col in diag.submission_columns
                            )
                            print(f"{label} types {mart}: {types}")
    return 0


def cmd_verify_release(args) -> int:
    """Re-verify a frozen release against the rules its OWN manifest records.

    `shasum -c checksums.sha256` covers only the byte-pinned files — the
    census-pinned .duckdb warehouses are comment lines to it. Exit 0 = verified,
    1 = a pin failed, 2 = nothing to verify."""
    from elt_taskgen.export import release as release_mod

    release_dir = getattr(args, "release", None)
    if release_dir is None:
        release_dir = Path(args.workspace).resolve() / "release"
    release_dir = Path(release_dir).resolve()
    if not (release_dir / "release_manifest.json").is_file():
        print(f"error: no release_manifest.json under {release_dir}")
        return 2
    result = release_mod.verify_release(release_dir)
    print(
        f"{release_dir}: {result.files_checked} pinned file(s) "
        f"({result.byte_pinned} by bytes, {result.census_pinned} by warehouse "
        f"census) — {'OK' if result.ok else 'FAILED'}"
    )
    if not result.ok:
        for failure in result.failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


def cmd_bench_verify_corpus(args) -> int:
    """Strict-parse every committed YAML/JSON artifact in an ELT-Bench checkout.

    The corpus gate for IR-010: the anchor adapter repairs broken YAML silently
    and the runtime evaluator only strict-parses at evaluation time — after the
    warehouse has been touched. Run this BEFORE any release or runtime run that
    consumes the checkout, so a committed artifact that stopped parsing fails
    loudly up front. Exit 0 = every artifact parses; 2 = offenders found, or
    nothing to scan (an empty corpus is never a pass)."""
    from elt_taskgen.verification import corpus as corpus_mod

    bench_root = Path(args.bench_root).resolve()
    corpus_root = bench_root / "elt-bench"
    if not corpus_root.is_dir():
        raise CliUsageError(f"no elt-bench/ directory under {bench_root}")
    if not any(
        (corpus_root / name).is_dir()
        for name in ("snowflake", "databricks", "redshift")
    ):
        raise CliUsageError(
            f"{corpus_root} contains none of the destination dirs "
            "snowflake/databricks/redshift — not an ELT-Bench corpus"
        )
    roots = [corpus_root]
    evaluation_root = bench_root / "evaluation"
    if evaluation_root.is_dir():
        roots.append(evaluation_root)
    scanned = 0
    failures: list[tuple[Path, str]] = []
    for root in roots:
        scanned += corpus_mod.count_artifacts(root)
        failures.extend(corpus_mod.strict_parse_failures(root))
    if scanned == 0:
        raise CliUsageError(
            f"zero YAML/JSON artifacts under {bench_root} — an empty corpus "
            "cannot pass the gate"
        )
    for path, message in failures:
        print(f"{path.relative_to(bench_root)}: {message}", file=sys.stderr)
    print(f"scanned {scanned} artifacts, {len(failures)} failures")
    return 0 if not failures else 2


# --- Combined ELT-Bench runtime -------------------------------------------

def _runtime_json(path: Path, *, label: str) -> dict:
    """Read a credential/config object without echoing its secret contents."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CliUsageError(f"invalid {label} JSON: {path}") from None
    if not isinstance(value, dict):
        raise CliUsageError(f"invalid {label} JSON: {path}")
    return value


def _runtime_yaml(path: Path, *, label: str) -> dict:
    import yaml

    try:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise CliUsageError(f"invalid {label} YAML: {path}") from None
    if not isinstance(value, dict):
        raise CliUsageError(f"invalid {label} YAML: {path}")
    return value


@contextmanager
def _runtime_secret_output(path: Path) -> Iterator[Callable[[dict], None]]:
    """Reserve one owner-only credential path before any external mutation.

    Provisioning rotates or creates cloud credentials.  Reserving the output
    path first prevents an existing file (or a concurrent creator) from being
    discovered only after that irreversible external change has happened.
    An uncommitted placeholder is removed on every failure path.
    """
    import os

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise CliUsageError(f"credential output already exists: {path}") from None
    committed = False

    def commit(value: dict) -> None:
        nonlocal committed
        if committed:
            raise RuntimeError("runtime credential output was already committed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        with os.fdopen(os.dup(descriptor), "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
        committed = True

    try:
        yield commit
    finally:
        os.close(descriptor)
        if not committed:
            path.unlink(missing_ok=True)


def _write_runtime_secret(path: Path, value: dict) -> None:
    """Create one credential file exclusively and mode it owner-only."""

    with _runtime_secret_output(path) as commit:
        commit(value)


def _refuse_confined_work_dir(work_dir: Path, release: Path) -> Path:
    """Validate the work directory before ``runtime prepare`` reads secrets.

    The attempt copy cannot be inside the repository runs root, the selected
    release, or any tree containing ``release_manifest.json``. Resolve paths so
    unrelated directories merely named ``runs`` remain valid. Refuse violations
    as usage errors before reading credentials or the release. Return the
    resolved work directory."""
    from elt_taskgen.review.tools.registry import (
        path_under_release_root,
        path_under_runs,
    )

    resolved = Path(work_dir).resolve()
    release = Path(release).resolve()
    if path_under_runs(resolved):
        raise CliUsageError(
            "--work-dir may not lie under the repository's runs/ directory "
            "(live credentials are installed there; ledgers, exports and tools "
            f"read it): {resolved}"
        )
    if (
        resolved == release
        or release in resolved.parents
        or path_under_release_root(resolved)
    ):
        raise CliUsageError(
            "--work-dir may not lie under a release root (the --release tree or "
            f"any tree carrying release_manifest.json): {resolved}"
        )
    return resolved


def _require_runtime_release(release_dir: Path) -> None:
    from elt_taskgen.export.release import (
        COMBINED_CORPUS_PROFILE,
        COMBINED_PUBLIC_LAYOUT,
        ReleaseManifest,
        verify_release,
    )

    try:
        release_dir = Path(release_dir).resolve()
        result = verify_release(release_dir)
        manifest = ReleaseManifest.model_validate_json(
            (release_dir / "release_manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise CliUsageError(f"cannot verify runtime release: {exc}") from None
    if not result.ok:
        detail = "; ".join(result.failures[:3])
        if len(result.failures) > 3:
            detail += f"; and {len(result.failures) - 3} more"
        raise CliUsageError(f"release verification failed: {detail}")
    if (
        manifest.corpus_profile != COMBINED_CORPUS_PROFILE
        or manifest.public_layout != COMBINED_PUBLIC_LAYOUT
    ):
        raise CliUsageError(
            "runtime commands require a schema-3 combined ELT-Bench release; "
            "legacy __el/__t DuckDB releases use the legacy score command"
        )


def _airbyte_client_from_credentials(url: str, credentials: dict):
    from elt_taskgen.runtime.airbyte import AirbyteClient, access_token_provider

    client_id = credentials.get("client_id")
    client_secret = credentials.get("client_secret")
    if isinstance(client_id, str) and client_id and isinstance(client_secret, str) and client_secret:
        return AirbyteClient(
            url,
            token_provider=access_token_provider(url, client_id, client_secret),
        )
    username = credentials.get("username") or credentials.get("email")
    password = credentials.get("password")
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        raise CliUsageError(
            "Airbyte credential JSON requires username/email + password or "
            "client_id + client_secret"
        )
    return AirbyteClient(url, username, password)


def cmd_runtime_install_airbyte(args) -> int:
    from elt_taskgen.runtime.bootstrap import BootstrapError, install_airbyte
    from elt_taskgen.runtime.process import ProcessFailure

    try:
        with _runtime_secret_output(args.credential_out) as commit:
            credentials = install_airbyte(
                chart_version=args.chart_version,
                abctl_version=args.abctl_version,
                timeout=args.timeout,
            )
            payload = {"password": credentials.password}
            if credentials.username:
                payload["username"] = credentials.username
            if credentials.client_id:
                payload["client_id"] = credentials.client_id
            if credentials.client_secret:
                payload["client_secret"] = credentials.client_secret
            commit(payload)
    except (BootstrapError, ProcessFailure, OSError, ValueError) as exc:
        raise CliUsageError(str(exc)) from None
    print(f"Airbyte is running; credentials written to {args.credential_out}")
    return 0


def cmd_runtime_bootstrap_task(args) -> int:
    from elt_taskgen.runtime.airbyte import AirbyteError
    from elt_taskgen.runtime.bootstrap import BootstrapError, bootstrap_task_airbyte

    try:
        with _runtime_secret_output(args.credential_out) as commit:
            release = Path(args.release).resolve()
            _require_runtime_release(release)
            credentials = _runtime_json(
                args.airbyte_credential, label="Airbyte credential"
            )
            client = _airbyte_client_from_credentials(
                args.airbyte_url, credentials
            )
            result = bootstrap_task_airbyte(
                release / "public" / args.task_id,
                release / "private" / args.task_id / "answer_key",
                args.task_id,
                client,
                workspace_id=args.workspace_id,
            )
            output = dict(credentials)
            output["workspace_id"] = result.workspace_id
            if result.custom_api_definition_id:
                output["custom_api_definition_id"] = result.custom_api_definition_id
                output["api_definition_id"] = result.custom_api_definition_id
            commit(output)
    except (AirbyteError, BootstrapError, OSError, ValueError) as exc:
        raise CliUsageError(str(exc)) from None
    print(f"workspace_id: {result.workspace_id}")
    if result.custom_api_definition_id:
        print(f"custom_api_definition_id: {result.custom_api_definition_id}")
    print(f"runtime credentials: {args.credential_out}")
    return 0


def cmd_runtime_prepare(args) -> int:
    from elt_taskgen.destinations import (
        destination_from_config,
        normalize_destination,
    )
    from elt_taskgen.runtime.install import install_task
    from elt_taskgen.runtime.source_environment import (
        SourceEnvironmentError,
        prepare_source_environment,
    )

    release = Path(args.release).resolve()
    work_dir_target = _refuse_confined_work_dir(args.work_dir, release)
    _require_runtime_release(release)
    airbyte = _runtime_json(args.airbyte_credential, label="Airbyte credential")
    public_config = _runtime_yaml(
        release / "public" / args.task_id / "config.yaml",
        label="public task config",
    )
    try:
        selected = destination_from_config(public_config)
        if (
            args.destination is not None
            and normalize_destination(args.destination) is not selected
        ):
            raise ValueError("--destination does not match the public task config")
        credential_paths = [
            path
            for path in (args.destination_credential, args.snowflake_credential)
            if path is not None
        ]
        if len(credential_paths) != 1:
            raise ValueError(
                "supply exactly one of --destination-credential or "
                "--snowflake-credential"
            )
        if args.snowflake_credential is not None and selected.value != "snowflake":
            raise ValueError(
                "--snowflake-credential cannot install a non-Snowflake task"
            )
    except ValueError as exc:
        raise CliUsageError(str(exc)) from None
    destination_values = _runtime_json(
        credential_paths[0], label=f"{selected.value} credential"
    )
    environment_dir = Path(args.environment_dir).resolve()
    created_environment = False
    try:
        environment = prepare_source_environment(
            release,
            args.task_id,
            args.population,
            environment_dir,
        )
        created_environment = True
        work_dir = install_task(
            release / "public" / args.task_id,
            work_dir_target,
            airbyte_credentials=airbyte,
            destination_credentials=destination_values,
            destination=selected,
            custom_api_definition_id=args.custom_api_definition_id,
            airbyte_server_url=args.airbyte_server_url,
        )
    except (SourceEnvironmentError, OSError, ValueError) as exc:
        if created_environment:
            shutil.rmtree(environment_dir, ignore_errors=True)
        raise CliUsageError(str(exc)) from None
    print(f"solver task:        {work_dir}")
    print(f"source environment: {environment.environment_dir}")
    return 0


def _runtime_source_environment(args):
    from elt_taskgen.runtime.source_environment import load_source_environment

    release = Path(args.release).resolve()
    _require_runtime_release(release)
    return load_source_environment(
        release,
        args.task_id,
        args.population,
        Path(args.environment_dir),
    )


def cmd_runtime_source_up(args) -> int:
    from elt_taskgen.runtime.process import ProcessFailure
    from elt_taskgen.runtime.source_environment import (
        DEFAULT_AIRBYTE_READINESS_POLL_INTERVAL_SECONDS,
        DEFAULT_AIRBYTE_READINESS_TIMEOUT_SECONDS,
        DEFAULT_AIRBYTE_STABILITY_WINDOW_SECONDS,
        SourceEnvironmentError,
        wait_for_airbyte_control_plane,
    )

    environment = None
    started = False
    readiness_timeout = _positive_seconds(
        str(
            getattr(
                args,
                "airbyte_readiness_timeout",
                DEFAULT_AIRBYTE_READINESS_TIMEOUT_SECONDS,
            )
        )
    )
    stability_window = _positive_seconds(
        str(
            getattr(
                args,
                "airbyte_stability_window",
                DEFAULT_AIRBYTE_STABILITY_WINDOW_SECONDS,
            )
        )
    )
    readiness_poll_interval = _positive_seconds(
        str(
            getattr(
                args,
                "airbyte_readiness_poll_interval",
                DEFAULT_AIRBYTE_READINESS_POLL_INTERVAL_SECONDS,
            )
        )
    )
    if stability_window >= readiness_timeout:
        raise CliUsageError(
            "Airbyte stability window must be shorter than the readiness timeout"
        )
    if readiness_poll_interval > stability_window / 2.0:
        raise CliUsageError(
            "Airbyte readiness poll interval cannot exceed half the stability window"
        )

    def cleanup_started_environment() -> bool:
        if started and environment is not None:
            try:
                environment.stop(airbyte_container=args.airbyte_container)
            except (ProcessFailure, OSError, ValueError):
                return False
        return True

    try:
        environment = _runtime_source_environment(args)
        environment.start(airbyte_container=args.airbyte_container)
        started = True
        environment.seed()
        wait_for_airbyte_control_plane(
            airbyte_container=args.airbyte_container,
            timeout=readiness_timeout,
            stable_for=stability_window,
            poll_interval=readiness_poll_interval,
        )
    except (ProcessFailure, SourceEnvironmentError, OSError, ValueError) as exc:
        cleanup_succeeded = cleanup_started_environment()
        detail = str(exc)
        if not cleanup_succeeded:
            detail += "; automatic source cleanup failed"
        raise CliUsageError(detail) from None
    except BaseException:
        cleanup_started_environment()
        raise
    print(
        f"source population {args.population!r} is running, seeded, and "
        f"Airbyte-ready on {environment.network}"
    )
    return 0


def cmd_runtime_source_down(args) -> int:
    from elt_taskgen.runtime.process import ProcessFailure
    from elt_taskgen.runtime.source_environment import SourceEnvironmentError

    try:
        environment = _runtime_source_environment(args)
        environment.stop(airbyte_container=args.airbyte_container)
    except (ProcessFailure, SourceEnvironmentError, OSError, ValueError) as exc:
        raise CliUsageError(str(exc)) from None
    print(f"stopped source environment: {environment.environment_dir}")
    return 0


def cmd_runtime_provision_snowflake(args) -> int:
    from elt_taskgen.runtime.snowflake import (
        SnowflakeRuntimeError,
        connect,
        load_credentials,
        provision_attempt,
    )

    try:
        with _runtime_secret_output(args.credential_out) as commit:
            admin = load_credentials(args.admin_snowflake_credential)
            connection = connect(admin)
            try:
                solver = provision_attempt(
                    connection,
                    args.database,
                    account=admin["account"],
                )
            finally:
                connection.close()
            commit(solver)
    except (SnowflakeRuntimeError, OSError, ValueError) as exc:
        raise CliUsageError(str(exc)) from None
    print(f"fresh scoped Snowflake attempt: {args.database}")
    print(f"solver credentials: {args.credential_out}")
    return 0


def cmd_runtime_reset_snowflake(args) -> int:
    from elt_taskgen.runtime.snowflake import (
        SnowflakeRuntimeError,
        connect,
        load_credentials,
        reset_database,
    )

    try:
        credentials = load_credentials(args.snowflake_credential)
        connection = connect(credentials)
        try:
            reset_database(connection, args.database, owner_role=args.owner_role)
        finally:
            connection.close()
    except (SnowflakeRuntimeError, OSError, ValueError) as exc:
        raise CliUsageError(str(exc)) from None
    print(f"fresh Snowflake database: {args.database}")
    return 0


def cmd_runtime_provision_databricks(args) -> int:
    from elt_taskgen.runtime.databricks import (
        DatabricksRuntimeError,
        UnityCatalogScope,
        assert_current_principal,
        connect,
        load_credentials,
        provision_attempt,
    )

    try:
        with _runtime_secret_output(args.credential_out) as commit:
            admin = load_credentials(args.admin_databricks_credential)
            solver = load_credentials(args.solver_databricks_credential)
            # A fresh target catalog does not exist yet, and an unprivileged
            # principal cannot select an existing target before its grants. Bind
            # identity through the SQL warehouse without selecting that target.
            identity_credentials = {
                key: value
                for key, value in solver.items()
                if key not in {"database", "schema"}
            }
            solver_connection = connect(identity_credentials)
            try:
                assert_current_principal(solver_connection, args.solver_principal)
            finally:
                solver_connection.close()
            connection = connect(admin)
            try:
                scoped = provision_attempt(
                    connection,
                    args.schema,
                    scope=UnityCatalogScope(
                        args.catalog,
                        allow_create_catalog=args.allow_create_catalog,
                        existing_dedicated_catalog=(
                            args.existing_dedicated_catalog
                        ),
                    ),
                    solver_principal=args.solver_principal,
                    solver_credentials=solver,
                    attempt_dedicated_principal=(
                        args.attempt_dedicated_principal
                    ),
                )
            finally:
                connection.close()
            commit(scoped)
    except (DatabricksRuntimeError, OSError, ValueError) as exc:
        raise CliUsageError(str(exc)) from None
    print(f"fresh scoped Databricks schema: {args.catalog}.{args.schema}")
    print(f"solver credentials: {args.credential_out}")
    return 0


def cmd_runtime_provision_redshift(args) -> int:
    from elt_taskgen.runtime.redshift import (
        RedshiftRuntimeError,
        connect,
        load_credentials,
        provision_attempt,
    )

    try:
        with _runtime_secret_output(args.credential_out) as commit:
            admin = load_credentials(args.admin_redshift_credential)
            connection = connect(admin)
            try:
                scoped = provision_attempt(
                    connection,
                    args.schema,
                    credentials=admin,
                    database=args.database,
                    s3_bucket_path=args.s3_bucket_path,
                    password=args.attempt_password,
                    attempt_dedicated_deployment=(
                        args.attempt_dedicated_deployment
                    ),
                )
            finally:
                connection.close()
            commit(scoped)
    except (RedshiftRuntimeError, OSError, ValueError) as exc:
        raise CliUsageError(str(exc)) from None
    print(f"fresh scoped Redshift database/schema: {args.database}.{args.schema}")
    print(f"solver credentials: {args.credential_out}")
    return 0


class _UsageArgumentError(CliUsageError, argparse.ArgumentTypeError):
    """A usage error raised inside argparse's type conversion. argparse reports
    the message itself and exits 2 (the `CliUsageError` code) before `main()`
    runs any command, so nothing is read and no container is started."""


def _positive_seconds(text: str) -> float:
    """argparse type for `--runner-timeout`: a finite number of seconds > 0,
    the rule `runtime.process._validate_timeout` applies at construction."""
    try:
        seconds = float(text)
    except (TypeError, ValueError):
        seconds = math.nan
    if not math.isfinite(seconds) or seconds <= 0.0:
        raise _UsageArgumentError(
            f"expected a positive number of seconds, got {text!r}"
        )
    return seconds


def _positive_usd(text: str) -> float:
    """Argparse type for finite, strictly positive USD circuit breakers."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value) or value <= 0.0:
        raise _UsageArgumentError(
            f"expected a finite positive USD amount, got {text!r}"
        )
    return value


def _unit_interval(text: str) -> float:
    """Argparse type for a finite fraction in the closed unit interval."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise _UsageArgumentError(
            f"expected a finite number between 0 and 1, got {text!r}"
        )
    return value


def _runtime_sandbox_lane(explicit: str | None, *, airbyte_url: str | None) -> str:
    """The network lane a Terraform-applying runtime command runs on.

    An explicit `--sandbox-lane` wins. Otherwise the lane follows the Airbyte
    server URL: `terraform apply` must reach the control plane (local abctl
    through host.docker.internal, or the cloud API), so a URL selects the
    proxy-bridge lane and no URL selects `none` (roadmap 0.A: `none` for every
    local tool, `proxy-bridge` for the Airbyte host-gateway lane). The dbt
    stage has its own narrower resolver and may explicitly select the separate
    `cloud-egress` lane."""
    from elt_taskgen.runtime.process import (
        DOCKER_LANE_NONE,
        DOCKER_LANE_PROXY_BRIDGE,
        DOCKER_LANES,
    )

    if explicit is not None:
        if explicit not in DOCKER_LANES:
            raise CliUsageError(
                f"unknown sandbox lane {explicit!r}; expected one of "
                f"{sorted(DOCKER_LANES)}"
            )
        return str(explicit)
    return DOCKER_LANE_PROXY_BRIDGE if airbyte_url else DOCKER_LANE_NONE


def _runtime_stage2_sandbox_lane(explicit: str | None) -> str:
    """Resolve Stage 2's deliberately narrower network policy.

    Transform execution may either have no network or join the dedicated
    outbound bridge.
    It must never reuse the Airbyte proxy lane, because that lane adds a host
    gateway and is a different trust boundary.  No flag remains fail-closed on
    ``none``; a real warehouse run must opt in to ``cloud-egress`` explicitly.
    """

    from elt_taskgen.runtime.process import (
        DOCKER_LANE_CLOUD_EGRESS,
        DOCKER_LANE_NONE,
    )

    lane = DOCKER_LANE_NONE if explicit is None else str(explicit)
    allowed = frozenset({DOCKER_LANE_NONE, DOCKER_LANE_CLOUD_EGRESS})
    if lane not in allowed:
        raise CliUsageError(
            f"unknown Stage 2 sandbox lane {lane!r}; expected one of "
            f"{sorted(allowed)}"
        )
    return lane


def _runtime_stage2_environment(
    work_dir: Path,
    *,
    destination_credential: Path | None,
) -> dict[str, str]:
    """Build the fixed, secret-conscious dbt environment for Stage 2.

    Snowflake requires explicit credentials matching the installed attempt
    config. Forward only its five fixed variable names; Docker receives values
    through its environment so passwords do not enter argv or command logs.
    Databricks and Redshift profiles carry scoped values directly and require
    no forwarded environment.
    """

    from elt_taskgen.destinations import Destination, destination_from_config

    config = _runtime_yaml(
        Path(work_dir) / "config.yaml", label="installed task config"
    )
    try:
        destination = destination_from_config(config)
    except ValueError as exc:
        raise CliUsageError(str(exc)) from None
    if destination is not Destination.SNOWFLAKE:
        if destination_credential is not None:
            raise CliUsageError(
                "--destination-credential environment forwarding is currently "
                "only needed for Snowflake Stage 2"
            )
        return {}
    if destination_credential is None:
        raise CliUsageError(
            "Snowflake Stage 2 requires --destination-credential so dbt "
            "env_var references can be populated"
        )

    from elt_taskgen.runtime.snowflake import (
        SnowflakeRuntimeError,
        load_credentials,
    )

    try:
        credential = load_credentials(destination_credential)
    except (SnowflakeRuntimeError, OSError, ValueError) as exc:
        raise CliUsageError(str(exc)) from None
    installed = (config.get("snowflake") or {}).get("config")
    if not isinstance(installed, dict):
        raise CliUsageError(
            "installed task config has no snowflake.config mapping"
        )

    # Credential and installed config are two independently supplied views of
    # the scoped principal.  A mismatch is a usage error reported without
    # echoing either value.  role/warehouse are absent from the reduced
    # installed DB-API credential, so use their attempt-config values when the
    # explicit file omits them.
    fields = (
        ("account", "account", "ELT_TASKGEN_SNOWFLAKE_ACCOUNT"),
        ("user", "username", "ELT_TASKGEN_SNOWFLAKE_USER"),
        ("password", "password", "ELT_TASKGEN_SNOWFLAKE_PASSWORD"),
        ("role", "role", "ELT_TASKGEN_SNOWFLAKE_ROLE"),
        ("warehouse", "warehouse", "ELT_TASKGEN_SNOWFLAKE_WAREHOUSE"),
    )
    environment: dict[str, str] = {}
    for credential_key, config_key, env_key in fields:
        configured = installed.get(config_key)
        if not isinstance(configured, str) or not configured:
            raise CliUsageError(
                f"installed Snowflake config has an empty/missing field: {config_key}"
            )
        supplied = credential.get(credential_key)
        if supplied is not None and supplied != configured:
            raise CliUsageError(
                "Snowflake Stage 2 credential does not match the installed "
                f"attempt field: {credential_key}"
            )
        if credential_key in {"account", "user", "password"} and supplied is None:
            # load_credentials already enforces these; keep the local invariant
            # explicit if its contract changes.
            raise CliUsageError(
                f"Snowflake credential has an empty/missing field: {credential_key}"
            )
        environment[env_key] = str(supplied or configured)
    return environment


def _runtime_stage1_receipt_payload(result) -> dict[str, object]:
    """Serialize exactly the public, reconstructable Stage 1 receipt fields."""

    return {
        "connection_ids": result.connection_ids,
        "execution_completed_at": result.execution_completed_at,
        "execution_started_at": result.execution_started_at,
        "job_ids": result.job_ids,
        "runner_image": result.runner_image,
        "statuses": result.statuses,
        "terraform_connection_resources": result.terraform_connection_resources,
        "terraform_input_tree_digest": result.terraform_input_tree_digest,
        "terraform_state_digest": result.terraform_state_digest,
        "terraform_state_lineage": result.terraform_state_lineage,
        "terraform_state_path": str(result.terraform_state_path),
        "terraform_state_serial": result.terraform_state_serial,
        "workspace_dir": str(result.workspace_dir),
    }


def _runtime_stage2_receipt_payload(result) -> dict[str, object]:
    """Serialize the closed, credential-free Stage 2 execution receipt."""

    preflight = result.preflight
    if preflight is None:
        raise ValueError("Stage 2 execution returned no preflight receipt")
    destination = getattr(preflight.destination, "value", None)
    if not isinstance(destination, str) or not destination:
        raise ValueError("Stage 2 preflight returned no destination")
    if result.run_results_path is None:
        raise ValueError("Stage 2 execution returned no dbt run artifact")
    return {
        "dbt_expected_model_ids": result.dbt_expected_model_ids,
        "dbt_input_tree_digest": result.dbt_input_tree_digest,
        "dbt_invocation_id": result.dbt_invocation_id,
        "dbt_observed_model_ids": result.dbt_observed_model_ids,
        "dbt_run_results_digest": result.dbt_run_results_digest,
        "execution_completed_at": result.execution_completed_at,
        "execution_started_at": result.execution_started_at,
        "preflight": {
            "adapter_version": preflight.adapter_version,
            "dbt_core_version": preflight.dbt_core_version,
            "destination": destination,
            "namespace": preflight.namespace,
            "physical_container": preflight.physical_container,
            "profile_name": preflight.profile_name,
            "target_name": preflight.target_name,
        },
        "profiles_dir": str(result.profiles_dir),
        "project_dir": str(result.project_dir),
        "run_results_path": str(result.run_results_path),
        "runner_image": result.runner_image,
    }


def cmd_runtime_run_stage1(args) -> int:
    from elt_taskgen.runtime.airbyte import AirbyteError
    from elt_taskgen.runtime.execution import ExecutionError, run_stage1_submission
    from elt_taskgen.runtime.process import DockerRunner, ProcessFailure

    work_dir = Path(args.work_dir).resolve()
    config = _runtime_yaml(work_dir / "config.yaml", label="installed task config")
    airbyte_config = (config.get("Airbyte") or {}).get("config")
    if not isinstance(airbyte_config, dict):
        raise CliUsageError("installed task config has no Airbyte.config mapping")
    if args.airbyte_credential:
        credentials = _runtime_json(args.airbyte_credential, label="Airbyte credential")
    else:
        credentials = dict(airbyte_config)
    url = args.airbyte_url or airbyte_config.get("server_url")
    if not isinstance(url, str) or not url:
        raise CliUsageError("no Airbyte API URL was supplied or installed")
    client = _airbyte_client_from_credentials(url, credentials)
    # `terraform apply` must reach the Airbyte control plane, so with a server
    # URL (always the case here) stage 1 defaults to the proxy-bridge lane;
    # `--network none` would fail every apply. Stage 2 has its own resolver:
    # it defaults to `none` and requires an explicit cloud-egress opt-in.
    lane = _runtime_sandbox_lane(getattr(args, "sandbox_lane", None), airbyte_url=url)
    try:
        runner = DockerRunner(
            work_dir,
            args.runner_image,
            timeout=args.runner_timeout,
            lane=lane,
        )
        result = run_stage1_submission(
            work_dir,
            client,
            runner=runner,
            terraform=args.terraform,
            workspace_id=str(airbyte_config.get("workspace_id") or ""),
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
        payload = _runtime_stage1_receipt_payload(result)
    except (AirbyteError, ExecutionError, ProcessFailure, OSError, ValueError) as exc:
        raise CliUsageError(str(exc)) from None
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_runtime_resync_stage1(args) -> int:
    """Run the second-sync append protocol without reapplying Terraform."""

    from elt_taskgen.runtime.airbyte import AirbyteError
    from elt_taskgen.runtime.execution import ExecutionError, rerun_stage1_syncs

    work_dir = Path(args.work_dir).resolve()
    config = _runtime_yaml(work_dir / "config.yaml", label="installed task config")
    airbyte_config = (config.get("Airbyte") or {}).get("config")
    if not isinstance(airbyte_config, dict):
        raise CliUsageError("installed task config has no Airbyte.config mapping")
    if args.airbyte_credential:
        credentials = _runtime_json(
            args.airbyte_credential, label="Airbyte credential"
        )
    else:
        credentials = dict(airbyte_config)
    url = args.airbyte_url or airbyte_config.get("server_url")
    if not isinstance(url, str) or not url:
        raise CliUsageError("no Airbyte API URL was supplied or installed")
    client = _airbyte_client_from_credentials(url, credentials)
    try:
        result = rerun_stage1_syncs(
            work_dir,
            client,
            workspace_id=str(airbyte_config.get("workspace_id") or ""),
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
        payload = _runtime_stage1_receipt_payload(result)
    except (AirbyteError, ExecutionError, OSError, ValueError) as exc:
        raise CliUsageError(str(exc)) from None
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_runtime_run_stage2(args) -> int:
    from elt_taskgen.runtime.execution import ExecutionError, run_stage2_submission
    from elt_taskgen.runtime.process import (
        DockerRunner,
        ProcessFailure,
    )

    try:
        work_dir = Path(args.work_dir).resolve()
        lane = _runtime_stage2_sandbox_lane(
            getattr(args, "sandbox_lane", None)
        )
        runner = DockerRunner(
            work_dir,
            args.runner_image,
            timeout=args.runner_timeout,
            lane=lane,
        )
        environment = _runtime_stage2_environment(
            work_dir,
            destination_credential=getattr(
                args, "destination_credential", None
            ),
        )
        result = run_stage2_submission(
            work_dir,
            runner=runner,
            dbt=tuple(args.dbt_command),
            project_dir=args.project_dir,
            profiles_dir=args.profiles_dir,
            target=args.target,
            env=environment,
            capture_provenance=True,
        )
        payload = _runtime_stage2_receipt_payload(result)
    except (ExecutionError, ProcessFailure, OSError, ValueError) as exc:
        raise CliUsageError(str(exc)) from None
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _runtime_verify(args, *, stage: int) -> int:
    from dataclasses import asdict

    from elt_taskgen.destinations import (
        Destination,
        destination_from_config,
        normalize_destination,
    )
    from elt_taskgen.runtime.evaluation import (
        EvaluationError,
        evaluate_end_to_end_release,
        evaluate_stage1_release,
        evaluate_stage2_release,
    )

    release = Path(args.release).resolve()
    _require_runtime_release(release)
    try:
        public_config = _runtime_yaml(
            release / "public" / args.task_id / "config.yaml",
            label="public task config",
        )
        selected = destination_from_config(public_config)
        if args.destination is not None:
            explicit = normalize_destination(args.destination)
            if explicit is not selected:
                raise ValueError(
                    "--destination does not match the public task config"
                )
        credential_paths = [
            path
            for path in (args.destination_credential, args.snowflake_credential)
            if path is not None
        ]
        if len(credential_paths) != 1:
            raise ValueError(
                "supply exactly one of --destination-credential or "
                "--snowflake-credential"
            )
        if (
            args.snowflake_credential is not None
            and selected is not Destination.SNOWFLAKE
        ):
            raise ValueError(
                "--snowflake-credential cannot verify a non-Snowflake task"
            )
        if selected is Destination.SNOWFLAKE:
            from elt_taskgen.runtime.snowflake import connect, load_credentials
        elif selected is Destination.DATABRICKS:
            from elt_taskgen.runtime.databricks import connect, load_credentials
        else:
            from elt_taskgen.runtime.redshift import (
                connect,
                load_connection_credentials as load_credentials,
            )

        credentials = load_credentials(credential_paths[0])
        physical_container = args.physical_container
        credential_container = credentials.get("database")
        if selected is Destination.DATABRICKS:
            if physical_container is None:
                physical_container = credential_container
            elif (
                credential_container is not None
                and physical_container != credential_container
            ):
                raise ValueError(
                    "--physical-container conflicts with the Databricks credential"
                )
        connection = connect(credentials)
        try:
            if stage == 1:
                evaluate = evaluate_stage1_release
            elif stage == 2:
                evaluate = evaluate_stage2_release
            else:
                evaluate = evaluate_end_to_end_release
            repetitions = getattr(args, "expected_repetitions", 1)
            result = evaluate(
                release,
                args.task_id,
                connection,
                population=args.population,
                database=args.database,
                destination=selected,
                physical_container=physical_container,
                **(
                    {
                        "certification_strict": args.certification_strict,
                        "expected_repetitions": repetitions,
                    }
                    if stage == 1
                    else (
                        {"certification_strict": True}
                        if args.certification_strict and stage == 0
                        else {}
                    )
                ),
            )
        finally:
            connection.close()
    except (EvaluationError, RuntimeError, OSError, ValueError) as exc:
        raise CliUsageError(str(exc)) from None
    if args.curator_details:
        payload = asdict(result)
    elif stage == 1:
        payload = {
            "stage": "el",
            "passed": result.passed,
            "reward": result.reward,
            "missing_table_count": len(result.missing_tables),
            "count_mismatch_count": len(result.count_mismatches),
            "error_count": len(result.errors),
            "expected_repetitions": result.expected_repetitions,
        }
    elif stage == 2:
        payload = {
            "stage": "t",
            "passed": result.passed,
            "reward": result.reward,
            "correct_marts": sum(result.mart_scores.values()),
            "total_marts": len(result.mart_scores),
            "error_count": len(result.errors),
        }
    else:
        stage2 = result.stage2
        payload = {
            "stage": "end_to_end",
            "passed": result.passed,
            "reward": result.reward,
            "el_passed": result.stage1.passed,
            "t_scored": stage2 is not None,
            "correct_marts": sum(stage2.mart_scores.values()) if stage2 else 0,
            "total_marts": len(stage2.mart_scores) if stage2 else 0,
        }
    if args.certification_strict:
        payload["certification_passed"] = result.certification_passed
        if stage == 1:
            payload["canonical_table_count"] = len(
                result.canonical_table_scores
            )
            payload["canonical_mismatch_count"] = sum(
                not matched for matched in result.canonical_table_scores.values()
            )
            payload["unexpected_table_count"] = len(result.unexpected_tables)
        elif stage == 2:
            payload["canonical_mart_count"] = len(result.canonical_mart_scores)
            payload["canonical_mismatch_count"] = sum(
                not matched for matched in result.canonical_mart_scores.values()
            )
        else:
            stage2 = result.stage2
            payload["canonical_table_mismatch_count"] = sum(
                not matched
                for matched in result.stage1.canonical_table_scores.values()
            )
            payload["canonical_mart_mismatch_count"] = (
                sum(not matched for matched in stage2.canonical_mart_scores.values())
                if stage2
                else 0
            )
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    if args.certification_strict:
        return 0 if result.certification_passed else 1
    return 0 if result.reward == 1.0 else 1


def cmd_runtime_verify_stage1(args) -> int:
    return _runtime_verify(args, stage=1)


def cmd_runtime_verify_stage2(args) -> int:
    return _runtime_verify(args, stage=2)


def cmd_runtime_verify_end_to_end(args) -> int:
    return _runtime_verify(args, stage=0)


def _runtime_certification_call(callback, /, **kwargs):
    from elt_taskgen.runtime.certification_lifecycle import (
        CertificationLifecycleError,
    )

    try:
        return callback(**kwargs)
    except (CertificationLifecycleError, OSError, ValueError) as exc:
        raise CliUsageError(str(exc)) from None


def cmd_runtime_certification_attest(args) -> int:
    from elt_taskgen.runtime.certification_lifecycle import (
        mint_release_bound_sandbox_attestation,
    )

    record = _runtime_certification_call(
        mint_release_bound_sandbox_attestation,
        release_dir=args.release,
        task_id=args.task_id,
        mount_root=args.mount_root,
        out=args.out,
        agents_config=args.agents_config,
        image_digest=args.image_digest,
        workspace_template_sha256=args.workspace_template_sha256,
    )
    print(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


def cmd_runtime_certification_attest_unbound(args) -> int:
    from elt_taskgen.runtime.certification_lifecycle import (
        mint_unbound_sandbox_attestation,
    )

    record = _runtime_certification_call(
        mint_unbound_sandbox_attestation,
        mount_root=args.mount_root,
        out=args.out,
        agents_config=args.agents_config,
        image_digest=args.image_digest,
        workspace_template_sha256=args.workspace_template_sha256,
    )
    print(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


def cmd_runtime_certification_begin(args) -> int:
    from elt_taskgen.runtime.certification_lifecycle import (
        begin_runtime_certification,
    )

    pending_dir, attempt_id = _runtime_certification_call(
        begin_runtime_certification,
        release_dir=args.release,
        task_id=args.task_id,
        certification_store=args.certification_store,
        sandbox_attestation=args.sandbox_attestation,
    )
    print(
        json.dumps(
            {"attempt_id": attempt_id, "pending_dir": str(pending_dir)},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_runtime_certification_status(args) -> int:
    from elt_taskgen.runtime.certification_lifecycle import (
        certification_lifecycle_status,
    )

    status = _runtime_certification_call(
        certification_lifecycle_status,
        release_dir=args.release,
        task_id=args.task_id,
        certification_store=args.certification_store,
    )
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def cmd_runtime_certification_record_stage1(args) -> int:
    from elt_taskgen.runtime.certification_lifecycle import record_stage1_evidence

    evidence = _runtime_certification_call(
        record_stage1_evidence,
        release_dir=args.release,
        task_id=args.task_id,
        certification_store=args.certification_store,
        population=args.population,
        execution_receipt=args.execution_receipt,
        evaluator_result=args.evaluator_result,
    )
    print(json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


def cmd_runtime_certification_record_stage2(args) -> int:
    from elt_taskgen.runtime.certification_lifecycle import record_stage2_evidence

    evidence = _runtime_certification_call(
        record_stage2_evidence,
        release_dir=args.release,
        task_id=args.task_id,
        certification_store=args.certification_store,
        population=args.population,
        execution_receipt=args.execution_receipt,
        evaluator_result=args.evaluator_result,
    )
    print(json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


def cmd_runtime_certification_record_observations(args) -> int:
    from elt_taskgen.runtime.certification_lifecycle import (
        record_runtime_observations,
    )

    observations = _runtime_json(
        args.observed_versions, label="runtime observations"
    )
    receipt = _runtime_certification_call(
        record_runtime_observations,
        release_dir=args.release,
        task_id=args.task_id,
        certification_store=args.certification_store,
        observed_versions=observations,
    )
    print(json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


def cmd_runtime_certification_record_cleanup(args) -> int:
    from dataclasses import asdict

    from elt_taskgen.runtime.certification_lifecycle import (
        record_certification_cleanup,
    )

    receipt = _runtime_certification_call(
        record_certification_cleanup,
        release_dir=args.release,
        task_id=args.task_id,
        certification_store=args.certification_store,
        cleanup_receipt=args.cleanup_receipt,
    )
    print(json.dumps(asdict(receipt), indent=2, sort_keys=True))
    return 0


def cmd_runtime_certification_complete(args) -> int:
    from elt_taskgen.runtime.certification_lifecycle import (
        complete_runtime_certification,
    )

    attestation, lifecycle = _runtime_certification_call(
        complete_runtime_certification,
        release_dir=args.release,
        task_id=args.task_id,
        certification_store=args.certification_store,
    )
    print(
        json.dumps(
            {
                "attestation": attestation.model_dump(mode="json"),
                "lifecycle": lifecycle.model_dump(mode="json"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_runtime_certify(args) -> int:
    """Crash-resumable trusted finish: evidence -> cleanup -> certify -> promote."""

    from elt_taskgen.runtime.certification_lifecycle import (
        finish_runtime_certification,
    )

    attestation, lifecycle, report = _runtime_certification_call(
        finish_runtime_certification,
        release_dir=args.release,
        task_id=args.task_id,
        certification_store=args.certification_store,
        run_spec=args.run_spec,
        difficulty_out=args.difficulty_out,
        workspace=args.workspace,
    )
    print(
        json.dumps(
            {
                "certification_id": attestation.certification_id,
                "certification_attestation_digest": attestation.attestation_digest,
                "lifecycle_digest": lifecycle.lifecycle_digest,
                "difficulty_out": str(Path(args.difficulty_out).resolve()),
                "difficulty_evidence_digest": report.evidence_digest,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_runtime_certify_difficulty(args) -> int:
    """Join current empirical evidence to a sealed live-runtime attestation.

    This is intentionally a promotion command rather than another score: it
    cannot create either kind of evidence, and exits with a usage refusal when
    the release, solver campaign, or certification store is incomplete.
    """
    from elt_taskgen.corpus.certified_difficulty import (
        CertifiedDifficultyError,
        build_runtime_certified_difficulty,
    )
    from elt_taskgen.runtime.certification_lifecycle import (
        CertificationLifecycleError,
        publish_immutable_json,
    )

    try:
        report = build_runtime_certified_difficulty(
            release_dir=Path(args.release),
            certification_store=Path(args.certification_store),
            task_id=str(args.task_id),
            workspace=(Path(args.workspace).resolve() if args.workspace else None),
        )
    except (CertifiedDifficultyError, OSError, ValueError) as exc:
        raise CliUsageError(str(exc)) from None
    payload = report.model_dump(mode="json")
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        out = Path(args.out).resolve()
        try:
            publish_immutable_json(out, payload)
        except CertificationLifecycleError:
            raise CliUsageError(
                f"certified difficulty output already exists (immutable): {out}"
            ) from None
        except OSError as exc:
            raise CliUsageError(
                f"could not create certified difficulty output {out}: {exc}"
            ) from None
    print(rendered, end="")
    return 0


def _parse_labels(pairs: list[str] | None) -> dict[str, str]:
    labels: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise CliUsageError(
                f"--labels entries must be key=value (got {pair!r})"
            )
        key, value = pair.split("=", 1)
        labels[key] = value
    return labels


def cmd_audit_list(args) -> int:
    """Print the pending human audit queue: every registered task with borderline
    collisions recorded at its current hash, with the fingerprints a reviewer would
    be signing off on."""
    engine = _open_engine(Path(args.workspace).resolve())
    try:
        tasks_root = engine.workspace / "tasks"
        rows = 0
        task_dirs = sorted(tasks_root.iterdir()) if tasks_root.is_dir() else []
        for tdir in task_dirs:
            if not (tdir / "task_ir.json").is_file():
                continue
            task = engine.load_task(tdir.name)
            current = task.content_hash()
            pending, problem = _pending_borderline(engine, task)
            if problem:
                pending = {}
            # Dual-build disagreements queue for human adjudication too
            # (reference/independent.py records them bound to a hash).
            from elt_taskgen.reference import independent

            adjudication = independent.load_adjudication(
                engine.workspace, task.task_id
            )
            adj_pending = (
                adjudication is not None
                and adjudication.get("task_content_hash") == current
            )
            # A repair the proposer ABSTAINED on queues for a human too
            # (review/repair_proposer.py `queue_adjudication`, bound to a hash).
            repair_pending = _pending_repair_adjudication(engine, task)
            # A REJECTED council proposal at the current hash is listed for
            # humans too (roadmap Phase 3 item 2; SoT T3 `project_proposal_
            # matrix`; A24): its post-session matrix projection — booleans
            # only — is rendered here and nowhere a model reads.
            rejected_matrices = _rejected_proposal_matrices(engine, task)
            if (
                not pending
                and not adj_pending
                and repair_pending is None
                and not rejected_matrices
            ):
                continue
            approval_path = _approval_path(engine, task.task_id)
            status = "none"
            if _rejection_path(engine, task.task_id).is_file():
                rejection = json.loads(
                    _rejection_path(engine, task.task_id).read_text(encoding="utf-8")
                )
                if rejection.get("task_content_hash") == current:
                    status = "rejected"
            if status == "none" and approval_path.is_file():
                try:
                    approval = AuditApproval.model_validate_json(
                        approval_path.read_text(encoding="utf-8")
                    )
                    status = (
                        "current"
                        if approval.task_content_hash == current
                        and set(approval.approved_collision_fingerprints) == set(pending)
                        else "stale"
                    )
                except Exception:
                    status = "invalid"
            rows += 1
            print(
                f"{task.task_id}  hash={current[:12]}  pending={len(pending)}  "
                f"approval={status}"
            )
            for fp in sorted(pending):
                print(f"  {fp[:16]}  {pending[fp]}")
            if adj_pending:
                # NOT A PENDING SIGN-OFF: a dual-build disagreement is a hard GATE
                # failure that no AuditApproval clears and no verb adjudicates.
                # Listing it beside sign-off items told operators to wait for a
                # decision they had no way to record.
                print(
                    "  DUAL-BUILD DISAGREEMENT (not a sign-off item — no "
                    "approval can clear it; the gates refuse this task until "
                    "the trusted reference or the independent build is fixed): "
                    + str(adjudication.get("detail", "(no detail recorded)"))
                )
            if repair_pending is not None:
                for line in _repair_adjudication_lines(repair_pending):
                    print(line)
            for line in _rejected_proposal_matrix_lines(rejected_matrices):
                print(line)
            triage = _load_triage(engine, task.task_id)
            if triage is not None and triage.get("task_content_hash") == current:
                labels = triage.get("per_axis_labels") or {}
                print(
                    "  TRIAGE (advisory, NOT an approval)  flag_for_human="
                    f"{str(bool(triage.get('flag_for_human'))).lower()}  "
                    + "  ".join(f"{k}={labels[k]}" for k in sorted(labels))
                )
        if rows == 0:
            print(
                "audit queue empty: no task has pending borderline collisions, "
                "dual-build adjudications or repair adjudications"
            )
        return 0
    finally:
        engine.close()


def _rejected_proposal_matrices(engine: Engine, task: TaskIR) -> list[tuple[str, dict]]:
    """`(case_name, projection_matrix)` for every `rejected_proposal.json`
    under the task's `attacks/` tree bound to the task's CURRENT content
    hash (`verification/attacks._record_rejected_proposal`), in case-name
    order. The matrix is the POST-SESSION `project_proposal_matrix` record
    (booleans only); a record written before the field existed is rendered
    from its `projection` sibling. Never the raw `measured` rewards."""
    from elt_taskgen.verification import attacks as attacks_mod

    attacks_root = engine.task_dir(task.task_id) / "attacks"
    if not attacks_root.is_dir():
        return []
    current = task.content_hash()
    out: list[tuple[str, dict]] = []
    for case_dir in sorted(attacks_root.iterdir()):
        path = case_dir / attacks_mod.REJECTED_PROPOSAL_FILENAME
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("task_content_hash") != current:
            continue
        matrix = record.get("projection_matrix")
        if not isinstance(matrix, dict):
            projection = record.get("projection")
            if not isinstance(projection, dict):
                continue
            matrix = {
                str(projection.get("finding_id", "")): {
                    "promoted": bool(projection.get("promoted", False)),
                    "per_population": dict(projection.get("per_population") or {}),
                    "fidelity_ok": bool(projection.get("fidelity_ok", False)),
                }
            }
        out.append((case_dir.name, matrix))
    return out


def _rejected_proposal_matrix_lines(matrices: list[tuple[str, dict]]) -> list[str]:
    """The `audit list` rendering of the post-session proposal matrix: one
    line per rejected proposal naming the case, the finding, the promoted
    and fidelity booleans and, per population, the adversary's own
    prediction beside the measured pass boolean. Booleans and public names
    only — no reward, no reason sentence."""
    lines: list[str] = []
    for case_name, matrix in matrices:
        for finding_id in sorted(matrix):
            entry = matrix[finding_id] if isinstance(matrix[finding_id], dict) else {}
            per_population = entry.get("per_population") or {}
            cells = "  ".join(
                f"{pop}=predicted:{str(bool(cell.get('predicted'))).lower()}/"
                f"measured_pass:{str(bool(cell.get('measured_pass'))).lower()}"
                for pop, cell in sorted(per_population.items())
                if isinstance(cell, dict)
            )
            lines.append(
                "  REJECTED PROPOSAL (not a sign-off item; the post-session matrix "
                f"projection for humans)  case={case_name}  finding={finding_id}  "
                f"promoted={str(bool(entry.get('promoted'))).lower()}  "
                f"fidelity_ok={str(bool(entry.get('fidelity_ok'))).lower()}"
                + (f"  {cells}" if cells else "")
            )
    return lines


# Audit triage writes advisory labels only. It cannot create or modify approval
# records, and its vocabulary contains no acceptance state.

TRIAGE_ADVISORY_NOTE = (
    "ADVISORY ONLY — audit triage never approves. A sign-off exists only when "
    "a named human runs 'elt-taskgen audit approve', which writes an "
    "AuditApproval bound to the task content hash; this record is not read by "
    "that command or by the audit stage."
)


def _triage_path(engine: Engine, task_id: str) -> Path:
    return _audit_dir(engine) / f"{task_id}.triage.json"


def _load_triage(engine: Engine, task_id: str) -> dict | None:
    path = _triage_path(engine, task_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return None


def _pending_repair_adjudication(engine: Engine, task: TaskIR) -> dict | None:
    """The repair proposer's NEEDS_ADJUDICATION entry for `task`
    (`<ws>/audit/<task>.repair_adjudication.json`, written by
    `review.repair_proposer.queue_adjudication` when every proposal abstained),
    if one is bound to the task's CURRENT content hash and has not been retired.
    Sequence-bound records remain visible from their triggering FAIL (including
    a crash before BLOCKED is appended) until a later same-hash PASS or FATAL;
    legacy unbound records retain the latest-BLOCKED rule.  A moved identity or
    rejected task also retires the entry without deleting append-only history."""
    from elt_taskgen.review import repair_proposer as proposer_mod

    record = proposer_mod.load_repair_adjudication(engine.workspace, task.task_id)
    if record is None or record.get("task_content_hash") != task.content_hash():
        return None
    if task.status is TaskStatus.REJECTED:
        return None
    stage = record.get("stage")
    if not isinstance(stage, str) or not stage:
        return None
    try:
        latest = engine.latest_report(task.task_id, stage)
    except ValueError:
        # The adjudication reader is intentionally raw/backward-compatible;
        # malformed historical stage names are not live queue entries.
        return None
    source_report_id = record.get("source_report_id")
    if isinstance(source_report_id, int) and not isinstance(source_report_id, bool):
        # New records bind to the FAIL written immediately before proposer
        # dispatch. The queue remains visible if the process dies before the
        # subsequent BLOCKED append; only a demonstrably later same-hash PASS
        # retires it.
        history = tuple(
            report
            for report in engine.report_history(task.task_id, stage)
            if report.content_hash == task.content_hash()
        )
        source = next(
            (report for report in history if report.id == source_report_id), None
        )
        if source is None or source.verdict != VERDICT_FAIL:
            return None
        if any(
            report.id > source_report_id
            and report.verdict in (VERDICT_PASS, VERDICT_FATAL)
            for report in history
        ):
            return None
        return record
    # Backward-compatible records predate sequence binding. They are live only
    # at the explicit BLOCKED resume point, preserving their historical rule.
    if latest is None or latest.content_hash != task.content_hash():
        return None
    if latest.verdict != VERDICT_BLOCKED:
        return None
    return record


def _audit_queue(
    engine: Engine, task_id: str | None = None, *, include_repair: bool = False
) -> list[tuple]:
    """Queue entries: (task, pending borderline collisions, adjudication|None).

    Same predicate as `audit list`: borderline collisions recorded at the task's
    current hash, or a dual-build adjudication bound to that hash. With
    `include_repair`, a pending repair adjudication (`_pending_repair_adjudication`)
    queues the task too and every entry gains a fourth element, the repair
    record or None; the default keeps the three-tuple the triage reads."""
    from elt_taskgen.reference import independent

    tasks_root = engine.workspace / "tasks"
    entries: list[tuple] = []
    task_dirs = sorted(tasks_root.iterdir()) if tasks_root.is_dir() else []
    for tdir in task_dirs:
        if not (tdir / "task_ir.json").is_file():
            continue
        if task_id is not None and tdir.name != task_id:
            continue
        task = engine.load_task(tdir.name)
        current = task.content_hash()
        pending, problem = _pending_borderline(engine, task)
        if problem:
            pending = {}
        adjudication = independent.load_adjudication(engine.workspace, task.task_id)
        if adjudication is not None and adjudication.get("task_content_hash") != current:
            adjudication = None
        repair = _pending_repair_adjudication(engine, task) if include_repair else None
        if not pending and adjudication is None and repair is None:
            continue
        entries.append(
            (task, pending, adjudication, repair) if include_repair else (task, pending, adjudication)
        )
    return entries


def _repair_adjudication_lines(record: dict) -> list[str]:
    """`audit list` rendering of one pending repair adjudication: NOT a sign-off
    item (no approval clears it; the engine already took its bounded round) —
    the stage, route, sessions/attempts and their rejection codes, so the human
    knows what the proposer could not repair and why."""
    attempts = [a for a in (record.get("attempts") or ()) if isinstance(a, dict)]
    codes = sorted(
        {
            str(a.get("rejection_code") or a.get("error_type") or "")
            for a in attempts
            if (a.get("rejection_code") or a.get("error_type"))
        }
    )
    lines = [
        "  REPAIR ADJUDICATION (not a sign-off item — the repair proposer "
        f"abstained; status={record.get('status', '?')} stage={record.get('stage', '?')} "
        f"route={record.get('route', '?')} attempts={len(attempts)}"
        + (f" codes={','.join(codes)}" if codes else "")
        + "): "
        + str(record.get("detail") or "(no detail recorded)")
    ]
    return lines


def _projected_gate_lines(task: TaskIR, payload: dict) -> list[str]:
    """The gate rows of the triage view: `{gate, passed, code}` and nothing else.

    Built on `review/tools/projection.project_gate_battery`, so a gate's raw
    `details` and `evidence` (count vectors, key tuples, per-population reward
    values, DuckDB text, paths) never enter the prompt. Every row is serialized
    by the projector and re-checked by the gatekeeper; a trip halts the triage
    as a harness fault rather than rendering the row."""
    from elt_taskgen.review.tools import projection

    lines: list[str] = []
    for row in projection.project_gate_battery(payload):
        diag = projection.Diagnostic(
            source=projection.DiagnosticSource.GATE,
            ok=bool(row["passed"]),
            code=str(row["code"]),
            subject=str(row["gate"]),
        )
        wire = projection.serialize_for_transport(diag, task=task, package=None)
        projection.assert_value_free(wire.encode("utf-8"), task=task, route=None)
        state = "PASS" if diag.ok else "FAIL"
        lines.append(f"  [{state}] {diag.subject}: {diag.code}")
    return lines


def _projected_adjudication_lines(task: TaskIR, adjudication: dict) -> list[str]:
    """The pending dual-build adjudication as ONE code: its raw `detail` is the
    independent build's own text (per-population outcomes, expected-versus-got
    row counts, DuckDB errors) and stays with the human adjudicator."""
    from elt_taskgen.review.tools import projection

    diag = projection.project_dual_build(adjudication)
    wire = projection.serialize_for_transport(diag, task=task, package=None)
    projection.assert_value_free(wire.encode("utf-8"), task=task, route=None)
    return [f"  code: {diag.code}"]


def _triage_view(engine: Engine, task: TaskIR, pending: dict, adjudication) -> str:
    """The audit_triage user prompt: the full bundle + projected gate rows + findings.

    Deterministic text (sorted, no wall clock) so the transcript key is stable and a
    triage pass replays offline like any other role call. Gate evidence is
    PROJECTED (`{gate, passed, code}` through `_projected_gate_lines`): the role
    is not shown gold, counts, rewards or paths. A pending dual-build record is
    likewise reduced to status plus one value-free code, and the prompt's claim
    to that effect is true by construction."""
    lines: list[str] = [
        "AUDIT QUEUE ENTRY — advisory triage",
        f"task_id: {task.task_id}",
        f"content_hash: {task.content_hash()}",
        f"family_id: {task.family_id}",
        f"origin: {task.origin.value}",
        f"license: {task.license}",
        f"attribution: {task.attribution}",
        "",
        "SOLVER-VISIBLE PROSE:",
        task.solver_prompt or "(none authored)",
        "",
        "SOURCE TABLES:",
    ]
    backends = {b.table: b.backend.value for b in task.backends}
    for table in sorted(task.tables, key=lambda t: t.name):
        cols = ", ".join(f"{c.name}:{c.type.value}" for c in table.columns)
        lines.append(
            f"  {table.name} [{backends.get(table.name, '?')}] ({cols})"
        )
    lines.append("")
    lines.append("MARTS:")
    for mart in sorted(task.marts, key=lambda m: m.name):
        lines.append(f"  {mart.name} — grain: {mart.grain}")
        lines.append(f"    keys: {', '.join(mart.key_columns)}")
        for column in mart.columns:
            lines.append(f"    {column.name}:{column.type.value} — {column.description}")
        for i, op in enumerate(mart.plan.ops):
            lines.append(f"    rule {i + 1} [{op.kind.value}]: {op.description}")

    lines.append("")
    lines.append("GATE EVIDENCE (latest recorded battery):")
    gates_row = engine.latest_report(task.task_id, StageName.GATES.value)
    if gates_row is None:
        lines.append("  (no gates report recorded)")
    else:
        stale = "" if gates_row.content_hash == task.content_hash() else " STALE"
        lines.append(f"  verdict: {gates_row.verdict}{stale}")
        lines.extend(_projected_gate_lines(task, json.loads(gates_row.payload_json)))

    lines.append("")
    lines.append("COUNCIL FINDINGS (latest recorded review):")
    review_row = engine.latest_report(task.task_id, StageName.REVIEW.value)
    if review_row is None:
        lines.append("  (no review report recorded)")
    else:
        findings = json.loads(review_row.payload_json).get("findings", [])
        if not findings:
            lines.append("  (none)")
        for finding in findings:
            lines.append(
                f"  [{finding.get('severity')}] {finding.get('role')}: "
                f"{finding.get('summary')}"
            )

    lines.append("")
    lines.append("PENDING BORDERLINE CONTAMINATION COLLISIONS:")
    if not pending:
        lines.append("  (none)")
    for fingerprint in sorted(pending):
        lines.append(f"  {fingerprint[:16]}  {pending[fingerprint]}")

    lines.append("")
    lines.append("DUAL-BUILD ADJUDICATION:")
    if adjudication is None:
        lines.append("  (none)")
    else:
        lines.append(f"  status: {adjudication.get('status', '?')}")
        lines.extend(_projected_adjudication_lines(task, adjudication))

    lines.append("")
    lines.append(
        "Label every axis and say whether a human must look. You cannot "
        "approve this task; nothing you write clears it."
    )
    return "\n".join(lines)


def cmd_triage(args, provider=None) -> int:
    """Advisory triage over the audit queue (labels only — never an approval)."""
    from elt_taskgen.review import providers as providers_mod
    from elt_taskgen.review.tools import projection as projection_mod

    workspace = Path(args.workspace).resolve()
    engine = _open_engine(workspace)
    try:
        if provider is None:
            provider = _resolve_provider(args, workspace)
        entries = _audit_queue(engine, getattr(args, "task_id", None) or None)
        if not entries:
            print(
                "audit queue empty: nothing to triage (no pending borderline "
                "collisions or dual-build adjudications)"
            )
            return 0
        for task, pending, adjudication in entries:
            try:
                view = _triage_view(engine, task, pending, adjudication)
            except projection_mod.DiagnosticTripwire as exc:
                # A producer leaked into the view: nothing was sent. A harness
                # fault — could not measure (2), never a task verdict.
                print(f"triage could not measure {task.task_id}: {exc}")
                return 2
            try:
                text = provider.complete(providers_mod.AUDIT_TRIAGE_ROLE, view)
                advice = providers_mod.parse_triage_response(text)
            except (
                providers_mod.TranscriptMissingError,
                providers_mod.MissingCredentialsError,
                providers_mod.BudgetExceededError,
            ) as exc:
                print(f"triage unavailable for {task.task_id}: {exc}")
                return 1
            except providers_mod.ProviderProtocolError as exc:
                # The measurement could not be TAKEN (malformed provider
                # output): exit 2, not 1 — a protocol fault is not a verdict
                # on the task, and nothing advisory is written.
                print(f"triage could not measure {task.task_id}: {exc}")
                return 2
            record = {
                "task_id": task.task_id,
                "task_content_hash": task.content_hash(),
                "role": providers_mod.AUDIT_TRIAGE_ROLE,
                "advisory": True,
                "approves": False,
                # Same vocabulary as AuditApproval.per_axis_labels, so a human can
                # carry a reading into 'audit approve' — the carrying is theirs.
                "per_axis_labels": dict(sorted(advice.labels.items())),
                "flag_for_human": bool(advice.flag_for_human),
                "rationale": advice.rationale,
                "pending_collision_fingerprints": sorted(pending),
                "adjudication_pending": adjudication is not None,
                "note": TRIAGE_ADVISORY_NOTE,
            }
            path = _triage_path(engine, task.task_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(readable_json(record), encoding="utf-8")
            labels = "  ".join(
                f"{axis}={record['per_axis_labels'][axis]}"
                for axis in sorted(record["per_axis_labels"])
            )
            print(
                f"{task.task_id}  hash={task.content_hash()[:12]}  "
                f"flag_for_human={str(record['flag_for_human']).lower()}"
            )
            print(f"  labels: {labels}")
            print(f"  advisory record: {path}")
        print(
            "\ntriage is ADVISORY: no approval was written. A human must run "
            "'elt-taskgen audit approve' to sign anything off."
        )
        return 0
    finally:
        engine.close()


def cmd_audit_approve(args) -> int:
    """Write an AuditApproval BOUND to the task's current content hash and the
    exact pending borderline collision set (computed here, never hand-typed)."""
    from datetime import datetime, timezone

    try:
        labels = _parse_labels(args.labels)
    except ValueError as exc:
        # A malformed --labels pair is a USAGE error, not a rejected task.
        print(f"error: {exc}")
        return 2
    engine = _open_engine(Path(args.workspace).resolve())
    try:
        task = engine.load_task(args.task_id)
        pending, problem = _pending_borderline(engine, task)
        if problem:
            print(f"cannot approve: {problem}")
            return 1
        approval = AuditApproval(
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            approved_collision_fingerprints=tuple(sorted(pending)),
            per_axis_labels=labels,
            reviewer=args.reviewer,
            approved_at=args.approved_at
            or datetime.now(timezone.utc).isoformat(),
        )
        path = _approval_path(engine, task.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(readable_json(approval.model_dump(mode="json")), encoding="utf-8")
        _rejection_path(engine, task.task_id).unlink(missing_ok=True)
        print(
            f"approval written: {path}\n"
            f"  reviewer={approval.reviewer}  bound_hash={approval.task_content_hash[:12]}  "
            f"fingerprints={len(pending)}"
        )
        return 0
    finally:
        engine.close()


def cmd_audit_reject(args) -> int:
    """Record a human rejection bound to the current content hash; the audit
    stage turns it into a fatal verdict (task rejected)."""
    engine = _open_engine(Path(args.workspace).resolve())
    try:
        task = engine.load_task(args.task_id)
        record = {
            "task_id": task.task_id,
            "task_content_hash": task.content_hash(),
            "reason": args.reason,
        }
        path = _rejection_path(engine, task.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(readable_json(record), encoding="utf-8")
        _approval_path(engine, task.task_id).unlink(missing_ok=True)
        print(f"rejection recorded: {path}")
        return 0
    finally:
        engine.close()


def _stamped_records(transcripts: Path):
    """Every JSON record under `<workspace>/transcripts` — turn entries,
    session records, trajectory records — with its admission stamp: the
    five `admission_*` keys `RoutedProvider._record_stamp` writes."""
    if not transcripts.is_dir():
        return
    for path in sorted(transcripts.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        stamp = data.get("admission")
        if not isinstance(stamp, dict):
            continue
        yield path, data, stamp


def cmd_admission_audit(args) -> int:
    """List transcript trajectories associated with an admission record.

    Include one-shot, session, and content-addressed records matched by stamped
    path or a tombstone's withdrawn routing fingerprint. ``PATH`` defaults to
    ``$ELT_TASKGEN_ADMISSION`` or the workspace admission record. Revocation is
    prospective, so this read-only audit also finds work run before withdrawal.
    """
    from elt_taskgen.review import metrology as metrology_mod

    workspace = Path(args.workspace).resolve()
    requested = getattr(args, "revoked", None)
    if requested:
        record_path = Path(requested)
    else:
        resolved = metrology_mod.admission_record_path(workspace)
        if resolved is None:
            print("error: no admission record path: pass --revoked PATH", file=sys.stderr)
            return 2
        record_path = resolved
    record: dict = {}
    if record_path.is_file():
        try:
            loaded = json.loads(record_path.read_text(encoding="utf-8"))
            record = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            record = {}
    revoked = bool(record.get("revoked", False))
    fingerprint = str(
        record.get("revoked_routing_fingerprint") or record.get("routing_fingerprint") or ""
    )
    targets = {str(record_path)}
    try:
        targets.add(str(record_path.resolve()))
    except OSError:
        pass
    print(
        f"admission audit: record {record_path} "
        + ("(REVOKED" + (f" [{record.get('revoked_reason_code')}]" if record.get("revoked_reason_code") else "") + ")"
           if revoked else "(not a tombstone)" if record else "(absent or unreadable)")
    )
    rows = 0
    for path, data, stamp in _stamped_records(workspace / "transcripts"):
        stamped_path = str(stamp.get("admission_record_path") or "")
        stamped_fp = str(stamp.get("admission_routing_fingerprint") or "")
        if stamped_path not in targets and not (fingerprint and stamped_fp == fingerprint):
            continue
        rows += 1
        kind = (
            "trajectory" if "trajectory_sha256" in data and "turns" in data
            else "session" if "session_key" in data
            else "exchange"
        )
        digest = str(
            data.get("trajectory_sha256") or data.get("session_sha256")
            or data.get("prompt_sha256") or ""
        )
        print(
            f"  {kind:<10} {str(data.get('role') or '?'):<24} task={data.get('task_id') or '?'} "
            f"digest={digest[:12]} mode={stamp.get('admission_mode') or '?'} "
            f"recorded_by={data.get('recorded_by') or '?'}  {path.relative_to(workspace)}"
        )
    print(f"{rows} record(s) ran under this admission" + (" (withdrawn)" if revoked else ""))
    return 0


def _metrology_budget_guidance() -> tuple[str, str]:
    """(flag, cost) from `metrology._COST_NOTE`, ONE number everywhere it is
    stated (tests/test_docs_consistency.py); the embedded fallback is the
    Phase 4 figure."""
    try:
        from elt_taskgen.review import metrology as metrology_mod

        note = str(metrology_mod._COST_NOTE)  # noqa: SLF001 - the operator-facing string
    except Exception:  # noqa: BLE001 - the parser must build without the module
        note = "~$25 per run at list rates with caching; use --budget-per-task 60"
    import re as _re

    flag = _re.search(r"--budget-per-task \d+", note)
    usd = _re.search(r"\$\d+(?:\.\d+)?", note)
    return (
        flag.group(0) if flag else "--budget-per-task 60",
        usd.group(0) if usd else "$25",
    )


_PIPELINE_DEFAULT_BUDGET_PER_TASK_USD = 7.00
_EXPLICIT_BUDGET_ATTRIBUTE = "_budget_per_task_was_explicit"


class _BudgetPerTaskAction(argparse.Action):
    """Remember that the operator supplied the shared budget flag.

    ``argparse`` parent actions are shared objects: calling ``set_defaults`` on
    only the pipeline child mutates the default observed by every sibling
    command. This marker lets the root parser apply a command-local default
    after ordinary parsing without mistaking an explicit ``--budget-per-task
    5`` for the shared default.
    """

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        setattr(namespace, self.dest, values)
        setattr(namespace, _EXPLICIT_BUDGET_ATTRIBUTE, True)


class _CliArgumentParser(argparse.ArgumentParser):
    """Root parser applying defaults that differ by selected subcommand."""

    def parse_known_args(self, args=None, namespace=None):
        parsed, extras = super().parse_known_args(args, namespace)
        # Subparsers are instances of this class too, but only the root result
        # carries `command`. Leave the explicit marker on an intermediate
        # namespace so the root can distinguish an explicit value of 5 from
        # the shared default; remove it before either public root parser API
        # returns to the caller.
        command = getattr(parsed, "command", None)
        if command is not None:
            supplied = bool(getattr(parsed, _EXPLICIT_BUDGET_ATTRIBUTE, False))
            if command == "pipeline":
                parsed.budget_per_task_explicit = supplied
                if not supplied:
                    parsed.budget_per_task = _PIPELINE_DEFAULT_BUDGET_PER_TASK_USD
            if hasattr(parsed, _EXPLICIT_BUDGET_ATTRIBUTE):
                delattr(parsed, _EXPLICIT_BUDGET_ATTRIBUTE)
        return parsed, extras


# --- Parser ---

def build_parser() -> argparse.ArgumentParser:
    budget_flag, budget_usd = _metrology_budget_guidance()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--workspace",
        type=Path,
        default=DEFAULT_WORKSPACE,
        help="pipeline workspace root (default: ./runs/default)",
    )
    common.add_argument(
        "--max-repair-rounds",
        type=int,
        default=3,
        help="bounded repair budget before rejection (default: 3)",
    )
    common.add_argument(
        "--agents-config",
        type=Path,
        default=None,
        help="role-routing YAML (default: packaged config/agents.yaml)",
    )
    common.add_argument(
        "--replay-only",
        action="store_true",
        help=(
            "never make live provider calls; a missing transcript FAILS the "
            "stage. For metrology this is diagnostic only and cannot grant or "
            "revoke admission"
        ),
    )
    common.add_argument(
        "--record",
        action="store_true",
        help=(
            "force fresh live calls even when a transcript exists (re-record); "
            "live metrology is always fresh even without this flag"
        ),
    )
    common.add_argument(
        "--budget-per-task",
        action=_BudgetPerTaskAction,
        type=_positive_usd,
        default=5.00,
        metavar="USD",
        help=(
            "live-call circuit-breaker target per task in USD, metered per API "
            "attempt at the four-rate price table (cache reads and writes "
            "included); the pre-call estimate may refuse a call, but a first "
            "turn reserves only its known prompt-input floor, so actual spend "
            "can cross the target by the final recorded call before the stage "
            "halts as infrastructure (exit 2, never a rejection). "
            "Most commands default to 5.00; pipeline defaults to 7.00 "
            "(candidate-count mode: 25.00); "
            f"metrology needs {budget_flag}. The pipeline's bounded proposer "
            "profile reserves each session's own cap plus the route's "
            "submit-time certification ceiling"
        ),
    )
    common.add_argument(
        "--budget-total",
        type=_positive_usd,
        default=None,
        metavar="USD",
        help=(
            "live-call circuit-breaker target in USD (default: unlimited; "
            "set it to cap a whole candidate run); "
            "legacy registered-task pipeline mode partitions it evenly across "
            "scheduled task processes and clamps each task to the smaller "
            "per-task target. Configured candidate mode (--candidate-count or "
            "--run-config) instead uses one durable transactional ledger shared "
            "by all workers; committed, reserved, and uncertain calls consume "
            "the same global headroom across resume. It is not a prepaid hard "
            "cap; it is a prospective circuit breaker, so actual cost may "
            "exceed an estimate on the final recorded call"
        ),
    )
    proposer_toggle = common.add_mutually_exclusive_group()
    proposer_toggle.add_argument(
        "--repair-proposer",
        dest="repair_proposer",
        action="store_true",
        help="enable the repair proposer (default)",
    )
    proposer_toggle.add_argument(
        "--no-repair-proposer",
        dest="repair_proposer",
        action="store_false",
        help="disable live/tool-using repair proposals",
    )
    common.set_defaults(repair_proposer=True)
    common.add_argument(
        "--repair-proposer-mode",
        choices=("one_shot", "bounded"),
        default="bounded",
        help=(
            "which repair proposer implementation to use: bounded (default; "
            "a tool-using session on a held trial copy, at most "
            "repair.max_attempts sessions per failure on the routes "
            "repair.routes_bounded names, certified by the same unchanged "
            "re-validation at submit) or one_shot (compatibility mode). "
            "Ignored with --no-repair-proposer"
        ),
    )
    common.add_argument(
        "--repair-attempts",
        type=int,
        default=None,
        metavar="N",
        help=(
            "override the per-failure proposal budget "
            "(default: config/agents.yaml repair.max_attempts)"
        ),
    )
    common.add_argument(
        "--agent-harness",
        choices=AGENT_HARNESS_MODES,
        default=None,
        help=(
            "which transport EVERY model-calling role runs on: api (the direct "
            "Anthropic and OpenAI-compatible transports) or headless (Claude "
            "Code through the Agent SDK for the Claude-family roles, Codex "
            "through its app server for the cross-family witness roles). "
            f"Default: ${AGENT_HARNESS_ENV} when set, else api. Each mode "
            "consults its own council admission record"
        ),
    )

    parser = _CliArgumentParser(
        prog="elt-taskgen",
        description="Offline factory: candidate data project -> verified RLVR ELT task.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    training = sub.add_parser(
        "training",
        help="deterministic cloud-free Terraform/Airbyte/dbt workflow proxy",
    )
    training_sub = training.add_subparsers(dest="training_command", required=True)

    def add_training_package_args(command):
        command.add_argument("--release", required=True, type=Path)
        command.add_argument("--task-id", required=True)

    tp = training_sub.add_parser("install", help="install a fresh authoring workspace")
    add_training_package_args(tp)
    tp.add_argument("--attempt", required=True, type=Path)
    tp.set_defaults(func=cmd_training_install)

    tp = training_sub.add_parser("seal", help="authenticate the candidate elt/ snapshot")
    add_training_package_args(tp)
    tp.add_argument("--attempt", required=True, type=Path)
    tp.add_argument("--output", required=True, type=Path)
    tp.set_defaults(func=cmd_training_seal)

    tp = training_sub.add_parser("score", help="replay every graded population locally")
    add_training_package_args(tp)
    candidate = tp.add_mutually_exclusive_group(required=True)
    candidate.add_argument("--candidate-workspace", type=Path)
    candidate.add_argument("--seal", type=Path)
    tp.add_argument("--seal-sha256", default=None)
    tp.add_argument("--destination", required=True, choices=("snowflake", "databricks", "redshift"))
    tp.add_argument("--attempts-dir", required=True, type=Path)
    tp.add_argument("--dbt-python", required=True, type=Path)
    tp.add_argument("--dbt-manifest", required=True, type=Path)
    tp.set_defaults(func=cmd_training_score)

    tp = training_sub.add_parser("inspect", help="inspect a profile or sealed manifest")
    add_training_package_args(tp)
    tp.add_argument("--destination", choices=("snowflake", "databricks", "redshift"), default=None)
    tp.add_argument("--seal", type=Path, default=None)
    tp.add_argument("--seal-sha256", default=None)
    tp.set_defaults(func=cmd_training_inspect)

    p = sub.add_parser(
        "record-transcripts",
        parents=[common],
        help=(
            "one-time seeding: run author+council LIVE for a task (requires "
            "API keys) and persist transcripts for offline replay"
        ),
    )
    p.add_argument(
        "--task-id",
        default=None,
        help="task to record (default: the built-in demo task -> committed fixtures)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "transcript output dir (default: tests/fixtures/transcripts for "
            "the demo task, <workspace>/transcripts otherwise)"
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="re-record even when a transcript already exists for a prompt",
    )
    p.add_argument(
        "--loader-only",
        action="store_true",
        help=(
            "seed ONLY the independent_loader exchange (pass 2 of the "
            "two-pass flow: after `review --replay-only` + `reference-run` "
            "have re-attested the prose-bearing task). Skips author/council/"
            "implementer entirely — their pass-1 transcripts stay canonical; "
            "re-running them against the prose-bearing task would record "
            "exchanges no stage ever replays."
        ),
    )
    p.set_defaults(func=cmd_record_transcripts)

    p = sub.add_parser(
        "metrology",
        parents=[common],
        help=(
            "council efficacy harness: defect-injection benchmark over the "
            "frozen fixture families (demo, clinic_visits, stock_ledger), the "
            "draw stratified by family; live mode always makes fresh provider "
            "calls and a "
            "pass writes the council.live_admitted marker. --replay-only is "
            "diagnostic and never admits. Every critic call is charged to the "
            "one task id 'council-metrology', so run it with "
            f"{budget_flag} (~{budget_usd} per run at list rates with caching: "
            "140 scored plus 12 canary trajectories; the 5.00 default halts "
            "mid-run with exit 2). Refuses to start (exit 2) when the "
            "installed duckdb or sqlglot differs from the pins in "
            "config/agents.yaml metrology.toolchain"
        ),
    )
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help=(
            "run the trials through N RoutedProviders over one routing, "
            "transcript store and cost meter (one per worker, OQ-20); the "
            "evidence is sorted by trial index before the manifest is "
            "hashed, so the admission digests are identical at any N. "
            "Default: 1"
        ),
    )
    p.add_argument(
        "--diagnostic-order-check",
        action="store_true",
        help=(
            "DIAGNOSTIC: run the seed's own schedule in canonical "
            "sha256(specimen name) order instead of the seeded shuffle (an "
            "order-dependence probe); exit 2 always — it never writes or "
            "revokes admission"
        ),
    )
    p.add_argument(
        "--seed",
        type=int,
        default=None,
        help=(
            "run seed: selects WHICH specimens are drawn from the pool and in "
            "which order. Default: a fresh random seed per run, so the "
            "schedule cannot be predicted. The seed is recorded in the report "
            "and in the admission marker; pass a recorded seed to reproduce "
            "that run exactly"
        ),
    )
    p.add_argument(
        "--parent-budget-workspace",
        type=Path,
        help=(
            "existing configured-run workspace whose durable aggregate ledger "
            "must also authorize every fresh metrology call"
        ),
    )
    p.add_argument(
        "--parent-budget-run-id",
        help="exact existing durable run id for --parent-budget-workspace",
    )
    p.add_argument(
        "--parent-budget-total",
        type=_positive_usd,
        metavar="USD",
        help=(
            "immutable aggregate limit already recorded for the parent run; "
            "must match it exactly"
        ),
    )
    # THE ONE COMMAND ENTITLED TO `council/`: metrology's home IS that directory and
    # README documents `--workspace council`. It constructs no Engine.
    p.set_defaults(func=cmd_metrology, container_allow=("council",))

    p = sub.add_parser(
        "ingest-five",
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help=(
            "preflight and register a pinned task roster spanning dbt, dlt, "
            "SynSQL, SchemaPile, and WikiDBs"
        ),
        description=(
            "Resolve and digest-check every source input, build every TaskIR,\n"
            "and contamination-check the complete roster before opening the target\n"
            "Engine. Schema v1 accepts one task per source; v2 accepts an exact-count\n"
            "batch with at least one task per source. No model/provider calls occur."
        ),
        epilog=INGEST_EXIT_CODE_EPILOG,
    )
    p.add_argument(
        "--manifest",
        required=True,
        help="versioned five-source v1 or exact-count v2 ingest YAML/JSON manifest",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="run the complete read-only preflight without registering tasks",
    )
    p.set_defaults(func=cmd_ingest_five)

    p = sub.add_parser(
        "ingest-dbt",
        parents=[common],
        help=(
            "vendored Fivetran package (--package, manifest built on demand) or a "
            "prebuilt dbt manifest.json (--manifest) -> TaskIR candidates"
        ),
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--package",
        help=(
            "vendored package dir name (e.g. dbt_servicenow); builds "
            "target/manifest.json with tools/build_dbt_manifest.py when absent"
        ),
    )
    src.add_argument("--manifest", help="path to an already-compiled dbt manifest.json")
    p.add_argument("--pool", default="dbt", help="pool prefix for family ids")
    p.add_argument("--root", default=None, help="override the vendored pool root")
    p.add_argument(
        "--sources-config", default=None, help="alternate config/sources.yaml"
    )
    p.add_argument(
        "--license",
        default=None,
        help="override the catalog license for this package (--package only)",
    )
    p.add_argument(
        "--source-commit",
        default=None,
        help=(
            "full verified upstream commit for a custom --root; overrides only "
            "the catalog provenance stamped into attribution"
        ),
    )
    p.add_argument(
        "--source-upstream",
        default=None,
        help=(
            "upstream URL paired with --source-commit (defaults to the catalog "
            "record's URL)"
        ),
    )
    p.add_argument(
        "--build-root",
        default=None,
        help="manifest build dir (default <workspace>/dbt_builds/<package>)",
    )
    p.add_argument(
        "--project",
        choices=["auto", "integration_tests", "package"],
        default="auto",
        help="which dbt project to parse (default auto: integration_tests when present)",
    )
    p.add_argument(
        "--rebuild-manifest", action="store_true", help="rebuild even if a manifest exists"
    )
    p.add_argument("--no-deps", action="store_true", help="skip `dbt deps` (offline rebuild)")
    p.add_argument("--dbt-python", default=None, help="python that can import dbt.cli.main")
    p.add_argument(
        "--strict",
        action="store_true",
        help="fail if ANY connected cut had to be skipped (adapter's strict policy)",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="report candidates without registering them"
    )
    p.set_defaults(func=cmd_ingest_dbt)

    p = sub.add_parser(
        "ingest-synsql",
        parents=[common],
        help="SynSQL tables.json (+data.json leak guard) -> TaskIR candidate",
    )
    p.add_argument("--tables", required=True, help="path to SynSQL tables.json")
    p.add_argument("--db-id", required=True, help="database id inside tables.json")
    p.add_argument(
        "--data",
        default=None,
        help="optional data.json to run the answer-leak guard against",
    )
    p.add_argument("--pool", default="synsql", help="pool prefix for family ids")
    p.set_defaults(func=cmd_ingest_synsql)

    p = sub.add_parser(
        "ingest-schemapile",
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help=(
            "clustered SchemaPile index -> one TaskIR candidate "
            "(--list shows the best candidates)"
        ),
        epilog=INGEST_EXIT_CODE_EPILOG,
    )
    p.add_argument(
        "--index",
        required=True,
        help=(
            "index built by elt-taskgen-schemapile-index (selection never reads "
            "the 327MB corpus)"
        ),
    )
    p.add_argument(
        "--source",
        default=None,
        help="schemapile-perm.json (default: the schemapile pool root in config/sources.yaml)",
    )
    p.add_argument(
        "--sources-config", default=None, help="override config/sources.yaml"
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--cluster", default=None, help="ingest this cluster's representative record"
    )
    group.add_argument("--key", default=None, help="ingest this exact record key")
    p.add_argument(
        "--list",
        action="store_true",
        help="list the best candidate clusters and exit (no registration)",
    )
    p.add_argument("--limit", type=int, default=20, help="rows shown by --list")
    p.add_argument("--pool", default="schemapile", help="pool prefix for family ids")
    for flag in (
        "--min-tables",
        "--max-tables",
        "--min-columns",
        "--min-foreign-keys",
        "--min-linked-table-pairs",
        "--min-tables-with-pk",
    ):
        p.add_argument(
            flag,
            type=int,
            default=None,
            help=f"override the index's relational-filter {flag.lstrip('-')}",
        )
    p.set_defaults(func=cmd_ingest_schemapile)

    p = sub.add_parser(
        ANCHOR_COMMAND,
        aliases=[DEPRECATED_ANCHOR_COMMAND],
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help=(
            "pinned ELT-Bench = the GOAL: load the reference store + arm the "
            "contamination firewall (measurement-only, NEVER trainable)"
        ),
        description=(
            "Load the pinned ELT-Bench checkout as REFERENCE/GOAL material.\n"
            "\n"
            "ELT-Bench is NOT a training source and never becomes one. This\n"
            "command exists to do two measurement jobs:\n"
            "  1. arm the contamination firewall with the benchmark's\n"
            "     fingerprints, so colliding candidates (e.g. dbt_iterable,\n"
            "     dbt_netsuite) are REFUSED at ingest;\n"
            "  2. record the target difficulty/connector distribution that\n"
            "     `select` compares the candidate corpus against.\n"
            "\n"
            "Two hard guards keep it non-trainable: corpus/selection.py refuses\n"
            "Origin.ELTBENCH_ANCHOR as unselectable, and export/release.py\n"
            "refuses to ship it. The store lands under\n"
            "<workspace>/reference/anchors/ — separate from <workspace>/tasks/\n"
            "so it never reads like a candidate pool. A pre-relocation\n"
            "<workspace>/anchors/ store is migrated here, never ignored.\n"
            "\n"
            f"'{DEPRECATED_ANCHOR_COMMAND}' still works as a deprecated alias."
        ),
    )
    p.add_argument("--bench-root", required=True, help="path to the ELT-Bench checkout")
    p.add_argument(
        "--db",
        default=None,
        help="measure a single database (default: all; a partial arm is NOT a firewall)",
    )
    p.set_defaults(func=cmd_ingest_anchor)

    p = sub.add_parser(
        "ingest-wikidbs",
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help=(
            "WikiDBs database dir -> TaskIR carrying its REAL rows "
            "(--list shows candidates)"
        ),
        epilog=INGEST_EXIT_CODE_EPILOG,
    )
    p.add_argument(
        "--db-dir",
        default=None,
        help="path of ONE '<NNNNN> <db_name>' database directory to ingest",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="list candidate databases instead of ingesting one",
    )
    p.add_argument("--part", type=int, default=0, help="--list: part-N to scan")
    p.add_argument(
        "--start", type=int, default=0, help="--list: first index within the part"
    )
    p.add_argument(
        "--scan",
        type=int,
        default=200,
        help="--list: how many database dirs to examine (the pool is 213GB)",
    )
    p.add_argument("--limit", type=int, default=10, help="--list: rows to print")
    p.add_argument("--root", default=None, help="WikiDBs root (default: source catalog)")
    p.add_argument(
        "--family-map",
        default=None,
        help="wikidbs_family_map.csv.gz (default: config/, built by tools/)",
    )
    p.add_argument("--pool", default="wikidbs", help="pool prefix for family ids")
    p.add_argument("--sources-config", default=None, help="alternate config/sources.yaml")
    p.add_argument(
        "--no-verify-nodes",
        action="store_true",
        help="skip the on-disk node-id correspondence check (5 part listings)",
    )
    p.set_defaults(func=cmd_ingest_wikidbs)

    p = sub.add_parser(
        "ingest-dlt",
        parents=[common],
        help=(
            "dlt connector manifest (config/dlt_connectors/) -> TaskIR candidate; "
            "--all ingests every ingestible connector"
        ),
    )
    p.add_argument(
        "--connector",
        default=None,
        help="connector name, e.g. freshdesk (dlt_freshdesk is accepted too)",
    )
    p.add_argument("--all", action="store_true", help="ingest every connector")
    p.add_argument(
        "--list",
        action="store_true",
        help="list the manifests and their resource counts, ingest nothing",
    )
    p.add_argument(
        "--manifest-dir",
        default=None,
        help="manifest directory (default: config/dlt_connectors)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="build and contamination-check the TaskIR without registering it",
    )
    p.add_argument("--pool", default="dlt", help="pool prefix for family ids")
    p.add_argument("--sources-config", default=None, help="alternate config/sources.yaml")
    p.set_defaults(func=cmd_ingest_dlt)

    p = sub.add_parser(
        "pipeline",
        parents=[common],
        help=(
            "run registered tasks from all five sources with isolated concurrent "
            "agent workers, then select/freeze once at corpus scope"
        ),
    )
    p.add_argument(
        "--ingest-manifest",
        type=Path,
        default=None,
        help=(
            "preflight/register an exact typed five-source manifest before "
            "resolving the pipeline roster or constructing any provider "
            f"(candidate-count mode default: {DEFAULT_CANDIDATE_POOL}, repinned "
            "in place when the installed generator has moved)"
        ),
    )
    p.add_argument(
        "--bench-root",
        type=Path,
        default=None,
        help=(
            "ELT-Bench checkout used to arm the contamination firewall when "
            f"the workspace has no anchor store (default: ${BENCH_ROOT_ENV}, "
            f"else the {DEFAULT_BENCH_ROOT_DIRNAME} checkout beside this repository)"
        ),
    )
    p.add_argument(
        "--run-config",
        type=Path,
        default=None,
        help=(
            "typed YAML/JSON generation-run-v1 configuration; activates the "
            "arbitrary-count workflow"
        ),
    )
    p.add_argument(
        "--candidate-count",
        type=int,
        default=None,
        metavar="N",
        help=(
            "request exactly N candidate TaskIRs from the pinned v2 source pool; "
            "separate from --size, which remains a post-selection quota"
        ),
    )
    p.add_argument(
        "--source-families",
        default=None,
        help=(
            "candidate-count mode: comma-separated eligible origins (default: "
            "dbt,dlt,synsql,schemapile,wikidbs)"
        ),
    )
    p.add_argument(
        "--source-allocation",
        default=None,
        help=(
            "candidate-count mode: optional exact allocation, e.g. "
            "dbt=1,synsql=3; values must sum to N"
        ),
    )
    p.add_argument(
        "--seed",
        dest="selection_seed",
        type=int,
        default=None,
        help="candidate-count mode: reproducible source/entry selection seed",
    )
    p.add_argument(
        "--readiness-profile",
        choices=(
            "draft",
            "local-ready",
            "packaged",
            "calibrated",
            "release-ready",
            "release",
        ),
        default=None,
        help=(
            "configured milestone (default: packaged); local-ready stops after "
            "real EL/T acceptance and does not require calibration"
        ),
    )
    p.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "resume an identical seeded roster (default in configured mode); "
            "--no-resume refuses existing task ids"
        ),
    )
    p.add_argument(
        "--run-id",
        default=None,
        help="optional stable readiness/accounting id (default: config+roster digest)",
    )
    p.add_argument(
        "--export-dir",
        type=Path,
        default=None,
        help=(
            "configured packaged/release profile output root; accepted local "
            "packages are written below it by task id (default: "
            f"<workspace>/{DEFAULT_EXPORT_DIRNAME})"
        ),
    )
    p.add_argument(
        "--destination",
        default=None,
        metavar="NAMES",
        help=(
            "configured evaluator package warehouse destinations: all, one of "
            "snowflake/databricks/redshift, or a comma-separated list (default: "
            "all). The first named destination is the bundle root; the others "
            "are packaged under destinations/<name>/"
        ),
    )
    p.add_argument(
        "--admission-reference",
        type=Path,
        default=None,
        help=(
            "configured mode: exact metrology admission record to consult; the "
            "record remains at its original revocable path (default: "
            f"$ELT_TASKGEN_ADMISSION, else {DEFAULT_ADMISSION_RECORD})"
        ),
    )
    p.add_argument(
        "--http-retries",
        type=int,
        default=None,
        help="configured mode: transient HTTP retries per call (default: 4)",
    )
    p.add_argument(
        "--schema-retries",
        type=int,
        default=None,
        help="configured mode: provider schema-correction retries (default: 2)",
    )
    p.add_argument(
        "--http-timeout-seconds",
        type=float,
        default=None,
        help="configured mode: per-HTTP-attempt timeout (default: 600)",
    )
    p.add_argument(
        "--http-backoff-seconds",
        type=float,
        default=None,
        help="configured mode: exponential retry backoff base (default: 2)",
    )
    p.add_argument(
        "--reingest",
        action="store_true",
        help=(
            "with --ingest-manifest, deliberately replace same-id source "
            "identities whose content changed; stale evidence remains history"
        ),
    )
    p.add_argument(
        "--report-revalidation-request",
        type=Path,
        default=None,
        help=(
            "configured generator-only --reingest (no adapter-pin movement): "
            "canonical, content-addressed run-local capability for one exact "
            "deterministic report revalidation"
        ),
    )
    p.add_argument(
        "--task-id",
        action="append",
        default=None,
        help="registered task to run (repeatable; default: every registered task)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=4,
        help="maximum isolated task worker processes (default: 4)",
    )
    p.add_argument(
        "--size",
        type=int,
        default=None,
        help=(
            "post-processing selection count (default: every scheduled task); "
            "does not reduce worker scheduling or live-call spend"
        ),
    )
    p.add_argument(
        "--val-fraction",
        type=_unit_interval,
        default=0.1,
        help="family-stable validation fraction (default: 0.1)",
    )
    p.add_argument(
        "--require-sources",
        default="dbt,dlt,synsql,schemapile,wikidbs",
        help=(
            "comma-separated source origins that must be represented; default: "
            "all five (pass an empty string for a partial development corpus)"
        ),
    )
    p.add_argument(
        "--recalibrate",
        action="store_true",
        help="ignore matching empirical calibration caches and rerun the solver roster",
    )
    p.add_argument(
        "--allow-structural-difficulty",
        action="store_true",
        help="development only: do not require/run empirical solver calibration",
    )
    p.add_argument(
        "--release-dir",
        type=Path,
        default=None,
        help="optional new immutable corpus release directory",
    )
    p.add_argument(
        "--sandbox-attestation",
        type=Path,
        default=None,
        help="fresh sealed tier A/B sandbox attestation for a certified release",
    )
    p.add_argument(
        "--development-release",
        action="store_true",
        help="explicitly cut an unlabelled development release without tier A/B attestation",
    )
    p.add_argument(
        "--allow-unlocked-env",
        action="store_true",
        help="record rather than refuse dependency drift from uv.lock",
    )
    p.set_defaults(func=cmd_pipeline, empirical=True, require_empirical=True)

    p = sub.add_parser(
        "pipeline-status",
        parents=[common],
        help="read-only revalidation of a configured pipeline readiness report",
    )
    p.add_argument("--run-id", required=True)
    p.set_defaults(func=cmd_pipeline_status)

    p = sub.add_parser(
        "verify-local-package",
        help="verify a standalone local evaluator package without its workspace",
    )
    p.add_argument("--package", type=Path, required=True)
    p.set_defaults(func=cmd_verify_local_package)

    stage_commands: tuple[tuple[str, StageName | None, str], ...] = (
        ("generate", StageName.GENERATE, "materialize the five populations"),
        ("reference-run", StageName.REFERENCE, "reference execution + gold freeze"),
        ("review", StageName.REVIEW, "semantic authoring + council review"),
        ("attack", StageName.ATTACK, "compile and execute attack mutants"),
        (
            "validate",
            StageName.TASK_INTEGRITY,
            "run shared project-integrity checks (not an RLVR task reward)",
        ),
        (
            "validate-el",
            StageName.GATES_EXTRACT_LOAD,
            "run the EXTRACT_LOAD subtask's own acceptance battery",
        ),
        (
            "validate-t",
            StageName.GATES_TRANSFORM,
            "run the TRANSFORM subtask's own acceptance battery",
        ),
        (
            "calibrate",
            StageName.CALIBRATE,
            "difficulty measurement (structural; --empirical adds solver runs)",
        ),
        ("select", StageName.SELECT, "selection predicate (pool of one)"),
        ("release", None, "run the remaining stages and freeze the release"),
    )
    for name, until, help_text in stage_commands:
        p = sub.add_parser(name, parents=[common], help=help_text)
        p.add_argument("--task-id", required=True)
        if name == "calibrate":
            # SolverCalibrator wiring. Opt-in: a campaign costs k attempts per tier
            # per variant, so it is never a side effect of running the pipeline.
            p.add_argument(
                "--empirical",
                action="store_true",
                help=(
                    "run the PINNED solver roster on the public bundle of each "
                    "variant and fill EmpiricalDifficulty; without keys or "
                    "transcripts this is a visible skip, never invented numbers"
                ),
            )
            p.add_argument(
                "--variants",
                default=None,
                help=(
                    "comma-separated variants to calibrate: full, el, t "
                    "(default: el,t; full is a legacy diagnostic only)"
                ),
            )
            p.add_argument(
                "--recalibrate",
                action="store_true",
                help=(
                    "ignore cached per-variant results at this content hash + "
                    "roster and re-run every attempt"
                ),
            )
        if name in {"select", "release"}:
            # Release-quality operation measures difficulty by default.  The
            # escape hatch is explicit and leaves an auditable structural-only
            # selection record; it is intended for local development, not a
            # certified corpus.
            p.set_defaults(empirical=True, require_empirical=True)
            p.add_argument(
                "--allow-structural-difficulty",
                action="store_true",
                help=(
                    "development only: permit selection/release without a "
                    "current pinned solver campaign"
                ),
            )
        if until is not None and name != "calibrate":
            # THE OPERATOR'S "RUN IT AGAIN ANYWAY": engine.run skips a stage that
            # already passed at the current hash, which keeps an exporter fix from
            # reaching a certified task and makes a "re-run X" remedy a no-op.
            # --re-emit shadows the pass and APPENDS the new report.
            p.add_argument(
                "--re-emit",
                action="store_true",
                help=(
                    "re-run this stage even though it already passed at the "
                    "current content hash (re-emits whatever it produces and "
                    "appends a new report; history is never rewritten)"
                ),
            )
        if name == "release":
            p.add_argument(
                "--allow-unlocked-env",
                action="store_true",
                help=(
                    "release even though the installed runtime differs from "
                    "uv.lock; the drift is RECORDED in the release manifest"
                ),
            )
            p.add_argument(
                "--sandbox-attestation",
                type=Path,
                default=None,
                help=(
                    "fresh sealed tier A/B isolation attestation; required for "
                    "the default certified release mode"
                ),
            )
            p.add_argument(
                "--development-release",
                dest="release_mode",
                action="store_const",
                const="development",
                help=(
                    "explicitly cut an unlabelled development release without "
                    "tier A/B isolation certification"
                ),
            )
            p.set_defaults(release_mode="certified")
        if name == "calibrate":
            p.set_defaults(func=cmd_calibrate)
        else:
            p.set_defaults(func=lambda a, u=until: _run_until(a, u))

    p = sub.add_parser(
        "export",
        parents=[common],
        help="write the upstream-compatible bundle for one task (from frozen gold)",
    )
    p.add_argument("--task-id", required=True)
    p.add_argument(
        "--destination",
        choices=("snowflake", "databricks", "redshift"),
        default="snowflake",
        help="agent-facing warehouse for the combined public task",
    )
    p.add_argument(
        "--variants",
        default=_ACCEPTED_TOKEN,
        help=(
            "'accepted' (default) emits the parent bundle plus every subtask "
            "variant whose OWN battery passed at the current content hash; or "
            "a comma-separated list: full, el (extract+load), t (transform). "
            "An explicitly named variant without a passing battery is refused"
        ),
    )
    p.set_defaults(func=cmd_export)

    p = sub.add_parser(
        "score",
        parents=[common],
        help=(
            "LEGACY ONLY: score a schema-1/2 __el/__t DuckDB warehouse; "
            "schema-3 tasks use runtime verify-stage1/verify-stage2"
        ),
    )
    p.add_argument("--release", required=True, type=Path, help="frozen release dir")
    p.add_argument(
        "--unit",
        default=None,
        help="released unit id (<parent>__el or <parent>__t)",
    )
    p.add_argument(
        "--task-id", default=None, help="alias for --unit (same value)"
    )
    p.add_argument("--duckdb", required=True, type=Path, help="submitted warehouse")
    p.add_argument(
        "--population",
        default="primary",
        help="graded population to score against (default: primary)",
    )
    p.add_argument(
        "--source-schema",
        default="main",
        help="schema holding the landed source tables (default: main)",
    )
    p.add_argument(
        "--mart-schema",
        default=None,
        help="schema holding the built marts (default: the harness convention)",
    )
    p.add_argument(
        "--backend",
        default="duckdb",
        choices=("duckdb",),
        help="warehouse backend (only duckdb is implemented)",
    )
    p.add_argument("--json", action="store_true", help="print the raw RewardResult")
    p.set_defaults(func=cmd_score)

    semantic = sub.add_parser(
        "semantic",
        help="score a combined task locally against private DuckDB populations",
    )
    semantic_sub = semantic.add_subparsers(
        dest="semantic_command", required=True
    )
    p = semantic_sub.add_parser(
        "score",
        help="score one versioned combined EL+T semantic submission",
    )
    p.add_argument("--release", required=True, type=Path, help="frozen release dir")
    p.add_argument("--task-id", required=True, help="combined parent task id")
    p.add_argument(
        "--submission",
        required=True,
        help="semantic submission JSON path, or '-' to read stdin",
    )
    p.add_argument(
        "--population",
        action="append",
        choices=tuple(population.value for population in PopulationName),
        default=None,
        help=(
            "population to score (repeatable); default: every hidden graded "
            "population"
        ),
    )
    p.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="kill an attempt after this wall time (default: 60)",
    )
    p.add_argument(
        "--memory-limit-mb",
        type=int,
        default=512,
        help="DuckDB memory limit inside the worker (default: 512)",
    )
    p.add_argument(
        "--max-result-rows",
        type=int,
        default=100_000,
        help="maximum rows fetched from one mart query (default: 100000)",
    )
    p.add_argument(
        "--max-result-bytes",
        type=int,
        default=16 * 1024 * 1024,
        help="maximum scalar bytes returned by one mart (default: 16777216)",
    )
    p.add_argument(
        "--strict-diagnostic",
        action="store_true",
        help=(
            "also report the strict typed no-tolerance diagnostic per mart "
            "(diagnostic only; the reward and exit code are unchanged)"
        ),
    )
    p.add_argument(
        "--strict-shadow",
        action="store_true",
        help=(
            "additionally diagnose against a strictly typed shadow warehouse "
            "(DECIMAL/JSON keep their declared types; diagnostic only)"
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="print deterministic, indented SemanticScoreResult JSON",
    )
    p.set_defaults(func=cmd_semantic_score)

    p = sub.add_parser(
        "verify",
        parents=[common],
        help=(
            "re-verify a frozen release against its own manifest (byte pins "
            "AND the census-pinned .duckdb warehouses `shasum -c` skips)"
        ),
    )
    p.add_argument(
        "--release",
        type=Path,
        default=None,
        help="release dir to verify (default: <workspace>/release)",
    )
    p.set_defaults(func=cmd_verify_release)

    p = sub.add_parser(
        "bench-verify-corpus",
        help=(
            "strict-parse every committed YAML/JSON artifact in a pinned "
            "ELT-Bench checkout (fail closed: an empty corpus is never a pass)"
        ),
    )
    p.add_argument(
        "--bench-root", required=True, help="path to the ELT-Bench checkout"
    )
    p.set_defaults(func=cmd_bench_verify_corpus)

    runtime = sub.add_parser(
        "runtime",
        help="run and score a combined Airbyte -> warehouse -> dbt task",
    )
    runtime_sub = runtime.add_subparsers(dest="runtime_command", required=True)

    from elt_taskgen.runtime.bootstrap import DEFAULT_INSTALL_TIMEOUT_SECONDS
    from elt_taskgen.runtime.process import (
        CLOUD_EGRESS_NETWORK as _CLOUD_EGRESS_NETWORK,
        DOCKER_LANE_CLOUD_EGRESS as _DOCKER_LANE_CLOUD_EGRESS,
        DOCKER_LANE_NONE as _DOCKER_LANE_NONE,
        DOCKER_LANES as _DOCKER_LANES,
        PROXY_BRIDGE_NETWORK as _PROXY_BRIDGE_NETWORK,
    )
    from elt_taskgen.runtime.source_environment import (
        DEFAULT_AIRBYTE_READINESS_POLL_INTERVAL_SECONDS,
        DEFAULT_AIRBYTE_READINESS_TIMEOUT_SECONDS,
        DEFAULT_AIRBYTE_STABILITY_WINDOW_SECONDS,
    )

    rp = runtime_sub.add_parser(
        "install-airbyte",
        help="install a version-pinned Airbyte OSS control plane with abctl",
    )
    rp.add_argument("--chart-version", required=True)
    rp.add_argument("--abctl-version", required=True)
    rp.add_argument("--credential-out", required=True, type=Path)
    rp.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_INSTALL_TIMEOUT_SECONDS,
        help=(
            "seconds each abctl step (version, install with its image pulls, "
            "credentials) may run before it is killed; default "
            f"{DEFAULT_INSTALL_TIMEOUT_SECONDS:g} (image pulls on a slow link can "
            "exceed the one-hour runner default)"
        ),
    )
    rp.set_defaults(func=cmd_runtime_install_airbyte)

    rp = runtime_sub.add_parser(
        "bootstrap-task",
        help=(
            "create/validate a task workspace and publish its REST definition; "
            "does not create task sources or connections"
        ),
    )
    rp.add_argument("--release", required=True, type=Path)
    rp.add_argument("--task-id", required=True)
    rp.add_argument("--airbyte-url", required=True)
    rp.add_argument("--airbyte-credential", required=True, type=Path)
    rp.add_argument("--credential-out", required=True, type=Path)
    rp.add_argument(
        "--workspace-id",
        default=None,
        help="validate an existing workspace instead of creating a fresh one",
    )
    rp.set_defaults(func=cmd_runtime_bootstrap_task)

    rp = runtime_sub.add_parser(
        "prepare",
        help="create a fresh source plan and install one credential-injected task",
    )
    rp.add_argument("--release", required=True, type=Path)
    rp.add_argument("--task-id", required=True)
    rp.add_argument("--population", default="primary")
    rp.add_argument("--work-dir", required=True, type=Path)
    rp.add_argument("--environment-dir", required=True, type=Path)
    rp.add_argument("--airbyte-credential", required=True, type=Path)
    rp.add_argument(
        "--destination",
        choices=("snowflake", "databricks", "redshift"),
        default=None,
        help="optional assertion; the public task remains authoritative",
    )
    rp.add_argument("--destination-credential", default=None, type=Path)
    rp.add_argument(
        "--snowflake-credential",
        default=None,
        type=Path,
        help="legacy Snowflake alias for --destination-credential",
    )
    rp.add_argument("--custom-api-definition-id", default=None)
    rp.add_argument(
        "--airbyte-server-url",
        required=True,
        help="Airbyte URL reachable from the solver/provider runtime",
    )
    rp.set_defaults(func=cmd_runtime_prepare)

    def add_source_environment_args(parser):
        parser.add_argument("--release", required=True, type=Path)
        parser.add_argument("--task-id", required=True)
        parser.add_argument("--population", default="primary")
        parser.add_argument("--environment-dir", required=True, type=Path)
        parser.add_argument(
            "--airbyte-container",
            default="airbyte-abctl-control-plane",
            help="abctl control-plane container joined to the source network",
        )

    rp = runtime_sub.add_parser(
        "source-up",
        help=(
            "start and seed the prepared source-service population, then wait "
            "for sustained Airbyte control-plane readiness"
        ),
    )
    add_source_environment_args(rp)
    rp.add_argument(
        "--airbyte-readiness-timeout",
        type=_positive_seconds,
        default=DEFAULT_AIRBYTE_READINESS_TIMEOUT_SECONDS,
        help=(
            "maximum seconds to wait for sustained Airbyte control-plane "
            "readiness (default: "
            f"{DEFAULT_AIRBYTE_READINESS_TIMEOUT_SECONDS:g})"
        ),
    )
    rp.add_argument(
        "--airbyte-stability-window",
        type=_positive_seconds,
        default=DEFAULT_AIRBYTE_STABILITY_WINDOW_SECONDS,
        help=(
            "consecutive ready seconds required before source-up succeeds "
            f"(default: {DEFAULT_AIRBYTE_STABILITY_WINDOW_SECONDS:g})"
        ),
    )
    rp.add_argument(
        "--airbyte-readiness-poll-interval",
        type=_positive_seconds,
        default=DEFAULT_AIRBYTE_READINESS_POLL_INTERVAL_SECONDS,
        help=(
            "seconds between Airbyte readiness samples (default: "
            f"{DEFAULT_AIRBYTE_READINESS_POLL_INTERVAL_SECONDS:g})"
        ),
    )
    rp.set_defaults(func=cmd_runtime_source_up)

    rp = runtime_sub.add_parser(
        "source-down",
        help="remove one source-service population and its fresh volumes",
    )
    add_source_environment_args(rp)
    rp.set_defaults(func=cmd_runtime_source_down)

    rp = runtime_sub.add_parser(
        "provision-snowflake",
        help=(
            "provision a fresh task-scoped role/user/warehouse/database with "
            "harness-admin credentials"
        ),
    )
    rp.add_argument(
        "--admin-snowflake-credential", required=True, type=Path
    )
    rp.add_argument("--database", required=True)
    rp.add_argument("--credential-out", required=True, type=Path)
    rp.set_defaults(func=cmd_runtime_provision_snowflake)

    rp = runtime_sub.add_parser(
        "reset-snowflake",
        help="legacy/shared-principal database reset (prefer provision-snowflake)",
    )
    rp.add_argument("--snowflake-credential", required=True, type=Path)
    rp.add_argument("--database", required=True)
    rp.add_argument("--owner-role", default="AIRBYTE_ROLE")
    rp.set_defaults(func=cmd_runtime_reset_snowflake)

    rp = runtime_sub.add_parser(
        "provision-databricks",
        help="grant a solver principal one fresh schema in a Unity Catalog",
    )
    rp.add_argument(
        "--admin-databricks-credential", required=True, type=Path
    )
    rp.add_argument(
        "--solver-databricks-credential", required=True, type=Path
    )
    rp.add_argument("--catalog", required=True)
    rp.add_argument("--schema", required=True)
    rp.add_argument("--solver-principal", required=True)
    rp.add_argument(
        "--attempt-dedicated-principal",
        action="store_true",
        required=True,
        help=(
            "assert that the solver principal and its group memberships are "
            "reserved for this attempt"
        ),
    )
    catalog_mode = rp.add_mutually_exclusive_group(required=True)
    catalog_mode.add_argument(
        "--allow-create-catalog",
        action="store_true",
        help="create a fresh attempt-only Unity Catalog",
    )
    catalog_mode.add_argument(
        "--existing-dedicated-catalog",
        action="store_true",
        help="assert that --catalog already exists and is attempt-only",
    )
    rp.add_argument("--credential-out", required=True, type=Path)
    rp.set_defaults(func=cmd_runtime_provision_databricks)

    rp = runtime_sub.add_parser(
        "provision-redshift",
        help=(
            "create a fresh attempt database and solver user on an "
            "attempt-dedicated deployment"
        ),
    )
    rp.add_argument(
        "--admin-redshift-credential", required=True, type=Path
    )
    rp.add_argument(
        "--database",
        required=True,
        help="fresh per-attempt database (must not be the admin management database)",
    )
    rp.add_argument("--schema", required=True)
    rp.add_argument(
        "--s3-bucket-path",
        default=None,
        help="unique staging prefix (default: elt-bench/<database>)",
    )
    rp.add_argument("--attempt-password", default=None)
    rp.add_argument(
        "--attempt-dedicated-deployment",
        action="store_true",
        required=True,
        help=(
            "assert that the cluster/workgroup is isolated to this attempt "
            "(Redshift users are deployment-global)"
        ),
    )
    rp.add_argument("--credential-out", required=True, type=Path)
    rp.set_defaults(func=cmd_runtime_provision_redshift)

    rp = runtime_sub.add_parser(
        "run-stage1",
        help="apply submitted Terraform, trigger exact Airbyte jobs, and wait",
    )
    rp.add_argument("--work-dir", required=True, type=Path)
    rp.add_argument("--airbyte-credential", default=None, type=Path)
    rp.add_argument("--airbyte-url", default=None)
    rp.add_argument("--terraform", default="terraform")
    rp.add_argument(
        "--runner-image",
        required=True,
        help="sandbox image containing Terraform, pinned as name@sha256:digest",
    )
    rp.add_argument(
        "--runner-timeout",
        type=_positive_seconds,
        default=3600.0,
        help="seconds the sandbox runner may take per command (a positive number)",
    )
    rp.add_argument(
        "--sandbox-lane",
        choices=sorted(_DOCKER_LANES),
        default=None,
        help=(
            "container network lane for the Terraform runner. Default: "
            "'proxy-bridge' when an Airbyte server URL is given (--airbyte-url "
            "or the installed Airbyte.config.server_url), 'none' otherwise. "
            "'proxy-bridge' joins the operator-provisioned "
            f"'{_PROXY_BRIDGE_NETWORK}' Docker network and maps "
            "host.docker.internal to the host gateway so the Airbyte provider "
            "can reach the control plane; provision that network once per "
            f"host with `docker network create {_PROXY_BRIDGE_NETWORK}` "
            "before the first run. 'none' attaches no network, so terraform "
            "apply cannot reach any API. 'cloud-egress' joins the separate "
            f"'{_CLOUD_EGRESS_NETWORK}' outbound bridge without a host "
            "gateway mapping."
        ),
    )
    rp.add_argument("--poll-interval", type=float, default=10.0)
    rp.add_argument("--timeout", type=float, default=3600.0)
    rp.set_defaults(func=cmd_runtime_run_stage1)

    rp = runtime_sub.add_parser(
        "resync-stage1",
        help=(
            "trigger the same Airbyte connections a second time for the "
            "full-refresh-append protocol probe"
        ),
    )
    rp.add_argument("--work-dir", required=True, type=Path)
    rp.add_argument("--airbyte-credential", default=None, type=Path)
    rp.add_argument("--airbyte-url", default=None)
    rp.add_argument("--poll-interval", type=float, default=10.0)
    rp.add_argument("--timeout", type=float, default=3600.0)
    rp.set_defaults(func=cmd_runtime_resync_stage1)

    rp = runtime_sub.add_parser(
        "run-stage2",
        help="run the submitted dbt project independently of Stage 1 execution",
    )
    rp.add_argument("--work-dir", required=True, type=Path)
    rp.add_argument(
        "--dbt-command",
        nargs="+",
        default=["dbt"],
        help="dbt executable prefix, e.g. python -m dbt.cli.main",
    )
    rp.add_argument(
        "--runner-image",
        required=True,
        help=(
            "sandbox image containing the matching dbt adapter, pinned as "
            "name@sha256:digest"
        ),
    )
    rp.add_argument(
        "--runner-timeout",
        type=_positive_seconds,
        default=3600.0,
        help="seconds the sandbox runner may take per command (a positive number)",
    )
    rp.add_argument(
        "--sandbox-lane",
        choices=(_DOCKER_LANE_NONE, _DOCKER_LANE_CLOUD_EGRESS),
        default=None,
        help=(
            "container network lane for dbt. Default: 'none' (no network). "
            "Real warehouse execution requires the explicit 'cloud-egress' "
            f"lane, which joins the operator-provisioned '{_CLOUD_EGRESS_NETWORK}' "
            "Docker network without adding a host-gateway mapping; provision "
            f"it once with `docker network create {_CLOUD_EGRESS_NETWORK}`"
        ),
    )
    rp.add_argument(
        "--destination-credential",
        "--snowflake-credential",
        dest="destination_credential",
        default=None,
        type=Path,
        help=(
            "scoped Snowflake credential JSON forwarded through the fixed dbt "
            "environment allowlist (required for Snowflake; values are not "
            "placed in container argv)"
        ),
    )
    rp.add_argument("--project-dir", default=None, type=Path)
    rp.add_argument("--profiles-dir", default=None, type=Path)
    rp.add_argument("--target", default=None)
    rp.set_defaults(func=cmd_runtime_run_stage2)

    def add_runtime_verifier_args(parser):
        parser.add_argument("--release", required=True, type=Path)
        parser.add_argument("--task-id", required=True)
        parser.add_argument("--population", default="primary")
        parser.add_argument(
            "--destination",
            choices=("snowflake", "databricks", "redshift"),
            default=None,
            help="optional assertion; the release config is authoritative",
        )
        parser.add_argument("--destination-credential", default=None, type=Path)
        parser.add_argument(
            "--snowflake-credential",
            default=None,
            type=Path,
            help="legacy Snowflake alias for --destination-credential",
        )
        parser.add_argument(
            "--physical-container",
            "--catalog",
            dest="physical_container",
            default=None,
            help="Databricks Unity Catalog (normally read from credentials)",
        )
        parser.add_argument("--database", default=None)
        parser.add_argument(
            "--curator-details",
            action="store_true",
            help="include private expected values and per-table/mart diagnostics",
        )
        parser.add_argument(
            "--certification-strict",
            action="store_true",
            help=(
                "require exact typed content parity and reject unexpected raw "
                "tables without changing the compatibility reward"
            ),
        )

    rp = runtime_sub.add_parser(
        "verify-stage1",
        help="collect destination raw-table counts and score strict EL reward",
    )
    add_runtime_verifier_args(rp)
    rp.add_argument(
        "--expected-repetitions",
        type=int,
        default=1,
        help=(
            "expected full-refresh-append copies (use 2 only after the "
            "resync-stage1 protocol probe)"
        ),
    )
    rp.set_defaults(func=cmd_runtime_verify_stage1)

    rp = runtime_sub.add_parser(
        "verify-stage2",
        help="query destination marts and score them against private gold",
    )
    add_runtime_verifier_args(rp)
    rp.set_defaults(func=cmd_runtime_verify_stage2)

    rp = runtime_sub.add_parser(
        "verify-end-to-end",
        help="apply the original reward rule: EL must pass before T is scored",
    )
    add_runtime_verifier_args(rp)
    rp.set_defaults(func=cmd_runtime_verify_end_to_end)

    certification_parser = runtime_sub.add_parser(
        "certification",
        help=(
            "trusted runtime-certification lifecycle: attest, begin, record, "
            "cleanup, and complete"
        ),
    )
    certification_sub = certification_parser.add_subparsers(
        dest="certification_command", required=True
    )

    def add_certification_context(parser, *, include_store=True):
        parser.add_argument("--release", required=True, type=Path)
        parser.add_argument("--task-id", required=True)
        if include_store:
            parser.add_argument(
                "--certification-store",
                required=True,
                type=Path,
                help=(
                    "external immutable certification store "
                    "(never inside the release)"
                ),
            )

    rp = certification_sub.add_parser(
        "attest",
        help=(
            "run the real host isolation preflight and mint a release-bound "
            "tier-A/B sandbox attestation"
        ),
    )
    add_certification_context(rp, include_store=False)
    rp.add_argument(
        "--mount-root",
        required=True,
        type=Path,
        help="credential-free read-only/captured input mount used by the run",
    )
    rp.add_argument("--out", required=True, type=Path)
    rp.add_argument("--agents-config", default=None, type=Path)
    rp.add_argument("--image-digest", default="")
    rp.add_argument("--workspace-template-sha256", default="")
    rp.set_defaults(func=cmd_runtime_certification_attest)

    rp = certification_sub.add_parser(
        "attest-unbound",
        help=(
            "run the real host isolation preflight and mint a pre-freeze "
            "tier-A/B sandbox attestation"
        ),
    )
    rp.add_argument(
        "--mount-root",
        required=True,
        type=Path,
        help="credential-free read-only/captured input mount used by the run",
    )
    rp.add_argument(
        "--out",
        required=True,
        type=Path,
        help="new immutable JSON path; an existing path is never overwritten",
    )
    rp.add_argument("--agents-config", default=None, type=Path)
    rp.add_argument("--image-digest", default="")
    rp.add_argument("--workspace-template-sha256", default="")
    rp.set_defaults(func=cmd_runtime_certification_attest_unbound)

    rp = certification_sub.add_parser(
        "begin",
        help="open the random pending nonce and bind its sandbox attestation",
    )
    add_certification_context(rp)
    rp.add_argument("--sandbox-attestation", required=True, type=Path)
    rp.set_defaults(func=cmd_runtime_certification_begin)

    rp = certification_sub.add_parser(
        "status",
        help="report low-level state and whether the cleanup lifecycle verifies",
    )
    add_certification_context(rp)
    rp.set_defaults(func=cmd_runtime_certification_status)

    for stage in (1, 2):
        rp = certification_sub.add_parser(
            f"record-stage{stage}",
            help=(
                f"revalidate public runtime/evaluator JSON and seal Stage {stage} "
                "evidence"
            ),
        )
        add_certification_context(rp)
        rp.add_argument("--population", required=True, choices=[p.value for p in PopulationName])
        rp.add_argument("--execution-receipt", required=True, type=Path)
        rp.add_argument("--evaluator-result", required=True, type=Path)
        rp.set_defaults(
            func=(
                cmd_runtime_certification_record_stage1
                if stage == 1
                else cmd_runtime_certification_record_stage2
            )
        )

    rp = certification_sub.add_parser(
        "record-observations",
        help="seal the closed release-matrix observation roster for this attempt",
    )
    add_certification_context(rp)
    rp.add_argument(
        "--observed-versions",
        required=True,
        type=Path,
        help="JSON mapping emitted by the trusted runtime observer",
    )
    rp.set_defaults(func=cmd_runtime_certification_record_observations)

    rp = certification_sub.add_parser(
        "record-cleanup",
        help=(
            "accept a sealed AttemptCopies cleanup receipt only after all "
            "stage evidence and observations exist"
        ),
    )
    add_certification_context(rp)
    rp.add_argument("--cleanup-receipt", required=True, type=Path)
    rp.set_defaults(func=cmd_runtime_certification_record_cleanup)

    rp = certification_sub.add_parser(
        "complete",
        help="publish certification only after the trusted cleanup gate passes",
    )
    add_certification_context(rp)
    rp.set_defaults(func=cmd_runtime_certification_complete)

    rp = runtime_sub.add_parser(
        "certify",
        help=(
            "crash-resumable trusted finish: record all evidence, require "
            "cleanup, complete certification, then promote difficulty"
        ),
    )
    add_certification_context(rp)
    rp.add_argument(
        "--run-spec",
        required=True,
        type=Path,
        help=(
            "secret-free JSON path manifest for stage receipts/results, runtime "
            "observations, sandbox attestation, and cleanup receipt"
        ),
    )
    rp.add_argument(
        "--difficulty-out",
        required=True,
        type=Path,
        help="new immutable runtime-certified difficulty JSON path",
    )
    rp.add_argument(
        "--workspace",
        default=None,
        type=Path,
        help="legacy difficulty-evidence fallback for an older release",
    )
    rp.set_defaults(func=cmd_runtime_certify)

    rp = runtime_sub.add_parser(
        "certify-difficulty",
        help=(
            "emit a sealed difficulty label only when empirical EL/T solver "
            "evidence and live runtime certification both verify"
        ),
    )
    rp.add_argument("--release", required=True, type=Path)
    rp.add_argument("--task-id", required=True)
    rp.add_argument(
        "--certification-store",
        required=True,
        type=Path,
        help="external immutable certification store (never inside the release)",
    )
    rp.add_argument(
        "--workspace",
        default=None,
        type=Path,
        help=(
            "legacy accepted workspace fallback when the release predates "
            "embedded difficulty evidence"
        ),
    )
    rp.add_argument(
        "--out",
        default=None,
        type=Path,
        help="optional new immutable JSON output path (also prints to stdout)",
    )
    rp.set_defaults(func=cmd_runtime_certify_difficulty)

    p = sub.add_parser(
        "triage",
        parents=[common],
        help=(
            "ADVISORY audit triage: label queued tasks for a human reviewer "
            "(never approves — only 'audit approve' signs anything off)"
        ),
    )
    p.add_argument(
        "--task-id",
        default=None,
        help="triage one task (default: every task in the audit queue)",
    )
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser(
        "admission",
        help="council admission records: audit what ran under a (revoked) admission",
    )
    admission_sub = p.add_subparsers(dest="admission_command", required=True)
    pa = admission_sub.add_parser(
        "audit",
        parents=[common],
        help=(
            "READ-ONLY: list every recorded trajectory of this workspace that "
            "ran under an admission record (by its stamped record path or "
            "the tombstone's withdrawn fingerprint)"
        ),
    )
    pa.add_argument(
        "--revoked",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help=(
            "the admission record (or tombstone) to audit against; with no "
            "PATH, the record this workspace consults ($ELT_TASKGEN_ADMISSION "
            "or <workspace>/state/council.live_admitted)"
        ),
    )
    pa.set_defaults(func=cmd_admission_audit, container_allow=("council",))

    p = sub.add_parser(
        "audit", help="human audit queue: list pending sign-offs, approve, reject"
    )
    audit_sub = p.add_subparsers(dest="audit_command", required=True)

    pa = audit_sub.add_parser(
        "list",
        parents=[common],
        help="pending borderline collisions per task, with fingerprints + hashes",
    )
    pa.set_defaults(func=cmd_audit_list)

    pa = audit_sub.add_parser(
        "approve",
        parents=[common],
        help="write an AuditApproval bound to the task's CURRENT content hash",
    )
    pa.add_argument("task_id")
    pa.add_argument("--reviewer", required=True, help="human reviewer name (recorded)")
    pa.add_argument(
        "--labels",
        nargs="*",
        default=None,
        metavar="K=V",
        help="per-axis review labels, e.g. --labels license=ok risk=low",
    )
    pa.add_argument(
        "--approved-at",
        default=None,
        help="ISO-8601 approval time (default: current UTC time)",
    )
    pa.set_defaults(func=cmd_audit_approve)

    pa = audit_sub.add_parser(
        "reject",
        parents=[common],
        help="record a human rejection bound to the CURRENT content hash (fatal)",
    )
    pa.add_argument("task_id")
    pa.add_argument("--reason", required=True, help="why the task is rejected")
    pa.set_defaults(func=cmd_audit_reject)

    # RE-INGEST IS AN OPERATOR DECISION, NOT A SIDE EFFECT: two extractions of one
    # source can share a task_id with different CONTENT, and overwriting left every
    # recorded report bound to an identity the workspace no longer held. The engine
    # refuses that once the task is past DRAFT; this is the escape.
    for _name in REINGESTING_COMMANDS:
        _p = sub.choices.get(_name)
        if _p is not None:
            _p.add_argument(
                "--reingest",
                action="store_true",
                help=(
                    "overwrite an existing task of the same id whose content "
                    "hash differs (every report recorded against the old "
                    "identity becomes stale evidence)"
                ),
            )

    return parser


def main(argv: list[str] | None = None) -> int:
    """The CLOSED error boundary (see the exit-code contract above).

    The container guard runs HERE, on `--workspace` itself, so it also covers the
    writers that never construct an Engine. The three "could not measure" exception
    types become a one-line message and exit 2 instead of a traceback and exit 1 —
    the code the contract reserves for a rejected task. Bare Exception is
    deliberately NOT caught: a real bug must still traceback."""
    args = build_parser().parse_args(argv)
    workspace = getattr(args, "workspace", None)
    if workspace is not None:
        _assert_workspace_is_not_a_container(
            Path(workspace), allow=getattr(args, "container_allow", ())
        )
    try:
        # The agent-harness mode goes into the environment before any command
        # reads a routing document (a configured run re-applies its spec's).
        if hasattr(args, "agent_harness"):
            apply_agent_harness(getattr(args, "agent_harness", None))
        return int(args.func(args))
    except CliUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except InfrastructureFailure as exc:
        print(f"could not measure: {exc}", file=sys.stderr)
        return 2
    except EngineError as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

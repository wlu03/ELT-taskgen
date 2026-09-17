"""Propose route-scoped repairs and certify them on trial copies.

Views, diff validation, revalidation, and discrimination checks fail closed. The changed
artifact determines the repair route, and only validated diffs reach the live tree.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Iterator, NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from elt_taskgen import repair as repair_mod
from elt_taskgen.engine import EngineError
from elt_taskgen.models import (
    CouncilRole,
    MAX_REPLACE_JSON_EDITS,
    PopulationName,
    RepairEdit,
    RepairEditOp,
    RepairPatch,
    RepairRoute,
    Severity,
    TaskIR,
    TaskVariant,
    _json_values_equal_exact,
    _parse_canonical_json_text,
    canonical_json,
    readable_json,
    sha256_hex,
    task_from_json,
)
from elt_taskgen.review import council
from elt_taskgen.review.tools.projection import (
    DIAGNOSTICS_VERSION,
    Diagnostic,
    DiagnosticSource,
    DiagnosticTripwire,
    RejectionCode,
    assert_value_free,
    codes_for,
    project_compile,
    project_gate,
    project_gate_battery,
    project_gate_details,
    project_promotion,
    project_prose_problems,
    serialize_for_transport,
)
from elt_taskgen.review.session import canonical_list_index
from elt_taskgen.training.models import _CODE_RE

if TYPE_CHECKING:  # avoid an import cycle at module load
    from elt_taskgen.engine import Engine

__all__ = [
    "ADJUDICATION_KIND",
    "AgenticRepairProposer",
    "CALIBRATE_EMPIRICAL_TAG",
    "CURRENCY_PREREQUISITES",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_USD_PER_FAILURE",
    "DEFAULT_NESTED_CEILING_USD",
    "DEFAULT_REPAIR_PROPOSER_MODE",
    "DEFAULT_ROUTES_BOUNDED",
    "DiffResult",
    "DiscriminationWeakened",
    "PatchApplicationError",
    "PatchRejected",
    "ProposerAttempt",
    "ProposerStep",
    "REPAIR_PROPOSER_MODES",
    "RepairAttemptRecord",
    "RepairAdjudicationError",
    "RepairOutcome",
    "RepairSettings",
    "RejectionCode",
    "RepairProposer",
    "ROLE_NAME",
    "ROUTE_ALLOWLIST",
    "SESSION_RECORD_DIRNAME",
    "STATUS_NEEDS_ADJUDICATION",
    "STATUS_ROUTE_NOT_BOUNDED",
    "SUBMIT_DEADLINE_S",
    "ScopeViolation",
    "TrialVerdict",
    "TrialWorkspaceError",
    "VERIFIER_INERT_IR_PATHS",
    "apply_patch_text",
    "attempt_patch",
    "budget_limit_scope",
    "check_population_cheap",
    "failure_detail",
    "load_repair_adjudication",
    "parse_patch",
    "patch_schema",
    "project_compile",
    "project_promotion",
    "project_prose_problems",
    "project_rejection",
    "propose_patch",
    "proposer_attempt_budget",
    "queue_adjudication",
    "recorded_certify_results",
    "repair_adjudication_path",
    "repair_settings",
    "revalidation_stages",
    "session_record_dir",
    "session_record_path",
    "session_view",
    "trial_phase",
    "trial_workspace",
    "validate_attack_flag_limits",
    "view_for_route",
    "witness_problem_codes",
    "withheld_population_conditions",
    "CONDITION_WITHHELD",
    "condition_path_is_withheld",
    "population_conditions_block",
    "resolved_condition_slot",
]


#: Provider role name. Deliberately NOT a CouncilRole: it proposes edits, it
#: reviews nothing.
ROLE_NAME = "repair_proposer"

#: Proposer attempts per failure when config/agents.yaml says nothing.
DEFAULT_MAX_ATTEMPTS = 2

STATUS_NEEDS_ADJUDICATION = "needs_adjudication"
ADJUDICATION_KIND = "repair_abstained"
#: `RepairAttemptRecord.status` of a proposal that stopped on a HARNESS fault.
STATUS_HALTED = "halted"

#: COMMITTED installs the patch. NEEDS_ADJUDICATION (including uncommitted
#: `not_applicable`) and BLOCKED_LIMIT create salted BLOCKED rows; HALTED records
#: an infrastructure FAIL and exits 2. Blocked and halted outcomes spend no round.
DISPOSITION_COMMITTED = "committed"
DISPOSITION_NEEDS_ADJUDICATION = "needs_adjudication"
DISPOSITION_BLOCKED_LIMIT = "blocked_limit"
DISPOSITION_HALTED = "halted"
REPAIR_DISPOSITIONS: frozenset[str] = frozenset(
    {
        DISPOSITION_COMMITTED,
        DISPOSITION_NEEDS_ADJUDICATION,
        DISPOSITION_BLOCKED_LIMIT,
        DISPOSITION_HALTED,
    }
)

#: `RepairOutcome.limit` kind of a USD stop, and the `limit_scope` a USD stop
#: must name: the `BudgetExceededError.scope` that tripped. Only `role` (the
#: session's own `max_usd`) is the agent-attributable `LIMIT_USD` of SoT T4,
#: which the engine blocks with a salt; `task` and `total` are the transport's
#: `PROVIDER_FAULT`, which the engine halts on.
LIMIT_USD = "usd"
LIMIT_SCOPE_ROLE = "role"
BUDGET_LIMIT_SCOPES: frozenset[str] = frozenset({"role", "task", "total"})
#: Class NAME of the role-cap subclass providers.py raises for the session's
#: own `max_usd`. Read by name through the MRO so this module imports nothing
#: from the provider layer; absent, the exception's `scope` attribute decides.
ROLE_CAP_EXCEPTION_NAME = "RoleCapExceeded"

#: Failure evidence is truncated (a view is a view, not a dump); the truncation
#: is marked so nobody mistakes it for the whole record.
MAX_FAILURE_DETAIL_CHARS = 4000

#: Trial calibration substitutes a structural runner for the empirical campaign;
#: the live ladder runs that campaign once after commit. `agents_config` selects
#: the roster used by the substitute.
CALIBRATE_EMPIRICAL_TAG = "calibrate_empirical"
#: The empirical runner's function name, the fallback when the tag was lost.
_CALIBRATE_EMPIRICAL_NAME = "run_calibrate_empirical"

#: The roadmap's name for the validated artifact diff `trial_phase` returns.
DiffResult = repair_mod.ArtifactDiff


# Errors — every one of these means NO COMMIT (the trial copy is discarded)

class PatchRejected(RuntimeError):
    """A proposed patch was refused. The real workspace is untouched.

    `code` is the projection-facing `RejectionCode` value (see
    `review/tools/projection.py`): the only thing a later session's view may
    carry about this rejection. The sentence itself stays in
    `ProposerAttempt.reason` for humans and is never sent to a model."""

    def __init__(self, message: str = "", *, code: str = "") -> None:
        super().__init__(message)
        self.code = str(code)
        #: The `TrialVerdict` `trial_phase` attaches to a red re-validation
        #: or a tripped discrimination guard (None for every other raise).
        self.verdict: "TrialVerdict | None" = None


class PatchApplicationError(PatchRejected):
    """The patch's edits could not be applied cleanly (missing/ambiguous
    anchor, no-op edit, unknown locator). Never applied partially."""


class ScopeViolation(PatchRejected):
    """The trial diff left the claimed route's allowlist. THIS is the
    anti-reward-hack boundary: a 'specification repair' that moved reference
    SQL, gold, populations, or attack cases dies here."""


class RevalidationFailed(PatchRejected):
    """The patch applied in scope but the invalidated stages did not go green
    on the trial copy. Only a fully green re-validation may commit."""


class DiscriminationWeakened(PatchRejected):
    """The patch went green but a mutant that USED to lose reward now keeps it.

    The fourth trust layer, and the only reason `populations.*.literal_rows` is
    admissible to the POPULATION route at all: the discrimination matrix is
    re-measured through the one reward implementation and must not weaken."""


class TrialWorkspaceError(RuntimeError):
    """The harness could not create an isolated repair trial.

    This is infrastructure, never evidence that a proposed patch is bad.  The
    engine's exception taxonomy names this class explicitly so neither proposer
    retries it as a model failure nor spends a repair round after it.
    """


# Route scope: which artifacts / which JSON fields each route may move

#: Route allowlists are task-relative; a trailing `/` means prefix. Gold and
#: populations are derived and editable only through IR fields, while RUNTIME
#: accepts no proposed patch.
ROUTE_ALLOWLIST: dict[RepairRoute, tuple[str, ...]] = {
    RepairRoute.SPECIFICATION: ("task_ir.json",),
    RepairRoute.REFERENCE: ("task_ir.json", "answer_key/reference/"),
    RepairRoute.POPULATION: ("task_ir.json",),
    RepairRoute.RUNTIME: (),
}

#: JSON paths inside task_ir.json each route may move ("*" is a list index).
#: That one file holds prose AND reference SQL AND attack cases, so the path
#: allowlist above cannot separate them — this does.
ROUTE_IR_PATHS: dict[RepairRoute, tuple[str, ...]] = {
    RepairRoute.SPECIFICATION: (
        "title",
        "solver_prompt",
        "tables.*.description",
        # `grain` is public specification text and may be repaired under normal
        # trial revalidation; executable plan fields and key columns stay immutable.
        "marts.*.grain",
        "tables.*.columns.*.description",
        "marts.*.description",
        "marts.*.columns.*.description",
        # Plan-op descriptions are declarative authoring text.  The compiler
        # does not consume them, but the semantic author does; allowing this
        # one leaf lets a repair remove a contradiction at its source.  Op
        # kind/tables/columns/predicate/details remain immutable here.
        "marts.*.plan.ops.*.description",
    ),
    # REFERENCE may change only verifier-read SQL; inert metadata stays outside
    # every route so hash-only edits cannot reuse stale verdicts.
    RepairRoute.REFERENCE: (
        "reference.sql_by_mart.*",
    ),
    RepairRoute.POPULATION: (
        "populations.*.conditions",
        "populations.*.conditions.*",
        "populations.*.scale.*",
        # `literal_rows` can repair missing counterfactual rows but also weaken
        # discrimination, so admit it only behind `attempt_patch`'s baseline guard.
        "populations.*.literal_rows",
        "populations.*.literal_rows.**",
    ),
    RepairRoute.RUNTIME: (),
}


#: Verifier-inert TaskIR fields are outside every route; changes limited to them
#: are rejected as `patch_noop` before allowlist checks.
VERIFIER_INERT_IR_PATHS: tuple[str, ...] = (
    "reference.dialect",
    "reference.implementation_id",
    "reference.load_notes",
    "reference.provenance",
    "reference.version",
)


def _path_allowed(rel_to_task: str, route: RepairRoute) -> bool:
    for pattern in ROUTE_ALLOWLIST[route]:
        if pattern.endswith("/"):
            if rel_to_task.startswith(pattern):
                return True
        elif rel_to_task == pattern:
            return True
    return False


def _ir_pattern_matches(json_path: str, pattern: str) -> bool:
    """One dotted JSON path against one allowlist pattern: "*" matches one
    segment; a trailing "**" matches one-or-more."""
    parts = json_path.split(".")
    pat = pattern.split(".")
    if pat[-1] == "**":
        head = pat[:-1]
        return len(parts) > len(head) and all(p == "*" or p == q for p, q in zip(head, parts))
    if len(pat) != len(parts):
        return False
    return all(p == "*" or p == q for p, q in zip(pat, parts))


def _ir_path_allowed(json_path: str, route: RepairRoute) -> bool:
    """Does one moved dotted JSON path fall inside a route's field allowlist?

    "*" matches one segment; a trailing "**" matches one-or-more, which
    `populations.*.literal_rows` needs because its diff surfaces at table
    granularity on a row-count change and at CELL granularity on an in-place
    edit. Both must be equally in scope.
    """
    # `.get`, not `[]`: FATAL is not a patchable route, so asking about it must
    # answer "no field is in scope", not raise.
    return any(_ir_pattern_matches(json_path, pattern) for pattern in ROUTE_IR_PATHS.get(route, ()))


def _verifier_inert(json_path: str) -> bool:
    """Is this moved task_ir.json field one no certifier reads?"""
    return any(_ir_pattern_matches(json_path, pattern) for pattern in VERIFIER_INERT_IR_PATHS)


# Route-scoped views (the information barrier)

def _private_fragments(task: TaskIR, route: RepairRoute) -> list[str]:
    """Normalized private-SQL fragments that must not reach THIS route's view.

    Reuses the council's leak detector so 'private' means the same thing in both
    places. For the reference route the reference SQL is the view's legitimate
    subject, so only attack MUTATIONS stay private."""
    by_source = council._private_sql_fragments(task)
    # Containment, not equality: a mutant's shingles are offset from the
    # reference's by whatever the mutation inserted, so comparing against the
    # whole normalized reference text is the honest test.
    reference_text = " ".join(
        council._normalize(sql)
        for _, sql in sorted((task.reference.sql_by_mart or {}).items())
    ) if task.reference is not None else ""
    # Allow exact normalized SQL fragments already published by MartSpec; all
    # other reference or mutant material remains private and fail-closed.
    specification_public_text = (
        _normalize(council.render_view(CouncilRole.SEMANTIC_AUTHOR, task))
        if route is RepairRoute.SPECIFICATION
        else ""
    )
    fragments: set[str] = set()
    for label, frags in by_source.items():
        if route is RepairRoute.REFERENCE:
            if label.startswith("reference:"):
                continue
            fragments.update(f for f in frags if f not in reference_text)
        elif route is RepairRoute.SPECIFICATION:
            fragments.update(f for f in frags if f not in specification_public_text)
        else:
            fragments.update(frags)
    return sorted(fragments)


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


_WITHHELD = "[withheld: private material]"

#: Per-population MEASURED reward values, in both shapes the gate battery emits
#: them: the evidence matrix and the failing-mutant prose.
_MEASURED_REWARD_RE = re.compile(
    r"\b(development|primary|resampled|counterfactual|stress)\b"
    r"(\s*=\s*|\s*,\s*got\s+)"
    r"-?\d+(?:\.\d+)?",
    re.IGNORECASE,
)

#: What a redacted MEASUREMENT is replaced by. Deliberately free of the words
#: the D1 detector refuses, so redacted evidence passes the gate. Distinct
#: from `_WITHHELD` above, which marks scrubbed PRIVATE MATERIAL — defining
#: this as `_WITHHELD` silently rebound that one and changed every private
#: scrub marker in the view.
_WITHHELD_MEASUREMENT = "[withheld: measurement]"

#: The PHRASE the detector refuses outright, independent of any number.
_MEASURED_REWARD_PHRASE_RE = re.compile(r"\bmeasured reward\b", re.IGNORECASE)

#: Reveal only that predicted populations disagreed, never the measured count.
_MEASURED_POPULATION_COUNT_RE = re.compile(
    r"(does\s+not\s+match[^;]*?\bon\s+)\d+(\s+populations?|\s+population\(s\))",
    re.IGNORECASE,
)


def _fragment_pattern(fragment: str) -> re.Pattern[str]:
    """Whitespace-flexible, case-insensitive matcher for one normalized
    fragment, so a fragment found in NORMALIZED text can be excised from the
    ORIGINAL text at its own granularity."""
    return re.compile(
        r"\s+".join(re.escape(tok) for tok in fragment.split()),
        re.IGNORECASE,
    )


@lru_cache(maxsize=4096)
def _cached_fragment_pattern(fragment: str) -> re.Pattern[str]:
    return _fragment_pattern(fragment)


def _scrub(text: str, fragments: list[str]) -> str:
    """Excise private material from the evidence at the granularity of the
    material itself. Withholding is explicit — never silent.

    SUBSTRING EXCISION, NOT LINE BLANKING: `failure_detail()` emits single-line
    canonical JSON, so blanking lines was all-or-nothing — one match destroyed
    the whole evidence payload and a near-miss passed all of it through.
    """
    if not fragments:
        return text
    scrubbed = text
    # Longest first: a long fragment must not be pre-empted by one of its own
    # sub-shingles, which would leave the tail of the leak in place.
    for fragment in sorted(fragments, key=len, reverse=True):
        if not fragment:
            continue
        scrubbed = _cached_fragment_pattern(fragment).sub(_WITHHELD, scrubbed)
    return scrubbed


def _redact_measured_rewards(text: str, route: RepairRoute) -> str:
    """Remove measured per-population rewards from evidence recursively.

    Preserve approved structure and predictions while replacing private measured values
    before any proposer view is assembled.
    """
    del route
    # Neutralize `measured reward` after numeric redaction so the redactor cannot
    # emit text its own measured-value gate rejects.
    text = _MEASURED_REWARD_RE.sub(r"\1\g<2>" + _WITHHELD_MEASUREMENT, text)
    text = _MEASURED_POPULATION_COUNT_RE.sub(r"\1" + _WITHHELD_MEASUREMENT + r"\2", text)
    return _MEASURED_REWARD_PHRASE_RE.sub(_WITHHELD_MEASUREMENT, text)


#: `DiagnosticTripwire.code` of the two view tripwires below.
VIEW_PRIVATE_FRAGMENT_CODE = "view_private_fragment"
VIEW_PRIVATE_AST_CODE = "view_private_ast"


def _view_trip(code: str, view: str) -> DiagnosticTripwire:
    """The tripwire a leaking VIEW raises: a `DiagnosticTripwire` (SoT T6
    `LEAK_TRIPWIRE`: halt, exit 2, a security incident, the producer bytes
    quarantined for `reports/leak_incident.json` and never in the message),
    which `halting_marker` / `_session_halt_marker` classify through
    `_INFRA_EXCEPTION_NAMES` so BOTH proposers halt with no second attempt
    and no round — never the plain `RuntimeError` that read as a failed
    proposal (Phase 3 review finding 2-2)."""
    return DiagnosticTripwire(
        "schema",
        code,
        source="repair_view",
        quarantined=view.encode("utf-8"),
    )


def _assert_scope(view: str, task: TaskIR, route: RepairRoute) -> None:
    """Raise `DiagnosticTripwire` when a proposer view contains private material.

    Check route-inaccessible paths, reference SQL, hidden population data, measured
    values, secrets, and forbidden identifiers before model delivery.
    """
    norm = _normalize(view)
    for frag in _private_fragments(task, route):
        if frag in norm:
            raise _view_trip(VIEW_PRIVATE_FRAGMENT_CODE, view)
    private_fps = council._private_ast_fingerprints(task)
    if route is RepairRoute.REFERENCE:
        # The reference SQL is this route's legitimate subject; only the attack
        # mutants stay private (mirrors _private_fragments).
        reference_fps: set[str] = {
            fp
            for label, fps in private_fps.items()
            if label.startswith("reference:")
            for fp in fps
        }
        private_fps = {
            label: frozenset(fps - reference_fps)
            for label, fps in private_fps.items()
            if not label.startswith("reference:")
        }
    elif route is RepairRoute.SPECIFICATION:
        # As with literal fragments above, a SQL shape derived independently
        # from the public MartSpec is not a private oracle bit.  Remove only
        # fingerprints already visible in the sanctioned author projection.
        public_fps: set[str] = set()
        public_context = council.render_view(CouncilRole.SEMANTIC_AUTHOR, task)
        for span in council._prose_sql_spans(public_context):
            public_fps |= council._sql_ast_fingerprints(span)
        if public_fps:
            private_fps = {
                label: frozenset(fps - public_fps)
                for label, fps in private_fps.items()
            }
    if not any(private_fps.values()):
        return
    view_fps: set[str] = set()
    for span in council._prose_sql_spans(view):
        view_fps |= council._sql_ast_fingerprints(span)
    if not view_fps:
        return
    for _label, fps in sorted(private_fps.items()):
        if fps & view_fps:
            # A DISGUISED copy of private SQL (canonical anonymized-AST
            # match); the label names a private artifact, so it stays out of
            # the message too.
            raise _view_trip(VIEW_PRIVATE_AST_CODE, view)


#: Closed vocabulary of the `codes.stage` entry a non-battery stage payload
#: projects to (a StagePayload has free-text `error`/`detail` fields that carry
#: exception reprs and paths; only the CLASS of failure travels).
STAGE_FAILURE_CODES: tuple[str, ...] = (
    "blocked",
    "infrastructure",
    "error",
    "failed",
    "unprojected",
)
#: Closed vocabulary of `codes.attack` for an attack-stage payload.
ATTACK_FAILURE_CODES: tuple[str, ...] = ("proposal_blocked", "mutant_leak", "attack_failed")
#: Closed vocabulary of `codes.calibrate` for a calibrate-stage payload.
CALIBRATE_FAILURE_CODES: tuple[str, ...] = ("skipped", "impossible", "trivial", "failed")
#: Closed vocabulary of `codes.review` for a review-stage payload.
REVIEW_FAILURE_CODES: tuple[str, ...] = ("findings_fatal", "findings_blocking", "no_findings")

#: The keys a projected evidence document may carry (the D1 gatekeeper in
#: `view_for_route` refuses any other key, any number and any free text).
#: No `proposals` key: the promoter's verdict codes are post-session only
#: (`test_population_failure_detail_never_carries_promoter_verdict_codes`).
_EVIDENCE_KEYS: frozenset[str] = frozenset(
    {
        "failing_gates",
        "codes",
        "projections",
        "findings",
        "blocking_finding",
        "variants",
        "skipped",
    }
)
#: Stage names (variant batteries) whose gate rows carry a `variant_local` scope.
_VARIANT_BATTERY_STAGES: dict[str, str] = {
    "gates_extract_load": "extract_load",
    "gates_transform": "transform",
}


def _is_gate_battery(dumped: Any) -> bool:
    gates = dumped.get("gates") if isinstance(dumped, dict) else None
    return (
        isinstance(gates, list)
        and bool(gates)
        and all(isinstance(g, dict) and "passed" in g for g in gates)
    )


def _is_attack_payload(dumped: Any) -> bool:
    return isinstance(dumped, dict) and (
        "rewards_by_variant" in dumped or ("rewards" in dumped and "cases" in dumped)
    )


def _is_calibrate_payload(dumped: Any) -> bool:
    return isinstance(dumped, dict) and "measurement" in dumped and "pass_rates" in dumped


def _is_review_payload(dumped: Any) -> bool:
    return isinstance(dumped, dict) and isinstance(dumped.get("findings"), list) and (
        "fatal_count" in dumped or "transcript_manifest" in dumped
    )


_STAGE_PAYLOAD_KEYS: frozenset[str] = frozenset({"detail", "error", "infrastructure", "data"})


def _is_stage_payload(dumped: Any) -> bool:
    return isinstance(dumped, dict) and bool(dumped) and set(dumped) <= _STAGE_PAYLOAD_KEYS


def _attack_failure_code(dumped: dict) -> str:
    detail = str(dumped.get("detail", ""))
    if "BLOCKED" in detail or dumped.get("rejected_proposals"):
        return "proposal_blocked"
    if "LEAK" in detail or "measured reward" in detail.lower():
        return "mutant_leak"
    return "attack_failed"


def _stage_failure_code(dumped: dict) -> str:
    data = dumped.get("data")
    if isinstance(data, dict) and str(data.get("blocked_on", "")).strip():
        return "blocked"
    if str(dumped.get("infrastructure", "") or "").strip():
        return "infrastructure"
    if str(dumped.get("error", "") or "").strip():
        return "error"
    return "failed"


def _calibrate_projection(dumped: dict) -> dict:
    skipped = bool(str(dumped.get("skipped_reason", "") or "").strip())
    impossible = tuple(str(v) for v in (dumped.get("impossible_variants") or ()))
    trivial = tuple(str(v) for v in (dumped.get("trivial_variants") or ()))
    if skipped:
        code = "skipped"
    elif impossible:
        code = "impossible"
    elif trivial:
        code = "trivial"
    else:
        code = "failed"
    return {
        "failing_gates": [],
        "codes": {"calibrate": code},
        "variants": {"impossible": sorted(impossible), "trivial": sorted(trivial)},
        "skipped": skipped,
    }


def _review_projection(dumped: dict) -> dict:
    """Council findings -> roles and severities only. A finding's `summary` and
    `detail` are model-authored prose over a role's view (the adversary's view
    carries the private population conditions), so no free text travels."""
    findings = [f for f in dumped.get("findings", []) if isinstance(f, dict)]
    rows = sorted(
        {
            (str(f.get("role", "")), str(f.get("severity", "")))
            for f in findings
            if f.get("role") and f.get("severity")
        }
    )
    if int(dumped.get("fatal_count") or 0) > 0 or any(sev == "fatal" for _, sev in rows):
        code = "findings_fatal"
    elif rows:
        code = "findings_blocking"
    else:
        code = "no_findings"
    return {
        "failing_gates": [],
        "codes": {"review": code},
        "findings": [{"role": role, "severity": sev} for role, sev in rows],
    }


def _projected_rows(
    failing: list[dict], *, task: TaskIR, route: RepairRoute, variant: str | None
) -> list[dict]:
    """The POPULATION route's `projections`: every failing gate's sanctioned
    rows, each serialized by the projector (D3, value-aware) and re-checked by
    the gatekeeper (D1) exactly as `cli._projected_gate_lines` does. A producer
    that still carries a count, a path or a value trips here — the row is never
    sent."""
    rows: list[dict] = []
    for gate in failing:
        for diag in project_gate_details(gate, variant=variant):
            wire = serialize_for_transport(diag, task=task, package=None, route=route)
            assert_value_free(wire.encode("utf-8"), task=task, route=route)
            rows.append(json.loads(wire))
    return rows


def failure_detail(
    payload: BaseModel | dict,
    *,
    route: RepairRoute | str | None = None,
    task: TaskIR | None = None,
    stage: str | None = None,
) -> str:
    """Project failure evidence into a bounded, code-only proposer view.

    Gate, attack, stage, calibration, and review payloads expose only approved codes,
    public identifiers, and sanctioned booleans. Counts, values, SQL, paths, exception
    text, and post-session promotion verdicts remain private. Unknown payloads produce
    `unprojected`; a missing route uses the specification projection.
    """
    route_v = RepairRoute(route) if route is not None else None
    dumped = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    variant = _VARIANT_BATTERY_STAGES.get(str(stage or ""))
    if _is_gate_battery(dumped):
        failing = [g for g in dumped["gates"] if not g.get("passed")]
        rows = project_gate_battery({"gates": failing})
        projected: dict[str, Any] = {
            "failing_gates": [row["gate"] for row in rows],
            "codes": {row["gate"]: row["code"] for row in rows},
        }
        if route_v is RepairRoute.POPULATION and task is not None:
            projected["projections"] = _projected_rows(
                failing, task=task, route=route_v, variant=variant
            )
    elif _is_attack_payload(dumped):
        # The promoter's outcome codes stay in `rejected_proposal.json` /
        # `audit list` (`project_promotion` is post-session).  The one repair
        # subject is separately shape- and membership-checked; no finding prose
        # or finding id enters this document.
        projected = {"failing_gates": [], "codes": {"attack": _attack_failure_code(dumped)}}
        raw_finding = dumped.get("blocking_finding")
        if raw_finding not in (None, {}) and task is not None:
            projected["blocking_finding"] = _validated_blocking_finding(
                raw_finding,
                task=task,
                evidence=canonical_json(raw_finding),
            )
    elif _is_calibrate_payload(dumped):
        projected = _calibrate_projection(dumped)
    elif _is_review_payload(dumped):
        projected = _review_projection(dumped)
    elif _is_stage_payload(dumped):
        projected = {"failing_gates": [], "codes": {"stage": _stage_failure_code(dumped)}}
    else:
        projected = {"failing_gates": [], "codes": {"stage": "unprojected"}}
    text = canonical_json(projected)
    if len(text) > MAX_FAILURE_DETAIL_CHARS:
        text = text[:MAX_FAILURE_DETAIL_CHARS] + " …[truncated]"
    return text


# The D1 gatekeeper over the evidence block (fail closed before transport)

_EVIDENCE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{0,127}")


def _evidence_trip(code: str, text: str) -> DiagnosticTripwire:
    return DiagnosticTripwire(
        "schema",
        code,
        source="repair_evidence",
        quarantined=text.encode("utf-8"),
    )


def _check_token(value: Any, *, allowed: frozenset[str] | None, text: str, what: str) -> None:
    if not isinstance(value, str) or _EVIDENCE_TOKEN_RE.fullmatch(value) is None:
        raise _evidence_trip(f"{what}_not_identifier", text)
    if allowed is not None and value not in allowed:
        raise _evidence_trip(f"{what}_not_in_vocabulary", text)


def _validated_blocking_finding(
    value: Any, *, task: TaskIR, evidence: str
) -> dict[str, Any]:
    """Validate and normalize the singular attack repair descriptor.

    This is called once by the value-aware producer and again by the D1
    gatekeeper.  Exact shape, enum vocabularies, canonical identifier order and
    membership in the task's public set are all required.  A hidden population,
    literal, finding prose/id or extra field is therefore a harness tripwire,
    never text delivered to a proposer.
    """
    from elt_taskgen.review.tools import projection as PJ

    if not isinstance(value, dict) or set(value) != {
        "role",
        "severity",
        "identifiers",
    }:
        raise _evidence_trip("blocking_finding_shape", evidence)
    roles = frozenset(role.value for role in CouncilRole)
    severities = frozenset(severity.value for severity in Severity)
    _check_token(value.get("role"), allowed=roles, text=evidence, what="role")
    _check_token(
        value.get("severity"),
        allowed=severities,
        text=evidence,
        what="severity",
    )
    identifiers = value.get("identifiers")
    if not isinstance(identifiers, list) or len(identifiers) > 16:
        raise _evidence_trip("blocking_finding_identifiers_shape", evidence)
    if identifiers != sorted(set(identifiers)):
        raise _evidence_trip("blocking_finding_identifiers_not_canonical", evidence)
    public = PJ.PublicIdentifierSet(task)
    for identifier in identifiers:
        _check_token(
            identifier,
            allowed=public.identifiers,
            text=evidence,
            what="finding_identifier",
        )
    return {
        "role": value["role"],
        "severity": value["severity"],
        "identifiers": list(identifiers),
    }


def _assert_evidence_value_free(evidence: str, task: TaskIR, route: RepairRoute) -> None:
    """Gatecheck the assembled proposer evidence block.

    Serialize through the approved projection vocabulary and reject numbers, private
    identifiers, SQL, paths, secrets, executor text, or other free-form leakage.
    """
    from elt_taskgen.review.tools import projection as PJ

    try:
        doc = json.loads(evidence)
    except (json.JSONDecodeError, ValueError):
        doc = None
    if not isinstance(doc, dict) or "failing_gates" not in doc:
        # Free text: shape rules only (no gold here).
        if PJ._MEASURED_VALUE_RE.search(evidence):
            raise _evidence_trip("measured_value", evidence)
        shape = PJ._detect_value_shapes(evidence) or PJ._detect_paths_and_secrets(evidence)
        if shape is not None:
            raise _evidence_trip(shape, evidence)
        return
    unknown = set(doc) - _EVIDENCE_KEYS
    if unknown:
        raise _evidence_trip("unknown_key", evidence)
    public = PJ.PublicIdentifierSet(task)
    gates = doc.get("failing_gates")
    if not isinstance(gates, list):
        raise _evidence_trip("failing_gates_not_a_list", evidence)
    for name in gates:
        _check_token(name, allowed=None, text=evidence, what="gate")
        if name not in public:
            raise _evidence_trip("gate_not_public", evidence)
    codes = doc.get("codes", {})
    if not isinstance(codes, dict):
        raise _evidence_trip("codes_not_a_mapping", evidence)
    vocab = {
        "stage": frozenset(STAGE_FAILURE_CODES),
        "attack": frozenset(ATTACK_FAILURE_CODES),
        "calibrate": frozenset(CALIBRATE_FAILURE_CODES),
        "review": frozenset(REVIEW_FAILURE_CODES),
    }
    for key, code in codes.items():
        _check_token(key, allowed=None, text=evidence, what="code_key")
        if key in vocab:
            _check_token(code, allowed=vocab[key], text=evidence, what="code")
        else:
            if key not in public:
                raise _evidence_trip("gate_not_public", evidence)
            _check_token(code, allowed=frozenset(PJ.GATE_CODES), text=evidence, what="code")
    for row in doc.get("projections", []):
        if not isinstance(row, dict):
            raise _evidence_trip("projection_not_an_object", evidence)
        assert_value_free(canonical_json(row).encode("utf-8"), task=task, route=route)
    roles = frozenset(r.value for r in CouncilRole)
    severities = frozenset(sv.value for sv in Severity)
    for finding in doc.get("findings", []):
        if not isinstance(finding, dict) or set(finding) != {"role", "severity"}:
            raise _evidence_trip("finding_shape", evidence)
        _check_token(finding["role"], allowed=roles, text=evidence, what="role")
        _check_token(finding["severity"], allowed=severities, text=evidence, what="severity")
    if "blocking_finding" in doc:
        if codes.get("attack") != "proposal_blocked":
            raise _evidence_trip("blocking_finding_without_blocked_attack", evidence)
        _validated_blocking_finding(
            doc["blocking_finding"], task=task, evidence=evidence
        )
    variants = doc.get("variants", {})
    if not isinstance(variants, dict) or set(variants) - {"impossible", "trivial"}:
        raise _evidence_trip("variants_shape", evidence)
    variant_names = frozenset(v.value for v in TaskVariant)
    for names in variants.values():
        if not isinstance(names, list):
            raise _evidence_trip("variants_shape", evidence)
        for name in names:
            _check_token(name, allowed=variant_names, text=evidence, what="variant")
    if "skipped" in doc and not isinstance(doc["skipped"], bool):
        raise _evidence_trip("skipped_not_boolean", evidence)


_STAGE_IN_MESSAGE_RE = re.compile(r"stage '([a-z][a-z0-9_]*)'")

#: Fallback code per rejection class for an instance raised without one.
_REJECTION_FALLBACK_CODES: dict[str, str] = {
    "PatchApplicationError": RejectionCode.PATCH_ANCHOR_NOT_FOUND.value,
    "ScopeViolation": RejectionCode.SCOPE_PATH_OUTSIDE_ALLOWLIST.value,
    "RevalidationFailed": RejectionCode.REVALIDATION_RED_UNKNOWN.value,
    "DiscriminationWeakened": RejectionCode.DISCRIMINATION_WEAKENED.value,
}


def project_rejection(exc: BaseException) -> Diagnostic:
    """A `PatchRejected` subclass -> `Diagnostic{source: rejection, code}`.

    The code is the one the raise site attached; a code-less instance falls
    back to its class's default (a `RevalidationFailed` still names its stage
    when the message does). The message — which can embed a 400-byte payload
    dump, a matrix sentence or an allowlist — never enters the projection."""
    if not isinstance(exc, PatchRejected):
        raise TypeError(
            f"project_rejection takes a PatchRejected subclass, not {type(exc).__name__}"
        )
    allowed = codes_for(DiagnosticSource.REJECTION)
    code = str(getattr(exc, "code", "") or "")
    if code not in allowed:
        code = ""
        if isinstance(exc, RevalidationFailed):
            match = _STAGE_IN_MESSAGE_RE.search(str(exc))
            if match and f"revalidation_red_{match.group(1)}" in allowed:
                code = f"revalidation_red_{match.group(1)}"
        if not code:
            for cls in type(exc).__mro__:
                if cls.__name__ in _REJECTION_FALLBACK_CODES:
                    code = _REJECTION_FALLBACK_CODES[cls.__name__]
                    break
    if not code:
        code = RejectionCode.SCOPE_PATH_OUTSIDE_ALLOWLIST.value
    return Diagnostic(source=DiagnosticSource.REJECTION, ok=False, code=code)


_PATCH_INSTRUCTIONS = (
    "Propose ONE minimal repair patch as a single JSON object with keys: "
    '"route" (exactly the route above), "artifact" (path relative to '
    'tasks/<task_id>/), "edits" (non-empty list of {"op": "replace"|"insert"'
    '|"delete"|"replace_json", "locator", "old", "new"}), "rationale", and '
    '"proposer_role". Locators: for a .json artifact use the dotted path of '
    'the field to edit (e.g. "solver_prompt"); for any other artifact use "whole" or '
    '"line:<n>". A replace edit needs the exact existing text in "old" (it '
    "must occur exactly once); an insert edit leaves \"old\" empty; a delete "
    'edit leaves "new" empty. A replace_json edit is JSON-artifact-only and '
    'uses bounded strict canonical JSON text in "old" and "new" to compare and '
    'replace one resolved typed value exactly (at most eight replace_json edits '
    'per patch). Your patch is applied to a throwaway copy and '
    "every changed artifact is diffed: anything outside this route is "
    "rejected, and nothing is committed unless re-validation passes."
)


#: What the POPULATION view prints in place of a hidden population's
#: condition the projector's gate refused (the same words `_scrub` uses for
#: private SQL in the evidence, so one label names one thing).
CONDITION_WITHHELD = "[withheld: private material]"

#: Population condition paths use canonical unsigned indices for both guards
#: and resolvers; aliases such as `-1`, `01`, whitespace, or underscores resolve
#: nowhere.
_CONDITION_PATH_RE = re.compile(r"^populations\.(?P<index>[^.]+)\.conditions(?:\.(?P<slot>[^.]+))?$")


def resolved_condition_slot(path: str) -> tuple[int, int | None] | None:
    """The `(population index, condition slot)` a dotted IR path RESOLVES to
    under the resolvers' canonical-index rule — `slot` None for the whole
    `populations.<i>.conditions` list — or None when the path names no
    condition or carries a list index in any non-canonical spelling (which
    `read_field` and `apply_patch_text` refuse too, so the withheld guard
    and the resolvers can never disagree)."""
    match = _CONDITION_PATH_RE.match(str(path))
    if match is None:
        return None
    index = canonical_list_index(match.group("index"))
    if index is None:
        return None
    raw_slot = match.group("slot")
    if raw_slot is None:
        return (index, None)
    slot = canonical_list_index(raw_slot)
    if slot is None:
        return None
    return (index, slot)


def withheld_population_conditions(task: TaskIR, *, package: Any = None) -> frozenset[tuple[int, int]]:
    """Return population-condition slots withheld from the proposer view.

    Reject conditions with private numeric, key, measured, path, secret, or executor
    shapes. Hidden populations receive stricter numeric checks; an available gold
    package extends the private-count set.
    """
    from elt_taskgen.review.tools import projection as PJ

    withheld: set[tuple[int, int]] = set()
    for index, spec in enumerate(task.populations):
        for slot, condition in enumerate(spec.conditions):
            shape = PJ.population_condition_private_shape(
                str(condition), task=task, population=spec.name, package=package
            )
            if shape is not None:
                withheld.add((index, slot))
    return frozenset(withheld)


def condition_path_is_withheld(path: str, withheld: frozenset[tuple[int, int]]) -> bool:
    """Does the dotted IR path RESOLVE to a withheld condition — one slot
    (`populations.<i>.conditions.<j>`) or the whole list
    (`populations.<i>.conditions`) holding one? Decided on the resolved
    `(index, slot)` pair (`resolved_condition_slot`), the very pair the
    resolvers address, so an aliasing spelling of an index can never reach
    a slot the guard did not see."""
    if not withheld:
        return False
    resolved = resolved_condition_slot(path)
    if resolved is None:
        return False
    index, slot = resolved
    if slot is None:
        return any(i == index for i, _slot in withheld)
    return (index, slot) in withheld


def population_conditions_block(task: TaskIR, *, package: Any = None) -> list[str]:
    """The POPULATION view's conditions block: `council._population_summary`
    (names, scales, conditions) with every withheld line printed as
    `CONDITION_WITHHELD` — the projector's gate applied to the one place the
    route shows hidden-population prose (review findings 0-0 / 0-2). The
    adversary's council view is not this function and is unchanged."""
    withheld = withheld_population_conditions(task, package=package)

    def shown(index: int, slot: int, condition: str) -> str:
        return CONDITION_WITHHELD if (index, slot) in withheld else condition

    return council._population_summary(task, with_conditions=True, condition_text=shown)


def view_for_route(
    task: TaskIR,
    route: RepairRoute,
    failure: str,
    *,
    instructions: str = _PATCH_INSTRUCTIONS,
) -> str:
    """The ONLY material a proposer sees for this route (see module docstring).

    RUNTIME raises: a runtime repair is a mechanical rebuild, so there is no
    view and no model call. `instructions` is the closing block: the one-shot
    patch format by default (byte-identical to every existing caller), the
    session tool instructions for the bounded proposer (`session_view`)."""
    route = RepairRoute(route)
    if route is RepairRoute.RUNTIME:
        raise ValueError(
            "runtime repairs are mechanical (rebuild environments/renders); "
            "no LLM view exists for the runtime route"
        )
    if route is RepairRoute.FATAL:
        raise ValueError(
            "fatal routes reject the task; there is nothing to propose"
        )

    fragments = _private_fragments(task, route)
    evidence = _redact_measured_rewards(_scrub(failure, fragments), route)
    # The D1 gatekeeper: a projected document is re-validated leaf by leaf
    # and free text is held to the shape rules; either trips BEFORE any
    # transport (never a delivery).
    _assert_evidence_value_free(evidence, task, route)
    header = [
        f"REPAIR ROUTE: {route.value}",
        f"TASK: {task.task_id}",
        "",
        "FAILURE EVIDENCE:",
        evidence,
        "",
    ]

    if route is RepairRoute.SPECIFICATION:
        body = [
            "SAFE TASK-PUBLIC AUTHOR CONTEXT (the same source schema and "
            "declarative MartSpec context supplied to the semantic author; "
            "it contains no reference SQL, attack mutations, gold outputs, "
            "population conditions, or generated rows):",
            council.render_view(CouncilRole.SEMANTIC_AUTHOR, task),
            "",
            "CURRENT AUTHORED SOLVER PROSE (editable at task_ir.json field "
            "'solver_prompt'):",
            task.solver_prompt or "(no prose authored yet)",
        ]
    elif route is RepairRoute.REFERENCE:
        lines = ["REFERENCE SQL (the only task material in this view):"]
        sql_by_mart = task.reference.sql_by_mart if task.reference is not None else {}
        if not sql_by_mart:
            lines.append("(no reference solution recorded)")
        for mart, sql in sorted(sql_by_mart.items()):
            lines.append(f"-- mart: {mart}  (task_ir.json: reference.sql_by_mart.{mart})")
            lines.append(sql)
        body = lines
    else:  # POPULATION
        body = [
            "POPULATION CONDITIONS (names, scales, and the prose conditions — "
            "the generated row arrays are not shown; a hidden population's "
            "condition that names a number other than a declared scale, a "
            "count vector, a key tuple, a path or a measured value is shown as "
            f"{CONDITION_WITHHELD} and may only be appended to):",
            *population_conditions_block(task),
            "",
            "Editable in task_ir.json at populations.<i>.conditions.<j> and "
            "populations.<i>.scale.<table>, where <i> is the 0-based position "
            "in the list above. Data is REGENERATED from these conditions — "
            "generated rows are never hand-edited.",
            *_failing_mutant_block(task, failure),
        ]

    view = "\n".join([*header, *body, "", str(instructions)])
    _assert_scope(view, task, route)
    return view


# Patch parsing (schema-enforced against the FROZEN model)

def patch_schema() -> dict:
    """JSON schema of a RepairPatch, derived from the frozen pydantic model so
    the wire shape can never drift from what validation accepts."""
    return RepairPatch.model_json_schema()


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]
        while lines and lines[-1].strip().startswith("```"):
            lines.pop()
        stripped = "\n".join(lines).strip()
    return stripped


def parse_patch(text: str) -> RepairPatch:
    """Strictly parse a provider response into a RepairPatch.

    Invalid JSON or a payload the frozen model rejects raises
    ProviderProtocolError — a malformed proposal is never repaired into a
    plausible-looking one."""
    candidate = _strip_fences(text)
    data: Any
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise council.ProviderProtocolError(
                f"repair proposer returned no JSON object: {candidate[:200]!r}"
            ) from None
        try:
            data = json.loads(candidate[start : end + 1])
        except (json.JSONDecodeError, ValueError) as exc:
            raise council.ProviderProtocolError(
                f"repair proposer returned unparseable JSON: {exc}"
            ) from exc
    try:
        return RepairPatch.model_validate(data)
    except Exception as exc:  # pydantic ValidationError and friends
        raise council.ProviderProtocolError(
            f"repair proposer payload is not a valid RepairPatch: {exc}"
        ) from exc


class _Provider(Protocol):
    def complete(self, role, prompt: str) -> str: ...


def propose_patch(
    task: TaskIR,
    route: RepairRoute,
    failure: str,
    provider: _Provider,
    *,
    attempt: int = 0,
) -> RepairPatch:
    """One route-scoped proposal. RUNTIME never calls a model (raises).

    `attempt` salts the view so a retry gets a distinct transcript key rather
    than replaying the rejected proposal verbatim."""
    route = RepairRoute(route)
    view = view_for_route(task, route, failure)
    if attempt:
        view = (
            f"{view}\n\nRETRY {attempt}: your previous proposal was rejected "
            "(it left this route's scope or failed re-validation). Propose a "
            "different, smaller patch strictly inside this route."
        )
    raw = provider.complete(ROLE_NAME, view)
    patch = parse_patch(raw)
    if patch.route is not route:
        raise ScopeViolation(
            f"proposal claims route {patch.route.value!r} but the failure "
            f"routed as {route.value!r} (a proposer cannot re-route a failure)",
            code=RejectionCode.SCOPE_ROUTE_MISMATCH.value,
        )
    return patch


# Edit application (deterministic, fail-closed)

def _json_get(doc: Any, path: str) -> Any:
    """Resolve a dotted locator. A list segment is an index ONLY in its
    canonical unsigned spelling (`session.canonical_list_index`): `-1`,
    `+1`, `01`, ` 1` or `1_0` — every alias `int()` would read — is a
    bad index, exactly as `read_field` and the withheld-condition guard
    read it (Phase 3 re-check verdict: one resolver rule everywhere)."""
    node: Any = doc
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                raise PatchApplicationError(
                    f"locator {path!r}: no key {part!r}",
                    code=RejectionCode.PATCH_ANCHOR_NOT_FOUND.value,
                )
            node = node[part]
        elif isinstance(node, list):
            index = canonical_list_index(part)
            if index is None or index >= len(node):
                raise PatchApplicationError(
                    f"locator {path!r}: bad list index {part!r}",
                    code=RejectionCode.PATCH_ANCHOR_NOT_FOUND.value,
                )
            node = node[index]
        else:
            raise PatchApplicationError(
                f"locator {path!r}: {part!r} does not address a container",
                code=RejectionCode.PATCH_ANCHOR_NOT_FOUND.value,
            )
    return node


def _json_set(doc: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    node = _json_get(doc, ".".join(parts[:-1])) if len(parts) > 1 else doc
    last = parts[-1]
    if isinstance(node, dict):
        node[last] = value
    elif isinstance(node, list):
        index = canonical_list_index(last)
        if index is None or index >= len(node):  # pragma: no cover - _json_get already resolved it
            raise PatchApplicationError(
                f"locator {path!r}: bad list index {last!r}",
                code=RejectionCode.PATCH_ANCHOR_NOT_FOUND.value,
            )
        node[index] = value
    else:  # pragma: no cover - _json_get already rejects this shape
        raise PatchApplicationError(f"locator {path!r} does not address a field")


def _anchor_code(occurrences: int) -> str:
    return (
        RejectionCode.PATCH_ANCHOR_NOT_FOUND.value
        if occurrences == 0
        else RejectionCode.PATCH_ANCHOR_AMBIGUOUS.value
    )


def _edit_string(current: str, edit: RepairEdit, where: str) -> str:
    if edit.op is RepairEditOp.REPLACE:
        occurrences = current.count(edit.old)
        if occurrences != 1:
            raise PatchApplicationError(
                f"{where}: replace anchor occurs {occurrences} times "
                f"(exactly one required): {edit.old[:60]!r}",
                code=_anchor_code(occurrences),
            )
        return current.replace(edit.old, edit.new, 1)
    if edit.op is RepairEditOp.DELETE:
        occurrences = current.count(edit.old)
        if occurrences != 1:
            raise PatchApplicationError(
                f"{where}: delete anchor occurs {occurrences} times "
                f"(exactly one required): {edit.old[:60]!r}",
                code=_anchor_code(occurrences),
            )
        return current.replace(edit.old, "", 1)
    if edit.op is RepairEditOp.INSERT:
        return current + edit.new  # INSERT: append at the locator
    raise PatchApplicationError(
        f"{where}: replace_json is valid only for a dotted locator in a JSON artifact",
        code=RejectionCode.PATCH_ANCHOR_NOT_FOUND.value,
    )


def _replace_json_value(current: Any, edit: RepairEdit, where: str) -> Any:
    """Apply one exact typed-value replacement from canonical JSON anchors."""

    try:
        expected = _parse_canonical_json_text(edit.old, field="old")
        replacement = _parse_canonical_json_text(edit.new, field="new")
    except ValueError as exc:  # defence in depth for model_construct callers
        raise PatchApplicationError(
            f"{where}: replace_json anchors are not bounded strict canonical JSON",
            code=RejectionCode.PATCH_ANCHOR_NOT_FOUND.value,
        ) from exc
    if not _json_values_equal_exact(current, expected):
        raise PatchApplicationError(
            f"{where}: typed JSON anchor does not exactly match the resolved value",
            code=RejectionCode.PATCH_ANCHOR_NOT_FOUND.value,
        )
    if _json_values_equal_exact(current, replacement):
        raise PatchApplicationError(
            f"{where}: replace_json changed nothing (no-op patch)",
            code=RejectionCode.PATCH_NOOP.value,
        )
    return replacement


def apply_patch_text(text: str, patch: RepairPatch) -> str:
    """Apply all ordered edits in a `RepairPatch` to one artifact string.

    Each locator and old-text anchor must match the current intermediate value exactly.
    Raise `PatchApplicationError` on missing, ambiguous, or invalid edits.
    """
    typed_edits = sum(
        edit.op is RepairEditOp.REPLACE_JSON for edit in patch.edits
    )
    if typed_edits > MAX_REPLACE_JSON_EDITS:
        # Defence in depth for callers that bypassed Pydantic with
        # model_construct: reject before parsing the artifact or applying a
        # prefix of an over-large edit sequence.
        raise PatchApplicationError(
            f"patch exceeds the {MAX_REPLACE_JSON_EDITS}-edit replace_json limit",
            code=RejectionCode.PATCH_ANCHOR_NOT_FOUND.value,
        )
    if patch.artifact.endswith(".json"):
        try:
            doc = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise PatchApplicationError(
                f"artifact {patch.artifact} is not valid JSON: {exc}",
                code=RejectionCode.PATCH_ARTIFACT_UNREADABLE.value,
            ) from exc
        for edit in patch.edits:
            current = _json_get(doc, edit.locator)
            if edit.op is RepairEditOp.REPLACE_JSON:
                updated = _replace_json_value(
                    current, edit, f"{patch.artifact}:{edit.locator}"
                )
                _json_set(doc, edit.locator, updated)
                continue
            if not isinstance(current, str):
                raise PatchApplicationError(
                    f"locator {edit.locator!r} addresses a "
                    f"{type(current).__name__}, not an editable string",
                    code=RejectionCode.PATCH_ANCHOR_NOT_FOUND.value,
                )
            updated = _edit_string(current, edit, f"{patch.artifact}:{edit.locator}")
            if updated == current:
                raise PatchApplicationError(
                    f"edit at {edit.locator!r} changed nothing (no-op patch)",
                    code=RejectionCode.PATCH_NOOP.value,
                )
            _json_set(doc, edit.locator, updated)
        return canonical_json(doc)

    result = text
    for edit in patch.edits:
        locator = edit.locator.strip()
        where = f"{patch.artifact}:{locator}"
        if locator == "whole":
            updated = _edit_string(result, edit, where)
        elif locator.startswith("line:"):
            try:
                index = int(locator.split(":", 1)[1]) - 1
            except ValueError as exc:
                raise PatchApplicationError(
                    f"{where}: bad line locator",
                    code=RejectionCode.PATCH_ANCHOR_NOT_FOUND.value,
                ) from exc
            lines = result.splitlines()
            if index < 0 or index >= len(lines):
                raise PatchApplicationError(
                    f"{where}: line out of range (artifact has {len(lines)} lines)",
                    code=RejectionCode.PATCH_ANCHOR_NOT_FOUND.value,
                )
            if edit.op is RepairEditOp.INSERT:
                lines.insert(index + 1, edit.new)
            else:
                lines[index] = _edit_string(lines[index], edit, where)
                if edit.op is RepairEditOp.DELETE and not lines[index].strip():
                    lines.pop(index)
            trailing = "\n" if result.endswith("\n") else ""
            updated = "\n".join(lines) + trailing
        else:
            raise PatchApplicationError(
                f"{where}: unknown locator (use 'whole' or 'line:<n>')",
                code=RejectionCode.PATCH_ANCHOR_NOT_FOUND.value,
            )
        if updated == result:
            raise PatchApplicationError(
                f"edit at {edit.locator!r} changed nothing (no-op patch)",
                code=RejectionCode.PATCH_NOOP.value,
            )
        result = updated
    return result


# Trial copy + the diff validator

def _trial_task_id(workspace: Path, task_id: str | None) -> str:
    """Validate an explicit task id, or infer the sole task for old callers.

    Production callers always pass the id.  The inference keeps the exported
    helper usable by direct certifier tests without ever broadening a trial to
    every task in a multi-task workspace.
    """
    from elt_taskgen.models import validate_task_id_segment

    if task_id is not None:
        try:
            return validate_task_id_segment(task_id)
        except (TypeError, ValueError) as exc:
            raise TrialWorkspaceError(f"unsafe repair trial task id {task_id!r}") from exc

    tasks_root = workspace / "tasks"
    try:
        if tasks_root.is_symlink() or not tasks_root.is_dir():
            raise TrialWorkspaceError("repair trial source has no regular tasks directory")
        candidates = sorted(
            child.name
            for child in tasks_root.iterdir()
            if not child.is_symlink()
            and child.is_dir()
            and not (child / "task_ir.json").is_symlink()
            and (child / "task_ir.json").is_file()
        )
    except OSError as exc:
        raise TrialWorkspaceError("repair trial could not inspect the tasks directory") from exc
    if len(candidates) != 1:
        raise TrialWorkspaceError(
            "repair trial task_id is required when the workspace does not "
            "contain exactly one task"
        )
    return candidates[0]


def _copy_regular_tree(source: Path, destination: Path) -> None:
    """Copy one relevant tree without following or preserving any symlink."""
    metadata = source.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise TrialWorkspaceError(f"repair trial source is not a regular directory: {source.name}")
    destination.mkdir(parents=True, exist_ok=False)
    with os.scandir(source) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            src = source / entry.name
            dst = destination / entry.name
            if entry.is_symlink():
                raise TrialWorkspaceError(
                    f"repair trial source contains a symbolic link: {entry.name}"
                )
            if entry.is_dir(follow_symlinks=False):
                _copy_regular_tree(src, dst)
            elif entry.is_file(follow_symlinks=False):
                shutil.copy2(src, dst, follow_symlinks=False)
            else:
                raise TrialWorkspaceError(
                    f"repair trial source contains a non-regular entry: {entry.name}"
                )


def _copy_regular_file(source: Path, destination: Path) -> None:
    metadata = source.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise TrialWorkspaceError(f"repair trial source is not a regular file: {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)


def _backup_trial_ledger(source: Path, destination: Path) -> None:
    """Take one transactionally consistent SQLite snapshot, including WAL."""
    metadata = source.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise TrialWorkspaceError("repair trial ledger is not a regular file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(
            source.as_uri() + "?mode=ro", uri=True, timeout=30.0
        )
        destination_connection = sqlite3.connect(str(destination), timeout=30.0)
        source_connection.backup(destination_connection)
        # A backup inherits the source's persistent WAL setting.  Normalize
        # the standalone snapshot before it becomes a source for a nested
        # certify copy; merely opening a WAL database creates SHM/WAL sidecars
        # in that held editing trial, violating its byte-isolation contract.
        mode = str(
            destination_connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        ).lower()
        if mode != "delete":
            raise TrialWorkspaceError(
                f"repair trial ledger refused standalone journal mode ({mode})"
            )
        integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise TrialWorkspaceError("repair trial ledger snapshot failed integrity_check")
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()


def _materialize_trial_workspace(workspace: Path, trial: Path, task_id: str) -> None:
    """Copy only state needed to certify one task; never scan workspace-wide."""
    from elt_taskgen.engine import DB_FILENAME

    trial.mkdir(parents=True, exist_ok=False)
    (trial / "tasks").mkdir()
    _copy_regular_tree(
        workspace / "tasks" / task_id,
        trial / "tasks" / task_id,
    )

    # The trial Engine needs the complete committed ledger view, but copying a
    # database file and its WAL separately can create an impossible snapshot.
    _backup_trial_ledger(
        workspace / "state" / DB_FILENAME,
        trial / "state" / DB_FILENAME,
    )

    # These are the only workspace-global inputs read by repair re-validation.
    # Build caches, provider transcripts, raw tool output, release trees and
    # every other task are intentionally outside the trial-copy boundary.
    contamination = workspace / "state" / "contamination"
    if contamination.exists() or contamination.is_symlink():
        _copy_regular_tree(contamination, trial / "state" / "contamination")
    for relative in (Path("reference") / "anchors", Path("anchors")):
        source = workspace / relative
        if source.exists() or source.is_symlink():
            _copy_regular_tree(source, trial / relative)
    audit = workspace / "audit"
    for suffix in ("approval", "rejection"):
        source = audit / f"{task_id}.{suffix}.json"
        if source.exists() or source.is_symlink():
            _copy_regular_file(source, trial / "audit" / source.name)


@contextmanager
def trial_workspace(workspace: Path, task_id: str | None = None) -> Iterator[Path]:
    """Create a disposable task workspace with an internally consistent ledger.

    Copy only the scoped task state needed for certification, yield the trial, and
    remove it unconditionally after use.
    """
    tmp: Path | None = None
    try:
        try:
            workspace = Path(workspace).resolve()
            safe_task_id = _trial_task_id(workspace, task_id)
            tmp = Path(tempfile.mkdtemp(prefix="elt-taskgen-repair-trial-"))
            trial = tmp / workspace.name
            _materialize_trial_workspace(workspace, trial, safe_task_id)
        except TrialWorkspaceError:
            raise
        except Exception as exc:
            raise TrialWorkspaceError(
                "task-scoped repair trial could not be created "
                f"({type(exc).__name__})"
            ) from exc
        yield trial
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _changed_json_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    """Dotted paths whose leaf value moved between two JSON documents."""
    if isinstance(before, dict) and isinstance(after, dict):
        moved: list[str] = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                moved.append(path)
            else:
                moved.extend(_changed_json_paths(before[key], after[key], path))
        return moved
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            return [prefix or "(root)"]
        moved = []
        for i, (b, a) in enumerate(zip(before, after)):
            moved.extend(_changed_json_paths(b, a, f"{prefix}.{i}" if prefix else str(i)))
        return moved
    return [] if before == after else [prefix or "(root)"]


def _route_from_ir_fields(moved: list[str]) -> RepairRoute:
    """Sharpen the route for the ONE file that carries everything.

    `route_from_diff` is path-based, so a task_ir.json-only diff always reads
    SPECIFICATION. Reading the MOVED FIELDS restores the router's precedence
    (REFERENCE > POPULATION > SPECIFICATION). Still the diff deciding."""
    if any(p == "reference" or p.startswith("reference.") for p in moved):
        return RepairRoute.REFERENCE
    if any(
        p == root or p.startswith(root + ".")
        for p in moved
        for root in ("populations", "attack_cases")
    ):
        return RepairRoute.POPULATION
    return RepairRoute.SPECIFICATION


def validate_scope(
    trial: Path,
    task_id: str,
    patch: RepairPatch,
    before: dict[str, str],
    after: dict[str, str],
    *,
    before_ir: str,
) -> repair_mod.ArtifactDiff:
    """THE anti-reward-hack boundary. Raises ScopeViolation on any escape.

    The route is derived from the CHANGED ARTIFACTS (sharpened by the moved
    task_ir.json fields) and must equal the patch's claim; every changed path
    and every moved JSON path must be inside that route's allowlist. A patch
    that changed NOTHING is refused here too — see the inert-repair rule."""
    diff = repair_mod.diff_snapshots(before, after)
    if not diff.changed:
        raise ScopeViolation(
            "patch changed no artifact; a repair that changes nothing can only "
            "launder a stale verdict (fail closed)",
            code=RejectionCode.PATCH_NOOP.value,
        )

    prefix = f"tasks/{task_id}/"
    ir_rel = f"{prefix}task_ir.json"
    moved_ir: list[str] = []
    if ir_rel in diff.changed:
        after_ir = (trial / ir_rel).read_text(encoding="utf-8")
        moved_ir = _changed_json_paths(json.loads(before_ir), json.loads(after_ir))

    if diff.changed == frozenset({ir_rel}) and not moved_ir:
        # Serializing task_ir.json can change its bytes even when a sequence of
        # edits cancels out semantically.  Such a formatting-only diff rotates
        # no verifier input and must never reach re-validation or commit.
        raise ScopeViolation(
            "patch changed only task_ir.json serialization; no semantic field "
            "changed (no-op patch, fail closed)",
            code=RejectionCode.PATCH_NOOP.value,
        )

    if moved_ir and diff.changed == frozenset({ir_rel}) and all(_verifier_inert(p) for p in moved_ir):
        # Every moved field is one no certifier reads: the content hash
        # would rotate with nothing a verifier could re-measure — a
        # semantic no-op the byte-level inert-repair rule above cannot see
        # (finding 3-0). Refused before any allowlist is consulted.
        raise ScopeViolation(
            f"patch moved only verifier-inert task_ir.json field(s) {moved_ir} "
            "(read by no certifier): the content hash would rotate with nothing "
            "a verifier re-measures, which can only launder a stale verdict "
            "(fail closed)",
            code=RejectionCode.PATCH_NOOP.value,
        )

    observed = repair_mod.route_from_diff(diff)
    if observed is RepairRoute.SPECIFICATION and moved_ir:
        observed = _route_from_ir_fields(moved_ir)
    if observed is not patch.route:
        raise ScopeViolation(
            f"patch claims route {patch.route.value!r} but the changed "
            f"artifacts route as {observed.value!r}: "
            f"{sorted(diff.changed)}"
            + (f" moving task_ir.json field(s) {moved_ir}" if moved_ir else "")
            + " — the diff decides, never the claim",
            code=RejectionCode.SCOPE_ROUTE_MISMATCH.value,
        )

    for rel in sorted(diff.changed):
        if not rel.startswith(prefix):
            raise ScopeViolation(
                f"patch changed {rel!r}, outside tasks/{task_id}/ (fail closed)",
                code=RejectionCode.SCOPE_PATH_ESCAPE.value,
            )
        rest = rel[len(prefix) :]
        if not _path_allowed(rest, patch.route):
            raise ScopeViolation(
                f"patch claims route {patch.route.value!r} but changed {rest!r}, "
                f"which is not in that route's allowlist "
                f"{ROUTE_ALLOWLIST[patch.route]}",
                code=RejectionCode.SCOPE_PATH_OUTSIDE_ALLOWLIST.value,
            )

    outside = [p for p in moved_ir if not _ir_path_allowed(p, patch.route)]
    if outside:
        raise ScopeViolation(
            f"patch claims route {patch.route.value!r} but moved task_ir.json "
            f"field(s) {outside} — outside that route's field allowlist "
            f"{ROUTE_IR_PATHS[patch.route]}",
            code=RejectionCode.SCOPE_FIELD_OUTSIDE_ALLOWLIST.value,
        )
    return diff


# Re-validation on the trial copy

def revalidation_stages(route: RepairRoute, failed_stage: str) -> tuple[str, ...]:
    """Stages a patch must turn green ON THE TRIAL COPY before it may commit.

    The route's invalidation set, truncated at the failing stage. Downstream
    stages are still invalidated after the commit and must re-attest at the NEW
    content hash — a stale pass can never certify."""
    from elt_taskgen.engine import StageName

    order = [s.value for s in StageName]
    if failed_stage not in order:
        raise ValueError(f"unknown stage {failed_stage!r}")
    limit = order.index(failed_stage)
    stages = {s for s in repair_mod.stages_to_rerun(route) if order.index(s) <= limit}
    stages.add(failed_stage)
    return tuple(sorted(stages, key=order.index))


#: Extra stages a literal_rows patch must re-run REGARDLESS of where the failure
#: was: the discrimination matrix is re-measured only when `attack` re-executes
#: every mutant, and `attack` scores against gold, which `reference` must
#: refreeze from the patched population first. Without both, the "after" matrix
#: is just the numbers the patch was trying to change.
LITERAL_ROWS_PROOF_STAGES: tuple[str, ...] = ("reference", "attack")

#: Trial reruns must recreate current-hash PASS rows: review before attack, then
#: attack before gates. Prepend only wired prerequisites; fixture doubles may omit
#: them, while real runners still fail closed on missing rows.
CURRENCY_PREREQUISITES: dict[str, tuple[str, ...]] = {
    "attack": ("review",),
    "gates": ("attack",),
    "gates_extract_load": ("attack",),
    "gates_transform": ("attack",),
}

#: The `blocked_on` reason (`engine.StageBlocked`, marker
#: `blocked_on:prerequisite_not_current_<stage>`) under which a
#: re-validation halts when a currency PREREQUISITE outside the route's own
#: rerun set answers non-PASS on the trial: a wait, never
#: `revalidation_red_<stage>` (finding 2-4).
BLOCKED_ON_PREREQUISITE_PREFIX = "prerequisite_not_current_"


def _with_currency_prerequisites(
    stages: list[str], stage_runners: dict, order: list[str]
) -> list[str]:
    """`stages` plus every WIRED currency prerequisite of its members
    (transitively), in pipeline order."""
    wanted = list(stages)
    queue = list(stages)
    while queue:
        stage = queue.pop(0)
        for prerequisite in CURRENCY_PREREQUISITES.get(stage, ()):
            if prerequisite in wanted or stage_runners.get(prerequisite) is None:
                continue
            wanted.append(prerequisite)
            queue.append(prerequisite)
    return sorted(wanted, key=order.index)


def _revalidate(
    trial: Path,
    task_id: str,
    route: RepairRoute,
    failed_stage: str,
    stage_runners: dict,
    *,
    max_repair_rounds: int = 0,
    extra_stages: tuple[str, ...] = (),
    repaired_finding: Mapping[str, Any] | None = None,
) -> None:
    """Re-run invalidated stages on a trial and require every member to pass.

    Runner exceptions that indicate infrastructure or inability to measure halt rather
    than reject the patch. Blocked members raise `StageBlocked`, and blocked currency
    prerequisites remain non-verdicts. Only returned non-pass outcomes produce
    revalidation rejection codes. All outcomes stay on the disposable trial ledger.
    """
    from elt_taskgen.engine import (
        _VERDICTS,
        VERDICT_BLOCKED,
        VERDICT_PASS,
        Engine,
        InfrastructureFailure,
        StageBlocked,
        StageName,
        _budget_scope_from_payload,
        _infrastructure_failure,
        _marker_text,
        blocked_on_of,
    )
    from elt_taskgen.review.session import (
        SessionPolicyViolation,
        SessionProtocolError,
        ToolHarnessFault,
    )
    from elt_taskgen.review.tools.certify import runner_exception_could_not_measure

    order = [s.value for s in StageName]
    stages = list(revalidation_stages(route, failed_stage))
    for stage in extra_stages:
        if stage not in stages:
            stages.append(stage)
    # The route's OWN members (the truncated rerun set plus the proof
    # stages); everything `_with_currency_prerequisites` adds beyond them is
    # a prerequisite whose non-PASS answer is a wait, never a red verdict.
    members = frozenset(stages)
    stages = _with_currency_prerequisites(stages, stage_runners, order)

    engine = Engine(trial, max_repair_rounds=max_repair_rounds)
    try:
        task = engine.load_task(task_id)  # re-validates the patched IR
        for stage in stages:
            runner = stage_runners.get(stage)
            red = f"revalidation_red_{stage}"
            if runner is None:
                raise RevalidationFailed(
                    f"stage {stage!r} has no wired runner, so the patch cannot "
                    "be proven green (fail closed)",
                    code=red,
                )
            try:
                outcome = runner(engine, task)
            except PatchRejected:
                # No runner raises one today; kept for symmetry with the
                # certifier's own rejections (a rejection stays a rejection).
                raise
            except Exception as exc:  # noqa: BLE001 - a raising stage could not MEASURE
                # Re-raise nested harness and provider faults under engine markers
                # so certification halts without charging a repair round.
                marker = _harness_marker(exc)
                if marker:
                    plain_protocol = isinstance(
                        exc, council.ProviderProtocolError
                    ) and not isinstance(exc, (SessionProtocolError, SessionPolicyViolation))
                    if plain_protocol or budget_limit_scope(exc) == LIMIT_SCOPE_ROLE:
                        # A nested critic's malformed output, or ANOTHER
                        # seat's role cap, is neither the proposer's patch
                        # nor its own LIMIT_USD: re-raised under the engine's
                        # marker so it halts.
                        raise InfrastructureFailure(task_id, stage, marker) from exc
                    raise
                # Unclassified OS, storage, or engine failures become
                # `ToolHarnessFault` and halt, never `revalidation_red_<stage>`.
                if runner_exception_could_not_measure(exc):
                    fault = ToolHarnessFault.from_exception(stage, exc)
                    raise InfrastructureFailure(task_id, stage, type(fault).__name__) from fault
                # Any other exception is patch-caused: reject and charge a round
                # like live scoring; keep its text for humans and expose only a code.
                raise RevalidationFailed(
                    f"re-validation stage {stage!r} raised {type(exc).__name__}: "
                    f"{str(exc)[:400]}",
                    code=red,
                ) from exc
            if outcome.task is not None:
                task = outcome.task
                engine.save_task(task)
            if outcome.verdict in _VERDICTS:
                # The row the NEXT member's currency check reads, on the
                # trial's ledger at the NEW hash (F1). A verdict outside the
                # ledger vocabulary is not recorded and fails below as before.
                engine.record_report(task, stage, outcome.verdict, outcome.payload)
            if (
                repaired_finding
                and stage == failed_stage
                and outcome.verdict != VERDICT_PASS
                and _holds_on_a_different_finding(outcome.payload, repaired_finding)
            ):
                # If the rerun clears the repaired finding but surfaces another
                # critic claim, commit this patch and let the live stage handle it;
                # do not rerun downstream stages before its prerequisite passes.
                break
            if outcome.verdict == VERDICT_BLOCKED:
                # WAIT is not patch evidence: block without rejection or round,
                # then resume the failed stage once the reason clears.
                raise StageBlocked(task_id, stage, blocked_on_of(outcome.payload))
            if outcome.verdict != VERDICT_PASS:
                infra = _marker_text(
                    getattr(outcome, "infrastructure", "")
                    or _infrastructure_failure(outcome.payload)
                )
                if infra:
                    # A stage that could not MEASURE (transport, admission,
                    # budget) proves nothing about the patch either way.
                    raise InfrastructureFailure(
                        task_id,
                        stage,
                        infra,
                        budget_scope=_budget_scope_from_payload(outcome.payload),
                    )
                if stage not in members:
                    # A currency PREREQUISITE outside the route's rerun set
                    # (`review` before `attack` on an execution route) that
                    # did not PASS: a wait, not a verdict on the patch
                    # (finding 2-4). The ledger resumes once it clears.
                    raise StageBlocked(
                        task_id, stage, f"{BLOCKED_ON_PREREQUISITE_PREFIX}{stage}"
                    )
                # The payload excerpt is for HUMANS (ProposerAttempt.reason and
                # the adjudication record); a session sees only `code`.
                raise RevalidationFailed(
                    f"re-validation stage {stage!r} verdict "
                    f"{outcome.verdict!r}: "
                    f"{canonical_json(outcome.payload.model_dump(mode='json'))[:400]}",
                    code=red,
                )
    finally:
        engine.close()


def _commit(trial: Path, workspace: Path, changed: frozenset[str]) -> None:
    """Copy a validated diff for direct certifier/test composition only.

    Production ``Engine._handle_failure`` grants a scoped commit capability;
    ``attempt_patch`` then journals the repair, invalidations and target bytes
    before installing anything and does not call this helper.  ONLY direct
    certifier use reaches this byte-only seam. Re-validation side effects
    (fresh reports, regenerated artifacts) stay in the trial."""
    for rel in sorted(changed):
        src = trial / rel
        dst = workspace / rel
        if not src.is_file():
            dst.unlink(missing_ok=True)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".repair-tmp")
        shutil.copyfile(src, tmp)
        tmp.replace(dst)


# One attempt, end to end: attempt_patch = trial_phase + _commit (RC §3.3)

class TrialVerdict(NamedTuple):
    """Record what trial certification proved about one patch.

    The verdict binds the validated diff, revalidation outcomes, discrimination result,
    and trial state required for the single commit path.
    """

    green: bool
    failing_stage: str | None
    discrimination_weakened: bool
    diff: DiffResult


def _resolve_target(trial: Path, task_id: str, patch: RepairPatch) -> Path:
    """The artifact the patch names, inside the trial's task tree. Raises the
    scope escape / missing-artifact rejections exactly as `attempt_patch`
    always has, before any snapshot is taken."""
    task_dir = (trial / "tasks" / task_id).resolve()
    target = (task_dir / patch.artifact).resolve()
    if not str(target).startswith(str(task_dir) + "/"):
        raise ScopeViolation(
            f"artifact {patch.artifact!r} escapes tasks/{task_id}/ (fail closed)",
            code=RejectionCode.SCOPE_PATH_ESCAPE.value,
        )
    if not target.is_file():
        raise PatchApplicationError(
            f"artifact {patch.artifact!r} does not exist under tasks/{task_id}/",
            code=RejectionCode.PATCH_ARTIFACT_MISSING.value,
        )
    return target


def _is_empirical_calibrate_runner(runner: Any) -> bool:
    """Is this the `calibrate --empirical` runner (the solver campaign)?

    Read off the tag `cli.make_calibrate_runner` sets — which survives the
    CLI's echo wrapper, `functools.wraps` copies it — else off the function
    name, for a runner handed in without the tag."""
    if runner is None:
        return False
    if bool(getattr(runner, CALIBRATE_EMPIRICAL_TAG, False)):
        return True
    return getattr(runner, "__name__", "") == _CALIBRATE_EMPIRICAL_NAME


def _structural_calibrate_substitute(runner: Any):
    """`cli.make_structural_calibrate_runner` over the empirical runner's own
    roster document. Imported lazily: the CLI wires this module, not the
    other way round, at import time."""
    from elt_taskgen import cli as cli_mod

    return cli_mod.make_structural_calibrate_runner(
        getattr(runner, "agents_config", None)
    )


def _trial_stage_runners(engine: "Engine") -> dict:
    """The live engine's wired runners as the TRIAL runs them: identical, except
    that the empirical `calibrate` runner is replaced by the structural one
    (certify addendum §3.4). A failure at `calibrate` or later under
    `--empirical` would otherwise launch the 40-call solver campaign inside
    the trial; the trial certifies structurally and the live ladder runs the
    campaign once, post-commit, at the new hash, as today."""
    from elt_taskgen.engine import StageName

    runners = dict(engine.stage_runners)
    calibrate = runners.get(StageName.CALIBRATE.value)
    if _is_empirical_calibrate_runner(calibrate):
        runners[StageName.CALIBRATE.value] = _structural_calibrate_substitute(calibrate)
    return runners


def _holds_on_a_different_finding(payload: Any, repaired: Mapping[str, Any]) -> bool:
    """Does this stage payload name a blocking finding OTHER than the one
    the patch under certification repairs? False when the payload names no
    blocking finding at all (a gate-case failure, an environment wait):
    those stay the verdicts they always were."""
    descriptor = getattr(payload, "blocking_finding", None)
    if descriptor is None and isinstance(payload, Mapping):
        descriptor = payload.get("blocking_finding")
    if descriptor in (None, {}):
        return False
    current = (
        descriptor.model_dump(mode="json")
        if hasattr(descriptor, "model_dump")
        else dict(descriptor)
    )
    plain = lambda value: json.dumps(value, sort_keys=True, default=str)
    return plain(current) != plain(dict(repaired))


def _repaired_finding_of(failure: str) -> dict[str, Any] | None:
    """The `blocking_finding` descriptor inside the projected failure
    evidence the proposer was handed (`failure_detail`), or None."""
    try:
        doc = json.loads(str(failure))
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    raw = doc.get("blocking_finding")
    return dict(raw) if isinstance(raw, dict) and raw else None


def _failing_stage_of(exc: RevalidationFailed) -> str | None:
    prefix = "revalidation_red_"
    code = str(getattr(exc, "code", "") or "")
    if code.startswith(prefix) and code != f"{prefix}unknown":
        return code[len(prefix):]
    match = _STAGE_IN_MESSAGE_RE.search(str(exc))
    return match.group(1) if match else None


def trial_phase(
    engine: "Engine",
    task: TaskIR,
    stage: str,
    patch: RepairPatch,
    trial: Path,
    *,
    before: dict[str, str],
    before_ir: str,
    repaired_finding: Mapping[str, Any] | None = None,
) -> TrialVerdict:
    """Apply and certify a patch on a trial without committing it.

    Validate the diff against the baseline, re-run invalidated stages, and enforce the
    discrimination guard. Task defects raise the existing patch or revalidation errors;
    infrastructure and blocked outcomes propagate. `_commit` remains the only live-tree
    write path.
    """
    route = patch.route
    workspace = engine.workspace
    task_id = task.task_id
    target = _resolve_target(trial, task_id, patch)
    target.write_text(
        apply_patch_text(target.read_text(encoding="utf-8"), patch),
        encoding="utf-8",
    )
    after = repair_mod.snapshot(trial, task_id)

    diff = validate_scope(trial, task_id, patch, before, after, before_ir=before_ir)

    # Measured BEFORE re-validation overwrites the trial's attack records,
    # and off the LIVE workspace, so the baseline is the discrimination the
    # task actually had at the identity that failed.
    moved_literal_rows = _moved_counterfactual_rows(
        before_ir, trial, task_id, diff
    )
    moved_population = moved_literal_rows or _moved_population_material(
        before_ir, trial, task_id, diff
    )
    matrix_before = _durable_discrimination_matrix(workspace, task)
    rows_before = repair_mod.counterfactual_row_counts(task)
    # The guard (`discrimination_guard_armed`): literal rows always (fail
    # closed, an empty baseline rejects); ANY other population material —
    # `conditions` drive row generation and the dangling-key lever, `scale`
    # the sizes — whenever a measured baseline exists at the live hash
    # (finding 2-1).
    guard_armed = discrimination_guard_armed(
        literal_rows_moved=moved_literal_rows,
        population_moved=moved_population,
        matrix_before=matrix_before,
    )

    try:
        _revalidate(
            trial,
            task_id,
            route,
            stage,
            _trial_stage_runners(engine),
            extra_stages=LITERAL_ROWS_PROOF_STAGES if guard_armed else (),
            repaired_finding=repaired_finding,
        )
        if guard_armed:
            _assert_discrimination_not_weakened(
                trial,
                task_id,
                matrix_before,
                # The deletion-count rule stays literal-rows-specific.
                rows_before if moved_literal_rows else None,
            )
    except RevalidationFailed as exc:
        exc.verdict = TrialVerdict(False, _failing_stage_of(exc), False, diff)
        raise
    except DiscriminationWeakened as exc:
        exc.verdict = TrialVerdict(False, None, True, diff)
        raise
    return TrialVerdict(True, None, False, diff)


def attempt_patch(
    engine: "Engine",
    task: TaskIR,
    stage: str,
    patch: RepairPatch,
    *,
    repaired_finding: Mapping[str, Any] | None = None,
) -> TaskIR:
    """Certify one patch on a trial copy, then commit its validated diff.

    Apply scope checks, stage revalidation, and discrimination protection before the
    only commit site. Failed or unmeasurable trials never modify the live tree.
    """
    route = patch.route
    if route is RepairRoute.RUNTIME:
        raise ScopeViolation(
            "runtime repairs are mechanical rebuilds; no proposed patch is "
            "accepted for the runtime route",
            code=RejectionCode.SCOPE_ROUTE_MISMATCH.value,
        )
    workspace = engine.workspace
    task_id = task.task_id
    with trial_workspace(workspace, task_id=task_id) as trial:
        _resolve_target(trial, task_id, patch)
        before = repair_mod.snapshot(trial, task_id)
        before_ir = (trial / "tasks" / task_id / "task_ir.json").read_text(
            encoding="utf-8"
        )
        verdict = trial_phase(
            engine, task, stage, patch, trial, before=before, before_ir=before_ir,
            repaired_finding=repaired_finding,
        )
        commit_reason = None
        reason_for = getattr(engine, "_certified_patch_reason", None)
        if callable(reason_for):
            commit_reason = reason_for(task_id, stage, route)
        if commit_reason is None:
            # Direct certifier use (including the trial-phase equivalence
            # tests) is deliberately a byte-only operation. Production
            # Engine._handle_failure grants the scoped journal capability.
            _commit(trial, workspace, verdict.diff.changed)
            return engine.load_task(task_id)

        staged_files: dict[str, bytes | None] = {}
        for rel in sorted(verdict.diff.changed):
            source = trial / rel
            staged_files[rel] = source.read_bytes() if source.is_file() else None
        task_ir_rel = f"tasks/{task_id}/task_ir.json"
        patched_task = (
            task_from_json(
                (trial / task_ir_rel).read_text(encoding="utf-8")
            )
            if task_ir_rel in verdict.diff.changed
            else task
        )
        fingerprint = repair_mod.repair_fingerprint_after_changes(
            workspace, patched_task, staged_files
        )
        return repair_mod.apply_repair(
            engine,
            patched_task,
            route,
            commit_reason,
            fingerprint=fingerprint,
            staged_files=staged_files,
        )


def _moved_counterfactual_rows(
    before_ir: str, trial: Path, task_id: str, diff: repair_mod.ArtifactDiff
) -> bool:
    """Did this patch touch the rows the attack battery discriminates on?

    TWO DOORS INTO THE SAME MATERIAL: `populations.*.literal_rows` (as
    SPECIFIED) and files under `populations/counterfactual/` (MATERIALIZED). The
    materialized door is already closed at the allowlist, so that half is
    defence in depth — it must keep answering True, so a future allowlist that
    reopened it could not slip past the discrimination proof.
    """
    if any(
        f"/populations/{PopulationName.COUNTERFACTUAL.value}/" in f"/{rel.strip('/')}/"
        for rel in diff.changed
    ):
        return True
    ir_path = trial / "tasks" / task_id / "task_ir.json"
    if not ir_path.is_file():
        return False
    try:
        moved = _changed_json_paths(
            json.loads(before_ir), json.loads(ir_path.read_text(encoding="utf-8"))
        )
    except json.JSONDecodeError:
        return True  # unreadable IR: assume the worst and demand the proof
    return any(
        p == "populations" or ".literal_rows" in f".{p}." for p in moved
    )


def _moved_population_material(
    before_ir: str, trial: Path, task_id: str, diff: repair_mod.ArtifactDiff
) -> bool:
    """Return whether a patch changes any population material.

    Detect conditions, scales, or literal rows from the validated locator/diff rather
    than trusting a patch's route claim.
    """
    if any("/populations/" in f"/{rel.strip('/')}/" for rel in diff.changed):
        return True
    ir_path = trial / "tasks" / task_id / "task_ir.json"
    if not ir_path.is_file():
        return False
    try:
        moved = _changed_json_paths(
            json.loads(before_ir), json.loads(ir_path.read_text(encoding="utf-8"))
        )
    except json.JSONDecodeError:
        return True  # unreadable IR: assume the worst and demand the proof
    return any(p == "populations" or p.startswith("populations.") for p in moved)


def discrimination_guard_armed(
    *,
    literal_rows_moved: bool,
    population_moved: bool,
    matrix_before: Mapping[str, Any],
) -> bool:
    """Return whether a population edit requires proof-stage revalidation and a
    non-regression check.

    Literal-row edits always arm the guard. Other population edits arm it only when a
    measured baseline exists. The in-session attack member uses the same predicate.
    """
    if literal_rows_moved:
        return True
    return bool(population_moved) and any(bool(pops) for pops in matrix_before.values())


def _durable_discrimination_matrix(
    workspace: Path, task: TaskIR
) -> dict[str, frozenset[str]]:
    """Return measured discrimination cells for cases persisted in the TaskIR.

    Ephemeral critic probes are excluded because they cannot be reconstructed after
    population edits.
    """
    durable = {case.name for case in task.attack_cases}
    measured = repair_mod.discrimination_matrix(
        workspace, task.task_id, task_content_hash=task.content_hash()
    )
    return {name: cells for name, cells in measured.items() if name in durable}


def _assert_discrimination_not_weakened(
    trial: Path,
    task_id: str,
    matrix_before: dict[str, frozenset[str]],
    rows_before: dict[str, int] | None,
) -> None:
    """Re-measure the discrimination matrix on the trial copy; raise if weaker.

    The trial's `attack` stage has just re-run every mutant against the PATCHED
    rows, so this is a genuine re-measurement, not a re-reading of the numbers
    the patch was changing. `rows_before` (the counterfactual row-count
    baseline, the deletion rule) is checked only when given — literal rows
    moved; a `conditions` / `scale` patch is held to the matrix alone."""
    from elt_taskgen.engine import Engine

    engine = Engine(trial, max_repair_rounds=0)
    try:
        patched = engine.load_task(task_id)
    finally:
        engine.close()
    matrix_after = _durable_discrimination_matrix(trial, patched)
    problems = repair_mod.discrimination_problems(matrix_before, matrix_after)
    if rows_before is not None:
        problems += repair_mod.counterfactual_row_problems(
            rows_before, repair_mod.counterfactual_row_counts(patched)
        )
    if problems:
        what = (
            "populations.*.literal_rows — the counterfactual rows ARE the "
            "discriminator the attack battery measures against —"
            if rows_before is not None
            else "population material (populations.*.conditions / scale — the "
            "rows the attack battery measures against are generated from them)"
        )
        raise DiscriminationWeakened(
            f"patch moved {what} and the re-measured discrimination matrix is "
            "not at least as strong: " + "; ".join(problems),
            code=RejectionCode.DISCRIMINATION_WEAKENED.value,
        )


# The POPULATION route's cheap gate (roadmap Phase 3, Table 7; SoT T3
# `check_cheap` row, rule R0.3; permission matrix `check_cheap`)

#: Map four known population coverage classes to specific codes; all others use
#: `population_problems`. Match fixed prefixes or clauses and never echo details.
_POPULATION_PROBLEM_CODES: tuple[tuple[str, str], ...] = (
    ("missing population:", "missing_population"),
    ("must share the same scale", "scale_drift"),
    ("no scale and no literal rows", "no_scale_no_rows"),
    ("counterfactual population has no literal rows", "counterfactual_untargeted"),
)

#: `mart_plan._witness_problems` sentence -> code (`projection.WITNESS_PROBLEM_CODES`).
_WITNESS_PROBLEM_CODES: tuple[tuple[str, str], ...] = (
    ("unknown witness", "witness_unknown"),
    ("needs a bridge table", "witness_no_bridge"),
    ("needs FactRoles.", "witness_role_missing"),
    ("needs a SECOND hop", "witness_no_second_hop"),
)

#: The anchor line `populations.witness_conditions` writes into a CONSTRUCTED
#: counterfactual's conditions: the anchor table, the bridge table (or
#: "(none)") and, on a two-hop shape, the child.
_WITNESS_ANCHOR_RE = re.compile(
    r"^Anchor rows live in (?P<anchor>[A-Za-z_][A-Za-z0-9_]*); their linked rows live in "
    r"(?P<bridge>\(none\)|[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:, fanning out onto (?P<child>[A-Za-z_][A-Za-z0-9_]*))?\.$"
)

# Every plan-library condition repeats this scope. Repetition is intentional:
# a task may contain several marts with identically named row-B witnesses, and
# a detached generic line does not say which mart edge owns the childless row.
_WITNESS_SCOPE_RE = re.compile(
    r"^WITNESS SCOPE \[mart=(?P<mart>[^;\]]+); shape=(?P<shape>[^;\]]+); "
    r"anchor=(?P<anchor>[^;\]]+); bridge=(?P<bridge>[^;\]]+); "
    r"child=(?P<child>[^\]]+)\]: (?P<payload>.+)$"
)

_NUMERIC_COLUMN_TYPES: frozenset[str] = frozenset({"integer", "bigint", "float", "decimal"})
_PERIOD_COLUMN_TYPES: frozenset[str] = frozenset({"date", "timestamp"})


def _declared_witness_scopes(
    task: TaskIR,
) -> tuple[tuple[tuple[str, ...], str, str, str], ...]:
    """Return each independently scoped public witness catalogue.

    New generated tasks repeat a machine-readable scope prefix on every line.
    Legacy/manual tasks retain the old unscoped two-line representation. The
    latter can describe only one edge, so it is returned as one scope.
    """
    from elt_taskgen.generation.mart_plan import WITNESS_CHILDLESS, WITNESS_ORDER
    from elt_taskgen.generation.populations import (
        _LEGACY_CHILDLESS_PROSE,
        _WITNESS_PROSE,
    )

    try:
        counterfactual = task.population(PopulationName.COUNTERFACTUAL)
    except KeyError:
        return ()
    conditions = tuple(" ".join(str(c).split()) for c in counterfactual.conditions)
    normalized_prose = {
        " ".join(prose.split()): witness for witness, prose in _WITNESS_PROSE.items()
    }
    normalized_prose[" ".join(_LEGACY_CHILDLESS_PROSE.split())] = (
        WITNESS_CHILDLESS
    )

    # Preserve shape order from the conditions. Every scoped line repeats the
    # same fields, so even an edited-away header cannot reassign row B to the
    # preceding mart.
    scoped: dict[tuple[str, str, str, str, str], set[str]] = {}
    for condition in conditions:
        match = _WITNESS_SCOPE_RE.match(condition)
        if match is None:
            continue
        key = (
            match.group("mart"),
            match.group("shape"),
            match.group("anchor"),
            match.group("bridge"),
            match.group("child"),
        )
        bucket = scoped.setdefault(key, set())
        witness = normalized_prose.get(match.group("payload"))
        if witness is not None:
            bucket.add(witness)
    if scoped:
        return tuple(
            (
                tuple(witness for witness in WITNESS_ORDER if witness in declared),
                anchor,
                "" if bridge == "(none)" else bridge,
                "" if child == "(none)" else child,
            )
            for (_mart, _shape, anchor, bridge, child), declared in scoped.items()
            if declared
        )

    declared = set(conditions)
    witnesses = tuple(
        witness
        for witness in WITNESS_ORDER
        if " ".join(_WITNESS_PROSE[witness].split()) in declared
    )
    anchor = bridge = child = ""
    for condition in conditions:
        match = _WITNESS_ANCHOR_RE.match(condition)
        if match is None:
            continue
        anchor = match.group("anchor")
        bridge = "" if match.group("bridge") == "(none)" else match.group("bridge")
        child = match.group("child") or ""
    return ((witnesses, anchor, bridge, child),) if witnesses else ()


def _declared_witnesses(task: TaskIR) -> tuple[tuple[str, ...], str, str, str]:
    """Compatibility view of the declared witness catalogues.

    The cheap gate consumes ``_declared_witness_scopes`` directly. This helper
    keeps its historical four-item shape for callers/tests that inspect one
    legacy catalogue; for multiple marts it returns the ordered witness union
    and the first scope's tables.
    """

    from elt_taskgen.generation.mart_plan import WITNESS_ORDER

    scopes = _declared_witness_scopes(task)
    if not scopes:
        return (), "", "", ""
    declared = {
        witness
        for witnesses, _anchor, _bridge, _child in scopes
        for witness in witnesses
    }
    _witnesses, anchor, bridge, child = scopes[0]
    return (
        tuple(witness for witness in WITNESS_ORDER if witness in declared),
        anchor,
        bridge,
        child,
    )


def _roles_from_counterfactual(task: TaskIR, anchor: str, bridge: str, child: str):
    """Infer counterfactual fact roles from public schema and relationships only.

    The result uses public column and enum identifiers for link, child, measure, label,
    predicate, domain, and period roles. It never reads literal rows.
    """
    from elt_taskgen.generation.mart_plan import FactRoles

    tables = {t.name: t for t in task.tables}

    def key_columns(table: str) -> frozenset[str]:
        spec = tables.get(table)
        if spec is None:
            return frozenset()
        keys = set(spec.primary_key) | set(spec.business_key or ())
        for rel in task.relationships:
            if rel.child_table == table:
                keys |= set(rel.child_columns)
            if rel.parent_table == table:
                keys |= set(rel.parent_columns)
        return frozenset(keys)

    def typed(table: str, kinds: frozenset[str], *, non_key: bool = True) -> str:
        spec = tables.get(table)
        if spec is None:
            return ""
        keys = key_columns(table) if non_key else frozenset()
        for column in spec.columns:
            if column.name in keys or column.enum_values:
                continue
            if column.type.value in kinds:
                return column.name
        return ""

    def enum_column(table: str) -> Any:
        spec = tables.get(table)
        if spec is None:
            return None
        for column in spec.columns:
            if column.enum_values:
                return column
        return None

    bridge_spec = tables.get(bridge)
    link_key = ""
    child_key = ""
    if bridge_spec is not None:
        link_key = bridge_spec.primary_key[0] if bridge_spec.primary_key else (
            bridge_spec.columns[0].name if bridge_spec.columns else ""
        )
        for rel in task.relationships:
            if rel.child_table == bridge and rel.parent_table == child and rel.child_columns:
                child_key = rel.child_columns[0]
                break
    child_spec = tables.get(child)
    child_primary_key = child_spec.primary_key[0] if child_spec is not None and child_spec.primary_key else ""
    predicate = enum_column(bridge)
    predicate_values = tuple(str(v) for v in predicate.enum_values) if predicate is not None else ()
    domain_column = enum_column(anchor)
    domain_values = tuple(str(v) for v in domain_column.enum_values) if domain_column is not None else ()
    return FactRoles(
        link_key=link_key,
        child_key=child_key,
        measure=typed(bridge, _NUMERIC_COLUMN_TYPES),
        label=typed(bridge, frozenset({"text"})) or typed(child, frozenset({"text"})),
        predicate_column=predicate.name if predicate is not None else "",
        predicate_pass=predicate_values[:1] if len(predicate_values) >= 2 else (),
        predicate_fail=predicate_values[1:] if len(predicate_values) >= 2 else (),
        domain=domain_values,
        out_of_domain=domain_values[-1] if len(domain_values) >= 2 else "",
        period_column=typed(bridge, _PERIOD_COLUMN_TYPES),
        child_primary_key=child_primary_key,
        domain_column=domain_column.name if domain_column is not None else "",
    )


def witness_problem_codes(task: TaskIR) -> tuple[str, ...]:
    """Return closed codes for structural witness problems.

    Derive them from public schema and counterfactual structure only. Never expose
    witness prose, literal values, or counts.
    """
    from elt_taskgen.generation.mart_plan import _witness_problems

    scopes = _declared_witness_scopes(task)
    if not scopes:
        return ()
    codes: set[str] = set()
    for witnesses, anchor, bridge, child in scopes:
        roles = _roles_from_counterfactual(task, anchor, bridge, child)
        for problem in _witness_problems(witnesses, roles, fact=bridge, child=child):
            for needle, code in _WITNESS_PROBLEM_CODES:
                if needle in str(problem):
                    codes.add(code)
                    break
            else:  # pragma: no cover - every sentence is mapped above
                codes.add("witness_unknown")
    from elt_taskgen.review.tools.projection import WITNESS_PROBLEM_CODES

    return tuple(code for code in WITNESS_PROBLEM_CODES if code in codes)


def check_population_cheap(trial: Path, task: TaskIR) -> Diagnostic:
    """Project population coverage and witness checks into a code-only diagnostic.

    The first known problem code wins and all present classes become flags. Never expose
    problem text, counts, or population names.
    """
    from elt_taskgen.generation.populations import validate_population_coverage
    from elt_taskgen.review.tools.projection import POPULATION_CHEAP_CODES, WITNESS_PROBLEM_CODES

    problems = [str(p) for p in validate_population_coverage(task)]
    codes: set[str] = set()
    other = False
    for problem in problems:
        for needle, code in _POPULATION_PROBLEM_CODES:
            if needle in problem:
                codes.add(code)
                break
        else:
            other = True
    witness = witness_problem_codes(task)
    flags: dict[str, bool] = {code: code in codes for code in POPULATION_CHEAP_CODES}
    flags.update({code: code in witness for code in WITNESS_PROBLEM_CODES})
    flags["witness_ok"] = not witness
    flags["population_ok"] = not problems and not witness
    if codes:
        code = next(c for c in POPULATION_CHEAP_CODES if c in codes)
    elif other:
        code = "population_problems"
    elif witness:
        code = witness[0]
    else:
        code = "cheap_green"
    return Diagnostic(source=DiagnosticSource.CHEAP, ok=code == "cheap_green", code=code, flags=flags)


# Attempt records + the workspace audit queue

class ProposerStep(BaseModel):
    """One tool-side turn of a bounded proposer session, as the attempt record
    keeps it for humans and the audit queue (never for a model): the runner's
    turn kind (`tool`, `refused`, `nudge`, `validator`), the tool, its outcome
    code, whether the call was refused, the state epoch it ran at and the
    observation digest. No argument, no observation text, no value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    kind: str
    tool: str = ""
    code: str = ""
    refused: bool = False
    state_epoch: int = 0
    observation_sha256: str = ""


class ProposerAttempt(BaseModel):
    """One proposal attempt and why it was accepted or refused.

    A one-shot attempt fills the first six fields; a bounded SESSION (Phase 1
    `AgenticRepairProposer`) also records its terminal state, the projected
    `RejectionCode` its submit earned (the only thing the next session's view
    carries about it), the abort reason, the session's chain digest and salt,
    its per-step records and its two USD readings. Every addition is
    defaulted, so a one-shot record reads unchanged."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=1)
    route: str
    artifact: str = ""
    accepted: bool = False
    #: Rejection class (ScopeViolation / PatchApplicationError / ...) or "".
    error_type: str = ""
    reason: str = ""
    #: SoT T4 terminal NAME of the session (`SUBMITTED`, `ABSTAINED`, ...); ''
    #: for a one-shot attempt.
    terminal: str = ""
    #: The `RejectionCode` value the submit-time certifier answered with
    #: (`revalidation_red_<stage>`, `scope_*`, `patch_*`,
    #: `discrimination_weakened`); '' when nothing was submitted or it committed.
    rejection_code: str = ""
    #: `abort(reason_code)` of an ABSTAINED session; '' otherwise.
    abort_reason: str = ""
    session_sha256: str = ""
    session_salt: int = 0
    steps: tuple[ProposerStep, ...] = ()
    #: The proposer's OWN turns (the role's meter delta across the session).
    usd_session: float = 0.0
    #: The nested spend of the submit-time certification (the task meter's
    #: delta across `attempt_patch`), attributed to the nested roles.
    usd_certification: float = 0.0


class RepairAttemptRecord(BaseModel):
    """Deterministic evidence for one _handle_failure repair episode."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    task_content_hash: str
    stage: str
    route: str
    status: str
    committed: bool
    attempts: tuple[ProposerAttempt, ...] = ()
    detail: str = ""
    #: Episode USD (certify addendum §3.2 / §4.4): the proposer's own turns
    #: summed over its sessions, and the reported submit-time certification
    #: spend. Defaulted: a one-shot record reads unchanged.
    usd_session: float = 0.0
    usd_certification: float = 0.0


class RepairAdjudicationError(EngineError):
    """A present repair-adjudication file is unreadable or violates its schema.

    Absence means there is no queue entry; corruption is workspace-state loss
    and must never be made indistinguishable from an empty human queue.
    """


class _RepairAdjudicationRecord(BaseModel):
    """Strict persisted shape written by :func:`queue_adjudication`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    task_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: str
    status: str
    stage: str = Field(min_length=1)
    route: str = Field(min_length=1)
    attempts: tuple[ProposerAttempt, ...] = ()
    detail: str = ""
    source_report_id: int | None = Field(default=None, gt=0, strict=True)


@dataclass(frozen=True)
class RepairOutcome:
    """What the proposer hands back to the engine.

    `disposition`, `infrastructure`, `limit` and `budget_scope` are additive,
    defaulted fields (Phase 0.C): an outcome built without them derives its
    disposition from `record.committed`, so every existing construction site
    reads unchanged."""

    record: RepairAttemptRecord
    #: The patched, committed TaskIR — None when nothing was committed.
    task: TaskIR | None = None
    #: Audit-queue entry written on abstention (None when a patch committed).
    adjudication: Path | None = None
    #: One of REPAIR_DISPOSITIONS; derived from the record when left ''.
    disposition: str = ""
    #: The engine's infrastructure marker (an exception class name) when the
    #: proposer HALTED on a harness fault; '' otherwise.
    infrastructure: str = ""
    #: The limit kind a BLOCKED_LIMIT session stopped at (`turns`,
    #: `tool_calls`, `tokens`, `usd`, `wall`, `oracle`, `stuck`); '' otherwise.
    limit: str = ""
    #: For `limit == "usd"` only: which budget tripped — `role` (the session's
    #: own cap: agent-attributable, blocked with a salt), `task` or `total`
    #: (the transport's: the engine halts). '' for every other limit kind.
    limit_scope: str = ""
    #: Exact `BudgetExceededError.scope` for a halted task/total budget fault.
    #: Empty for marker-only compatibility, non-budget faults and role-local
    #: session limits. Last to preserve the additive dataclass's positional API.
    budget_scope: str = ""

    def __post_init__(self) -> None:
        disposition = str(self.disposition or "")
        if not disposition:
            disposition = (
                DISPOSITION_COMMITTED
                if self.record.committed
                else DISPOSITION_NEEDS_ADJUDICATION
            )
            object.__setattr__(self, "disposition", disposition)
        if disposition not in REPAIR_DISPOSITIONS:
            raise ValueError(
                f"disposition {disposition!r} is not one of {sorted(REPAIR_DISPOSITIONS)}"
            )
        if (disposition == DISPOSITION_COMMITTED) != bool(self.record.committed):
            raise ValueError("disposition 'committed' and record.committed must agree")
        if disposition == DISPOSITION_COMMITTED and self.task is None:
            raise ValueError("a committed outcome carries the patched task")
        marker = str(self.infrastructure or "").strip()
        if (disposition == DISPOSITION_HALTED) != bool(marker):
            raise ValueError(
                "a halted outcome carries its infrastructure marker, and only a "
                "halted outcome does"
            )
        budget_scope = str(self.budget_scope or "").strip().lower()
        if budget_scope and (
            disposition != DISPOSITION_HALTED
            or budget_scope not in {"task", "total"}
        ):
            raise ValueError(
                "only a halted task/total budget fault carries budget_scope"
            )
        object.__setattr__(self, "budget_scope", budget_scope)
        scope = str(self.limit_scope or "")
        if disposition == DISPOSITION_BLOCKED_LIMIT:
            if _CODE_RE.fullmatch(str(self.limit or "")) is None:
                raise ValueError(
                    "a blocked_limit outcome names the limit kind as a stable code"
                )
            if self.limit == LIMIT_USD and scope not in BUDGET_LIMIT_SCOPES:
                raise ValueError(
                    "a usd limit stop names the budget scope that tripped "
                    f"({', '.join(sorted(BUDGET_LIMIT_SCOPES))})"
                )
            if self.limit != LIMIT_USD and scope:
                raise ValueError("only a usd limit stop names a budget scope")
        elif self.limit or scope:
            raise ValueError("only a blocked_limit outcome names a limit")
        if disposition in (DISPOSITION_HALTED, DISPOSITION_BLOCKED_LIMIT):
            if self.adjudication is not None:
                raise ValueError("a halt or a limit stop queues no adjudication")
            if self.task is not None:
                raise ValueError("a halt or a limit stop commits nothing")

    @property
    def committed(self) -> bool:
        return self.record.committed


def _harness_marker(exc: BaseException) -> str:
    """The engine's infrastructure marker for ANY exception the engine itself
    would classify as one (the `_INFRA_EXCEPTION_NAMES` MRO walk, the message
    prefixes, `SessionFault`, an `InfrastructureFailure` already carrying a
    marker), or '' — used inside re-validation, where a nested
    `ProviderProtocolError` is a critic's fault, not the proposer's patch."""
    from elt_taskgen.engine import InfrastructureFailure, _infra_marker_for
    from elt_taskgen.review.session import SessionFault

    if isinstance(exc, InfrastructureFailure):
        return str(exc.marker)
    if isinstance(exc, SessionFault):
        return type(exc).__name__
    return _infra_marker_for(exc)


def _budget_scope_of(link: BaseException) -> str:
    """The budget scope of ONE `BudgetExceededError` object ('' otherwise)."""
    names = {cls.__name__ for cls in type(link).__mro__}
    if ROLE_CAP_EXCEPTION_NAME in names:
        return LIMIT_SCOPE_ROLE
    if "BudgetExceededError" not in names:
        return ""
    scope = str(getattr(link, "scope", "") or "").strip().lower()
    # An unknown or missing scope reads as the TASK budget: a breach nobody
    # can attribute to the agent is the transport's (fail toward a halt).
    return scope if scope in BUDGET_LIMIT_SCOPES else "task"


def budget_limit_scope(exc: BaseException) -> str:
    """Which budget a `BudgetExceededError` anywhere in `exc`'s chain tripped:
    `role`, `task` or `total`; '' when no budget breach is in the chain.

    `RoleCapExceeded` (providers.py) is the ROLE scope by NAME whatever its
    `scope` says; any other `BudgetExceededError` answers with its `scope`
    attribute. The role scope is the session's own `max_usd` (SoT T4
    `LIMIT_USD`, agent-attributable); the other two are the transport's."""
    from elt_taskgen.engine import _exception_chain

    for link in _exception_chain(exc):
        scope = _budget_scope_of(link)
        if scope:
            return scope
    return ""


def halting_marker(exc: BaseException) -> str:
    """Return the engine infrastructure marker for an exception, or an empty string for a
    failed attempt.

    Walk chained exceptions so wrapped harness, provider, sanitizer, sandbox, task, and
    session faults halt without spending a repair round. Plain malformed-patch protocol
    errors remain retryable. Role-cap exhaustion is a session limit; task or total
    budget exhaustion halts.
    """
    from elt_taskgen.engine import _exception_chain, _marker_for_one
    from elt_taskgen.review.session import SessionPolicyViolation, SessionProtocolError

    for link in _exception_chain(exc):
        if isinstance(link, council.ProviderProtocolError) and not isinstance(
            link, (SessionProtocolError, SessionPolicyViolation)
        ):
            continue
        if _budget_scope_of(link) == LIMIT_SCOPE_ROLE:
            continue
        marker = _marker_for_one(link)
        if marker:
            return marker
    return ""


def repair_adjudication_path(workspace: Path, task_id: str) -> Path:
    """Round-1 workspace audit queue (<workspace>/audit/)."""
    return Path(workspace) / "audit" / f"{task_id}.repair_adjudication.json"


def _atomic_replace_text(path: Path, text: str) -> None:
    """Durably replace a mutable adjudication record without a torn final."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.stage-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
        if os.name != "nt":
            descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def queue_adjudication(
    workspace: Path,
    task: TaskIR,
    record: RepairAttemptRecord,
    *,
    source_report_id: int | None = None,
) -> Path:
    """Queue NEEDS_ADJUDICATION for a failure the proposer could not repair.

    Bound to the task content hash: a later repair that moves the identity
    leaves this entry visibly stale rather than silently satisfied."""
    if source_report_id is not None and (
        isinstance(source_report_id, bool)
        or not isinstance(source_report_id, int)
        or source_report_id < 1
    ):
        raise ValueError("source_report_id must be positive when provided")
    path = repair_adjudication_path(workspace, task.task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": task.task_id,
        "task_content_hash": task.content_hash(),
        "kind": ADJUDICATION_KIND,
        "status": STATUS_NEEDS_ADJUDICATION,
        "stage": record.stage,
        "route": record.route,
        "attempts": [a.model_dump(mode="json") for a in record.attempts],
        "detail": record.detail,
    }
    if source_report_id is not None:
        # The engine writes the triggering FAIL before invoking the proposer.
        # Binding that sequence closes the crash window in which this file is
        # durable but the following BLOCKED row has not yet been appended.
        payload["source_report_id"] = source_report_id
    _atomic_replace_text(path, readable_json(payload))
    return path


def _queue_engine_adjudication(
    engine: "Engine", task: TaskIR, record: RepairAttemptRecord
) -> Path:
    """Queue and bind to the triggering same-hash FAIL when it is present."""
    from elt_taskgen.engine import VERDICT_FAIL

    latest = engine.latest_report(task.task_id, record.stage)
    source_report_id = (
        latest.id
        if latest is not None
        and latest.stage == record.stage
        and latest.content_hash == task.content_hash()
        and latest.verdict == VERDICT_FAIL
        else None
    )
    return queue_adjudication(
        engine.workspace,
        task,
        record,
        source_report_id=source_report_id,
    )


def load_repair_adjudication(workspace: Path, task_id: str) -> dict | None:
    """Validated repair-adjudication record, or ``None`` only when absent.

    A present but unreadable/malformed record is typed corrupt engine state;
    callers must surface it instead of silently reporting an empty queue.
    """
    path = repair_adjudication_path(workspace, task_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RepairAdjudicationError(
            f"cannot read repair adjudication {path}: {type(exc).__name__}"
        ) from exc
    try:
        record = _RepairAdjudicationRecord.model_validate_json(raw)
    except ValidationError as exc:
        raise RepairAdjudicationError(
            f"invalid repair adjudication {path}: schema validation failed"
        ) from exc
    if record.task_id != task_id:
        raise RepairAdjudicationError(
            f"invalid repair adjudication {path}: task_id does not match filename"
        )
    if record.kind != ADJUDICATION_KIND or record.status != STATUS_NEEDS_ADJUDICATION:
        raise RepairAdjudicationError(
            f"invalid repair adjudication {path}: kind/status mismatch"
        )
    return record.model_dump(mode="json", exclude_none=True)


def proposer_attempt_budget(config_path: Path | None = None) -> int:
    """Attempts per failure, from config/agents.yaml `repair.max_attempts`.

    Key-optional: a missing file or key yields DEFAULT_MAX_ATTEMPTS. Values
    outside 0..DEFAULT_MAX_ATTEMPTS*5 are refused (a budget is a bound)."""
    import yaml

    from elt_taskgen.review.providers import default_agents_config_path

    path = Path(config_path) if config_path is not None else default_agents_config_path()
    if not path.is_file():
        return DEFAULT_MAX_ATTEMPTS
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = doc.get("repair") or {}
    if not isinstance(section, dict) or "max_attempts" not in section:
        return DEFAULT_MAX_ATTEMPTS
    value = int(section["max_attempts"])
    if value < 0 or value > DEFAULT_MAX_ATTEMPTS * 5:
        raise ValueError(
            f"repair.max_attempts={value} in {path} is out of range "
            f"(0..{DEFAULT_MAX_ATTEMPTS * 5}); a budget must bound something"
        )
    return value


# The proposer the engine wires in

class RepairProposer:
    """Bounded, route-scoped repair proposals with mechanical certification.

    `repair()` runs at most `max_attempts` proposals for ONE failure, each
    through the trial copy + diff validator + re-validation. The first fully
    green attempt commits; if none does the proposer ABSTAINS, queues a
    NEEDS_ADJUDICATION entry, and the engine records a human BLOCKED resume
    point without fabricating a repair round.
    """

    def __init__(
        self,
        provider: _Provider,
        *,
        max_attempts: int | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.provider = provider
        self.max_attempts = (
            int(max_attempts)
            if max_attempts is not None
            else proposer_attempt_budget(config_path)
        )
        if self.max_attempts < 0:
            raise ValueError("max_attempts must be >= 0")

    def repair(
        self,
        engine: "Engine",
        task: TaskIR,
        stage: str,
        route: RepairRoute,
        failure: str,
    ) -> RepairOutcome:
        route = RepairRoute(route)
        attempts: list[ProposerAttempt] = []

        if route in (RepairRoute.RUNTIME, RepairRoute.FATAL):
            # No LLM: runtime is a mechanical rebuild, fatal is a rejection.
            return RepairOutcome(
                record=RepairAttemptRecord(
                    task_id=task.task_id,
                    task_content_hash=task.content_hash(),
                    stage=stage,
                    route=route.value,
                    status="not_applicable",
                    committed=False,
                    detail=(
                        "runtime repairs are mechanical rebuilds; no patch is "
                        "proposed"
                        if route is RepairRoute.RUNTIME
                        else "fatal route: the task is rejected, not patched"
                    ),
                ),
            )

        for index in range(1, self.max_attempts + 1):
            artifact = ""
            try:
                patch = propose_patch(
                    task, route, failure, self.provider, attempt=index - 1
                )
                artifact = patch.artifact
                new_task = attempt_patch(engine, task, stage, patch)
            except Exception as exc:  # noqa: BLE001 - any failure means NO COMMIT
                attempts.append(
                    ProposerAttempt(
                        index=index,
                        route=route.value,
                        artifact=artifact,
                        accepted=False,
                        error_type=type(exc).__name__,
                        reason=str(exc)[:600],
                    )
                )
                marker = halting_marker(exc)
                if marker:
                    # A HARNESS fault is not a failed proposal: stop here, queue
                    # nothing, hand the engine the marker to halt on (C7).
                    budget_scope = budget_limit_scope(exc)
                    return RepairOutcome(
                        record=RepairAttemptRecord(
                            task_id=task.task_id,
                            task_content_hash=task.content_hash(),
                            stage=stage,
                            route=route.value,
                            status=STATUS_HALTED,
                            committed=False,
                            attempts=tuple(attempts),
                            detail=(
                                f"repair proposer HALTED on a harness fault "
                                f"({marker}) during attempt {index}/"
                                f"{self.max_attempts} on stage {stage!r} "
                                f"({route.value} route); no round is spent and "
                                "the task is not rejected — the workspace is "
                                "unchanged"
                            ),
                        ),
                        disposition=DISPOSITION_HALTED,
                        infrastructure=marker,
                        budget_scope=(
                            budget_scope
                            if budget_scope in {"task", "total"}
                            else ""
                        ),
                    )
                if budget_limit_scope(exc) == LIMIT_SCOPE_ROLE:
                    # The session's own max_usd (SoT T4 LIMIT_USD): an
                    # agent-attributable stop — not a harness fault, not a
                    # failed proposal. The engine BLOCKS it with a salt and
                    # takes the round only after the bounded re-runs.
                    return RepairOutcome(
                        record=RepairAttemptRecord(
                            task_id=task.task_id,
                            task_content_hash=task.content_hash(),
                            stage=stage,
                            route=route.value,
                            status=DISPOSITION_BLOCKED_LIMIT,
                            committed=False,
                            attempts=tuple(attempts),
                            detail=(
                                "repair proposer stopped at its usd limit (the "
                                "role's own max_usd) during attempt "
                                f"{index}/{self.max_attempts} on stage {stage!r} "
                                f"({route.value} route) without a certified "
                                "patch; no round is spent and the task is not "
                                "rejected — the workspace is unchanged"
                            ),
                        ),
                        disposition=DISPOSITION_BLOCKED_LIMIT,
                        limit=LIMIT_USD,
                        limit_scope=LIMIT_SCOPE_ROLE,
                    )
                continue
            attempts.append(
                ProposerAttempt(
                    index=index,
                    route=route.value,
                    artifact=artifact,
                    accepted=True,
                    reason="committed after green re-validation on the trial copy",
                )
            )
            return RepairOutcome(
                record=RepairAttemptRecord(
                    task_id=new_task.task_id,
                    task_content_hash=new_task.content_hash(),
                    stage=stage,
                    route=route.value,
                    status="committed",
                    committed=True,
                    attempts=tuple(attempts),
                    detail=(
                        f"repair patch on {artifact!r} committed after green "
                        f"re-validation (attempt {index}/{self.max_attempts})"
                    ),
                ),
                task=new_task,
            )

        record = RepairAttemptRecord(
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            stage=stage,
            route=route.value,
            status=STATUS_NEEDS_ADJUDICATION,
            committed=False,
            attempts=tuple(attempts),
            detail=(
                f"repair proposer ABSTAINED after {len(attempts)} attempt(s) on "
                f"stage {stage!r} ({route.value} route); human adjudication "
                "required — the workspace is unchanged"
            ),
        )
        return RepairOutcome(
            record=record,
            adjudication=_queue_engine_adjudication(engine, task, record),
        )


# The bounded proposer runs up to `max_attempts` sessions, each on one held-open
# trial workspace. It submits at most one supervised certification per session;
# limits block, aborts adjudicate, harness faults halt, and rejections return codes.

#: `--repair-proposer-mode` vocabulary. The default keeps today's one-shot
#: `RepairProposer` byte-identical; `bounded` selects `AgenticRepairProposer`.
REPAIR_PROPOSER_MODES: tuple[str, ...] = ("one_shot", "bounded")
DEFAULT_REPAIR_PROPOSER_MODE = "one_shot"

#: `repair.*` config sets allowed routes, submit-time reserve, attempts per
#: failure, and optional attack certification, which defaults off with a bounded
#: deadline.
DEFAULT_ROUTES_BOUNDED: tuple[str, ...] = ("specification", "reference", "population")
DEFAULT_NESTED_CEILING_USD: Mapping[str, float] = MappingProxyType(
    {"specification": 0.56, "population": 0.12, "reference": 0.05}
)
DEFAULT_MAX_USD_PER_FAILURE = 2.00
DEFAULT_CERTIFY_ATTACK_ENABLED = False

#: The supervisor deadline of the ONE `attempt_patch` a session's submit runs
#: (certify addendum §3.2: 30 min, outside the session clock and USD cap).
SUBMIT_DEADLINE_S = 30 * 60.0

#: `RepairAttemptRecord.status` of a route the bounded proposer refuses: no
#: session, no model call; the engine takes its ordinary bounded round.
STATUS_ROUTE_NOT_BOUNDED = "route_not_bounded"

#: Where a session's record lands: `<ws>/tasks/<id>/reports/sessions/
#: <role>.<stage>.<session_sha256>.json` (state machine §6). `reports/` is
#: outside `repair.snapshot`, so a record never moves the repair fingerprint.
SESSION_RECORD_DIRNAME = "sessions"

#: The closing block of a session's view (in place of the one-shot patch
#: format): what the tools do and that nothing commits inside the session.
_SESSION_INSTRUCTIONS = (
    "SESSION: you are editing a throwaway trial copy of this task with tools; "
    "nothing you do here touches the live task. Edit ONLY task_ir.json, one "
    "edit per apply_edit_trial call, inside this route's editable fields "
    "(every write is scope-checked against the live task and refused when it "
    "leaves the route). read_field reads a public schema field by dotted "
    "path; check_scope and check_cheap validate what you have applied; "
    "certify runs the provider-free stages of this route on a disposable copy "
    "(at most twice; refused at no cost when the route has none or nothing "
    "changed since your last certify). Finish with submit_patch, which hands "
    "everything you applied to the certifier as ONE patch: it re-validates on "
    "a fresh trial and commits only on green, and you will not see its result "
    "in this session.\n"
    "ONE TOOL CALL PER TURN. The session policy refuses a turn that issues "
    "two calls at once and executes NEITHER (code `multiple_tool_use`), so a "
    "batched turn costs a turn and returns nothing. Ask for one thing, read "
    "the answer, then ask for the next.\n"
    "A GREEN check_cheap DOES NOT MEAN THERE IS NOTHING TO REPAIR. Those "
    "gates read the prose and the IR shape only. They cannot see an "
    "ambiguity a critic found, an attack case that failed to promote, or a "
    "population that fails to discriminate — and those are most of what you "
    "are called to repair. When FAILURE EVIDENCE names a critic's claim and "
    "check_cheap comes back green, the claim is still the defect: the gates "
    "simply do not measure it. Repair what the evidence says, not what the "
    "cheap gates report, and never abort merely because check_cheap is "
    "green.\n"
    "{route_paths}"
    "NEVER REPLACE A WHOLE FIELD. `old` must be a SHORT anchor — one sentence "
    "or clause that occurs exactly once — and `new` that same anchor plus your "
    "change. Trying to swap an entire 17,000-character solver_prompt in one "
    "call is how a session ends: the arguments come back malformed, the policy "
    "refuses them as `invalid_arguments`, and three refusals in a row exhaust "
    "the session with nothing repaired. Never blank a field to a placeholder "
    "to 'see what happens' either; the cheap check then reports empty prose "
    "and you have spent an edit learning nothing.\n"
    "{route_recipe}"
    "Call abort(reason_code) when no repair inside this route exists — not "
    "because the edit is long or fiddly. `insufficient_information` means the "
    "view genuinely lacks what you need to write the fix, never that you have "
    "not looked yet."
)


#: Route-specific recipes keep the proposer on the correct editable surface;
#: unsupported routes get no recipe.
_ROUTE_RECIPES: dict[RepairRoute, str] = {
    RepairRoute.SPECIFICATION: (
        "REPAIRING SOLVER PROSE, CONCRETELY. A prose-fidelity failure names "
        "the exact items it cannot find, and coverage is judged PER MART "
        "SECTION: a passage only counts for the mart whose labelled header it "
        "sits under. So `insert`, which appends to the END of the field, "
        "lands in the LAST mart's section and cannot fix an item of any "
        "earlier mart. Use `replace` instead: pick a short sentence that "
        "already sits inside the right mart's section and occurs exactly once "
        "in the whole field, and set new to that same sentence followed by "
        "the passage you are adding. Add one item per edit, keep every "
        "passage the failure did not name, and restate the requirement in the "
        "wording the MART PLAN SUMMARY and the column descriptions use rather "
        "than paraphrasing it. Then check_cheap to see whether the item list "
        "shrank.\n"
        "A CRITIC'S TWO-READING FORK IS DECIDED BY THE MART PLAN SUMMARY. You "
        "cannot see the reference, and you do not need to: the numbered rules "
        "in MART PLAN SUMMARIES are the reference's own behaviour, rule by "
        "rule, and every fork the critic names is settled by one of them (a "
        "filter's condition, a placeholder rule, a default, a boundary). Find "
        "that rule, then add one sentence in the section the claim names that "
        "states the rule's outcome for the exact case the critic describes, in "
        "the rule's own words; where two passages disagree, correct the one "
        "that contradicts the rule. Aborting because you cannot see which "
        "reading is right is not `insufficient_information` — the summary "
        "carries the answer (batch10 2026-09-11).\n"
    ),
    RepairRoute.POPULATION: (
        "REPAIRING A POPULATION, CONCRETELY. The conditions ARE the repair "
        "surface: the generator reads them and rebuilds every row from them, "
        "so you change the data by STATING A CONDITION, never by writing "
        "rows. A discrimination claim says no population guarantees some "
        "situation; your repair is to guarantee it. Append one sentence to "
        "the conditions of the populations the claim names, in the form the "
        "POPULATION CONDITIONS above already use — a short upper-case label, "
        "a colon, then one sentence naming the tables and columns involved, "
        "as in `OPTIONAL-LINK WITNESS: ...` above. A tie claim becomes `TIE "
        "WITNESS: within at least one <parent> row, two distinct <child> rows "
        "share the largest <column>.`; a boundary claim names the boundary "
        "value a row must sit exactly on. Apply it with `apply_edit_trial`, "
        "op `insert`, an EMPTY `old`, and the locator "
        "`populations.<i>.conditions.<j>`, taking <i> from the 0-based "
        "position of that population in the list above and <j> as the "
        "0-based index of that population's LAST existing condition (count "
        "the conditions listed above for it): the sentence is appended to "
        "that condition's text. The whole-list locator "
        "`populations.<i>.conditions` and a slot that does not exist yet "
        "both refuse the edit. One population per edit. The "
        "development population is solver-visible; guarantee the situation in "
        "the hidden populations the claim names.\n"
        "DO NOT SPEND TURNS READING `marts.*` ON THIS ROUTE. The MART PLAN "
        "SUMMARIES above already carry every rule, column and tie-break you "
        "need, `marts.*.plan` and `marts.*.grain` are not readable here at "
        "all, and no mart field is editable on this route. Every read you "
        "spend there is a turn not spent writing the condition.\n"
    ),
}


#: A required mutant's name as the gates report it (`custom__wrong_boundary_else`).
_MUTANT_NAME_RE = re.compile(r"\b([a-z_]+__[a-z0-9_]+)\b")


def _failing_mutant_block(task: TaskIR, failure: str) -> list[str]:
    """Render the failing mutant's public name and description.

    Per-population reward predictions and mutation directives remain private.
    """
    cases = {str(getattr(c, "name", "")): c for c in (task.attack_cases or ())}
    named = [n for n in dict.fromkeys(_MUTANT_NAME_RE.findall(failure or "")) if n in cases]
    if not named:
        return []
    lines = [
        "",
        "WHAT THE DATA MUST CATCH (the required mutant(s) this failure names, "
        "and what each one does — their predicted reward matrices are NOT "
        "shown; design conditions that make each one produce a different "
        "output from the correct logic on the population(s) named above):",
    ]
    for name in named:
        description = str(getattr(cases[name], "description", "") or "").strip()
        lines.append(f"- {name}: {description}" if description else f"- {name}")
    return lines


def _session_instructions(route: RepairRoute) -> str:
    """Render bounded-proposer instructions with the selected route's readable and editable
    paths.

    The lists derive from code allowlists and add no material beyond the route-scoped
    view.
    """
    # `literal_rows` is editable but NEVER readable, and naming it here both
    # misleads the proposer and puts the forbidden field class on the wire —
    # `read_field` answers it with a `forbidden_argument` security event.
    paths = sorted(
        path for path in ROUTE_IR_PATHS.get(route, ())
        if "literal_rows" not in path
    )
    listed = ", ".join(f"`{path}`" for path in paths) or "(none)"
    # Render readable and editable paths separately from the validators that
    # enforce them; readable surfaces are intentionally wider.
    from elt_taskgen.review.tools.validators import readable_field_paths

    def _roots(pool) -> set[str]:
        return {
            path.split(".", 1)[0]
            for path in pool
            if "literal_rows" not in path
        }

    mine = readable_field_paths(route)
    # List only roots readable as whole objects; hidden-row roots such as
    # `populations` are listed by safe subpaths to avoid forbidden whole reads.
    from elt_taskgen.review.tools.validators import field_is_readable as _readable

    whole = sorted(r for r in _roots(mine) if _readable(r, route))
    partial = sorted(
        path for path in mine
        if "literal_rows" not in path
        and path.split(".", 1)[0] not in whole
        and "*" in path
    )
    readable_listed = ", ".join(f"`{root}`" for root in whole) or "(none)"
    if partial:
        readable_listed += (
            ", and ONLY these sub-fields of the rest: "
            + ", ".join(f"`{path}`" for path in partial)
            + " (the objects above them, such as `populations` or "
            "`populations.0`, are refused as a whole because they contain "
            "hidden rows, and that refusal ends the session)"
        )
    # The fields a proposer actually reaches for, tested one by one against
    # the validator that answers `read_field`. A curated probe rather than a
    # set difference over every route: the difference surfaces answer-side
    # paths such as the reference SQL, and naming those in the prompt invites
    # the reach it exists to refuse. Public schema only.
    from elt_taskgen.review.tools.validators import field_is_readable

    _PROBES = (
        "solver_prompt",
        "marts.*.plan",
        "marts.*.grain",
        "marts.*.description",
        "marts.*.columns.*.description",
        "tables.*.description",
        "populations.*.conditions",
    )
    withheld = [path for path in _PROBES if not field_is_readable(path, route)]
    withheld_listed = ", ".join(f"`{path}`" for path in withheld)
    block = (
        f"EDIT ONLY WHAT THIS ROUTE OWNS. On the {route.value} route the "
        f"editable fields are exactly: {listed}. A write anywhere else is "
        "scope-checked against the live task and refused, and three refusals "
        "in a row end the session with nothing repaired.\n"
        "READ_FIELD ANSWERS MORE THAN YOU MAY EDIT, AND LESS THAN YOU MAY "
        f"EXPECT. On this route it answers the public schema under "
        f"{readable_listed}"
        + (
            f", but NOT {withheld_listed}, which come back "
            "`field_outside_allowlist` and cost you the turn"
            if withheld_listed
            else ""
        )
        + ". The view above already carries everything this route needs, so "
        "read only to confirm a detail you are about to write.\n"
    )
    return _SESSION_INSTRUCTIONS.replace("{route_paths}", block).replace(
        "{route_recipe}", _ROUTE_RECIPES.get(route, "")
    )


@dataclass(frozen=True)
class RepairSettings:
    """The `repair:` block of config/agents.yaml as the bounded proposer reads
    it (`repair_settings`): sessions per failure, the routes it may run, the
    per-route nested certification ceiling and the per-failure USD cap."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    routes_bounded: tuple[str, ...] = DEFAULT_ROUTES_BOUNDED
    nested_ceiling_usd: Mapping[str, float] = DEFAULT_NESTED_CEILING_USD
    max_usd_per_failure: float = DEFAULT_MAX_USD_PER_FAILURE
    #: `repair.certify.attack_enabled` (Phase 3): may the in-session certify
    #: run the `attack` member (review/tools/certify.py module docstring)?
    certify_attack_enabled: bool = DEFAULT_CERTIFY_ATTACK_ENABLED
    #: `repair.certify.deadline_s`: the worker deadline of a call whose stage
    #: set may hold `attack`; None = `certify.CERTIFY_ATTACK_DEADLINE_S`.
    certify_deadline_s: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "routes_bounded", tuple(str(r) for r in self.routes_bounded))
        object.__setattr__(
            self,
            "nested_ceiling_usd",
            MappingProxyType({str(k): float(v) for k, v in dict(self.nested_ceiling_usd).items()}),
        )

    def nested_ceiling(self, route: RepairRoute | str) -> float:
        """What INIT reserves beside `session.max_usd` on `route` (0 when the
        route declares none)."""
        return float(self.nested_ceiling_usd.get(RepairRoute(route).value, 0.0))

    def route_is_bounded(self, route: RepairRoute | str) -> bool:
        return RepairRoute(route).value in self.routes_bounded


def repair_settings(config_path: Path | None = None) -> RepairSettings:
    """`RepairSettings` from config/agents.yaml `repair.*` (key-optional: a
    missing file or key yields the defaults above; a malformed value is
    refused, because a budget must bound something)."""
    import yaml

    from elt_taskgen.review.providers import default_agents_config_path

    path = Path(config_path) if config_path is not None else default_agents_config_path()
    max_attempts = proposer_attempt_budget(path)
    if not path.is_file():
        return RepairSettings(max_attempts=max_attempts)
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = doc.get("repair") or {}
    if not isinstance(section, Mapping):
        raise ValueError(f"repair: in {path} must be a mapping")
    routes = section.get("routes_bounded", DEFAULT_ROUTES_BOUNDED)
    if not isinstance(routes, (list, tuple)):
        raise ValueError(f"repair.routes_bounded in {path} must be a list of routes")
    bounded: list[str] = []
    for item in routes:
        route = RepairRoute(str(item))
        if route in (RepairRoute.RUNTIME, RepairRoute.FATAL):
            raise ValueError(
                f"repair.routes_bounded in {path} names {route.value!r}, which no "
                "proposer may run (runtime is a mechanical rebuild, fatal a rejection)"
            )
        bounded.append(route.value)
    ceilings_doc = section.get("nested_ceiling_usd", DEFAULT_NESTED_CEILING_USD)
    if not isinstance(ceilings_doc, Mapping):
        raise ValueError(f"repair.nested_ceiling_usd in {path} must be a mapping route -> USD")
    ceilings: dict[str, float] = {}
    for key, value in ceilings_doc.items():
        route = RepairRoute(str(key))
        ceiling = float(value)
        if ceiling < 0:
            raise ValueError(f"repair.nested_ceiling_usd.{route.value} in {path} must be >= 0")
        ceilings[route.value] = ceiling
    cap = float(section.get("max_usd_per_failure", DEFAULT_MAX_USD_PER_FAILURE))
    if cap <= 0:
        raise ValueError(f"repair.max_usd_per_failure in {path} must be > 0")
    certify_doc = section.get("certify") or {}
    if not isinstance(certify_doc, Mapping):
        raise ValueError(f"repair.certify in {path} must be a mapping")
    attack_enabled = certify_doc.get("attack_enabled", DEFAULT_CERTIFY_ATTACK_ENABLED)
    if not isinstance(attack_enabled, bool):
        raise ValueError(f"repair.certify.attack_enabled in {path} must be true or false")
    deadline = certify_doc.get("deadline_s")
    if deadline is not None and float(deadline) <= 0:
        raise ValueError(f"repair.certify.deadline_s in {path} must be > 0")
    return RepairSettings(
        max_attempts=max_attempts,
        routes_bounded=tuple(bounded),
        nested_ceiling_usd=ceilings,
        max_usd_per_failure=cap,
        certify_attack_enabled=attack_enabled,
        certify_deadline_s=float(deadline) if deadline is not None else None,
    )


# -- the view of one session ---------------------------------------------------

def _rejection_for_view(diagnostic: Diagnostic, task: TaskIR, route: RepairRoute) -> str:
    """The rendered `RejectionCode` projection a later session's view carries,
    passed through the projector and the gatekeeper exactly like a tool
    result (it is one, delivered a session late)."""
    if diagnostic.source is not DiagnosticSource.REJECTION:
        raise ValueError("a session view carries a rejection projection only")
    wire = serialize_for_transport(diagnostic, task=task, package=None, route=route)
    assert_value_free(wire.encode("utf-8"), task=task, route=route)
    return diagnostic.render()


def session_view(
    task: TaskIR,
    route: RepairRoute,
    failure: str,
    *,
    previous: Diagnostic | None = None,
    index: int = 1,
    max_sessions: int = 1,
    salt: int = 0,
) -> str:
    """Build one repair-session user view.

    Include route-scoped failure evidence, allowed paths, prior rejection code, and
    session index. Exclude private values and prior raw diagnostics.
    """
    route = RepairRoute(route)
    parts: list[str] = []
    if previous is not None:
        parts.append(
            f"PREVIOUS SESSION {max(1, int(index) - 1)} OF {int(max_sessions)}: its "
            "submitted patch was rejected by the certifier: "
            + _rejection_for_view(previous, task, route)
            + ". That code is the only information available about the "
            "rejection; propose a different, smaller repair strictly inside "
            "this route."
        )
    parts.append(_session_instructions(route))
    if int(salt) > 0:
        parts.append(
            f"SESSION RE-RUN {int(salt)}: an earlier session on this failure "
            "stopped at a harness limit; its turns are not shown."
        )
    return view_for_route(task, route, failure, instructions="\n\n".join(parts))


# -- session records (replay serves recorded certify results from them) ---------

def session_record_dir(workspace: Path, task_id: str) -> Path:
    return Path(workspace) / "tasks" / str(task_id) / "reports" / SESSION_RECORD_DIRNAME


def session_record_path(workspace: Path, task_id: str, stage: str, session_sha256: str) -> Path:
    """`<ws>/tasks/<id>/reports/sessions/repair_proposer.<stage>.<sha>.json`."""
    return session_record_dir(workspace, task_id) / f"{ROLE_NAME}.{stage}.{session_sha256}.json"


def _certify_entries_to_keep(path: Path, fresh: list) -> list:
    """The `certify` list a session record at `path` keeps: a record that
    already holds EXECUTED entries (`served: false`) is never overwritten by
    a replay's merely SERVED entries (`served: true`) at the same
    `session_sha256` — the executed evidence stands and the served copies,
    which carry nothing the executed ones do not, are dropped. Anything else
    (no existing record, an unreadable one, executed or verified fresh
    entries) writes `fresh`."""
    if not fresh or not path.is_file():
        return list(fresh)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return list(fresh)
    kept = existing.get("certify") if isinstance(existing, dict) else None
    if not isinstance(kept, list) or not kept:
        return list(fresh)
    executed_before = any(
        isinstance(e, dict) and not bool(e.get("served", False)) for e in kept
    )
    served_only_now = all(
        isinstance(e, dict) and bool(e.get("served", False)) and not bool(e.get("verified", False))
        for e in fresh
    )
    if executed_before and served_only_now:
        return list(kept)
    return list(fresh)


def _certify_key(args_sha256: str, surface_fingerprint: str) -> str:
    """A recorded `certify` result is a pure function of the held trial's
    bytes (`generate` byte-compares, `reference` carries its determinism
    probe) and the call's arguments (none): that is its serving key."""
    return f"{args_sha256}:{surface_fingerprint}"


def recorded_certify_results(
    workspace: Path,
    task_id: str,
    stage: str,
    *,
    task_content_hash: str,
    policy_sha256: str,
    tools_sha256: str,
) -> dict[str, dict]:
    """The `certify` observations every session record at THIS identity
    (task hash, policy, tools) holds, by serving key. A replayed session
    serves them without re-executing a runner (certify addendum §3.1
    "Record / replay"); a `verify_tools` replay re-executes and compares."""
    served: dict[str, dict] = {}
    root = session_record_dir(workspace, task_id)
    if not root.is_dir():
        return served
    for path in sorted(root.glob(f"{ROLE_NAME}.{stage}.*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if (
            str(data.get("task_content_hash") or "") != task_content_hash
            or str(data.get("policy_sha256") or "") != policy_sha256
            or str(data.get("tools_sha256") or "") != tools_sha256
        ):
            continue
        for entry in data.get("certify") or ():
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or "")
            observation = entry.get("observation")
            sha = str(entry.get("observation_sha256") or "")
            if key and isinstance(observation, dict) and sha:
                served.setdefault(key, entry)
    return served


class _ProposerWorker:
    """Execute proposer validators and retain certification receipts for one session.

    Replay may serve a receipt for identical trial bytes; verification mode re-executes
    it and requires the same digest. Call counts, oracle bits, and certified epoch
    remain exact.
    """

    def __init__(
        self,
        session: Any,
        *,
        base: Any,
        served: Mapping[str, Mapping[str, Any]] | None = None,
        verify_tools: bool = False,
    ) -> None:
        self.session = session
        self.base = base
        self.served = dict(served or {})
        self.verify_tools = bool(verify_tools)
        self.certify_records: list[dict] = []

    def surface_fingerprint(self, ctx: Any) -> str | None:
        return self.base.surface_fingerprint(ctx)

    def current_draft(self, ctx: Any) -> Any:
        return self.base.current_draft(ctx)

    def run(self, tool: Any, ctx: Any, args: Mapping[str, Any], *, deadline_s: float) -> Any:
        from elt_taskgen.review.session import ToolHarnessFault, WorkerResult
        from elt_taskgen.review.tools import certify as certify_mod

        name = str(getattr(tool, "name", ""))
        refusal_for = getattr(tool, "refusal_for", None)
        if name != certify_mod.TOOL_NAME or not callable(refusal_for) or refusal_for(self.session):
            # Not a certify, or one the tool refuses at no cost (no runner
            # would run): nothing to serve or record.
            return self.base.run(tool, ctx, args, deadline_s=deadline_s)
        args_sha = sha256_hex(canonical_json(dict(args)))
        surface = str(self.session.surface_fingerprint())
        key = _certify_key(args_sha, surface)
        recorded = self.served.get(key)
        if recorded is not None and not self.verify_tools:
            diagnostic = Diagnostic.model_validate(recorded["observation"])
            payload = serialize_for_transport(
                diagnostic, task=getattr(ctx, "task", None), package=None,
                route=getattr(ctx, "route", None),
            )
            recorded_sha = str(recorded.get("observation_sha256") or "")
            if sha256_hex(payload) != recorded_sha:
                # The recorded body and its recorded digest disagree (a
                # flipped observation, a corrupt record): never served as the
                # runner's verdict and never re-hashed into consistency — a
                # harness fault, no runner spawned.
                raise ToolHarnessFault(certify_mod.TOOL_NAME, code="replay_mismatch")
            # The tool did not run: keep its per-session accounting exact.
            self.session.tool_calls += 1
            self.session.certify_calls += 1
            self.session.oracle_bits_used += int(
                getattr(self.session, "certify_oracle_bits", certify_mod.CERTIFY_ORACLE_BITS)
            )
            self.session.certified_epoch = self.session.state_epoch
            self.certify_records.append(
                {
                    "key": key,
                    "args_sha256": args_sha,
                    "surface_fingerprint": surface,
                    "state_epoch": int(self.session.state_epoch),
                    "observation": diagnostic.model_dump(mode="json"),
                    # The RECORDED digest (verified above), never a re-hash.
                    "observation_sha256": recorded_sha,
                    "served": True,
                    "verified": False,
                }
            )
            return WorkerResult(observation=diagnostic, payload=payload)
        result = self.base.run(tool, ctx, args, deadline_s=deadline_s)
        observation = result.observation
        sha = sha256_hex(str(result.payload))
        verified = recorded is not None
        if verified and str(recorded.get("observation_sha256") or "") != sha:
            raise ToolHarnessFault(certify_mod.TOOL_NAME, code="replay_mismatch")
        dumped = observation.model_dump(mode="json") if hasattr(observation, "model_dump") else None
        self.certify_records.append(
            {
                "key": key,
                "args_sha256": args_sha,
                "surface_fingerprint": surface,
                "state_epoch": int(self.session.state_epoch),
                "observation": dumped,
                "observation_sha256": sha,
                "served": False,
                "verified": verified,
            }
        )
        return result


class _SupervisedEngine:
    """Wrap patch certification with a submit deadline.

    Check the deadline before and after every revalidation runner. Expiry raises
    `ToolDeadlineExceeded` before commit, preventing abandoned work from later writing
    the live tree.
    """

    def __init__(self, engine: "Engine", *, deadline_s: float, clock: Callable[[], float]) -> None:
        if float(deadline_s) <= 0:
            raise ValueError("deadline_s must be > 0")
        self._engine = engine
        self._deadline_s = float(deadline_s)
        self._clock = clock
        self._started = float(clock())

    @property
    def elapsed_s(self) -> float:
        return max(0.0, float(self._clock()) - self._started)

    def check(self) -> None:
        from elt_taskgen.review.session import ToolDeadlineExceeded
        from elt_taskgen.review.tools.validators import SUBMIT_TOOL

        if self.elapsed_s > self._deadline_s:
            raise ToolDeadlineExceeded(SUBMIT_TOOL, deadline_s=self._deadline_s)

    @property
    def workspace(self) -> Path:
        return self._engine.workspace

    @property
    def stage_runners(self) -> dict:
        return {name: self._guard(runner) for name, runner in dict(self._engine.stage_runners).items()}

    def _guard(self, runner: Any) -> Any:
        @functools.wraps(runner)
        def guarded(engine: Any, task: TaskIR) -> Any:
            self.check()
            outcome = runner(engine, task)
            self.check()
            return outcome

        return guarded

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)


@dataclass
class _Episode:
    """What one session of one failure produced (harness-side)."""

    index: int
    salt: int
    view: str
    policy_sha256: str
    tools_sha256: str
    result: Any = None
    fault: BaseException | None = None
    patch: RepairPatch | None = None
    certify_records: list = field(default_factory=list)
    usd_session: float = 0.0
    usd_certification: float = 0.0
    submit: dict | None = None

    @property
    def session_sha256(self) -> str:
        sha = str(getattr(self.result, "session_sha256", "") or "")
        return sha or sha256_hex("no-session:" + sha256_hex(self.view))

    @property
    def terminal_name(self) -> str:
        terminal = getattr(self.result, "terminal", None)
        return str(getattr(terminal, "name", "") or "")


@dataclass(frozen=True)
class _Submission:
    """What the ONE `attempt_patch` of a session answered."""

    task: TaskIR | None = None
    rejection: Diagnostic | None = None
    error_type: str = ""
    reason: str = ""
    halt_marker: str = ""
    budget_scope: str = ""
    usd_certification: float = 0.0
    elapsed_s: float = 0.0


def _session_halt_marker(exc: BaseException) -> str:
    """The infrastructure marker of an exception a SESSION raised (`halting_marker`
    plus the two things a session, unlike a one-shot exchange, never treats as
    a failed proposal: a plain `ProviderProtocolError` out of the runner is the
    transport's, and any other unclassified exception is a runner defect)."""
    marker = halting_marker(exc)
    if marker:
        return marker
    if budget_limit_scope(exc) == LIMIT_SCOPE_ROLE:
        return ""
    from elt_taskgen.engine import _infra_marker_for

    return _infra_marker_for(exc) or type(exc).__name__


def _steps_of(result: Any) -> tuple[ProposerStep, ...]:
    """The tool-side turns of a `SessionResult` as `ProposerStep` records."""
    steps: list[ProposerStep] = []
    for turn in tuple(getattr(result, "turns", ()) or ()):
        kind = str(getattr(turn, "kind", "") or "")
        if kind == "model":
            continue
        steps.append(
            ProposerStep(
                index=int(getattr(turn, "turn_index", 0) or 0),
                kind=kind,
                tool=str(getattr(turn, "tool_name", "") or ""),
                code=str(getattr(turn, "outcome_code", "") or ""),
                refused=bool(getattr(turn, "refused", False)),
                state_epoch=int(getattr(turn, "state_epoch", 0) or 0),
                observation_sha256=str(getattr(turn, "output_sha256", "") or ""),
            )
        )
    return tuple(steps)


def validate_attack_flag_limits(
    *,
    attack_enabled: bool,
    max_oracle_bits: int,
    wall_clock_s: float | None,
    max_certify: int,
    certify_deadline_s: float,
    config_path: Path | None = None,
) -> None:
    """Validate resource limits when repair certification enables `attack`.

    Require `max_oracle_bits >= max_certify * 6` and, when bounded, `wall_clock_s >=
    max_certify * certify_deadline_s`. Raise `ValueError` for insufficient declarations;
    do nothing while the feature is disabled.
    """
    from elt_taskgen.review.tools import certify as certify_mod

    if not attack_enabled:
        return
    where = f" in {config_path}" if config_path is not None else ""
    calls = max(1, int(max_certify))
    need_bits = calls * certify_mod.CERTIFY_ATTACK_ORACLE_BITS
    if int(max_oracle_bits) < need_bits:
        raise ValueError(
            f"repair.certify.attack_enabled{where} needs roles.{ROLE_NAME}.session."
            f"max_oracle_bits >= {need_bits} (max_certify {calls} x "
            f"{certify_mod.CERTIFY_ATTACK_ORACLE_BITS} bits per certify), not "
            f"{int(max_oracle_bits)}: the first certify would trip LIMIT_ORACLE at PERMIT "
            "and the flag would disable certify instead of admitting the attack member"
        )
    need_wall = calls * float(certify_deadline_s)
    if wall_clock_s is not None and float(wall_clock_s) < need_wall:
        raise ValueError(
            f"repair.certify.attack_enabled{where} needs roles.{ROLE_NAME}.session."
            f"wall_clock_s >= {need_wall:g} (max_certify {calls} x the {float(certify_deadline_s):g} s "
            f"certify deadline), not {float(wall_clock_s):g}: a certify could outlive the session"
        )


#: Abort reason codes that judge THE ATTEMPT rather than the task: with an
#: attempt left, the proposer takes it instead of demanding adjudication.
#: `infeasible`, `spec_conflict` and `out_of_scope` judge the task and stop.
_ATTEMPT_SHAPED_ABORT_CODES: frozenset[str] = frozenset(
    {"insufficient_information", "cannot_repair"}
)


class AgenticRepairProposer:
    """Run bounded, route-scoped repair sessions.

    Each attempt edits one held trial through the role policy, then submits the
    accumulated patch once to the unchanged certifier outside the session cap. Route
    allowlists exclude hidden rows and other private material. Rejections feed the next
    attempt; aborts queue adjudication; limit stops block; harness faults halt. Only a
    certified diff is committed.
    """

    def __init__(
        self,
        provider: Any,
        *,
        max_attempts: int | None = None,
        config_path: Path | None = None,
        worker: Any = None,
        clock: Callable[[], float] = time.monotonic,
        verify_tools: bool = False,
        submit_deadline_s: float = SUBMIT_DEADLINE_S,
        certify_runners: Mapping[str, Callable[..., Any]] | None = None,
        certify_worker: str | None = None,
    ) -> None:
        from elt_taskgen.review.providers import role_loop_limits
        from elt_taskgen.review.session import SessionLimits
        from elt_taskgen.review.tools import certify as certify_mod
        from elt_taskgen.review.tools.validators import session_defaults_for

        self.provider = provider
        self.config_path = Path(config_path) if config_path is not None else None
        self.settings = repair_settings(self.config_path)
        self.max_attempts = (
            int(max_attempts) if max_attempts is not None else int(self.settings.max_attempts)
        )
        if self.max_attempts < 0:
            raise ValueError("max_attempts must be >= 0")
        # The role block plus the document's session-wide `session:`
        # defaults (un-hashed), so `session.format_error_disposition` at
        # its documented location reaches this session too.
        self.limits = SessionLimits.from_block(
            role_loop_limits(ROLE_NAME, agents_config=self.config_path),
            session_defaults=session_defaults_for(self.config_path),
        )
        self.worker = worker
        self.clock = clock
        self.verify_tools = bool(verify_tools)
        self.submit_deadline_s = float(submit_deadline_s)
        if self.submit_deadline_s <= 0:
            raise ValueError("submit_deadline_s must be > 0")
        self.certify_runners = certify_runners
        #: The certify worker shape (`certify.resolve_worker_kind`): the
        #: spawned process for the production runner dict, the in-process
        #: thread for an injected one, unless named explicitly.
        self.certify_worker = certify_mod.resolve_worker_kind(certify_worker, certify_runners)
        # Fail closed on a flag the role block cannot afford (finding 1-2).
        validate_attack_flag_limits(
            attack_enabled=self.attack_enabled,
            max_oracle_bits=self.limits.max_oracle_bits,
            wall_clock_s=self.limits.session_wall_seconds,
            max_certify=self._max_certify(),
            certify_deadline_s=self._certify_deadline_s(),
            config_path=self.config_path,
        )

    # -- declared limits ---------------------------------------------------------

    @property
    def max_usd(self) -> float:
        cap = self.limits.max_usd
        return float(cap) if cap is not None else 0.0

    def session_reserve_usd(self, route: RepairRoute | str) -> float:
        """What INIT reserves: `session.max_usd` plus the route's nested ceiling."""
        return self.max_usd + self.settings.nested_ceiling(route)

    @property
    def attack_enabled(self) -> bool:
        """May this proposer's certify hold the `attack` member (Phase 3)?
        The canonical key is `repair.certify.attack_enabled` (roadmap Table
        7, certify addendum §3.1); the proposer's own `session.certify.
        attack_enabled` (the Phase 1 declaration, hashed with the block) is
        honoured too, so a declared pilot arm carries the flag in its key."""
        block = self.limits.block.get("certify")
        declared = bool(block.get("attack_enabled", False)) if isinstance(block, Mapping) else False
        return bool(self.settings.certify_attack_enabled or declared)

    def _certify_deadline_s(self) -> float:
        """The worker deadline per certify: the session block's
        `certify.deadline_s` (300 s shipped) for the Phase 1 stage set; with
        the `attack` member, `repair.certify.deadline_s` or
        `CERTIFY_ATTACK_DEADLINE_S` (1 200 s; certify addendum §3.1)."""
        from elt_taskgen.review.tools import certify as certify_mod

        if self.attack_enabled:
            declared = self.settings.certify_deadline_s
            return float(declared) if declared else certify_mod.CERTIFY_ATTACK_DEADLINE_S
        block = self.limits.block.get("certify")
        value = block.get("deadline_s") if isinstance(block, Mapping) else None
        return float(value) if value is not None else certify_mod.CERTIFY_DEADLINE_S

    def _max_certify(self) -> int:
        from elt_taskgen.review.tools import certify as certify_mod

        declared = self.limits.max_certify
        return int(declared) if declared > 0 else certify_mod.MAX_CERTIFY_PER_SESSION

    # -- meter helpers (every one guarded: a provider double may carry no meter) --

    def _meter(self) -> Any:
        return getattr(self.provider, "meter", None)

    def _meter_task_id(self, task: TaskIR) -> str:
        return str(getattr(self.provider, "task_id", "") or "") or task.task_id

    def _role_usd(self) -> float:
        meter = self._meter()
        per_role = getattr(meter, "per_role", None)
        if not isinstance(per_role, Mapping):
            return 0.0
        reading = per_role.get(ROLE_NAME) or {}
        try:
            return float(reading.get("usd", 0.0))
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def _task_usd(self, task: TaskIR) -> float:
        meter = self._meter()
        per_task = getattr(meter, "per_task_usd", None)
        if not isinstance(per_task, Mapping):
            return 0.0
        try:
            return float(per_task.get(self._meter_task_id(task), 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _init_reserve(self, task: TaskIR, needed: float) -> None:
        """INIT: `CostMeter.reserve(max_usd + nested_ceiling[route])` on the
        task meter (certify addendum §3.2). The trajectory cap passed is the
        reserve itself, so this pre-flight can refuse only on the task or
        total scope; the session's own cap is enforced per turn by
        `run_session`'s trajectory. Nothing is spent."""
        meter = self._meter()
        reserve = getattr(meter, "reserve", None)
        if not callable(reserve):
            return
        reserve(
            task_id=self._meter_task_id(task),
            role_name=ROLE_NAME,
            est_usd=float(needed),
            trajectory_spent_usd=0.0,
            max_usd=float(needed),
        )

    def _bind_evidence(self, task: TaskIR) -> None:
        begin = getattr(self.provider, "begin_task_evidence", None)
        if callable(begin):
            begin(task.task_id, task.content_hash())

    # -- records -------------------------------------------------------------------

    @staticmethod
    def _session_salt(engine: "Engine", task: TaskIR, stage: str) -> int:
        reruns = getattr(engine, "session_limit_reruns", None)
        if not callable(reruns):
            return 0
        try:
            return int(reruns(task.task_id, stage))
        except Exception:  # noqa: BLE001 - a ledger without the column salts nothing
            return 0

    def _write_record(
        self,
        engine: "Engine",
        task: TaskIR,
        stage: str,
        route: RepairRoute,
        episode: _Episode,
    ) -> Path:
        """Persist the session (partial on a halt) under
        `session_record_path`; rewritten with the submit block once the
        certifier answered."""
        result = episode.result
        fault = episode.fault
        fault_block = None
        if fault is not None:
            record = getattr(result, "fault", None)
            fault_block = record.as_dict() if hasattr(record, "as_dict") else {
                "exception_type": type(fault).__name__
            }
        data = {
            "role": ROLE_NAME,
            "stage": str(stage),
            "route": route.value,
            "task_id": task.task_id,
            "task_content_hash": task.content_hash(),
            "session_index": int(episode.index),
            "max_sessions": int(self.max_attempts),
            "session_salt": int(episode.salt),
            "initial_view_sha256": sha256_hex(episode.view),
            "policy_sha256": episode.policy_sha256,
            "tools_sha256": episode.tools_sha256,
            "terminal": episode.terminal_name,
            "session": result.as_dict() if hasattr(result, "as_dict") else None,
            "fault": fault_block,
            "certify": list(episode.certify_records or ()),
            "steps": [s.model_dump(mode="json") for s in _steps_of(result)],
            "patch": episode.patch.model_dump(mode="json") if episode.patch is not None else None,
            "submit": episode.submit,
            "usd_session": float(episode.usd_session),
            "usd_certification": float(episode.usd_certification),
        }
        path = session_record_path(engine.workspace, task.task_id, stage, episode.session_sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        data["certify"] = _certify_entries_to_keep(path, data["certify"])
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(readable_json(data), encoding="utf-8")
        tmp.replace(path)
        return path

    # -- one session -----------------------------------------------------------------

    def _run_session(
        self,
        engine: "Engine",
        task: TaskIR,
        stage: str,
        route: RepairRoute,
        failure: str,
        *,
        index: int,
        previous: Diagnostic | None,
        salt: int,
    ) -> _Episode:
        from elt_taskgen.review.tools.validators import (
            ProposerSession,
            proposer_policy,
            proposer_validator_worker,
        )

        policy = proposer_policy(self.limits, session_salt=salt, attack_enabled=self.attack_enabled)
        served: dict[str, dict] = {}
        if bool(getattr(self.provider, "replay_only", False)) or self.verify_tools:
            served = recorded_certify_results(
                engine.workspace,
                task.task_id,
                stage,
                task_content_hash=task.content_hash(),
                policy_sha256=policy.sha256(),
                tools_sha256=policy.tools_sha256(),
            )
        self._bind_evidence(task)
        usd_before = self._role_usd()
        with ProposerSession.open(
            engine.workspace,
            task,
            route,
            stage,
            failure="",
            certify_runners=self.certify_runners,
            certify_deadline_s=self._certify_deadline_s(),
            max_certify=self._max_certify(),
            clock=self.clock,
            certify_worker=self.certify_worker,
            attack_enabled=self.attack_enabled,
        ) as session:
            worker = _ProposerWorker(
                session,
                base=self.worker if self.worker is not None else proposer_validator_worker(),
                served=served,
                verify_tools=self.verify_tools,
            )
            episode = _Episode(
                index=index, salt=salt, view="",
                policy_sha256=policy.sha256(), tools_sha256=policy.tools_sha256(),
                certify_records=worker.certify_records,
            )
            try:
                # The view is built INSIDE the fault boundary: a view that
                # still carries private material trips `_assert_scope` (a
                # `DiagnosticTripwire`), which lands in `episode.fault` and
                # is classified by `_session_halt_marker` like any other
                # session fault — halted, no model call, no round (finding 2-2).
                view = session_view(
                    task, route, failure,
                    previous=previous, index=index, max_sessions=self.max_attempts, salt=salt,
                )
                episode.view = view
                session.view = view
                episode.result = self.provider.run_session(
                    ROLE_NAME, view, policy, session.context(),
                    worker=worker, clock=self.clock,
                )
            except Exception as exc:  # noqa: BLE001 - classified by the caller, never swallowed
                episode.fault = exc
                episode.result = getattr(exc, "session_result", None)
            if session.edits:
                episode.patch = session.accumulated_patch()
        episode.usd_session = max(0.0, self._role_usd() - usd_before)
        return episode

    def _submit(
        self, engine: "Engine", task: TaskIR, stage: str, patch: RepairPatch
    ) -> _Submission:
        """The ONE certification of a session: the unchanged `attempt_patch`,
        exactly once, under the supervisor deadline, its nested spend read off
        the task meter (reported, never charged to the session)."""
        supervised = _SupervisedEngine(engine, deadline_s=self.submit_deadline_s, clock=self.clock)
        usd_before = self._task_usd(task)
        try:
            committed = attempt_patch(
                supervised, task, stage, patch,
                repaired_finding=getattr(self, "_repaired_finding", None),
            )
        except PatchRejected as exc:
            marker = halting_marker(exc)
            if marker:
                budget_scope = budget_limit_scope(exc)
                return _Submission(
                    error_type=type(exc).__name__, reason=str(exc)[:600], halt_marker=marker,
                    budget_scope=(
                        budget_scope
                        if budget_scope in {"task", "total"}
                        else ""
                    ),
                    usd_certification=max(0.0, self._task_usd(task) - usd_before),
                    elapsed_s=supervised.elapsed_s,
                )
            return _Submission(
                rejection=project_rejection(exc),
                error_type=type(exc).__name__,
                reason=str(exc)[:600],
                usd_certification=max(0.0, self._task_usd(task) - usd_before),
                elapsed_s=supervised.elapsed_s,
            )
        except Exception as exc:  # noqa: BLE001 - classified by CLASS (C7; finding 1-0)
            from elt_taskgen.review.session import ToolHarnessFault
            from elt_taskgen.review.tools.certify import runner_exception_could_not_measure

            usd = max(0.0, self._task_usd(task) - usd_before)
            marker = halting_marker(exc)
            if marker or runner_exception_could_not_measure(exc):
                # A harness fault inside the certifier (transport, tripwire,
                # a deadline, a sandbox death, an OS / storage-engine fault
                # the engine has no name for — halted under the certify
                # rule's `ToolHarnessFault` marker) halts: exit 2, reward
                # None, no round (C7).
                budget_scope = budget_limit_scope(exc)
                return _Submission(
                    error_type=type(exc).__name__,
                    reason=str(exc)[:600],
                    halt_marker=marker or ToolHarnessFault.__name__,
                    budget_scope=(
                        budget_scope
                        if budget_scope in {"task", "total"}
                        else ""
                    ),
                    usd_certification=usd,
                    elapsed_s=supervised.elapsed_s,
                )
            # Unnamed patch-caused certifier errors become charged
            # `revalidation_red_unknown` rejections, never free infrastructure halts.
            return _Submission(
                rejection=project_rejection(
                    RevalidationFailed(
                        f"certifier raised {type(exc).__name__}: {str(exc)[:400]}",
                        code=RejectionCode.REVALIDATION_RED_UNKNOWN.value,
                    )
                ),
                error_type=type(exc).__name__,
                reason=str(exc)[:600],
                usd_certification=usd,
                elapsed_s=supervised.elapsed_s,
            )
        return _Submission(
            task=committed,
            usd_certification=max(0.0, self._task_usd(task) - usd_before),
            elapsed_s=supervised.elapsed_s,
        )

    # -- outcomes ---------------------------------------------------------------------

    @staticmethod
    def _record(
        task: TaskIR,
        stage: str,
        route: RepairRoute,
        *,
        status: str,
        committed: bool,
        attempts: list[ProposerAttempt],
        detail: str,
    ) -> RepairAttemptRecord:
        return RepairAttemptRecord(
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            stage=str(stage),
            route=route.value,
            status=status,
            committed=committed,
            attempts=tuple(attempts),
            detail=detail,
            usd_session=float(sum(a.usd_session for a in attempts)),
            usd_certification=float(sum(a.usd_certification for a in attempts)),
        )

    def _halted(
        self, task: TaskIR, stage: str, route: RepairRoute, attempts: list[ProposerAttempt],
        *, marker: str, why: str, budget_scope: str = "",
    ) -> RepairOutcome:
        return RepairOutcome(
            record=self._record(
                task, stage, route, status=STATUS_HALTED, committed=False, attempts=attempts,
                detail=(
                    f"repair proposer HALTED on a harness fault ({marker}) {why} on stage "
                    f"{stage!r} ({route.value} route); no round is spent and the task is "
                    "not rejected — the workspace is unchanged"
                ),
            ),
            disposition=DISPOSITION_HALTED,
            infrastructure=marker,
            budget_scope=budget_scope,
        )

    def _blocked(
        self, task: TaskIR, stage: str, route: RepairRoute, attempts: list[ProposerAttempt],
        *, limit: str, scope: str, why: str,
    ) -> RepairOutcome:
        limit = str(limit or "").strip() or "unknown"
        scope = str(scope or "").strip().lower() if limit == LIMIT_USD else ""
        if limit == LIMIT_USD and scope not in BUDGET_LIMIT_SCOPES:
            scope = "task"
        return RepairOutcome(
            record=self._record(
                task, stage, route, status=DISPOSITION_BLOCKED_LIMIT, committed=False,
                attempts=attempts,
                detail=(
                    f"repair proposer session stopped at its {limit} limit"
                    + (f" ({scope} scope)" if scope else "")
                    + f" {why} on stage {stage!r} ({route.value} route) without a "
                    "certified patch; no round is spent and the task is not rejected — "
                    "the workspace is unchanged"
                ),
            ),
            disposition=DISPOSITION_BLOCKED_LIMIT,
            limit=limit,
            limit_scope=scope,
        )

    def _abstained(
        self, engine: "Engine", task: TaskIR, stage: str, route: RepairRoute,
        attempts: list[ProposerAttempt], *, why: str,
    ) -> RepairOutcome:
        record = self._record(
            task, stage, route, status=STATUS_NEEDS_ADJUDICATION, committed=False,
            attempts=attempts,
            detail=(
                f"repair proposer ABSTAINED after {len(attempts)} session(s) on stage "
                f"{stage!r} ({route.value} route): {why}; human adjudication required — "
                "the workspace is unchanged"
            ),
        )
        return RepairOutcome(
            record=record,
            adjudication=_queue_engine_adjudication(engine, task, record),
        )

    # -- the seam ----------------------------------------------------------------------

    def repair(
        self,
        engine: "Engine",
        task: TaskIR,
        stage: str,
        route: RepairRoute,
        failure: str,
    ) -> RepairOutcome:
        from elt_taskgen.review.session import TerminalState

        route = RepairRoute(route)
        stage = str(stage)
        # The finding this repair is asked to resolve, for the certifier: a
        # re-run that fails on a DIFFERENT finding is not red for this patch.
        self._repaired_finding = _repaired_finding_of(failure)
        if route in (RepairRoute.RUNTIME, RepairRoute.FATAL):
            return RepairOutcome(
                record=self._record(
                    task, stage, route, status="not_applicable", committed=False, attempts=[],
                    detail=(
                        "runtime repairs are mechanical rebuilds; no patch is proposed"
                        if route is RepairRoute.RUNTIME
                        else "fatal route: the task is rejected, not patched"
                    ),
                )
            )
        if not self.settings.route_is_bounded(route):
            # A route outside `repair.routes_bounded` (the shipped key names
            # SPECIFICATION, REFERENCE and POPULATION; without `population`
            # the Phase 1 pair) is refused before any model call and the
            # engine takes its ordinary bounded round.
            return RepairOutcome(
                record=self._record(
                    task, stage, route, status=STATUS_ROUTE_NOT_BOUNDED, committed=False,
                    attempts=[],
                    detail=(
                        f"the bounded repair proposer runs no session on the {route.value} "
                        f"route (repair.routes_bounded = {list(self.settings.routes_bounded)}); "
                        "no patch is proposed and the workspace is unchanged"
                    ),
                )
            )
        if not callable(getattr(self.provider, "run_session", None)):
            raise TypeError(
                "the bounded repair proposer needs a provider with run_session "
                "(RoutedProvider); a one-shot provider double cannot drive a session"
            )

        attempts: list[ProposerAttempt] = []
        spent = 0.0
        previous: Diagnostic | None = None
        salt = self._session_salt(engine, task, stage)
        needed = self.session_reserve_usd(route)

        for index in range(1, self.max_attempts + 1):
            if index > 1 and spent + needed > self.settings.max_usd_per_failure:
                return self._abstained(
                    engine, task, stage, route, attempts,
                    why=(
                        f"the per-failure cap repair.max_usd_per_failure = "
                        f"${self.settings.max_usd_per_failure:.2f} cannot cover another "
                        f"session (${spent:.4f} spent, ${needed:.2f} to reserve)"
                    ),
                )
            try:
                self._init_reserve(task, needed)
            except Exception as exc:  # noqa: BLE001 - only budget classes come out of reserve
                scope = budget_limit_scope(exc)
                if not scope:
                    raise
                return self._blocked(
                    task, stage, route, attempts, limit=LIMIT_USD, scope=scope,
                    why=f"at INIT of session {index}/{self.max_attempts} (reserve ${needed:.2f})",
                )

            episode = self._run_session(
                engine, task, stage, route, failure,
                index=index, previous=previous, salt=salt,
            )
            spent += episode.usd_session
            result = episode.result
            base = dict(
                index=index,
                route=route.value,
                artifact=episode.patch.artifact if episode.patch is not None else "",
                terminal=episode.terminal_name,
                session_sha256=episode.session_sha256,
                session_salt=salt,
                steps=_steps_of(result),
                usd_session=episode.usd_session,
            )

            if episode.fault is not None:
                marker = _session_halt_marker(episode.fault)
                budget_scope = budget_limit_scope(episode.fault)
                if not marker:
                    # The session's own cap surfaced as an exception (INIT of
                    # the runner): the agent's LIMIT_USD, blocked with a salt.
                    self._write_record(engine, task, stage, route, episode)
                    attempts.append(
                        ProposerAttempt(
                            **base, accepted=False,
                            error_type=type(episode.fault).__name__,
                            reason=str(episode.fault)[:600],
                        )
                    )
                    return self._blocked(
                        task, stage, route, attempts, limit=LIMIT_USD, scope=LIMIT_SCOPE_ROLE,
                        why=f"during session {index}/{self.max_attempts}",
                    )
                self._write_record(engine, task, stage, route, episode)
                attempts.append(
                    ProposerAttempt(
                        **base, accepted=False,
                        error_type=type(episode.fault).__name__,
                        reason=str(episode.fault)[:600],
                    )
                )
                return self._halted(
                    task, stage, route, attempts, marker=marker,
                    # The fault's own text travels on the ledger row: a halt
                    # that said only "ProviderProtocolError" left the cause
                    # unrecoverable after the run (dlt__personio, batch10
                    # run D, 2026-09-11).
                    why=(
                        f"during session {index}/{self.max_attempts}: "
                        + " ".join(str(episode.fault).split())[:400]
                    ),
                    budget_scope=(
                        budget_scope
                        if budget_scope in {"task", "total"}
                        else ""
                    ),
                )

            terminal = getattr(result, "terminal", None)
            if not isinstance(terminal, TerminalState):
                raise TypeError("run_session returned no SessionResult terminal")
            auto = bool(getattr(result, "auto_submitted", False)) and episode.patch is not None

            if terminal.is_limit_stop and not auto:
                self._write_record(engine, task, stage, route, episode)
                attempts.append(
                    ProposerAttempt(
                        **base, accepted=False, error_type="SessionLimit",
                        reason=f"session stopped at its {result.limit} limit without a "
                        "validator-green draft",
                    )
                )
                return self._blocked(
                    task, stage, route, attempts, limit=result.limit,
                    scope=getattr(result, "limit_scope", ""),
                    why=f"during session {index}/{self.max_attempts}",
                )

            if terminal is TerminalState.ABSTAINED:
                final = getattr(result, "final", None)
                reason_code = str(final.get("reason_code", "")) if isinstance(final, Mapping) else ""
                self._write_record(engine, task, stage, route, episode)
                attempts.append(
                    ProposerAttempt(
                        **base, accepted=False, error_type="Abstained",
                        reason=f"the session aborted ({reason_code or 'no reason code'})",
                        abort_reason=reason_code,
                    )
                )
                # Retry attempt-scoped aborts while budget remains; task-scoped
                # aborts still stop.
                if (
                    reason_code in _ATTEMPT_SHAPED_ABORT_CODES
                    and index < self.max_attempts
                ):
                    continue
                return self._abstained(
                    engine, task, stage, route, attempts,
                    why=f"session {index}/{self.max_attempts} aborted ({reason_code})",
                )

            if terminal is not TerminalState.SUBMITTED and not auto:
                # A returned terminal that produced nothing to certify (the
                # declared `stage_fail` disposition of PROTOCOL_EXHAUSTED).
                self._write_record(engine, task, stage, route, episode)
                attempts.append(
                    ProposerAttempt(
                        **base, accepted=False, error_type="SessionStopped",
                        reason=f"the session ended as {terminal.name} with nothing submitted",
                    )
                )
                return self._abstained(
                    engine, task, stage, route, attempts,
                    why=f"session {index}/{self.max_attempts} ended as {terminal.name}",
                )

            if episode.patch is None:
                # submit_patch with no applied edit: nothing to certify.
                rejection = Diagnostic(
                    source=DiagnosticSource.REJECTION, ok=False,
                    code=RejectionCode.PATCH_NOOP.value,
                )
                episode.submit = {
                    "called": False, "outcome": "rejected", "rejection_code": rejection.code,
                }
                self._write_record(engine, task, stage, route, episode)
                attempts.append(
                    ProposerAttempt(
                        **base, accepted=False, error_type="PatchRejected",
                        reason="submit_patch with no applied edit: there is no patch to certify",
                        rejection_code=rejection.code,
                    )
                )
                previous = rejection
                continue

            submission = self._submit(engine, task, stage, episode.patch)
            spent += submission.usd_certification
            episode.usd_certification = submission.usd_certification
            base["usd_certification"] = submission.usd_certification
            episode.submit = {
                "called": True,
                "auto_submitted": auto,
                "outcome": (
                    "halted" if submission.halt_marker
                    else "committed" if submission.task is not None
                    else "rejected"
                ),
                "rejection_code": submission.rejection.code if submission.rejection else "",
                "error_type": submission.error_type,
                "halt_marker": submission.halt_marker,
                "elapsed_s": float(submission.elapsed_s),
                "usd_certification": float(submission.usd_certification),
            }
            if submission.budget_scope:
                episode.submit["budget_scope"] = submission.budget_scope
            self._write_record(engine, task, stage, route, episode)

            if submission.halt_marker:
                attempts.append(
                    ProposerAttempt(
                        **base, accepted=False, error_type=submission.error_type,
                        reason=submission.reason,
                    )
                )
                if str(submission.halt_marker) == "blocked_on:human":
                    # `blocked_on:human` from certification is adjudication, not
                    # infrastructure failure; preserve the certifier's reason.
                    return self._abstained(
                        engine, task, stage, route, attempts,
                        why=(
                            f"the certifier's re-validation of session "
                            f"{index}/{self.max_attempts} is waiting on human "
                            "adjudication ("
                            + " ".join(str(submission.reason).split())[:300]
                            + ")"
                        ),
                    )
                return self._halted(
                    task, stage, route, attempts, marker=submission.halt_marker,
                    why=(
                        f"inside the certifier of session {index}/{self.max_attempts}: "
                        + " ".join(str(submission.reason).split())[:400]
                    ),
                    budget_scope=submission.budget_scope,
                )
            if submission.task is not None:
                attempts.append(
                    ProposerAttempt(
                        **base, accepted=True,
                        reason=(
                            "committed after green re-validation on the trial copy"
                            + (" (auto-submitted validator-green draft)" if auto else "")
                        ),
                    )
                )
                committed = submission.task
                return RepairOutcome(
                    record=RepairAttemptRecord(
                        task_id=committed.task_id,
                        task_content_hash=committed.content_hash(),
                        stage=stage,
                        route=route.value,
                        status="committed",
                        committed=True,
                        attempts=tuple(attempts),
                        detail=(
                            f"repair patch on {episode.patch.artifact!r} committed after "
                            f"green re-validation (session {index}/{self.max_attempts})"
                        ),
                        usd_session=float(sum(a.usd_session for a in attempts)),
                        usd_certification=float(sum(a.usd_certification for a in attempts)),
                    ),
                    task=committed,
                )
            assert submission.rejection is not None
            attempts.append(
                ProposerAttempt(
                    **base, accepted=False, error_type=submission.error_type,
                    reason=submission.reason, rejection_code=submission.rejection.code,
                )
            )
            previous = submission.rejection

        return self._abstained(
            engine, task, stage, route, attempts,
            why=(
                f"{len(attempts)} session(s) submitted no certifiable patch"
                if attempts else "no session ran (repair.max_attempts = 0)"
            ),
        )

"""Expose the ELT task-production stages as CLI commands.

Exit 0 means passed, 1 means rejected, and 2 means usage, infrastructure, or a
blocked measurement. Metrology commands use 1 for not admitted.
"""

from __future__ import annotations

import argparse
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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from elt_taskgen import demo_fixture
from elt_taskgen.generation.config import DEFAULT_GENERATION_CONFIG
from elt_taskgen.generation.difficulty_profiles import DEFAULT_GENERATION_PROFILE
from elt_taskgen.engine import (
    BLOCKED_ON_KEY,
    EVIDENCE_SUPERSESSION_KEY,
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
    DifficultyMeasurement,
    Origin,
    PopulationName,
    RLVR_TASK_VARIANTS,
    TaskIR,
    TaskVariant,
    canonical_json,
    readable_json,
    sha256_hex,
    task_from_json,
    validate_task_id_segment,
)

from elt_taskgen.pipeline import gates as _pipeline_gates
from elt_taskgen.pipeline import generation as _pipeline_generation
from elt_taskgen.pipeline import release as _pipeline_release
from elt_taskgen.pipeline import review as _pipeline_review
from elt_taskgen.pipeline import stages as _pipeline_stages
from elt_taskgen.pipeline import support as _pipeline_support

# Compatibility imports; implementations live in their owning modules.
from elt_taskgen.pipeline.attacks import (
    _SCHEMA_EQUIVALENT_DROP_FRAME_PROOF,
    _SCHEMA_EQUIVALENT_ZERO_DEFAULT_PROOF,
    _attack_finding_descriptor,
    _attacker_corrected_in_session,
    _blocking_proposal_failures,
    _claim_suffix,
    _drop_frame_equivalence_proof,
    _names_add_dedup,
    _names_zero_is_missing,
    _population_blind_spot_refuted,
    _proposal_failure_route,
    _selected_blocking_finding,
    _source_dedup_equivalence_proof,
    _validated_executable_findings,
    _zero_default_equivalence_proof,
    _zero_default_identity_accepted,
    _zero_is_missing_realized_targets,
    run_attack_stage,
)
from elt_taskgen.pipeline.calibration import (
    make_calibrate_runner,
    make_structural_calibrate_runner,
    run_calibrate,
)
from elt_taskgen.pipeline.gates import (
    DUAL_BUILD_NOTES_FILENAME,
    EL_EVIDENCE_NOTES_FILENAME,
    STAGE_SUBCOMMANDS,
    VARIANT_ACCEPTANCE_EVIDENCE,
    _blocked_on_stale_evidence,
    _ensure_canonical_reachability,
    _ensure_independent_build,
    _evidence_currency_block,
    _independent_adjudication_block,
    _is_currency_refusal,
    _transport_marker,
    make_gates_runner,
    make_variant_gates_runner,
)
from elt_taskgen.pipeline.generation import (
    _REBUILD_SCRATCH_DIR,
    _REBUILD_STALE_SUFFIX,
    _admitted_tasks,
    _arm_contamination_index,
    _attack_matrix_failure_detail,
    _contamination_index,
    _coverage_payload,
    _pipeline_batch_intake_preflight,
    _population_drift_failure,
    _required_firewall_coverage,
    _seed_embedded_deny_lists,
    _task_was_admitted,
    _with_execution_effect_filter,
    _with_intake_filters,
    run_contamination_pre,
    run_execution_effect_filter,
    run_generate,
    run_intake_filters,
    run_reference_stage,
)
from elt_taskgen.pipeline.handoff import (
    CRITIC_AMBIGUITY_FAILURE_CODE,
    CRITIC_DILIGENCE_FAILURE_CODE,
    CRITIC_DILIGENCE_RECOVERY,
    CRITIC_GOLD_DISAGREEMENT_CODE,
    CRITIC_MANIFEST_FAILURE_CODE,
    CRITIC_MANIFEST_RECOVERY,
    CRITIC_PROTOCOL_FAILURE_CLASS,
    CRITIC_PROTOCOL_FAILURE_CODE,
    CRITIC_PROTOCOL_RECOVERY,
    MUTATION_NAMES_NO_SECOND_HOP_SIGNAL,
    MUTATION_TARGETS_NO_DECLARED_RULE_SIGNAL,
    UNCOMPILABLE_AFTER_CORRECTIONS_SIGNAL,
    UNCOMPILABLE_NO_CHANNEL_SIGNAL,
    WITHDRAWN_BY_DISPOSITION_SIGNAL,
    _claimed_critic_handoff,
    _critic_adjudication_block,
    _critic_protocol_block,
    _proposal_names_a_hop_the_task_never_joins,
    _proposal_targets_a_rule_the_task_never_states,
    _seat_has_compile_channel,
)
from elt_taskgen.pipeline.payloads import (
    AttackFindingDescriptor,
    AttackPayload,
    CalibratePayload,
    ContaminationPayload,
    ReviewPayload,
)
from elt_taskgen.pipeline.release import (
    run_contamination_post,
    run_release,
    run_select,
)
from elt_taskgen.pipeline.review import (
    AUTHOR_BEHAVIOR_SHA_KEY,
    AUTHOR_DRAFTS_KEY,
    AUTHOR_PROSE_SHA_KEY,
    AUTHOR_REVISIONS_KEY,
    AUTHOR_SESSION_REPORTS_REL,
    AUTHOR_SESSION_SHA_KEY,
    AUTHOR_SESSION_TERMINAL_KEY,
    AUTHOR_SOURCE_CANDIDATE,
    AUTHOR_SOURCE_KEY,
    AUTHOR_SOURCE_PRESERVED,
    AUTHOR_SOURCE_REVISED,
    _accepts_keyword,
    _admission_failure_detail,
    _admission_gate,
    _author_prose_for,
    _author_session_block,
    _begin_task_evidence,
    _stamp_admission,
    _structural_failure_detail,
    _validated_review_manifest,
    make_author_runner,
    make_review_runner,
)
from elt_taskgen.pipeline.stages import build_stage_runners
from elt_taskgen.pipeline.support import (
    CliUsageError,
    _answer_key_dir,
    _atomic_replace_text,
    _evidence_dir,
    _force_stage_rerun,
    _public_dir,
    _require_pass_payload,
    _workspace_rel,
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


class _LockedRunRefusal(RuntimeError):
    """A lock-scoped pre-run check refused execution without judging the task."""


# --- Small helpers ---

def _open_engine(workspace: Path, **kwargs) -> Engine:
    """THE ONLY WAY THIS MODULE CONSTRUCTS AN ENGINE.

    `Engine.__init__` creates state/ and tasks/ on construction, so every bare
    `Engine(workspace)` is a writer that skips the container guard. The guard
    belongs here and not in Engine.__init__, which is the library layer where
    programmatic callers legitimately root an Engine anywhere."""
    return _pipeline_support.open_engine(workspace, **kwargs)


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
    from elt_taskgen.review import provider_backends as _provider_backends_module, provider_config as _provider_config_module, provider_protocol as _provider_protocol_module

    retry_values = {
        name: getattr(args, name)
        for name in (
            "http_retries", "schema_retries",
            "http_timeout_seconds", "http_backoff_seconds",
        )
        if getattr(args, name, None) is not None
    }
    if retry_values:
        _provider_protocol_module.configure_retry_policy(**retry_values)

    routing = _provider_config_module.load_role_routing(
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
        raise _pipeline_support.CliUsageError(
            "--record and --replay-only are contradictory: --record forces "
            "fresh LIVE calls, --replay-only forbids live calls. Pick one "
            "(drop --record to replay recorded transcripts)."
        )
    resolved_refresh = requested_refresh if refresh is None else bool(refresh)
    if resolved_refresh and resolved_replay_only:
        raise _pipeline_support.CliUsageError(
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
        raise _pipeline_support.CliUsageError(f"invalid active calibration campaign: {exc}") from None
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
        raise _pipeline_support.CliUsageError(
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
        stage_runners=_pipeline_stages.build_stage_runners(
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
            raise _pipeline_support.CliUsageError(f"invalid sandbox attestation: {exc}") from None
    if engine.release_mode == "certified" and engine.sandbox_attestation is None:
        engine.close()
        raise _pipeline_support.CliUsageError(
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
    from elt_taskgen.review import provider_backends as _provider_backends_module, provider_config as _provider_config_module, provider_protocol as _provider_protocol_module

    routing = _provider_config_module.load_role_routing(
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
    problems = _provider_config_module.credential_problems(routing, live_roles)
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
        detail, admission = _pipeline_review._admission_gate(provider, workspace)
        if detail is not None:
            # 1, not 2: the same "blocked, the council is not admitted" answer
            # metrology itself reports, and no live call was made.
            print(f"cannot record transcripts — {detail}")
            print(
                "run `elt-taskgen metrology --workspace council` and export "
                "ELT_TASKGEN_ADMISSION (or re-run it in this workspace)."
            )
            return 1
        _pipeline_review._stamp_admission(provider, admission)

    print(f"recording live transcripts for task {task.task_id!r} into {out_dir}")
    from elt_taskgen.reference import independent

    # Seed one exchange for a one-shot witness, but the full trajectory for an
    # enabled session. Read the flag from the agents document used for routing.
    _agents_doc_path = _provider_config_module.agents_config_of(routing)

    def _witness_session_enabled(role_name: str) -> bool:
        try:
            block = _provider_config_module.role_loop_limits(
                role_name, agents_config=_agents_doc_path
            )
        except Exception:  # noqa: BLE001 — an unreadable declaration enables nothing
            return False
        return bool(block.get("enabled", False))

    implementer_session = _witness_session_enabled(independent.ROLE_NAME)
    loader_session = _witness_session_enabled(independent.LOADER_ROLE_NAME)

    findings: list = []
    if not loader_only:
        _pipeline_review._begin_task_evidence(provider, task)
        # Seed the exact author protocol the review stage will replay.  A bounded
        # author stores a session/trajectory key, not the historical one-shot
        # exchange key; recording the latter made an enabled-by-default author
        # impossible to use with ``--replay-only``.
        author_engine = _open_engine(workspace)
        try:
            prose, _author_session_data = _pipeline_review._author_prose_for(
                author_engine, task, provider
            )
        finally:
            author_engine.close()
        authored = task.model_copy(update={"solver_prompt": prose})
        from elt_taskgen.review.model_specification import author_if_configured

        authored = author_if_configured(authored, provider)
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
        _pipeline_review._begin_task_evidence(provider, authored)
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
        answer_key = _pipeline_support._answer_key_dir(loader_engine, stored)
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
                        _pipeline_gates._variants_root(loader_engine, stored)
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
    from elt_taskgen.review import provider_backends as _provider_backends_module, provider_config as _provider_config_module, provider_protocol as _provider_protocol_module

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
    task_id: str, collisions: Iterable[_pipeline_generation._CollisionLike]
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
            difficulty_profile=DEFAULT_GENERATION_PROFILE.name,
            recipe_selection=DEFAULT_GENERATION_CONFIG.recipe_selection(),
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
    _pipeline_generation._arm_contamination_index(idx)
    engine = _open_engine(workspace) if not args.dry_run else None
    try:
        registered = 0
        refused = 0
        clean = 0
        for task in tasks:
            try:
                task = _apply_generation_semantics(task, args)
            except ValueError as exc:
                print(f"refused semantic generation for {task.task_id}: {exc}")
                refused += 1
                continue
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
    tail = f" ({refused} refused by semantic admission or the contamination pre-check)" if refused else ""
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
        args.db_id, Path(args.tables).resolve(), pool=args.pool,
        difficulty_profile=DEFAULT_GENERATION_PROFILE,
    )
    task = _apply_generation_semantics(task, args)

    workspace = Path(args.workspace).resolve()
    index_dir = workspace / "state" / "contamination"
    idx = cont.ContaminationIndex(index_dir)
    if not idx.is_armed():
        print("contamination index seeded with the embedded benchmark deny lists")
    _pipeline_generation._arm_contamination_index(idx)
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
        difficulty_profile=DEFAULT_GENERATION_PROFILE,
    )
    task = _apply_generation_semantics(task, args)

    # Contamination is ONE service with TWO call points; the pre call point runs here
    # so a colliding candidate is refused BEFORE it costs a workspace entry.
    from elt_taskgen.verification import contamination as cont

    workspace = Path(args.workspace).resolve()
    idx = cont.ContaminationIndex(workspace / "state" / "contamination")
    if not idx.is_armed():
        print("contamination index was empty — seeded with the embedded deny lists")
    _pipeline_generation._arm_contamination_index(idx)
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
    _pipeline_generation._arm_contamination_index(idx, announce=False)
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
        task = _apply_generation_semantics(task, args)
    except (wikidbs.WikiDbsIngestError, wikidbs.ProvenanceLeakError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2

    workspace = Path(args.workspace).resolve()
    idx = cont.ContaminationIndex(workspace / "state" / "contamination")
    if not idx.is_armed():
        print("contamination index seeded with the embedded benchmark deny lists")
    _pipeline_generation._arm_contamination_index(idx)
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
    _pipeline_generation._arm_contamination_index(idx)

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
                task = _apply_generation_semantics(task, args)
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
    subcommand = _pipeline_gates.STAGE_SUBCOMMANDS.get(row.stage, row.stage)
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

    diff_path = _pipeline_support._evidence_dir(engine, task) / "difficulty.json"
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
    selection_path = _pipeline_support._evidence_dir(engine, task) / "selection.json"
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
            _pipeline_support._force_stage_rerun(
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
            _pipeline_support._force_stage_rerun(
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
                    _pipeline_support._force_stage_rerun(
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
                _pipeline_support._force_stage_rerun(
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
                _pipeline_support._atomic_replace_text(
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
        _pipeline_support._atomic_replace_text(
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
            _pipeline_support._atomic_replace_text(
                attempt_path, readable_json(final_attempt) + "\n"
            )
        return result

    try:
        provider = _resolve_provider(worker_args, workspace)
        engine = _make_engine(worker_args, provider)
        task = engine.load_task(worker_args.task_id)
        if attempt_path is not None:
            attempt_document["task_content_hash"] = task.content_hash()
            _pipeline_support._atomic_replace_text(
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
                    _pipeline_support._force_stage_rerun(
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
                        _pipeline_support._force_stage_rerun(
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
    except _pipeline_support.CliUsageError as exc:
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
        raise _pipeline_support.CliUsageError(
            "pipeline found no registered tasks; ingest candidates from the source "
            "adapters first"
        )
    for task_id in task_ids:
        try:
            validate_task_id_segment(task_id)
        except (TypeError, ValueError) as exc:
            raise _pipeline_support.CliUsageError(f"unsafe pipeline task id {task_id!r}: {exc}") from None
    missing = [
        task_id
        for task_id in task_ids
        if not (workspace / "tasks" / task_id / "task_ir.json").is_file()
    ]
    if missing:
        raise _pipeline_support.CliUsageError(f"pipeline task ids are not registered: {missing}")
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
    """Adapt corpus CLI arguments to the shared pipeline services."""
    from elt_taskgen.pipeline.corpus import run_corpus

    return run_corpus(
        args, open_engine=_open_engine,
        active_campaign_fingerprint=_active_campaign_fingerprint,
        registered_task_ids=_registered_task_ids,
        worker_payload=_pipeline_worker_payload, worker=_pipeline_worker,
    )


def _parse_source_allocation(value: str | None) -> dict[str, int] | None:
    if value is None:
        return None
    allocation: dict[str, int] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise _pipeline_support.CliUsageError(
                "--source-allocation must be a comma list such as dbt=2,dlt=1"
            )
        family, raw_count = (part.strip() for part in item.split("=", 1))
        if not family or family in allocation:
            raise _pipeline_support.CliUsageError(
                f"invalid or repeated source allocation family {family!r}"
            )
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise _pipeline_support.CliUsageError(
                f"source allocation for {family!r} is not an integer"
            ) from exc
        if count < 0:
            raise _pipeline_support.CliUsageError(
                f"source allocation for {family!r} must be non-negative"
            )
        allocation[family] = count
    if not allocation:
        raise _pipeline_support.CliUsageError("--source-allocation must not be empty")
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
        raise _pipeline_support.CliUsageError(
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
        raise _pipeline_support.CliUsageError(f"--agent-harness must be one of {list(AGENT_HARNESS_MODES)}, not {resolved!r}")
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
    _pipeline_support._atomic_replace_text(
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
        raise _pipeline_support.CliUsageError(
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
            raise _pipeline_support.CliUsageError(f"invalid --run-config {path}: {exc}") from None
        if not isinstance(loaded, dict):
            raise _pipeline_support.CliUsageError("--run-config must contain a mapping")
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
        raise _pipeline_support.CliUsageError(
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
        raise _pipeline_support.CliUsageError(f"invalid configured pipeline request: {exc}") from None


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


def _configured_worker_args(args, spec, run_id: str) -> argparse.Namespace:
    """Apply validated run settings to either configured execution path."""
    options = vars(args).copy()
    options.update(
        workers=spec.workers,
        max_repair_rounds=spec.max_repair_rounds,
        repair_attempts=spec.repair_attempts,
        budget_per_task=spec.budget_per_task,
        budget_total=spec.budget_total,
        agents_config=Path(spec.agents_config) if spec.agents_config else None,
        admission_reference=spec.admission_reference,
        destination=spec.destination,
        extra_destinations=spec.extra_destinations,
        http_retries=spec.http_retries,
        schema_retries=spec.schema_retries,
        http_timeout_seconds=spec.http_timeout_seconds,
        http_backoff_seconds=spec.http_backoff_seconds,
        global_budget_run_id=run_id if spec.budget_total is not None else "",
        global_budget_total=spec.budget_total,
        allow_structural_difficulty=not bool(spec.empirical),
    )
    return argparse.Namespace(**options)


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

    workspace = Path(args.workspace).resolve()
    preflight_engine = _open_engine(workspace, max_repair_rounds=0)
    try:
        scheduled = {
            task_id: preflight_engine.load_task(task_id) for task_id in task_ids
        }
        stopped, preflight_results = _pipeline_generation._pipeline_batch_intake_preflight(
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
    worker_args = _configured_worker_args(args, spec, run_id)
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
    from elt_taskgen.pipeline.workers import run_workers

    return run_workers(
        payloads, worker=_pipeline_worker, max_workers=max_workers,
        initial_results=preflight_results,
        attempts_dir=workspace / "state" / "pipeline_runs" / run_id / "attempts",
    )


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

    from elt_taskgen.review import provider_accounting, providers as providers_mod
    from elt_taskgen.review import provider_backends as _provider_backends_module, provider_config as _provider_config_module, provider_protocol as _provider_protocol_module

    roles = list(_CONFIGURED_LIVE_ROLES)
    if bool(getattr(args, "repair_proposer", False)):
        roles.append("repair_proposer")
    try:
        routing = _provider_config_module.load_role_routing(
            Path(spec.agents_config) if spec.agents_config else None
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return (f"cannot load provider routing: {type(exc).__name__}: {exc}",)

    problems = list(_provider_config_module.credential_problems(routing, roles))
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
            provider_accounting.rate_card_for(
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
        raise _pipeline_support.CliUsageError(
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
        raise _pipeline_support.CliUsageError(
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
                raise _pipeline_support.CliUsageError("implementation revalidation receipt path is missing")
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


from elt_taskgen.pipeline.migration import (
    _ManifestAdapterTransition,
    _PendingConfiguredMigration,
    _GeneratorRevalidationTask,
    _GeneratorEquivalenceCommit,
    _ImplementationEquivalenceCommit,
    _GeneratorRevalidationReceipt,
    _ImplementationRevalidationReceipt,
    _EquivalenceCommit,
    _RevalidationReceipt,
    _is_implementation_authorization,
    _equivalence_evidence_filename,
    _publish_immutable_run_document,
    _workspace_relative,
    _bound_workspace_path,
    _logical_budget_snapshot,
    _prepare_generator_authorization,
    _load_report_revalidation_request,
    _iter_json_strings,
    _regular_json_bytes,
    _build_defect_revalidation,
    _commit_generator_equivalences,
    _finalize_generator_migration,
    _selected_implementation_transition,
    _pool_reconstructed_at_selected_implementation,
    _historical_committed_supersession_is_intact,
    _committed_supersession_or_forward_pass_is_current,
    _load_committed_generator_migration,
    _assert_configured_run_identity,
)


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
            raise _pipeline_support.CliUsageError(
                f"configured pipeline: default candidate pool {pool_path} is "
                "missing; pass --ingest-manifest pointing to a pinned "
                "five-source-ingest-v2 candidate pool"
            )
    if getattr(args, "task_id", None):
        raise _pipeline_support.CliUsageError(
            "--task-id cannot be combined with candidate-count mode; the seeded "
            "source plan is the complete candidate roster"
        )
    if getattr(args, "size", None) is not None:
        raise _pipeline_support.CliUsageError(
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
        raise _pipeline_support.CliUsageError(f"candidate selection refused: {exc}") from None

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
        raise _pipeline_support.CliUsageError(f"unsafe --run-id {run_id!r}: {exc}") from None

    if getattr(args, "reingest", False) and not spec.resume:
        raise _pipeline_support.CliUsageError("--reingest cannot be combined with resume=false")
    _ensure_firewall_armed(args, workspace)
    def write_report(**fields) -> Path:
        return _write_configured_report(
            workspace=workspace,
            run_id=run_id,
            spec=spec,
            selected_manifest=selected,
            **fields,
        )

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
        raise _pipeline_support.CliUsageError(
            "--report-revalidation-request requires a code-only --reingest migration"
        )
    if (
        requested_revalidation_path is not None
        and migration is not None
        and migration.adapter_transitions
    ):
        raise _pipeline_support.CliUsageError(
            "--report-revalidation-request is limited to a historical "
            "generator-only migration and cannot be used while adapter "
            "implementation pins move"
        )
    if migration is not None:
        if migration.committed_receipt is not None:
            if requested_revalidation_path is not None:
                raise _pipeline_support.CliUsageError(
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
            difficulty_profile=DEFAULT_GENERATION_PROFILE.name,
            recipe_selection=spec.generation.recipe_selection(spec.seed),
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
        path = write_report(
            task_ids=tuple(
                entry.expected_task_id for entry in selected.ordered_entries()
            ),
            state="BLOCKED",
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
    running_report = write_report(
        task_ids=task_ids,
        state="RUNNING",
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
        path = write_report(
            task_ids=task_ids,
            state=("COMPLETE_WITH_ISSUES" if ingest_failures else "COMPLETE"),
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
        path = write_report(
            task_ids=task_ids,
            state="BLOCKED",
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
            path = write_report(
                task_ids=task_ids,
                state="COMPLETE_WITH_ISSUES",
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
        legacy = _configured_worker_args(args, spec, run_id)
        legacy.task_id = list(runnable_task_ids)
        legacy.ingest_manifest = None
        legacy.reingest = False
        legacy.size = len(runnable_task_ids)
        legacy.require_sources = ",".join(
            origin.value
            for origin, count in selected.source_allocation.items()
            if count
        )
        legacy.allow_structural_difficulty = False
        legacy.empirical = True
        legacy.require_empirical = True
        legacy.configured_run_spec = spec.model_dump(mode="json")
        legacy.configured_run_id = run_id
        legacy.allow_partial_candidates = True
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
        path = write_report(
            task_ids=task_ids,
            state=state,
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
                write_report(
                    task_ids=task_ids,
                    state="RUNNING",
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
    path = write_report(
        task_ids=task_ids,
        state=state,
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
        raise _pipeline_support.CliUsageError(f"unsafe --run-id {run_id!r}: {exc}") from None
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
        raise _pipeline_support.CliUsageError(f"readiness report is missing or invalid: {exc}") from None
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
        except _pipeline_support.CliUsageError as exc:
            problems.append(str(exc))
            return None
        try:
            return _canonical_configured_package_receipt(
                workspace=workspace,
                run_id=run_id,
                task_id=recorded.task_id,
                candidate=candidate,
            )
        except _pipeline_support.CliUsageError as exc:
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
            # RELEASE_READY stops at the selection gate. A release-profile status
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
            raise _pipeline_support.CliUsageError(
                f"unknown variant {token!r} (choose from: full, el, t)"
            )
        if variant not in out:
            out.append(variant)
    if not out:
        raise _pipeline_support.CliUsageError("--variants must name at least one variant")
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
            accepted = _pipeline_release._accepted_variants(engine, task)
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
        gold = gold_mod.load_gold(_pipeline_support._answer_key_dir(engine, task))
        variants_root = engine.task_dir(task.task_id) / "variants"
        emits_combined_parent = (
            TaskVariant.FULL in variants
            or all(variant in variants for variant in RLVR_TASK_VARIANTS)
        )
        if emits_combined_parent:
            eltbench_mod.export_task(
                task,
                gold,
                _pipeline_support._public_dir(engine, task),
                _pipeline_support._answer_key_dir(engine, task),
                destination=args.destination,
            )
            print(f"exported public bundle: {_pipeline_support._public_dir(engine, task)}")
            print(f"private answer key:     {_pipeline_support._answer_key_dir(engine, task)}")
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
        raise _pipeline_support.CliUsageError("training package could not be verified") from error


def cmd_training_install(args) -> int:
    from elt_taskgen.training import WorkspaceLifecycleError, install_workspace

    package = _training_package(args)
    try:
        attempt = install_workspace(package, Path(args.attempt).resolve())
    except WorkspaceLifecycleError as error:
        raise _pipeline_support.CliUsageError(error.code.value) from None
    print(canonical_json({"attempt": str(attempt.root), "claim": "deterministic_protocol_workflow_proxy", "task_id": package.task_id}))
    return 0


def cmd_training_seal(args) -> int:
    from elt_taskgen.training import WorkspaceLifecycleError, load_attempt_workspace, seal_workspace

    package = _training_package(args)
    try:
        attempt = load_attempt_workspace(package, Path(args.attempt).resolve())
        sealed = seal_workspace(attempt, package, Path(args.output).resolve())
    except WorkspaceLifecycleError as error:
        raise _pipeline_support.CliUsageError(error.code.value) from None
    print(canonical_json({"artifact_sha256": sealed.submission.artifact_sha256, "seal": str(sealed.root), "seal_sha256": sealed.seal_sha256, "task_id": package.task_id}))
    return 0


def cmd_training_score(args) -> int:
    from elt_taskgen.training import DbtRuntimeConfig, WorkspaceLifecycleError, load_attempt_workspace, score_workspace, seal_workspace, warehouse_profile

    package = _training_package(args)
    profile = warehouse_profile(args.destination)
    if profile.destination.value != package.destination.value:
        raise _pipeline_support.CliUsageError("destination personality does not match the release")
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
            raise _pipeline_support.CliUsageError(error.code.value) from None
    else:
        if not args.seal_sha256:
            raise _pipeline_support.CliUsageError("--seal requires --seal-sha256 authentication")
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
            raise _pipeline_support.CliUsageError(error.code.value) from None
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
        raise _pipeline_support.CliUsageError(f"no elt-bench/ directory under {bench_root}")
    if not any(
        (corpus_root / name).is_dir()
        for name in ("snowflake", "databricks", "redshift")
    ):
        raise _pipeline_support.CliUsageError(
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
        raise _pipeline_support.CliUsageError(
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
        raise _pipeline_support.CliUsageError(f"invalid {label} JSON: {path}") from None
    if not isinstance(value, dict):
        raise _pipeline_support.CliUsageError(f"invalid {label} JSON: {path}")
    return value


def _runtime_yaml(path: Path, *, label: str) -> dict:
    import yaml

    try:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise _pipeline_support.CliUsageError(f"invalid {label} YAML: {path}") from None
    if not isinstance(value, dict):
        raise _pipeline_support.CliUsageError(f"invalid {label} YAML: {path}")
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
        raise _pipeline_support.CliUsageError(f"credential output already exists: {path}") from None
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
        raise _pipeline_support.CliUsageError(
            "--work-dir may not lie under the repository's runs/ directory "
            "(live credentials are installed there; ledgers, exports and tools "
            f"read it): {resolved}"
        )
    if (
        resolved == release
        or release in resolved.parents
        or path_under_release_root(resolved)
    ):
        raise _pipeline_support.CliUsageError(
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
        raise _pipeline_support.CliUsageError(f"cannot verify runtime release: {exc}") from None
    if not result.ok:
        detail = "; ".join(result.failures[:3])
        if len(result.failures) > 3:
            detail += f"; and {len(result.failures) - 3} more"
        raise _pipeline_support.CliUsageError(f"release verification failed: {detail}")
    if (
        manifest.corpus_profile != COMBINED_CORPUS_PROFILE
        or manifest.public_layout != COMBINED_PUBLIC_LAYOUT
    ):
        raise _pipeline_support.CliUsageError(
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
        raise _pipeline_support.CliUsageError(
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
        raise _pipeline_support.CliUsageError(str(exc)) from None
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
        raise _pipeline_support.CliUsageError(str(exc)) from None
    print(f"workspace_id: {result.workspace_id}")
    if result.custom_api_definition_id:
        print(f"custom_api_definition_id: {result.custom_api_definition_id}")
    print(f"runtime credentials: {args.credential_out}")
    return 0


def _task_workspace_id(credentials: dict, *, label: str) -> str:
    workspace_id = credentials.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise _pipeline_support.CliUsageError(f"{label} JSON carries no workspace_id")
    return workspace_id


def cmd_runtime_airbyte_gateway(args) -> int:
    """Serve the Airbyte API with Basic authentication until interrupted."""
    from elt_taskgen.runtime.airbyte_gateway import (
        AirbyteGateway,
        GatewayError,
        TokenSource,
        load_basic_credential,
        log_to_stderr,
        serve,
    )

    upstream_credential = _runtime_json(args.airbyte_credential, label="Airbyte credential")
    host, _, port_text = str(args.listen).rpartition(":")
    if not host or not port_text.isdigit():
        raise _pipeline_support.CliUsageError("--listen must be host:port")
    try:
        basic = load_basic_credential(args.gateway_credential, create=args.create_credential)
        tokens = TokenSource(
            args.upstream,
            str(upstream_credential.get("client_id") or ""),
            str(upstream_credential.get("client_secret") or ""),
        )
        tokens.token()
        gateway = AirbyteGateway(args.upstream, tokens, basic)
        server = serve(gateway, host, int(port_text), log=None if args.quiet else log_to_stderr)
    except (GatewayError, OSError) as exc:
        raise _pipeline_support.CliUsageError(str(exc)) from None
    print(f"airbyte gateway on {host}:{port_text} -> {args.upstream}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_runtime_teardown_task(args) -> int:
    """Delete the Airbyte workspace one task run bootstrapped.

    A shared-instance credential lets a later agent list an earlier run's
    connector configuration and stream selection for the same task, which is a
    stage-1 reference.  Deleting the workspace after scoring removes it.
    """
    from elt_taskgen.runtime.airbyte import AirbyteError

    credentials = _runtime_json(args.airbyte_credential, label="task Airbyte credential")
    workspace_id = _task_workspace_id(credentials, label="task Airbyte credential")
    if args.base_credential is not None:
        base = _runtime_json(args.base_credential, label="base Airbyte credential")
        if base.get("workspace_id") == workspace_id:
            raise _pipeline_support.CliUsageError("refusing to delete the base Airbyte workspace")
    client = _airbyte_client_from_credentials(args.airbyte_url, credentials)
    try:
        client.delete_workspace(workspace_id)
        state = "deleted"
    except AirbyteError as exc:
        if "HTTP 404" not in str(exc):
            raise _pipeline_support.CliUsageError(str(exc)) from None
        state = "already absent"
    except ValueError as exc:
        raise _pipeline_support.CliUsageError(str(exc)) from None
    if args.remove_credential:
        try:
            Path(args.airbyte_credential).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise _pipeline_support.CliUsageError(f"could not remove {args.airbyte_credential}: {exc}") from None
    print(f"airbyte workspace {workspace_id}: {state}")
    return 0


def cmd_runtime_purge_workspaces(args) -> int:
    """List (default) or delete stale task workspaces on a shared instance."""
    from elt_taskgen.runtime.airbyte import AirbyteError

    credentials = _runtime_json(args.airbyte_credential, label="base Airbyte credential")
    base_workspace = credentials.get("workspace_id")
    client = _airbyte_client_from_credentials(args.airbyte_url, credentials)
    import glob as _glob

    keep_ids = set()
    for pattern in args.keep_credentials or ():
        for path in sorted(Path(match) for match in _glob.glob(pattern)):
            try:
                keep_ids.add(_task_workspace_id(_runtime_json(path, label="kept credential"), label=str(path)))
            except _pipeline_support.CliUsageError:
                continue
    try:
        workspaces = client.iter_workspaces()
    except AirbyteError as exc:
        raise _pipeline_support.CliUsageError(str(exc)) from None
    targets = []
    for workspace in workspaces:
        workspace_id = workspace.get("workspaceId")
        name = workspace.get("name")
        if not isinstance(workspace_id, str) or not isinstance(name, str):
            continue
        if workspace_id == base_workspace or workspace_id in keep_ids:
            continue
        if not name.startswith(args.name_prefix):
            continue
        if name in set(args.keep_name or ()):
            continue
        targets.append((workspace_id, name))
    mode = "deleting" if args.apply else "would delete (pass --apply)"
    print(f"{len(workspaces)} workspaces; {mode} {len(targets)}")
    failures = 0
    for workspace_id, name in targets:
        if args.apply:
            try:
                client.delete_workspace(workspace_id)
            except (AirbyteError, ValueError) as exc:
                failures += 1
                print(f"  FAILED {workspace_id} {name}: {exc}")
                continue
        print(f"  {workspace_id} {name}")
    return 1 if failures else 0


def cmd_runtime_verify_process(args) -> int:
    """Report whether an agent run followed the process rules the reward does not check."""
    from elt_taskgen.destinations import destination_from_config, normalize_destination
    from elt_taskgen.runtime.airbyte import AirbyteError
    from elt_taskgen.runtime.evaluation import EvaluationError, _load_stage1_counts
    from elt_taskgen.runtime.process_compliance import (
        DEFAULT_IGNORED_TOP_LEVEL,
        ProcessComplianceError,
        evaluate_process_compliance,
        terraform_intent_report,
        workspace_connection_streams,
    )

    release = Path(args.release).resolve()
    _require_runtime_release(release)
    agent_dir = Path(args.agent_dir).resolve()
    inputs_dir = Path(args.inputs_dir).resolve()
    if not agent_dir.is_dir():
        raise _pipeline_support.CliUsageError(f"agent dir is not a directory: {agent_dir}")
    if not inputs_dir.is_dir():
        raise _pipeline_support.CliUsageError(f"inputs dir is not a directory: {inputs_dir}")
    public_config = _runtime_yaml(
        release / "public" / args.task_id / "config.yaml", label="public task config"
    )
    try:
        selected = destination_from_config(public_config)
        if args.destination is not None:
            explicit = normalize_destination(args.destination)
            if explicit is not selected:
                candidate = (
                    release / "public" / args.task_id / "destinations"
                    / explicit.value / "config.yaml"
                )
                if not candidate.is_file():
                    raise ValueError("--destination does not match the public task config")
                selected = explicit
        answer_key = release / "private" / args.task_id / "answer_key"
        _, expected_counts = _load_stage1_counts(
            answer_key, None, args.population, selected
        )
    except (EvaluationError, ValueError) as exc:
        raise _pipeline_support.CliUsageError(str(exc)) from None
    credentials = _runtime_json(args.airbyte_credential, label="task Airbyte credential")
    workspace_id = _task_workspace_id(credentials, label="task Airbyte credential")
    client = _airbyte_client_from_credentials(args.airbyte_url, credentials)
    try:
        listed = client.list_connections(workspace_id)
    except AirbyteError as exc:
        raise _pipeline_support.CliUsageError(str(exc)) from None
    intent = None
    if not args.skip_terraform_intent:
        intent = terraform_intent_report(
            release, args.task_id, agent_dir / "elt", destination=selected.value
        )
    ignore = tuple(args.ignore) if args.ignore else DEFAULT_IGNORED_TOP_LEVEL
    try:
        result = evaluate_process_compliance(
            agent_dir=agent_dir,
            inputs_dir=inputs_dir,
            expected_tables=expected_counts,
            workspace_connections=workspace_connection_streams(listed),
            terraform_intent=intent,
            ignore_top_level=ignore,
        )
    except ProcessComplianceError as exc:
        raise _pipeline_support.CliUsageError(str(exc)) from None
    payload = result.to_json()
    payload["task_id"] = args.task_id
    payload["workspace_id"] = workspace_id
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if (args.strict and not result.compliant) else 0

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
    basic_credential_path = getattr(args, "airbyte_basic_credential", None)
    if basic_credential_path is not None:
        basic = _runtime_json(basic_credential_path, label="Airbyte gateway credential")
        airbyte = {
            key: value
            for key, value in airbyte.items()
            if key not in ("client_id", "client_secret", "username", "email", "password")
        }
        airbyte["username"] = basic.get("username")
        airbyte["password"] = basic.get("password")
    public_config = _runtime_yaml(
        release / "public" / args.task_id / "config.yaml",
        label="public task config",
    )
    shipped_destination_config: Path | None = None
    try:
        selected = destination_from_config(public_config)
        if (
            args.destination is not None
            and normalize_destination(args.destination) is not selected
        ):
            # Every other destination the release ships keeps its own public
            # config under destinations/<name>/; select it when asked for.
            candidate = (
                release / "public" / args.task_id / "destinations"
                / normalize_destination(args.destination).value / "config.yaml"
            )
            if not candidate.is_file():
                raise ValueError("--destination does not match the public task config")
            shipped_destination_config = candidate
            public_config = _runtime_yaml(candidate, label="shipped destination config")
            selected = destination_from_config(public_config)
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
        raise _pipeline_support.CliUsageError(str(exc)) from None
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
            shipped_destination_config=shipped_destination_config,

            custom_api_definition_id=args.custom_api_definition_id,
            airbyte_server_url=args.airbyte_server_url,
        )
    except (SourceEnvironmentError, OSError, ValueError) as exc:
        if created_environment:
            shutil.rmtree(environment_dir, ignore_errors=True)
        raise _pipeline_support.CliUsageError(str(exc)) from None
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
        raise _pipeline_support.CliUsageError(
            "Airbyte stability window must be shorter than the readiness timeout"
        )
    if readiness_poll_interval > stability_window / 2.0:
        raise _pipeline_support.CliUsageError(
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
        raise _pipeline_support.CliUsageError(detail) from None
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
        raise _pipeline_support.CliUsageError(str(exc)) from None
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
        raise _pipeline_support.CliUsageError(str(exc)) from None
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
        raise _pipeline_support.CliUsageError(str(exc)) from None
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
        raise _pipeline_support.CliUsageError(str(exc)) from None
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
        raise _pipeline_support.CliUsageError(str(exc)) from None
    print(f"fresh scoped Redshift database/schema: {args.database}.{args.schema}")
    print(f"solver credentials: {args.credential_out}")
    return 0


class _UsageArgumentError(_pipeline_support.CliUsageError, argparse.ArgumentTypeError):
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
            raise _pipeline_support.CliUsageError(
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
        raise _pipeline_support.CliUsageError(
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
        raise _pipeline_support.CliUsageError(str(exc)) from None
    if destination is not Destination.SNOWFLAKE:
        if destination_credential is not None:
            raise _pipeline_support.CliUsageError(
                "--destination-credential environment forwarding is currently "
                "only needed for Snowflake Stage 2"
            )
        return {}
    if destination_credential is None:
        raise _pipeline_support.CliUsageError(
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
        raise _pipeline_support.CliUsageError(str(exc)) from None
    installed = (config.get("snowflake") or {}).get("config")
    if not isinstance(installed, dict):
        raise _pipeline_support.CliUsageError(
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
            raise _pipeline_support.CliUsageError(
                f"installed Snowflake config has an empty/missing field: {config_key}"
            )
        supplied = credential.get(credential_key)
        if supplied is not None and supplied != configured:
            raise _pipeline_support.CliUsageError(
                "Snowflake Stage 2 credential does not match the installed "
                f"attempt field: {credential_key}"
            )
        if credential_key in {"account", "user", "password"} and supplied is None:
            # load_credentials already enforces these; keep the local invariant
            # explicit if its contract changes.
            raise _pipeline_support.CliUsageError(
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
        raise _pipeline_support.CliUsageError("installed task config has no Airbyte.config mapping")
    if args.airbyte_credential:
        credentials = _runtime_json(args.airbyte_credential, label="Airbyte credential")
    else:
        credentials = dict(airbyte_config)
    url = args.airbyte_url or airbyte_config.get("server_url")
    if not isinstance(url, str) or not url:
        raise _pipeline_support.CliUsageError("no Airbyte API URL was supplied or installed")
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
        raise _pipeline_support.CliUsageError(str(exc)) from None
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
        raise _pipeline_support.CliUsageError("installed task config has no Airbyte.config mapping")
    if args.airbyte_credential:
        credentials = _runtime_json(
            args.airbyte_credential, label="Airbyte credential"
        )
    else:
        credentials = dict(airbyte_config)
    url = args.airbyte_url or airbyte_config.get("server_url")
    if not isinstance(url, str) or not url:
        raise _pipeline_support.CliUsageError("no Airbyte API URL was supplied or installed")
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
        raise _pipeline_support.CliUsageError(str(exc)) from None
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
        raise _pipeline_support.CliUsageError(str(exc)) from None
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
                # Every other destination the release ships keeps its own
                # public config under destinations/<name>/; verify against it.
                candidate = (
                    release / "public" / args.task_id / "destinations"
                    / explicit.value / "config.yaml"
                )
                if not candidate.is_file():
                    raise ValueError(
                        "--destination does not match the public task config"
                    )
                public_config = _runtime_yaml(candidate, label="shipped destination config")
                selected = destination_from_config(public_config)
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
        raise _pipeline_support.CliUsageError(str(exc)) from None
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
        raise _pipeline_support.CliUsageError(str(exc)) from None


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
        raise _pipeline_support.CliUsageError(str(exc)) from None
    payload = report.model_dump(mode="json")
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        out = Path(args.out).resolve()
        try:
            publish_immutable_json(out, payload)
        except CertificationLifecycleError:
            raise _pipeline_support.CliUsageError(
                f"certified difficulty output already exists (immutable): {out}"
            ) from None
        except OSError as exc:
            raise _pipeline_support.CliUsageError(
                f"could not create certified difficulty output {out}: {exc}"
            ) from None
    print(rendered, end="")
    return 0



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

def _apply_generation_semantics(task, args):
    from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError

    try:
        return DEFAULT_GENERATION_CONFIG.apply(task, seed=getattr(args, "selection_seed", None) or 0)
    except SemanticDifficultyError as exc:
        raise _pipeline_support.CliUsageError(f"semantic generation refused for {task.task_id}: {exc}") from exc


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
        help="reproducible source/entry and mart recipe selection seed",
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
    rp.add_argument(
        "--airbyte-basic-credential",
        default=None,
        type=Path,
        help=(
            "write this username/password pair into the task config in place of the "
            "client-credential pair; serve it with runtime airbyte-gateway and pass "
            "the gateway's URL as --airbyte-server-url"
        ),
    )
    rp.set_defaults(func=cmd_runtime_prepare)

    rp = runtime_sub.add_parser(
        "airbyte-gateway",
        help=(
            "serve Airbyte to agents with the username/password interface ELT-Bench "
            "configures, forwarding with a client-credential token"
        ),
    )
    rp.add_argument("--listen", default="0.0.0.0:8006", help="host:port to bind")
    rp.add_argument("--upstream", default="http://localhost:8000",
                    help="Airbyte control plane URL reachable from this host")
    rp.add_argument("--airbyte-credential", required=True, type=Path,
                    help="client_id/client_secret file used against the control plane")
    rp.add_argument("--gateway-credential", required=True, type=Path,
                    help="username/password file agents use; see --create-credential")
    rp.add_argument("--create-credential", action="store_true",
                    help="write a random username/password file when it does not exist")
    rp.add_argument("--quiet", action="store_true", help="do not log each request")
    rp.set_defaults(func=cmd_runtime_airbyte_gateway)

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
        "teardown-task",
        help="delete the Airbyte workspace one task run bootstrapped",
    )
    rp.add_argument("--airbyte-url", required=True)
    rp.add_argument("--airbyte-credential", required=True, type=Path,
                    help="the per-task credential bootstrap-task wrote")
    rp.add_argument("--base-credential", default=None, type=Path,
                    help="refuse to delete this credential's workspace")
    rp.add_argument("--remove-credential", action="store_true",
                    help="delete the per-task credential file after the workspace")
    rp.set_defaults(func=cmd_runtime_teardown_task)

    rp = runtime_sub.add_parser(
        "purge-workspaces",
        help="list (or with --apply delete) stale task workspaces on a shared instance",
    )
    rp.add_argument("--airbyte-url", required=True)
    rp.add_argument("--airbyte-credential", required=True, type=Path,
                    help="the base instance credential; its workspace is kept")
    rp.add_argument("--name-prefix", default="ELT-Bench ",
                    help="only workspaces whose name starts with this are candidates")
    rp.add_argument("--keep-name", action="append", default=[])
    rp.add_argument("--keep-credentials", action="append", default=[],
                    help="glob of per-task credential files whose workspaces are kept")
    rp.add_argument("--apply", action="store_true", help="delete instead of listing")
    rp.set_defaults(func=cmd_runtime_purge_workspaces)

    rp = runtime_sub.add_parser(
        "verify-process",
        help=(
            "report Terraform/Airbyte/elt-only process compliance of one agent run "
            "(the reward is unchanged)"
        ),
    )
    rp.add_argument("--release", required=True, type=Path)
    rp.add_argument("--task-id", required=True)
    rp.add_argument("--population", default="primary")
    rp.add_argument("--destination", choices=("snowflake", "databricks", "redshift"), default=None)
    rp.add_argument("--agent-dir", required=True, type=Path,
                    help="the agent's mount directory (contains elt/)")
    rp.add_argument("--inputs-dir", required=True, type=Path,
                    help="the served task directory the runner copied into the mount")
    rp.add_argument("--airbyte-url", required=True)
    rp.add_argument("--airbyte-credential", required=True, type=Path,
                    help="the per-task credential bootstrap-task wrote")
    rp.add_argument("--ignore", action="append", default=[],
                    help="top-level mount entries owned by the runner (default: known runner dirs)")
    rp.add_argument("--skip-terraform-intent", action="store_true")
    rp.add_argument("--strict", action="store_true", help="exit 1 when not compliant")
    rp.set_defaults(func=cmd_runtime_verify_process)

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
    except _pipeline_support.CliUsageError as exc:
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

"""Prepare and publish fatal-recovery evidence without provider calls.

Preparation does not change the ledger. Application appends one non-PASS row.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from elt_taskgen.engine import (
    BLOCKED_ON_HUMAN,
    FATAL_RECOVERY_EVIDENCE_DIR,
    FATAL_RECOVERY_REVALIDATOR_MODULE,
    FATAL_RECOVERY_REVALIDATOR_QUALNAME,
    MAX_FATAL_RECOVERY_OBSERVATIONS,
    RETRY_GUARD_EXPLICIT,
    VERDICT_BLOCKED,
    VERDICT_FAIL,
    Engine,
    EngineError,
    FatalRecoveryEvidence,
    FatalReportRecovery,
    RecoveryCallableIdentity,
    RecoveryCodeIdentity,
    RecoveryObservation,
    ReportRow,
    recovery_code_fingerprint,
    recovery_code_identity,
)
from elt_taskgen.models import TaskIR, canonical_json, readable_json, validate_task_id_segment


REQUEST_SCHEMA = "fatal-recovery-request-v1"
EVIDENCE_DIR = FATAL_RECOVERY_EVIDENCE_DIR


class FatalRecoveryRequest(BaseModel):
    """One bounded request; no roster wildcard and no implicit target lookup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["fatal-recovery-request-v1"] = REQUEST_SCHEMA
    task_id: str = Field(min_length=1)
    recovery_kind: str = Field(pattern=r"^[a-z][a-z0-9_]{2,127}$")
    stage: str = Field(min_length=1)
    cause_report_id: int = Field(gt=0)
    cause_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_report_id: int = Field(gt=0)
    target_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_paths: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_FATAL_RECOVERY_OBSERVATIONS,
    )

    @model_validator(mode="after")
    def _bounded_exact_request(self) -> "FatalRecoveryRequest":
        validate_task_id_segment(self.task_id)
        if len(set(self.observation_paths)) != len(self.observation_paths):
            raise ValueError("observation_paths must be unique")
        if self.cause_report_id > self.target_report_id:
            raise ValueError("fatal recovery cause cannot follow its target")
        return self


class PreparedFatalRecovery(BaseModel):
    """Non-secret result of preparing or applying one recovery record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    stage: str
    target_report_id: int
    disposition: Literal["fail", "blocked"]
    evidence_path: str
    evidence_sha256: str
    ledger_appended: bool = False


RecoveryAnalyzer = Callable[
    [TaskIR, ReportRow, ReportRow, Mapping[str, bytes]],
    dict[str, str],
]


@dataclass(frozen=True)
class RecoveryKind:
    """Closed policy for one historically observed pipeline failure class."""

    name: str
    stage: str
    disposition: Literal["fail", "blocked"]
    failure_class: Literal["stale_evidence", "pending_adjudication"]
    failure_code: str
    blocked_on: str
    retry_guard: str
    recovery_prerequisite: str
    reason: str
    fixed_modules: tuple[str, ...]
    analyzer: RecoveryAnalyzer


def _decoded_payload(row: ReportRow, label: str) -> dict:
    try:
        payload = json.loads(row.payload_json)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} payload is malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} payload is not an object")
    return payload


def _author_prose_fidelity_analyzer(
    task: TaskIR,
    cause: ReportRow,
    target: ReportRow,
    observations: Mapping[str, bytes],
) -> dict[str, str]:
    """Re-run the exact deterministic author gate on its persisted prose."""

    from elt_taskgen.review import prose_fidelity

    if observations:
        raise ValueError("author prose recovery accepts no free-form observations")
    cause_payload = _decoded_payload(cause, "author cause")
    cause_data = cause_payload.get("data")
    old_error = str(cause_payload.get("error") or "")
    prompt = task.solver_prompt or ""
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    if (
        not isinstance(cause_data, dict)
        or cause_data.get("gate") != prose_fidelity.GATE_NAME
        or cause_data.get("prose_sha256") != prompt_sha256
        or cause_data.get("source")
        not in {"authored_candidate", "authored_revised", "repair_patch_preserved"}
        or not old_error.startswith("prose fidelity:")
    ):
        raise ValueError(
            "author cause is not the persisted prose-fidelity failure shape"
        )
    findings = tuple(prose_fidelity.check_prose_fidelity(task))
    if findings:
        raise ValueError("current prose-fidelity analyzer still rejects the task")
    target_payload = _decoded_payload(target, "author fatal")
    target_data = target_payload.get("data")
    terminal_text = " ".join(
        str(target_payload.get(key) or "") for key in ("detail", "error")
    ).casefold()
    if (
        not isinstance(target_data, dict)
        or target_data.get("failed_stage") != "author"
        or target_data.get("route") != "specification"
        or not any(
            marker in terminal_text
            for marker in ("repair budget exhausted", "inert repair path")
        )
    ):
        raise ValueError("author target is not the fatal caused by that failed gate")
    return {
        "analyzer": "prose_fidelity.check_prose_fidelity",
        "cause_error_sha256": hashlib.sha256(old_error.encode("utf-8")).hexdigest(),
        "current_finding_count": "0",
        "current_findings_sha256": hashlib.sha256(
            canonical_json(findings).encode("utf-8")
        ).hexdigest(),
        "prose_sha256": prompt_sha256,
    }


def _author_instruction_regeneration_analyzer(
    task: TaskIR,
    cause: ReportRow,
    target: ReportRow,
    observations: Mapping[str, bytes],
) -> dict[str, str]:
    """Bind an invalid historical draft and require a new author run.

    The recovery remains FAIL after verifying the current TaskIR-derived prompt
    and independent fidelity result.
    """

    from elt_taskgen.models import CouncilRole
    from elt_taskgen.review import council, prompts, prose_fidelity

    if observations:
        raise ValueError("author regeneration recovery accepts no observations")
    cause_payload = _decoded_payload(cause, "author regeneration cause")
    cause_data = cause_payload.get("data")
    old_error = str(cause_payload.get("error") or "")
    prompt = task.solver_prompt or ""
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    if (
        not isinstance(cause_data, dict)
        or cause_data.get("gate") != prose_fidelity.GATE_NAME
        or cause_data.get("prose_sha256") != prompt_sha256
        or cause_data.get("source")
        not in {"authored_candidate", "authored_revised"}
        or not old_error.startswith("prose fidelity:")
    ):
        raise ValueError("cause is not an exact incomplete authored draft")

    findings = tuple(prose_fidelity.check_prose_fidelity(task))
    if not findings:
        raise ValueError(
            "persisted prose is already green; use the false-fatal recovery kind"
        )
    author_view = council.render_view(CouncilRole.SEMANTIC_AUTHOR, task)
    required_fragments = (
        "SOURCE SCHEMA:",
        "MART PLAN SUMMARIES:",
        "=== BEGIN DATA MODEL ===",
        "=== END DATA MODEL ===",
    )
    if any(fragment not in author_view for fragment in required_fragments):
        raise ValueError("current author view lacks a TaskIR requirement block")
    for relationship in task.relationships:
        declaration = (
            f"{relationship.child_table}({', '.join(relationship.child_columns)}) -> "
            f"{relationship.parent_table}({', '.join(relationship.parent_columns)})"
        )
        if declaration not in author_view:
            raise ValueError("current author view omits a source relationship")
    for mart in task.marts:
        if mart.name not in author_view:
            raise ValueError("current author view omits a mart requirement block")

    author_system = prompts.ROLE_SYSTEM[CouncilRole.SEMANTIC_AUTHOR.value]
    prompt_requirements = (
        "COMPLETENESS IS MECHANICALLY CHECKED",
        "relationship's child table + keys and parent table + keys",
        "every output column",
        "including labels such as no_activity",
        "required join preservation direction",
    )
    if any(requirement not in author_system for requirement in prompt_requirements):
        raise ValueError("current semantic-author prompt lacks required coverage rules")

    target_payload = _decoded_payload(target, "author regeneration fatal")
    target_data = target_payload.get("data")
    terminal_text = " ".join(
        str(target_payload.get(key) or "") for key in ("detail", "error")
    ).casefold()
    if (
        not isinstance(target_data, dict)
        or target_data.get("failed_stage") != "author"
        or target_data.get("route") != "specification"
        or not any(
            marker in terminal_text
            for marker in ("repair budget exhausted", "inert repair path")
        )
    ):
        raise ValueError("target is not the fatal caused by incomplete author prose")

    return {
        "analyzer": "prose_fidelity.check_prose_fidelity",
        "current_finding_count": str(len(findings)),
        "current_findings_sha256": hashlib.sha256(
            canonical_json(findings).encode("utf-8")
        ).hexdigest(),
        "old_failure_sha256": hashlib.sha256(old_error.encode("utf-8")).hexdigest(),
        "prose_sha256": prompt_sha256,
        "taskir_author_view_sha256": hashlib.sha256(
            author_view.encode("utf-8")
        ).hexdigest(),
        "semantic_author_prompt_sha256": hashlib.sha256(
            author_system.encode("utf-8")
        ).hexdigest(),
    }


def _independent_gold_disagreement_analyzer(
    task: TaskIR,
    cause: ReportRow,
    target: ReportRow,
    observations: Mapping[str, bytes],
) -> dict[str, str]:
    """Validate a row-level disagreement record against its current artifacts."""

    from elt_taskgen.reference import independent
    from elt_taskgen.reference.adjudication import (
        ADJUDICATION_STATUS_PENDING,
        ANALYSIS_STATUS_DIFFERENCES,
        IndependentDisagreementAnalysis,
    )
    from elt_taskgen.reference.gold import MANIFEST_FILENAME

    independent_path = (
        f"tasks/{task.task_id}/{independent.INDEPENDENT_BUILD_EVIDENCE_REL}"
    )
    gold_path = f"tasks/{task.task_id}/answer_key/{MANIFEST_FILENAME}"
    analysis_paths = [
        path
        for path in observations
        if path.startswith(f"audit/{task.task_id}.dual_build_analysis.")
        and path.endswith(".json")
    ]
    if set(observations) != {independent_path, gold_path, *analysis_paths} or len(
        analysis_paths
    ) != 1:
        raise ValueError(
            "independent disagreement recovery requires exactly the current "
            "independent build, gold manifest, and one content-addressed analysis"
        )
    analysis_bytes = observations[analysis_paths[0]]
    try:
        analysis = IndependentDisagreementAnalysis.model_validate_json(analysis_bytes)
    except ValueError as exc:
        raise ValueError("row-level disagreement analysis is malformed") from exc
    analysis_sha256 = hashlib.sha256(analysis_bytes).hexdigest()
    if (
        analysis.task_id != task.task_id
        or analysis.task_content_hash != task.content_hash()
        or analysis.analysis_status != ANALYSIS_STATUS_DIFFERENCES
        or analysis.adjudication_status != ADJUDICATION_STATUS_PENDING
        or analysis.independent_build_sha256
        != hashlib.sha256(observations[independent_path]).hexdigest()
        or analysis.gold_manifest_sha256
        != hashlib.sha256(observations[gold_path]).hexdigest()
        or not any(not mart.comparator_match for mart in analysis.marts)
        or not any(mart.differences for mart in analysis.marts)
        or not analysis_paths[0].endswith(f".{analysis_sha256}.json")
    ):
        raise ValueError("row-level disagreement analysis is stale or unbound")
    cause_payload = _decoded_payload(cause, "independent disagreement cause")
    gates = cause_payload.get("gates")
    trusted = [
        gate
        for gate in gates or ()
        if isinstance(gate, dict) and gate.get("gate") == "trusted-solution"
    ]
    if (
        cause_payload.get("accepted") is not False
        or len(trusted) != 1
        or trusted[0].get("passed") is not False
        or "needs_adjudication" not in str(trusted[0].get("details") or "")
    ):
        raise ValueError("cause is not an independent-gold adjudication failure")
    target_payload = _decoded_payload(target, "independent disagreement fatal")
    target_data = target_payload.get("data")
    if (
        not isinstance(target_data, dict)
        or target_data.get("failed_stage") != "gates"
        or "repair budget exhausted"
        not in str(target_payload.get("detail") or "").casefold()
    ):
        raise ValueError("target is not the fatal caused by the disagreement")
    mismatch_count = sum(not mart.comparator_match for mart in analysis.marts)
    row_difference_count = sum(len(mart.differences) for mart in analysis.marts)
    return {
        "adjudication_status": analysis.adjudication_status,
        "analysis_sha256": analysis_sha256,
        "independent_build_sha256": analysis.independent_build_sha256,
        "gold_manifest_sha256": analysis.gold_manifest_sha256,
        "mismatching_population_marts": str(mismatch_count),
        "recorded_row_differences": str(row_difference_count),
    }


def _independent_witness_error_adjudicated_analyzer(
    task: TaskIR,
    cause: ReportRow,
    target: ReportRow,
    observations: Mapping[str, bytes],
) -> dict[str, str]:
    """Reopen a fatal only for an exact decision authorizing one blind build."""

    from elt_taskgen.reference import independent
    from elt_taskgen.reference.adjudication import (
        IndependentDisagreementDecision,
        validate_fresh_build_decision_evidence,
    )
    from elt_taskgen.reference.gold import MANIFEST_FILENAME

    independent_path = (
        f"tasks/{task.task_id}/{independent.INDEPENDENT_BUILD_EVIDENCE_REL}"
    )
    gold_path = f"tasks/{task.task_id}/answer_key/{MANIFEST_FILENAME}"
    analysis_paths = [
        path
        for path in observations
        if path.startswith(f"audit/{task.task_id}.dual_build_analysis.")
        and path.endswith(".json")
    ]
    diagnosis_paths = [
        path
        for path in observations
        if path.startswith(f"audit/{task.task_id}.dual_build_diagnosis.")
        and path.endswith(".json")
    ]
    decision_paths = [
        path
        for path in observations
        if path.startswith(f"audit/{task.task_id}.dual_build_decision.")
        and path.endswith(".json")
    ]
    expected = {
        independent_path,
        gold_path,
        *analysis_paths,
        *diagnosis_paths,
        *decision_paths,
    }
    if (
        set(observations) != expected
        or len(analysis_paths) != 1
        or len(diagnosis_paths) != 1
        or len(decision_paths) != 1
    ):
        raise ValueError(
            "adjudicated witness recovery requires exactly the current "
            "independent build, gold manifest, analysis, diagnosis, and decision"
        )

    # Preserve the exact neutral-disagreement checks used by unresolved holds.
    base = _independent_gold_disagreement_analyzer(
        task,
        cause,
        target,
        {
            independent_path: observations[independent_path],
            gold_path: observations[gold_path],
            analysis_paths[0]: observations[analysis_paths[0]],
        },
    )
    diagnosis_bytes = observations[diagnosis_paths[0]]
    decision_bytes = observations[decision_paths[0]]
    diagnosis_sha256 = hashlib.sha256(diagnosis_bytes).hexdigest()
    decision_sha256 = hashlib.sha256(decision_bytes).hexdigest()
    if not diagnosis_paths[0].endswith(f".{diagnosis_sha256}.json"):
        raise ValueError("witness-error diagnosis filename is not content-addressed")
    if not decision_paths[0].endswith(f".{decision_sha256}.json"):
        raise ValueError("witness-error decision filename is not content-addressed")
    try:
        decision = IndependentDisagreementDecision.model_validate_json(decision_bytes)
    except ValueError as exc:
        raise ValueError("witness-error adjudication decision is malformed") from exc
    if decision.digest() != decision_sha256:
        raise ValueError("witness-error adjudication decision is non-canonical")
    validate_fresh_build_decision_evidence(
        task,
        independent_build_bytes=observations[independent_path],
        gold_manifest_bytes=observations[gold_path],
        analysis_bytes=observations[analysis_paths[0]],
        diagnosis_bytes=diagnosis_bytes,
        decision=decision,
    )
    return {
        **base,
        "adjudication_status": "witness_error_adjudicated",
        "diagnosis_sha256": diagnosis_sha256,
        "decision_action": decision.action,
        "decision_sha256": decision_sha256,
        "gate_effect": decision.gate_effect,
    }


def _critic_attack_handoff_analyzer(
    task: TaskIR,
    cause: ReportRow,
    target: ReportRow,
    observations: Mapping[str, bytes],
) -> dict[str, str]:
    """Recognize only the two saved handoff paths the fixed protocol closes."""

    from elt_taskgen import cli
    from elt_taskgen.models import AttackKind
    from elt_taskgen.verification import attacks

    cause_payload = _decoded_payload(cause, "critic handoff cause")
    detail = str(cause_payload.get("detail") or "")
    error = str(cause_payload.get("error") or "")
    target_payload = _decoded_payload(target, "critic handoff fatal")
    target_data = target_payload.get("data")
    if (
        not isinstance(target_data, dict)
        or target_data.get("failed_stage") != "attack"
        or "repair budget exhausted"
        not in str(target_payload.get("detail") or "").casefold()
    ):
        raise ValueError("target is not the fatal caused by the attack handoff")

    if (
        "critic-to-mutation handoff BLOCKED:" in detail
        and "major finding has no structured proposed_case; it is unresolved"
        in detail
    ):
        if observations:
            raise ValueError("saved handoff-block recovery accepts no observation")
        blocking = cause_payload.get("blocking_finding")
        if (
            not isinstance(blocking, dict)
            or blocking.get("severity") != "major"
            or blocking.get("role")
            not in {"ambiguity_critic", "population_adversary"}
        ):
            raise ValueError("saved blocking-finding descriptor is not exact")
        signature = "missing_proposed_case"
        subject = canonical_json(blocking)
    elif error.startswith(
        "ValueError: attack kind 'wrong_agg_stage' has no variant ''"
    ):
        expected_path = f"tasks/{task.task_id}/reports/"
        review_paths = [
            path
            for path in observations
            if path.startswith(expected_path) and path.endswith("_review.json")
        ]
        if set(observations) != set(review_paths) or len(review_paths) != 1:
            raise ValueError(
                "legacy wrong-agg handoff requires exactly one bound review report"
            )
        try:
            review_copy = json.loads(observations[review_paths[0]])
        except (TypeError, ValueError) as exc:
            raise ValueError("bound review report is malformed") from exc
        if (
            not isinstance(review_copy, dict)
            or review_copy.get("task_id") != task.task_id
            or review_copy.get("stage") != "review"
            or review_copy.get("verdict") != "pass"
            or review_copy.get("content_hash") != task.content_hash()
        ):
            raise ValueError("bound review report identity differs")
        review_payload = review_copy.get("payload")
        findings = review_payload.get("findings") if isinstance(review_payload, dict) else None
        wrong_agg = [
            finding
            for finding in findings or ()
            if isinstance(finding, dict)
            and finding.get("severity") == "major"
            and finding.get("suggested_attack") == "wrong_agg_stage"
            and finding.get("proposed_case") is None
        ]
        if len(wrong_agg) != 1:
            raise ValueError("review has no unique missing wrong-agg proposal")
        if attacks._default_attack_directive(AttackKind.WRONG_AGG_STAGE) is not None:
            raise ValueError("current attack compiler unexpectedly guesses a default")
        if attacks.KIND_VARIANTS[AttackKind.WRONG_AGG_STAGE] != frozenset(
            {"filter_before_aggregate"}
        ):
            raise ValueError("current wrong-agg variant registry differs")
        signature = "wrong_agg_variant_was_guessed"
        subject = canonical_json(wrong_agg[0])
    else:
        raise ValueError("cause is not a recognized historical critic handoff failure")

    if (
        cli.CRITIC_PROTOCOL_FAILURE_CODE != "critic_attack_handoff_invalid"
        or cli.CRITIC_PROTOCOL_RECOVERY != "correct_or_replace_critic_handoff"
    ):
        raise ValueError("current critic protocol disposition identity differs")
    return {
        "current_disposition": "protocol_block_before_attack_execution",
        "failure_code": cli.CRITIC_PROTOCOL_FAILURE_CODE,
        "historical_signature": signature,
        "subject_sha256": hashlib.sha256(subject.encode("utf-8")).hexdigest(),
    }


def _reserved_identifier_filter_analyzer(
    task: TaskIR,
    cause: ReportRow,
    target: ReportRow,
    observations: Mapping[str, bytes],
) -> dict[str, str]:
    """Re-run the shipped intake filter and shared identifier renderer."""

    from elt_taskgen.sql_identifiers import (
        identifier_needs_quoting,
        quote_sql_identifier,
    )
    from elt_taskgen.verification import filters

    if cause.id != target.id or observations:
        raise ValueError("reserved-identifier recovery requires one direct fatal only")
    payload = _decoded_payload(target, "reserved identifier fatal")
    data = payload.get("data")
    if (
        not isinstance(data, dict)
        or data.get("filter") != filters.FORMAT_BLACKLIST_FILTER
        or data.get("action") != "reject"
        or "blacklisted candidate shape: reserved-word-relation-name"
        not in str(payload.get("error") or "")
    ):
        raise ValueError("fatal is not the retired reserved-identifier blacklist rule")
    names = sorted(
        {
            *(table.name for table in task.tables),
            *(mart.name for mart in task.marts),
        }
    )
    quoted = {
        name: quote_sql_identifier(name)
        for name in names
        if identifier_needs_quoting(name)
    }
    if not quoted or not all(rendered != name for name, rendered in quoted.items()):
        raise ValueError("task has no reserved identifier handled by shared quoting")
    reports = filters.run_intake_filters(task, (), filters.load_filter_config())
    blacklist = [
        report
        for report in reports
        if report.filter == filters.FORMAT_BLACKLIST_FILTER
    ]
    if (
        len(blacklist) != 1
        or not blacklist[0].passed
        or "rule:reserved-word-relation-name" in blacklist[0].evidence
    ):
        raise ValueError("current shipped intake policy still rejects reserved names")
    return {
        "current_filter_result": "pass",
        "quoted_identifier_count": str(len(quoted)),
        "quoted_identifiers_sha256": hashlib.sha256(
            canonical_json(quoted).encode("utf-8")
        ).hexdigest(),
        "retired_rule": "reserved-word-relation-name",
    }


RECOVERY_KINDS: dict[str, RecoveryKind] = {
    "author_prose_fidelity_false_fatal_v1": RecoveryKind(
        name="author_prose_fidelity_false_fatal_v1",
        stage="author",
        disposition=VERDICT_FAIL,
        failure_class="stale_evidence",
        failure_code="obsolete_author_validator_fatal",
        blocked_on="",
        retry_guard="",
        recovery_prerequisite="rerun_author_with_current_validator",
        reason=(
            "the exact persisted prose-fidelity failure is green under the "
            "current deterministic validator and must be remeasured"
        ),
        fixed_modules=(
            "elt_taskgen.review.declarative_prose",
            "elt_taskgen.review.prose_fidelity",
        ),
        analyzer=_author_prose_fidelity_analyzer,
    ),
    "author_instruction_regeneration_v1": RecoveryKind(
        name="author_instruction_regeneration_v1",
        stage="author",
        disposition=VERDICT_FAIL,
        failure_class="stale_evidence",
        failure_code="incomplete_author_prose_requires_regeneration",
        blocked_on="",
        retry_guard="",
        recovery_prerequisite="rerun_author_from_current_taskir_requirements",
        reason=(
            "the exact historical author draft remains incomplete, while the "
            "current TaskIR-derived author view, prompt contract, and independent "
            "coverage checker require a newly generated draft"
        ),
        fixed_modules=(
            "elt_taskgen.review.council",
            "elt_taskgen.review.prompts",
            "elt_taskgen.review.prose_fidelity",
            "elt_taskgen.review.tools.validators",
        ),
        analyzer=_author_instruction_regeneration_analyzer,
    ),
    "independent_gold_disagreement_pending_v1": RecoveryKind(
        name="independent_gold_disagreement_pending_v1",
        stage="gates",
        disposition=VERDICT_BLOCKED,
        failure_class="pending_adjudication",
        failure_code="independent_gold_disagreement",
        blocked_on=BLOCKED_ON_HUMAN,
        retry_guard=RETRY_GUARD_EXPLICIT,
        recovery_prerequisite="bound_adjudication_or_new_independent_build",
        reason=(
            "the exact trusted-solution disagreement has row-level bound "
            "evidence and remains unresolved pending adjudication"
        ),
        fixed_modules=(
            "elt_taskgen.engine",
            "elt_taskgen.reference.adjudication",
            "elt_taskgen.verification.gates",
        ),
        analyzer=_independent_gold_disagreement_analyzer,
    ),
    "independent_witness_error_adjudicated_v1": RecoveryKind(
        name="independent_witness_error_adjudicated_v1",
        stage="gates",
        disposition=VERDICT_FAIL,
        failure_class="stale_evidence",
        failure_code="independent_witness_error_adjudicated",
        blocked_on="",
        retry_guard="",
        recovery_prerequisite="rerun_gates_for_one_fresh_blind_independent_build",
        reason=(
            "the exact historical disagreement is bound to an explicit "
            "witness-error decision that authorizes one new solver-isolated "
            "build, without changing gold or declaring acceptance"
        ),
        fixed_modules=(
            "elt_taskgen.cli",
            "elt_taskgen.engine",
            "elt_taskgen.reference.adjudication",
            "elt_taskgen.reference.independent",
            "elt_taskgen.verification.gates",
        ),
        analyzer=_independent_witness_error_adjudicated_analyzer,
    ),
    "critic_attack_handoff_misclassified_fatal_v1": RecoveryKind(
        name="critic_attack_handoff_misclassified_fatal_v1",
        stage="attack",
        disposition=VERDICT_FAIL,
        failure_class="stale_evidence",
        failure_code="critic_attack_handoff_misclassified_fatal",
        blocked_on="",
        retry_guard="",
        recovery_prerequisite="rerun_attack_under_current_handoff_protocol",
        reason=(
            "the exact critic handoff was historically converted into a task "
            "fatal; current closed protocol requires a non-judging block"
        ),
        fixed_modules=(
            "elt_taskgen.cli",
            "elt_taskgen.review.tools.critic_validators",
            "elt_taskgen.verification.attacks",
        ),
        analyzer=_critic_attack_handoff_analyzer,
    ),
    "reserved_identifier_filter_false_fatal_v1": RecoveryKind(
        name="reserved_identifier_filter_false_fatal_v1",
        stage="contamination_pre",
        disposition=VERDICT_FAIL,
        failure_class="stale_evidence",
        failure_code="retired_reserved_identifier_blacklist",
        blocked_on="",
        retry_guard="",
        recovery_prerequisite="rerun_intake_and_sql_stages_with_shared_quoting",
        reason=(
            "the exact fatal came from the retired reserved-word relation "
            "blacklist; current policy admits and shared SQL rendering quotes it"
        ),
        fixed_modules=(
            "elt_taskgen.sql_identifiers",
            "elt_taskgen.verification.filters",
        ),
        analyzer=_reserved_identifier_filter_analyzer,
    ),
}


def validate_recovery_evidence(
    *,
    evidence: FatalRecoveryEvidence,
    task: TaskIR,
    cause: ReportRow,
    target: ReportRow,
    observations: Mapping[str, bytes],
) -> None:
    """Re-run the closed analyzer and require its deterministic certification."""

    if (
        task.task_id != evidence.task_id
        or task.content_hash() != evidence.task_content_hash
        or cause.id != evidence.cause_report_id
        or cause.task_id != evidence.task_id
        or cause.stage != evidence.stage
        or cause.content_hash != evidence.task_content_hash
        or target.id != evidence.target_report_id
        or target.task_id != evidence.task_id
        or target.stage != evidence.stage
        or target.content_hash != evidence.task_content_hash
    ):
        raise ValueError("recovery target/task identity differs")
    target_payload_sha256 = hashlib.sha256(
        target.payload_json.encode("utf-8")
    ).hexdigest()
    if target_payload_sha256 != evidence.target_payload_sha256:
        raise ValueError("recovery target payload digest differs")
    cause_payload_sha256 = hashlib.sha256(cause.payload_json.encode("utf-8")).hexdigest()
    if cause_payload_sha256 != evidence.cause_payload_sha256:
        raise ValueError("recovery cause payload digest differs")
    if set(observations) != {item.path for item in evidence.observations}:
        raise ValueError("recovery observation roster differs")
    kind = RECOVERY_KINDS.get(evidence.recovery_kind)
    if kind is None:
        raise ValueError("recovery kind is not in the closed analyzer registry")
    expected_policy = {
        "stage": kind.stage,
        "disposition": kind.disposition,
        "failure_class": kind.failure_class,
        "failure_code": kind.failure_code,
        "blocked_on": kind.blocked_on,
        "retry_guard": kind.retry_guard,
        "recovery_prerequisite": kind.recovery_prerequisite,
        "reason": kind.reason,
    }
    dumped = evidence.model_dump(mode="json")
    if any(dumped[key] != value for key, value in expected_policy.items()):
        raise ValueError("recovery evidence differs from its registered policy")
    current_code = _fixed_code_bindings(kind.fixed_modules)
    if evidence.fixed_code != current_code:
        raise ValueError("recovery evidence fixed-code registry identity changed")
    certification = kind.analyzer(task, cause, target, observations)
    if evidence.certification != certification:
        raise ValueError("recovery analyzer certification differs")


def _observation_bindings(
    engine: Engine,
    paths: Sequence[str],
) -> tuple[RecoveryObservation, ...]:
    bindings: list[RecoveryObservation] = []
    for path in sorted(paths):
        payload = engine._read_supersession_evidence(  # package-internal safe reader
            path,
            label="fatal recovery observation",
        )
        bindings.append(
            RecoveryObservation(
                path=path,
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
    return tuple(bindings)


def _fixed_code_bindings(modules: Sequence[str]) -> tuple[RecoveryCodeIdentity, ...]:
    return tuple(recovery_code_identity(module) for module in sorted(modules))


def _write_content_addressed(
    workspace: Path,
    evidence: FatalRecoveryEvidence,
) -> tuple[str, str]:
    payload = evidence.deterministic_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    relative = f"{EVIDENCE_DIR}/{digest}.json"
    destination = workspace / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != payload:
            raise EngineError("fatal recovery content-addressed path changed")
        return relative, digest

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{digest}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            if destination.is_symlink() or destination.read_bytes() != payload:
                raise EngineError(
                    "fatal recovery content-addressed publication conflicted"
                ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return relative, digest


def _marker_from_evidence(
    *,
    evidence_path: str,
    evidence_sha256: str,
    evidence: FatalRecoveryEvidence,
) -> FatalReportRecovery:
    return FatalReportRecovery.from_evidence(
        evidence_path=evidence_path,
        evidence_sha256=evidence_sha256,
        evidence=evidence,
    )


def prepare_recovery(
    workspace: Path,
    request: FatalRecoveryRequest,
) -> PreparedFatalRecovery:
    """Persist immutable evidence without changing task or ledger state."""

    engine = Engine(workspace)
    try:
        task = engine.load_task(request.task_id)
        kind = RECOVERY_KINDS.get(request.recovery_kind)
        if kind is None:
            raise EngineError(
                "fatal recovery kind is not in the closed analyzer registry"
            )
        if request.stage != kind.stage:
            raise EngineError("fatal recovery request stage differs from its kind")
        cause = engine.report_by_id(request.cause_report_id)
        target = engine.report_by_id(request.target_report_id)
        if cause is None or target is None:
            raise EngineError("fatal recovery cause or target report does not exist")
        cause_payload_sha256 = hashlib.sha256(
            cause.payload_json.encode("utf-8")
        ).hexdigest()
        target_payload_sha256 = hashlib.sha256(
            target.payload_json.encode("utf-8")
        ).hexdigest()
        if (
            cause.task_id != task.task_id
            or cause.stage != request.stage
            or cause.content_hash != task.content_hash()
            or (
                cause.verdict != ("fatal" if cause.id == target.id else VERDICT_FAIL)
            )
            or cause.id > target.id
            or cause_payload_sha256 != request.cause_payload_sha256
            or target.task_id != task.task_id
            or target.stage != request.stage
            or target.content_hash != task.content_hash()
            or target.verdict != "fatal"
            or target_payload_sha256 != request.target_payload_sha256
        ):
            raise EngineError(
                "fatal recovery request does not name an exact current fatal"
            )
        fixed_code = _fixed_code_bindings(kind.fixed_modules)
        revalidator_code = recovery_code_identity(
            FATAL_RECOVERY_REVALIDATOR_MODULE
        )
        observation_bindings = _observation_bindings(
            engine,
            request.observation_paths,
        )
        observation_bytes = {
            item.path: engine._read_supersession_evidence(
                item.path,
                label="fatal recovery observation",
            )
            for item in observation_bindings
        }
        try:
            certification = kind.analyzer(task, cause, target, observation_bytes)
        except (TypeError, ValueError) as exc:
            raise EngineError(f"fatal recovery analyzer refused evidence: {exc}") from exc
        evidence = FatalRecoveryEvidence(
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            recovery_kind=kind.name,
            stage=request.stage,
            cause_report_id=cause.id,
            cause_payload_sha256=cause_payload_sha256,
            target_report_id=target.id,
            target_payload_sha256=target_payload_sha256,
            disposition=kind.disposition,
            failure_class=kind.failure_class,
            failure_code=kind.failure_code,
            blocked_on=kind.blocked_on,
            retry_guard=kind.retry_guard,
            recovery_prerequisite=kind.recovery_prerequisite,
            reason=kind.reason,
            fixed_code_fingerprint=recovery_code_fingerprint(fixed_code),
            fixed_code=fixed_code,
            revalidator=RecoveryCallableIdentity(
                module=FATAL_RECOVERY_REVALIDATOR_MODULE,
                qualname=FATAL_RECOVERY_REVALIDATOR_QUALNAME,
                module_sha256=revalidator_code.sha256,
            ),
            observations=observation_bindings,
            certification=certification,
        )
        # Exercise the same pure checks before publishing the evidence file.
        validate_recovery_evidence(
            evidence=evidence,
            task=task,
            cause=cause,
            target=target,
            observations=observation_bytes,
        )
        evidence_path, evidence_sha256 = _write_content_addressed(
            engine.workspace,
            evidence,
        )
        marker = _marker_from_evidence(
            evidence_path=evidence_path,
            evidence_sha256=evidence_sha256,
            evidence=evidence,
        )
        engine.validate_fatal_recovery(task, marker)
        return PreparedFatalRecovery(
            task_id=task.task_id,
            stage=evidence.stage,
            target_report_id=target.id,
            disposition=evidence.disposition,
            evidence_path=evidence_path,
            evidence_sha256=evidence_sha256,
        )
    finally:
        engine.close()


def apply_recovery(
    workspace: Path,
    evidence_path: str,
) -> PreparedFatalRecovery:
    """Append the evidence's exact FAIL/BLOCKED disposition, idempotently."""

    engine = Engine(workspace)
    try:
        payload = engine._read_supersession_evidence(  # package-internal safe reader
            evidence_path,
            label="fatal recovery evidence",
        )
        digest = hashlib.sha256(payload).hexdigest()
        try:
            evidence = FatalRecoveryEvidence.model_validate_json(payload)
        except ValueError as exc:
            raise EngineError(f"fatal recovery evidence is invalid: {exc}") from exc
        if payload != evidence.deterministic_bytes():
            raise EngineError("fatal recovery evidence bytes are not canonical")
        task = engine.load_task(evidence.task_id)
        marker = _marker_from_evidence(
            evidence_path=evidence_path,
            evidence_sha256=digest,
            evidence=evidence,
        )
        appended = engine.recover_fatal_report(task, marker)
        return PreparedFatalRecovery(
            task_id=task.task_id,
            stage=evidence.stage,
            target_report_id=evidence.target_report_id,
            disposition=evidence.disposition,
            evidence_path=evidence_path,
            evidence_sha256=digest,
            ledger_appended=appended,
        )
    finally:
        engine.close()


def _load_request(path: Path) -> FatalRecoveryRequest:
    try:
        return FatalRecoveryRequest.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise EngineError(f"cannot load fatal recovery request {path}: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare/apply one provider-free exact fatal recovery"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare",
        help="write content-addressed evidence; do not mutate the ledger",
    )
    prepare.add_argument("--workspace", type=Path, required=True)
    prepare.add_argument("--request", type=Path, required=True)
    apply = commands.add_parser(
        "apply",
        help="append the evidence's non-PASS ledger disposition",
    )
    apply.add_argument("--workspace", type=Path, required=True)
    apply.add_argument("--evidence", required=True, help="workspace-relative path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_recovery(args.workspace, _load_request(args.request))
        else:
            result = apply_recovery(args.workspace, args.evidence)
    except (EngineError, ValueError) as exc:
        parser.error(str(exc))
    print(readable_json(result.model_dump(mode="json")))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via module CLI
    raise SystemExit(main())


__all__ = [
    "EVIDENCE_DIR",
    "FatalRecoveryRequest",
    "PreparedFatalRecovery",
    "RECOVERY_KINDS",
    "RecoveryKind",
    "apply_recovery",
    "main",
    "prepare_recovery",
    "validate_recovery_evidence",
]

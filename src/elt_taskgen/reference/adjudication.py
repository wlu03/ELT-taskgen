"""Diagnose independent-build disagreements from recorded private evidence.

Diagnostics replay recorded SQL against frozen populations with the production
comparator. They cannot pass a gate or alter either result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from elt_taskgen.models import PopulationName, Row, TaskIR, canonical_json, task_from_json
from elt_taskgen.reference import independent
from elt_taskgen.reference.gold import MANIFEST_FILENAME, load_gold
from elt_taskgen.verification import upstream_eval


ANALYSIS_SCHEMA_VERSION = "independent-disagreement-analysis-v1"
ANALYSIS_STATUS_DIFFERENCES = "differences_confirmed"
ANALYSIS_STATUS_AGREED = "recorded_witness_agrees"
ADJUDICATION_STATUS_PENDING = "pending_adjudication"
DIAGNOSIS_SCHEMA_VERSION = "independent-disagreement-diagnosis-v1"
DECISION_SCHEMA_VERSION = "independent-disagreement-decision-v1"
DECISION_ACTION_NEW_BLIND_BUILD = "authorize_new_blind_independent_build"
DIAGNOSIS_CAUSES = (
    "reference",
    "witness",
    "specification",
    "data",
    "comparator",
)


class CellDifference(BaseModel):
    """One comparator-visible value disagreement in a paired output row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    column: str
    expected: Any = None
    actual: Any = None


class RowDifference(BaseModel):
    """One bounded row-level difference, aligned by the declared mart grain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["missing", "extra", "value_mismatch"]
    grain_key: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] | None = None
    actual: dict[str, Any] | None = None
    cells: tuple[CellDifference, ...] = ()


class MartDisagreement(BaseModel):
    """Comparator result plus a bounded explanation for one mart/population."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    population: PopulationName
    mart: str
    comparator_match: bool
    expected_row_count: int = Field(ge=0)
    actual_row_count: int = Field(ge=0)
    differences: tuple[RowDifference, ...] = ()
    omitted_difference_count: int = Field(default=0, ge=0)


class IndependentDisagreementAnalysis(BaseModel):
    """Content-bound private evidence; it records facts, never a verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["independent-disagreement-analysis-v1"] = (
        ANALYSIS_SCHEMA_VERSION
    )
    task_id: str
    task_content_hash: str
    independent_build_sha256: str
    gold_manifest_sha256: str
    witness_sql_sha256: dict[str, str]
    analysis_status: Literal[
        "differences_confirmed", "recorded_witness_agrees"
    ]
    adjudication_status: Literal["pending_adjudication"] = (
        ADJUDICATION_STATUS_PENDING
    )
    cause_candidates: tuple[str, ...] = (
        "reference",
        "witness",
        "specification",
        "data",
        "comparator",
    )
    stage1_mismatches: dict[str, dict[str, str]] = Field(default_factory=dict)
    marts: tuple[MartDisagreement, ...]

    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()


class IndependentDisagreementDiagnosis(BaseModel):
    """Content-bound diagnosis that records findings without admission authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["independent-disagreement-diagnosis-v1"] = (
        DIAGNOSIS_SCHEMA_VERSION
    )
    task_id: str
    task_content_hash: str
    analysis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    determined_cause: Literal[
        "reference", "witness", "specification", "data", "comparator"
    ]
    determination_basis: str = Field(min_length=1, max_length=4000)
    adjudication_status: Literal["pending_adjudication"] = (
        ADJUDICATION_STATUS_PENDING
    )
    gate_effect: Literal["none"] = "none"

    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()


class IndependentDisagreementDecision(BaseModel):
    """Content-bound human decision authorizing one new blind witness, not a pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["independent-disagreement-decision-v1"] = (
        DECISION_SCHEMA_VERSION
    )
    task_id: str
    task_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    independent_build_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gold_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    analysis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    diagnosis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    determined_cause: Literal["witness"] = "witness"
    action: Literal["authorize_new_blind_independent_build"] = (
        DECISION_ACTION_NEW_BLIND_BUILD
    )
    adjudicator: str = Field(min_length=1, max_length=200)
    decision_basis: str = Field(min_length=1, max_length=4000)
    gate_effect: Literal["authorize_fresh_build_only"] = (
        "authorize_fresh_build_only"
    )

    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _comparison_key(value: Any) -> tuple[str, str]:
    """Return a diagnostic grain key using comparator-compatible normalization."""

    if upstream_eval._is_null(value):  # noqa: SLF001 - same-package diagnostic
        return ("null", "")
    numeric = upstream_eval._numeric_value(value)  # noqa: SLF001
    if numeric is not None:
        return ("numeric", numeric.hex())
    text = upstream_eval.cell_text(value)
    return ("text", (text or "").strip().casefold())


def _grain_key(row: Row, key_columns: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    return tuple(_comparison_key(row.get(column)) for column in key_columns)


def _public_grain_key(row: Row, key_columns: tuple[str, ...]) -> dict[str, Any]:
    return {column: row.get(column) for column in key_columns}


def _row_differences(
    *,
    expected_rows: list[Row],
    actual_rows: list[Row],
    columns: tuple[str, ...],
    key_columns: tuple[str, ...],
    limit: int,
) -> tuple[tuple[RowDifference, ...], int]:
    """Pair rows by grain and return at most ``limit`` concrete differences."""

    expected_by_key: dict[tuple[tuple[str, str], ...], list[Row]] = defaultdict(list)
    actual_by_key: dict[tuple[tuple[str, str], ...], list[Row]] = defaultdict(list)
    for row in expected_rows:
        expected_by_key[_grain_key(row, key_columns)].append(row)
    for row in actual_rows:
        actual_by_key[_grain_key(row, key_columns)].append(row)

    all_differences: list[RowDifference] = []
    for key in sorted(set(expected_by_key) | set(actual_by_key)):
        expected = sorted(expected_by_key.get(key, ()), key=canonical_json)
        actual = sorted(actual_by_key.get(key, ()), key=canonical_json)
        paired = min(len(expected), len(actual))
        for index in range(paired):
            expected_row = expected[index]
            actual_row = actual[index]
            cells = tuple(
                CellDifference(
                    column=column,
                    expected=expected_row.get(column),
                    actual=actual_row.get(column),
                )
                for column in columns
                if not upstream_eval._vectors_match(  # noqa: SLF001
                    [expected_row.get(column)], [actual_row.get(column)]
                )
            )
            if cells:
                all_differences.append(
                    RowDifference(
                        kind="value_mismatch",
                        grain_key=_public_grain_key(expected_row, key_columns),
                        expected=dict(expected_row),
                        actual=dict(actual_row),
                        cells=cells,
                    )
                )
        for row in expected[paired:]:
            all_differences.append(
                RowDifference(
                    kind="missing",
                    grain_key=_public_grain_key(row, key_columns),
                    expected=dict(row),
                )
            )
        for row in actual[paired:]:
            all_differences.append(
                RowDifference(
                    kind="extra",
                    grain_key=_public_grain_key(row, key_columns),
                    actual=dict(row),
                )
            )

    kept = tuple(all_differences[:limit])
    return kept, max(0, len(all_differences) - len(kept))


def analyze_independent_disagreement(
    workspace: Path,
    task: TaskIR,
    *,
    max_differences_per_mart: int = 20,
) -> IndependentDisagreementAnalysis:
    """Replay recorded witness SQL locally and explain comparator disagreements."""

    if max_differences_per_mart < 1:
        raise ValueError("max_differences_per_mart must be positive")
    workspace = Path(workspace).resolve()
    task_root = workspace / "tasks" / task.task_id
    evidence_path = task_root / independent.INDEPENDENT_BUILD_EVIDENCE_REL
    evidence_bytes = evidence_path.read_bytes()
    raw = json.loads(evidence_bytes)
    result = independent.IndependentBuildResult.model_validate(raw)
    if result.task_id != task.task_id:
        raise ValueError("independent-build evidence names a different task")
    if result.task_content_hash != task.content_hash():
        raise ValueError("independent-build evidence is stale for this TaskIR")
    if result.status != independent.STATUS_NEEDS_ADJUDICATION:
        raise ValueError(
            "row-level disagreement analysis requires needs_adjudication evidence"
        )
    if not result.samples:
        raise ValueError("independent-build evidence contains no executed sample")
    sql_by_mart = dict(result.samples[-1].sql_by_mart)
    expected_marts = {mart.name for mart in task.marts}
    if set(sql_by_mart) != expected_marts or any(
        not isinstance(sql, str) or not sql.strip() for sql in sql_by_mart.values()
    ):
        raise ValueError("final independent-build sample has an invalid mart SQL roster")

    answer_key = task_root / "answer_key"
    manifest_bytes = (answer_key / MANIFEST_FILENAME).read_bytes()
    gold = load_gold(answer_key)
    if gold.task_id != task.task_id or gold.task_content_hash != task.content_hash():
        raise ValueError("gold bundle is not bound to the current TaskIR")

    stage1_mismatches: dict[str, dict[str, str]] = {}
    mart_results: list[MartDisagreement] = []
    all_match = True
    for population in sorted((p.name for p in task.populations), key=lambda p: p.value):
        stage1, actual_by_mart = independent._execute_population(  # noqa: SLF001
            task, sql_by_mart, population, workspace
        )
        stage1_ok, stage1_detail = upstream_eval.compare_stage1(
            gold.stage1.get(population.value, {}), stage1
        )
        if not stage1_ok:
            stage1_mismatches[population.value] = {
                table: detail
                for table, detail in sorted(stage1_detail.items())
                if not detail.startswith("ok (")
            }
            all_match = False

        for mart in task.marts:
            gold_csv = gold.stage2_csv.get(population.value, {}).get(mart.name)
            if gold_csv is None:
                raise ValueError(
                    f"gold is missing mart {mart.name!r} on {population.value!r}"
                )
            columns, expected_rows = upstream_eval.parse_canonical_csv(gold_csv)
            actual_rows = [
                {column: upstream_eval.cell_text(row.get(column)) for column in columns}
                for row in actual_by_mart.get(mart.name, ())
            ]
            matched = upstream_eval.compare_mart(
                gold_csv,
                list(actual_by_mart.get(mart.name, ())),
                mart,
            )
            differences, omitted = _row_differences(
                expected_rows=expected_rows,
                actual_rows=actual_rows,
                columns=columns,
                key_columns=mart.key_columns,
                limit=max_differences_per_mart,
            )
            if matched and differences:
                raise RuntimeError(
                    "diagnostic row alignment disagrees with the production comparator"
                )
            all_match = all_match and matched
            mart_results.append(
                MartDisagreement(
                    population=population,
                    mart=mart.name,
                    comparator_match=matched,
                    expected_row_count=len(expected_rows),
                    actual_row_count=len(actual_rows),
                    differences=differences,
                    omitted_difference_count=omitted,
                )
            )

    return IndependentDisagreementAnalysis(
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        independent_build_sha256=_sha256_bytes(evidence_bytes),
        gold_manifest_sha256=_sha256_bytes(manifest_bytes),
        witness_sql_sha256={
            mart: _sha256_bytes(sql.encode("utf-8"))
            for mart, sql in sorted(sql_by_mart.items())
        },
        analysis_status=(
            ANALYSIS_STATUS_AGREED if all_match else ANALYSIS_STATUS_DIFFERENCES
        ),
        stage1_mismatches=stage1_mismatches,
        marts=tuple(mart_results),
    )


def persist_analysis(
    workspace: Path, analysis: IndependentDisagreementAnalysis
) -> Path:
    """Create a content-addressed audit record without replacing history."""

    workspace = Path(workspace).resolve()
    document = canonical_json(analysis.model_dump(mode="json")).encode("utf-8")
    digest = _sha256_bytes(document)
    path = (
        workspace
        / "audit"
        / f"{analysis.task_id}.dual_build_analysis.{digest}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != document:
            raise ValueError("content-addressed adjudication analysis collision")
        return path
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(document)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def persist_diagnosis(
    workspace: Path,
    analysis_path: Path,
    *,
    determined_cause: Literal[
        "reference", "witness", "specification", "data", "comparator"
    ],
    determination_basis: str,
) -> Path:
    """Persist a gate-neutral diagnosis bound to an exact row-level analysis."""

    workspace = Path(workspace).resolve()
    candidate = Path(analysis_path)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    audit_root = (workspace / "audit").resolve()
    resolved = candidate.resolve()
    if (
        resolved.parent != audit_root
        or candidate.is_symlink()
        or ".dual_build_analysis." not in resolved.name
    ):
        raise ValueError("diagnosis analysis must be a direct private audit artifact")
    analysis_bytes = resolved.read_bytes()
    analysis_sha256 = _sha256_bytes(analysis_bytes)
    if not resolved.name.endswith(f".{analysis_sha256}.json"):
        raise ValueError("diagnosis analysis filename is not content-addressed")
    try:
        analysis = IndependentDisagreementAnalysis.model_validate_json(
            analysis_bytes
        )
    except ValueError as exc:
        raise ValueError("diagnosis analysis is malformed") from exc
    if analysis.analysis_status != ANALYSIS_STATUS_DIFFERENCES:
        raise ValueError("a diagnosis requires confirmed row-level differences")
    basis = str(determination_basis).strip()
    if not basis:
        raise ValueError("determination_basis must be non-empty")
    diagnosis = IndependentDisagreementDiagnosis(
        task_id=analysis.task_id,
        task_content_hash=analysis.task_content_hash,
        analysis_sha256=analysis_sha256,
        determined_cause=determined_cause,
        determination_basis=basis,
    )
    document = canonical_json(diagnosis.model_dump(mode="json")).encode("utf-8")
    digest = _sha256_bytes(document)
    path = audit_root / f"{analysis.task_id}.dual_build_diagnosis.{digest}.json"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != document:
            raise ValueError("content-addressed adjudication diagnosis collision")
        return path
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(document)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _read_content_addressed_audit_artifact(
    workspace: Path,
    path: Path,
    *,
    task_id: str,
    artifact_kind: str,
) -> tuple[bytes, str]:
    """Read one direct audit artifact and verify its content-addressed name."""

    workspace = Path(workspace).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    audit_root = (workspace / "audit").resolve()
    resolved = candidate.resolve()
    if resolved.parent != audit_root or candidate.is_symlink():
        raise ValueError(f"{artifact_kind} must be a direct private audit artifact")
    payload = resolved.read_bytes()
    digest = _sha256_bytes(payload)
    expected = f"{task_id}.{artifact_kind}.{digest}.json"
    if resolved.name != expected:
        raise ValueError(f"{artifact_kind} filename is not content-addressed")
    return payload, digest


def validate_fresh_build_decision_evidence(
    task: TaskIR,
    *,
    independent_build_bytes: bytes,
    gold_manifest_bytes: bytes,
    analysis_bytes: bytes,
    diagnosis_bytes: bytes,
    decision: IndependentDisagreementDecision,
) -> IndependentDisagreementDecision:
    """Validate byte-bound authority for one solver-isolated rebuild."""

    if decision.task_id != task.task_id or decision.task_content_hash != task.content_hash():
        raise ValueError("independent-build decision is stale for this TaskIR")

    if _sha256_bytes(independent_build_bytes) != decision.independent_build_sha256:
        raise ValueError("independent-build decision names another witness")
    try:
        build = independent.IndependentBuildResult.model_validate_json(
            independent_build_bytes
        )
    except ValueError as exc:
        raise ValueError("decision-bound independent build is malformed") from exc
    if (
        build.task_id != task.task_id
        or build.task_content_hash != task.content_hash()
        or build.status != independent.STATUS_NEEDS_ADJUDICATION
        or not build.samples
        or not build.samples[-1].sql_by_mart
    ):
        raise ValueError("decision-bound independent build is not an executable disagreement")

    if _sha256_bytes(gold_manifest_bytes) != decision.gold_manifest_sha256:
        raise ValueError("independent-build decision names another gold manifest")
    try:
        gold_manifest = json.loads(gold_manifest_bytes)
    except (TypeError, ValueError) as exc:
        raise ValueError("decision-bound gold manifest is malformed") from exc
    if (
        not isinstance(gold_manifest, dict)
        or gold_manifest.get("task_id") != task.task_id
        or gold_manifest.get("task_content_hash") != task.content_hash()
        or not isinstance(gold_manifest.get("files"), dict)
    ):
        raise ValueError("decision-bound gold manifest is stale or malformed")

    analysis_sha256 = _sha256_bytes(analysis_bytes)
    try:
        analysis = IndependentDisagreementAnalysis.model_validate_json(analysis_bytes)
    except ValueError as exc:
        raise ValueError("decision-bound row-level analysis is malformed") from exc
    final_sql = dict(build.samples[-1].sql_by_mart)
    final_sql_sha256 = {
        mart: _sha256_bytes(sql.encode("utf-8"))
        for mart, sql in sorted(final_sql.items())
    }
    if (
        analysis_sha256 != decision.analysis_sha256
        or analysis.task_id != task.task_id
        or analysis.task_content_hash != task.content_hash()
        or analysis.independent_build_sha256 != decision.independent_build_sha256
        or analysis.gold_manifest_sha256 != decision.gold_manifest_sha256
        or analysis.witness_sql_sha256 != final_sql_sha256
        or analysis.analysis_status != ANALYSIS_STATUS_DIFFERENCES
        or analysis.adjudication_status != ADJUDICATION_STATUS_PENDING
        or not any(not mart.comparator_match for mart in analysis.marts)
        or not any(mart.differences for mart in analysis.marts)
    ):
        raise ValueError("decision-bound row-level analysis is stale or incomplete")

    diagnosis_sha256 = _sha256_bytes(diagnosis_bytes)
    try:
        diagnosis = IndependentDisagreementDiagnosis.model_validate_json(
            diagnosis_bytes
        )
    except ValueError as exc:
        raise ValueError("decision-bound engineering diagnosis is malformed") from exc
    if (
        diagnosis_sha256 != decision.diagnosis_sha256
        or diagnosis.task_id != task.task_id
        or diagnosis.task_content_hash != task.content_hash()
        or diagnosis.analysis_sha256 != decision.analysis_sha256
        or diagnosis.determined_cause != "witness"
        or diagnosis.adjudication_status != ADJUDICATION_STATUS_PENDING
        or diagnosis.gate_effect != "none"
        or decision.determined_cause != diagnosis.determined_cause
        or decision.action != DECISION_ACTION_NEW_BLIND_BUILD
        or decision.gate_effect != "authorize_fresh_build_only"
    ):
        raise ValueError("decision is not bound to a pending witness-error diagnosis")
    return decision


def _validate_decision_bindings(
    workspace: Path,
    task: TaskIR,
    decision: IndependentDisagreementDecision,
) -> None:
    """Fail closed unless a decision still names every current exact byte."""

    workspace = Path(workspace).resolve()
    task_root = workspace / "tasks" / task.task_id
    build_bytes = (
        task_root / independent.INDEPENDENT_BUILD_EVIDENCE_REL
    ).read_bytes()
    manifest_bytes = (task_root / "answer_key" / MANIFEST_FILENAME).read_bytes()

    analysis_path = (
        workspace
        / "audit"
        / f"{task.task_id}.dual_build_analysis.{decision.analysis_sha256}.json"
    )
    analysis_bytes, _ = _read_content_addressed_audit_artifact(
        workspace,
        analysis_path,
        task_id=task.task_id,
        artifact_kind="dual_build_analysis",
    )
    diagnosis_path = (
        workspace
        / "audit"
        / f"{task.task_id}.dual_build_diagnosis.{decision.diagnosis_sha256}.json"
    )
    diagnosis_bytes, _ = _read_content_addressed_audit_artifact(
        workspace,
        diagnosis_path,
        task_id=task.task_id,
        artifact_kind="dual_build_diagnosis",
    )
    validate_fresh_build_decision_evidence(
        task,
        independent_build_bytes=build_bytes,
        gold_manifest_bytes=manifest_bytes,
        analysis_bytes=analysis_bytes,
        diagnosis_bytes=diagnosis_bytes,
        decision=decision,
    )


def persist_witness_error_decision(
    workspace: Path,
    task: TaskIR,
    analysis_path: Path,
    diagnosis_path: Path,
    *,
    adjudicator: str,
    decision_basis: str,
) -> Path:
    """Authorize one new blind attempt without altering evidence or gate state."""

    workspace = Path(workspace).resolve()
    persisted_task = task_from_json(
        (workspace / "tasks" / task.task_id / "task_ir.json").read_text(
            encoding="utf-8"
        )
    )
    if persisted_task.content_hash() != task.content_hash():
        raise ValueError("adjudication TaskIR differs from the persisted task")

    analysis_bytes, analysis_sha256 = _read_content_addressed_audit_artifact(
        workspace,
        analysis_path,
        task_id=task.task_id,
        artifact_kind="dual_build_analysis",
    )
    diagnosis_bytes, diagnosis_sha256 = _read_content_addressed_audit_artifact(
        workspace,
        diagnosis_path,
        task_id=task.task_id,
        artifact_kind="dual_build_diagnosis",
    )
    try:
        analysis = IndependentDisagreementAnalysis.model_validate_json(analysis_bytes)
        diagnosis = IndependentDisagreementDiagnosis.model_validate_json(
            diagnosis_bytes
        )
    except ValueError as exc:
        raise ValueError("adjudication analysis or diagnosis is malformed") from exc
    if (
        diagnosis.analysis_sha256 != analysis_sha256
        or diagnosis.determined_cause != "witness"
        or diagnosis.adjudication_status != ADJUDICATION_STATUS_PENDING
        or diagnosis.gate_effect != "none"
    ):
        raise ValueError("only a bound pending witness-error diagnosis can authorize a retry")

    decision = IndependentDisagreementDecision(
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        independent_build_sha256=analysis.independent_build_sha256,
        gold_manifest_sha256=analysis.gold_manifest_sha256,
        analysis_sha256=analysis_sha256,
        diagnosis_sha256=diagnosis_sha256,
        adjudicator=str(adjudicator).strip(),
        decision_basis=str(decision_basis).strip(),
    )
    _validate_decision_bindings(workspace, task, decision)

    document = canonical_json(decision.model_dump(mode="json")).encode("utf-8")
    digest = _sha256_bytes(document)
    path = (
        workspace
        / "audit"
        / f"{task.task_id}.dual_build_decision.{digest}.json"
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != document:
            raise ValueError("content-addressed adjudication decision collision")
        return path
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(document)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def load_fresh_build_decision(
    workspace: Path,
    task: TaskIR,
) -> IndependentDisagreementDecision | None:
    """Load the current valid decision authorizing one blind witness."""

    workspace = Path(workspace).resolve()
    build_path = (
        workspace
        / "tasks"
        / task.task_id
        / independent.INDEPENDENT_BUILD_EVIDENCE_REL
    )
    if not build_path.is_file():
        return None
    build_sha256 = _sha256_bytes(build_path.read_bytes())
    current: list[IndependentDisagreementDecision] = []
    for path in sorted(
        (workspace / "audit").glob(f"{task.task_id}.dual_build_decision.*.json")
    ):
        payload, digest = _read_content_addressed_audit_artifact(
            workspace,
            path,
            task_id=task.task_id,
            artifact_kind="dual_build_decision",
        )
        try:
            decision = IndependentDisagreementDecision.model_validate_json(payload)
        except ValueError as exc:
            raise ValueError("independent-build adjudication decision is malformed") from exc
        if decision.digest() != digest:
            raise ValueError("independent-build adjudication decision is non-canonical")
        if (
            decision.task_content_hash == task.content_hash()
            and decision.independent_build_sha256 == build_sha256
        ):
            _validate_decision_bindings(workspace, task, decision)
            current.append(decision)
    if len(current) > 1:
        raise ValueError("multiple current independent-build decisions conflict")
    return current[0] if current else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay a recorded independent-build disagreement and write a "
            "private, content-addressed row-level audit analysis."
        )
    )
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--max-differences-per-mart", type=int, default=20)
    parser.add_argument("--determined-cause", choices=DIAGNOSIS_CAUSES)
    parser.add_argument("--determination-basis")
    parser.add_argument(
        "--adjudicate-witness-error",
        action="store_true",
        help=(
            "write a bound decision authorizing one fresh blind independent "
            "build; requires --determined-cause witness and --adjudicator"
        ),
    )
    parser.add_argument("--adjudicator")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if bool(args.determined_cause) != bool(args.determination_basis):
        parser.error(
            "--determined-cause and --determination-basis must be supplied together"
        )
    if args.adjudicate_witness_error and (
        args.determined_cause != "witness" or not args.adjudicator
    ):
        parser.error(
            "--adjudicate-witness-error requires --determined-cause witness, "
            "--determination-basis, and --adjudicator"
        )
    if args.adjudicator and not args.adjudicate_witness_error:
        parser.error("--adjudicator requires --adjudicate-witness-error")
    workspace = args.workspace.resolve()
    task_path = workspace / "tasks" / args.task_id / "task_ir.json"
    task = task_from_json(task_path.read_text(encoding="utf-8"))
    analysis = analyze_independent_disagreement(
        workspace,
        task,
        max_differences_per_mart=args.max_differences_per_mart,
    )
    path = persist_analysis(workspace, analysis)
    diagnosis_path = None
    decision_path = None
    if args.determined_cause:
        diagnosis_path = persist_diagnosis(
            workspace,
            path,
            determined_cause=args.determined_cause,
            determination_basis=args.determination_basis,
        )
    if args.adjudicate_witness_error:
        assert diagnosis_path is not None
        decision_path = persist_witness_error_decision(
            workspace,
            task,
            path,
            diagnosis_path,
            adjudicator=args.adjudicator,
            decision_basis=args.determination_basis,
        )
    mismatching_marts = sum(not item.comparator_match for item in analysis.marts)
    difference_rows = sum(len(item.differences) for item in analysis.marts)
    print(
        canonical_json(
            {
                "adjudication_status": analysis.adjudication_status,
                "analysis_sha256": analysis.digest(),
                "analysis_status": analysis.analysis_status,
                "determined_cause": args.determined_cause,
                "diagnosis": (
                    diagnosis_path.relative_to(workspace).as_posix()
                    if diagnosis_path is not None
                    else None
                ),
                "decision": (
                    decision_path.relative_to(workspace).as_posix()
                    if decision_path is not None
                    else None
                ),
                "decision_action": (
                    DECISION_ACTION_NEW_BLIND_BUILD
                    if decision_path is not None
                    else None
                ),
                "mismatching_population_marts": mismatching_marts,
                "recorded_row_differences": difference_rows,
                "report": path.relative_to(workspace).as_posix(),
                "task_id": analysis.task_id,
            }
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

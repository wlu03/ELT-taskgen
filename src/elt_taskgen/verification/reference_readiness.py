"""Run deterministic fail-closed readiness checks for reference-stage examples.

The reduced roster uses only evidence available after population and DuckDB gold
creation. It checks relationships and source dependence but is not release
certification.
"""

from __future__ import annotations

from pathlib import Path

from elt_taskgen.generation.lineage import effective_lineage
from elt_taskgen.models import (
    AcceptanceReport,
    GateResult,
    MartOpKind,
    PopulationName,
    TaskIR,
    canonical_json,
    readable_json,
    sha256_hex,
)
from elt_taskgen.reference.gold import GoldBundle
from elt_taskgen.verification import gates, perturbation


REFERENCE_READINESS_EVIDENCE_REL = "reports/reference_readiness.json"
REFERENCE_READINESS_ROSTER: tuple[str, ...] = (
    "determinism",
    "data-sensitivity",
    "info-content",
    "populations-load",
    "referential-integrity",
    "declared-scale-reconciliation",
    "mart-key-unique",
    "transform-surface",
    "effective-lineage",
)
REFERENCE_READINESS_ROSTER_DIGEST = sha256_hex(
    canonical_json(REFERENCE_READINESS_ROSTER)
)

# Challenging tasks require complete mart column-kind declarations and the
# measured plan-library floor. Reference examples stop before provider review.
CHALLENGING_REFERENCE_READINESS_ROSTER: tuple[str, ...] = (
    *REFERENCE_READINESS_ROSTER,
    "challenging-mart-contract",
)
CHALLENGING_REFERENCE_READINESS_ROSTER_DIGEST = sha256_hex(
    canonical_json(CHALLENGING_REFERENCE_READINESS_ROSTER)
)


def ensure_reference_readiness_evidence(
    task: TaskIR, workspace: Path, gold: GoldBundle
) -> tuple[Path, ...]:
    """Create deterministic local evidence required by the readiness roster."""

    produced: list[Path] = []
    if gates.needs_perturbation_probe(task, workspace):
        produced.append(
            perturbation.record_perturbation_probe(task, workspace, gold)
        )
    return tuple(produced)


def _effective_lineage_gate(task: TaskIR, gold: GoldBundle) -> GateResult:
    """Reject ungraded dead sources while allowing machine-visible EL-only inputs.

    An EL-only source requires a declared `SOURCE` operation and task-bound counts for
    every population, including a positive graded count. Transform SQL remains
    authoritative for transform lineage.
    """

    try:
        lineage = effective_lineage(task)
    except (ValueError, RuntimeError) as exc:
        return GateResult(
            gate="effective-lineage",
            passed=False,
            details=f"could not establish executable source lineage: {exc}",
        )
    declared_tables = {table.name for table in task.tables}
    source_declared = {
        table
        for mart in task.marts
        for op in mart.plan.ops
        if op.kind is MartOpKind.SOURCE
        for table in op.tables
        if table in declared_tables
    }

    gold_is_bound = getattr(gold, "task_id", None) == task.task_id
    stage1 = getattr(gold, "stage1", None)
    positively_graded: set[str] = set()
    if gold_is_bound and isinstance(stage1, dict):
        for table in sorted(declared_tables):
            counts: dict[PopulationName, int] = {}
            for population in PopulationName:
                vector = stage1.get(population.value)
                count = vector.get(table) if isinstance(vector, dict) else None
                if (
                    not isinstance(count, int)
                    or isinstance(count, bool)
                    or count < 0
                ):
                    break
                counts[population] = count
            else:
                if any(
                    counts[population] > 0
                    for population in gates.GRADED_POPULATIONS
                ):
                    positively_graded.add(table)

    unused = set(lineage.unused_tables)
    extract_load_only = unused & source_declared & positively_graded
    undeclared_dead = unused - source_declared
    ungraded_dead = (unused & source_declared) - positively_graded
    unpoliced = undeclared_dead | ungraded_dead

    evidence = {
        mart: ",".join(tables) for mart, tables in sorted(lineage.by_mart.items())
    }
    evidence["used_tables"] = str(len(lineage.used_tables))
    evidence["unused_tables"] = ",".join(lineage.unused_tables)
    evidence["stage1_gold_task_bound"] = str(gold_is_bound).lower()
    evidence["source_declared_tables"] = ",".join(sorted(source_declared))
    evidence["positively_graded_stage1_tables"] = ",".join(
        sorted(positively_graded)
    )
    evidence["extract_load_only_tables"] = ",".join(sorted(extract_load_only))
    evidence["unpoliced_tables"] = ",".join(sorted(unpoliced))
    if unpoliced:
        reasons: list[str] = []
        if undeclared_dead:
            reasons.append(
                "not declared by any SOURCE operation: "
                + ", ".join(sorted(undeclared_dead))
            )
        if ungraded_dead:
            reasons.append(
                "not present with valid counts in every frozen stage-1 vector "
                "and positive in at least one graded population: "
                + ", ".join(sorted(ungraded_dead))
            )
        return GateResult(
            gate="effective-lineage",
            passed=False,
            details=(
                "source tables absent from every compiled mart must either be "
                "explicit, positively graded extract/load-only inputs or be "
                "pruned before population generation; " + "; ".join(reasons)
            ),
            evidence=evidence,
        )
    if extract_load_only:
        return GateResult(
            gate="effective-lineage",
            passed=True,
            details=(
                f"{len(lineage.used_tables)} source table(s) affect compiled "
                f"marts; {len(extract_load_only)} additional SOURCE-declared "
                "table(s) are positively graded by frozen stage-1 vectors and "
                "are extract/load-only"
            ),
            evidence=evidence,
        )
    return GateResult(
        gate="effective-lineage",
        passed=True,
        details=(
            f"all {len(lineage.used_tables)} source table(s) affect at least one "
            "compiled mart"
        ),
        evidence=evidence,
    )


def _challenging_mart_contract_gate(task: TaskIR) -> GateResult:
    """Fail challenging tasks whose marts are shallow or lack declared types.

    The profile-specific gate preserves readability of older TaskIRs while requiring
    newly generated challenging tasks to meet the stronger contract.
    """

    from elt_taskgen.generation.mart_plan import (
        MIN_MART_COLUMNS,
        column_kind_problems,
        unclassified_columns,
    )

    evidence: dict[str, str] = {}
    problems: list[str] = []
    for mart in task.marts:
        unclassified = unclassified_columns(mart)
        kind_faults = column_kind_problems(mart)
        evidence[f"{mart.name}:columns"] = str(len(mart.columns))
        evidence[f"{mart.name}:unclassified"] = (
            ",".join(unclassified) if unclassified else "0"
        )
        if unclassified:
            problems.append(
                f"mart {mart.name!r} has unclassified target columns "
                f"{unclassified}"
            )
        if kind_faults:
            problems.extend(kind_faults)
        if len(mart.columns) < MIN_MART_COLUMNS:
            problems.append(
                f"mart {mart.name!r} has {len(mart.columns)} target columns, "
                f"below the challenging minimum of {MIN_MART_COLUMNS}"
            )
    if problems:
        return GateResult(
            gate="challenging-mart-contract",
            passed=False,
            details="; ".join(problems),
            evidence=evidence,
        )
    return GateResult(
        gate="challenging-mart-contract",
        passed=True,
        details=(
            f"all {len(task.marts)} mart(s) declare a certified kind for every "
            f"target column and meet the {MIN_MART_COLUMNS}-column minimum"
        ),
        evidence=evidence,
    )


def run_reference_readiness(
    task: TaskIR,
    workspace: Path,
    gold: GoldBundle,
    *,
    record: bool = True,
    challenging: bool = False,
) -> AcceptanceReport:
    """Run and optionally record the reference-readiness roster.

    `challenging=True` adds the strict mart contract. The check does not run or waive
    provider-backed independent reconstruction required for later acceptance.
    """

    workspace = Path(workspace)
    measured: tuple[GateResult, ...] = (
        gates._guarded(  # noqa: SLF001 - one package-owned gate composition
            "determinism", lambda: gates._gate_determinism(task, workspace, gold)
        ),
        gates._guarded(
            "data-sensitivity",
            lambda: gates._gate_data_sensitivity(task, workspace, gold),
        ),
        gates._guarded(
            "info-content", lambda: gates._gate_t_info_content(task, gold)
        ),
        gates._guarded(
            "populations-load",
            lambda: gates._gate_populations_load(task, workspace, gold),
        ),
        gates._guarded(
            "referential-integrity",
            lambda: gates._gate_referential_integrity(task, workspace),
        ),
        gates._guarded(
            "declared-scale-reconciliation",
            lambda: gates._gate_declared_scale_reconciliation(task, gold),
        ),
        gates._guarded(
            "mart-key-unique", lambda: gates._gate_mart_key_unique(task, gold)
        ),
        gates._guarded(
            "transform-surface", lambda: gates._gate_transform_surface(task)
        ),
        gates._guarded(
            "effective-lineage", lambda: _effective_lineage_gate(task, gold)
        ),
    )
    roster = REFERENCE_READINESS_ROSTER
    roster_digest = REFERENCE_READINESS_ROSTER_DIGEST
    if challenging:
        measured += (
            gates._guarded(
                "challenging-mart-contract",
                lambda: _challenging_mart_contract_gate(task),
            ),
        )
        roster = CHALLENGING_REFERENCE_READINESS_ROSTER
        roster_digest = CHALLENGING_REFERENCE_READINESS_ROSTER_DIGEST
    report = AcceptanceReport.from_gates(
        task_id=task.task_id,
        revision=task.current_revision,
        task_content_hash=task.content_hash(),
        gates=measured,
        scorer_version=gates.SCORER_VERSION,
        roster_digest=roster_digest,
        roster=roster,
    )
    if record:
        path = workspace / "tasks" / task.task_id / REFERENCE_READINESS_EVIDENCE_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            readable_json(report.model_dump(mode="json")) + "\n", encoding="utf-8"
        )
    return report


__all__ = [
    "CHALLENGING_REFERENCE_READINESS_ROSTER",
    "CHALLENGING_REFERENCE_READINESS_ROSTER_DIGEST",
    "REFERENCE_READINESS_EVIDENCE_REL",
    "REFERENCE_READINESS_ROSTER",
    "REFERENCE_READINESS_ROSTER_DIGEST",
    "ensure_reference_readiness_evidence",
    "_challenging_mart_contract_gate",
    "run_reference_readiness",
]

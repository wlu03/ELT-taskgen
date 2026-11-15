"""Targeted, executable mutations of the two-fact reference plan."""

from __future__ import annotations

from elt_taskgen.generation.mart_plan import BranchedFactCandidate, BranchedFactShape
from elt_taskgen.models import AttackCase, AttackKind, MartOpKind, MartSpec, PopulationName, TaskIR, canonical_json


def branched_attack_cases(shape: BranchedFactShape) -> tuple[AttackCase, ...]:
    from elt_taskgen.verification.attacks import STRUCTURED_DIRECTIVE_PREFIX

    cases = []

    def add(variant: str, kind: AttackKind, description: str, **parameters: int) -> None:
        suffix = "".join(f"__{key}_{value}" for key, value in parameters.items())
        cases.append(AttackCase(
            name=f"{shape.mart}__{variant}{suffix}", kind=kind,
            description=description,
            mutation=STRUCTURED_DIRECTIVE_PREFIX + canonical_json({
                "operation": "branched_fact_rollup", "mart": shape.mart,
                "variant": variant, **parameters,
            }),
            expected_pass={PopulationName.COUNTERFACTUAL: False},
        ))

    add("join_before_aggregate", AttackKind.WRONG_AGG_STAGE,
        "Join both raw facts before aggregation. The branch_cross_product witness multiplies counts and totals.")
    for index, branch in enumerate(shape.branches):
        if branch.measure:
            add("sum_distinct", AttackKind.CUSTOM,
                f"Count equal {branch.table}.{branch.measure} values only once. The branch_cross_product witness "
                "has separate source rows carrying the same non-zero amount.", branch=index)
        if len(shape.parent_keys) > 1:
            for component, key in enumerate(shape.parent_keys):
                add("missing_composite_component", AttackKind.WRONG_GRAIN,
                    f"Omit {key} only when joining the {branch.table} summary. The composite_collision witness "
                    "contains populated keys equal on every remaining component.",
                    branch=index, component=component)
    return tuple(cases)


def bound_branched_candidate(task: TaskIR, mart: MartSpec) -> BranchedFactCandidate:
    """Bind a generated mart to its schema evidence for mutation and reporting."""
    from elt_taskgen.adapters.evidence import branched_fact_candidates
    from elt_taskgen.generation.shapes.branched_fact_rollup import _plan

    body = tuple(op for op in mart.plan.ops if op.kind is not MartOpKind.SOURCE)
    evidence = next((candidate for candidate in branched_fact_candidates(task.tables, task.relationships)
                     if tuple(op for op in _plan(candidate, mart=mart.name).ops if op.kind is not MartOpKind.SOURCE) == body), None)
    if evidence is None:
        raise ValueError("branched mutation cannot bind the reference to declared full-key relationships")
    return evidence


def materialize_branched_mutation(task: TaskIR, payload: dict[str, object]) -> dict[str, str]:
    """Rebuild only the named mart with one explicit change, using the normal compiler."""
    from elt_taskgen.generation.shapes.branched_fact_rollup import _plan
    from elt_taskgen.reference.solution import build_reference, compile_plan_sql

    variant = payload.get("variant")
    required = {"operation", "mart", "variant"}
    if variant in ("sum_distinct", "missing_composite_component"):
        required.add("branch")
    if variant == "missing_composite_component":
        required.add("component")
    if (variant not in ("join_before_aggregate", "sum_distinct", "missing_composite_component")
            or set(payload) != required or payload.get("operation") != "branched_fact_rollup"):
        raise ValueError("invalid branched mutation directive")
    for parameter in ("branch", "component"):
        if parameter in payload and type(payload[parameter]) is not int:
            raise ValueError(f"branched mutation {parameter} must be an integer")
    mart = task.mart(str(payload["mart"]))
    if mart.plan.template_id != "branched_fact_rollup":
        raise ValueError("branched mutation targets a different template")
    evidence = bound_branched_candidate(task, mart)
    mutated = _plan(
        evidence, mart=mart.name, variant=str(variant),
        branch_index=payload.get("branch", -1), component=payload.get("component", -1),
    )
    # Adapters can prepend extraction-only source declarations. Preserve them.
    mutated = mutated.model_copy(update={"ops": (
        tuple(op for op in mart.plan.ops if op.kind is MartOpKind.SOURCE)
        + tuple(op for op in mutated.ops if op.kind is not MartOpKind.SOURCE)
    )})
    reference = dict(build_reference(task).sql_by_mart)
    sql = compile_plan_sql(task, mart.model_copy(update={"plan": mutated}))
    if sql == reference[mart.name]:
        raise ValueError("branched mutation changed no SQL")
    return {**reference, mart.name: sql}

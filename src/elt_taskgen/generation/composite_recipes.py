"""Admission and mutation execution for source-bound composite reports."""

import duckdb

from elt_taskgen.generation.composite_evidence import (
    COMPOSITE_RECIPES, composite_candidates, composite_witness_rows,
)
from elt_taskgen.generation.composite_plan import composite_plan, composite_variants, read_binding
from elt_taskgen.generation.recipe_admission import RecipeFamily, SemanticDifficultyError
from elt_taskgen.generation.recipe_evidence import RecipeEvidence
from elt_taskgen.generation.scenario_evidence import ScenarioEvidence
from elt_taskgen.models import MartSpec, TaskIR


def bound_composite(task: TaskIR, mart: MartSpec) -> RecipeEvidence | ScenarioEvidence:
    """Reconstruct the complete report against the current source schema."""
    recipe = mart.plan.template_id
    try:
        evidence, sources = read_binding(mart.plan.notes, recipe)
        if sources != tuple(sorted(table.name for table in task.tables)):
            raise ValueError("Source tables have changed")
        if evidence not in composite_candidates(task, recipe):
            raise ValueError("Source no longer supports the composite binding")
        if composite_plan(task, evidence, mart.name, recipe) != mart:
            raise ValueError("Composite mart no longer matches its source binding")
    except (TypeError, ValueError, KeyError) as exc:
        raise SemanticDifficultyError(f"Invalid composite recipe binding: {exc}") from exc
    return evidence


_FAMILY = RecipeFamily(
    name="composite",
    recipes=COMPOSITE_RECIPES,
    candidates=composite_candidates,
    plan=composite_plan,
    witness_rows=composite_witness_rows,
    variants=composite_variants,
    bind=bound_composite,
    witness_description="counterexamples for the final report",
    retryable_errors=(SemanticDifficultyError, duckdb.Error),
)

composite_cases = _FAMILY.cases
materialize_composite_mutation = _FAMILY.materialize_mutation


def apply_composite_difficulty(task: TaskIR, *, recipe: str) -> TaskIR:
    """Admit the first binding whose required mutations change the final report."""
    return _FAMILY.apply(task, (recipe,))


def apply_bound_composite(
    task: TaskIR, recipe: str, evidence: RecipeEvidence | ScenarioEvidence,
) -> TaskIR:
    """Admit or revalidate the exact binding selected in a source proposal."""
    return _FAMILY.apply(task, (recipe,), required_evidence=evidence)

"""Schema binding and recipe hooks for the scenario family."""
import duckdb
from elt_taskgen.generation.scenario_evidence import SCENARIO_RECIPES, scenario_candidates
from elt_taskgen.generation.scenario_plan import read_binding, scenario_plan, scenario_variants
from elt_taskgen.generation.scenario_witness import scenario_witness_rows
from elt_taskgen.generation.recipe_admission import RecipeFamily
from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError


def bound_scenario(task, mart):
    try:
        e, sources=read_binding(mart.plan.notes)
        if sources != tuple(sorted(t.name for t in task.tables)) or e not in scenario_candidates(task,mart.plan.template_id):
            raise ValueError("Source no longer supports scenario evidence")
        if scenario_plan(task,e,mart.name,mart.plan.template_id) != mart:
            raise ValueError("Scenario no longer matches its public contract")
        return e
    except (TypeError,ValueError,KeyError) as exc:
        raise SemanticDifficultyError(f"Invalid scenario binding: {exc}") from exc


_FAMILY = RecipeFamily(
    name="scenario", recipes=SCENARIO_RECIPES, candidates=scenario_candidates,
    plan=scenario_plan, witness_rows=scenario_witness_rows,
    variants=scenario_variants, bind=bound_scenario,
    witness_description="executable counterexamples",
    retryable_errors=(SemanticDifficultyError, duckdb.Error),
)


def scenario_cases(e, mart, recipe):
    return _FAMILY.cases(e, mart, recipe)


materialize_scenario_mutation = _FAMILY.materialize_mutation


def certify_scenario(task, mart, e, rows):
    return _FAMILY.certify(task, mart, e, rows)


def apply_scenario_difficulty(task, *, recipe):
    return _FAMILY.apply(task, (recipe,))

"""Schema binding and recipe hooks for the advanced family."""
from elt_taskgen.generation.advanced_certification import read_binding
from elt_taskgen.generation.advanced_evidence import ADVANCED_RECIPES, advanced_candidates
from elt_taskgen.generation.advanced_plan import advanced_plan, advanced_variants
from elt_taskgen.generation.advanced_witness import advanced_witness_rows
from elt_taskgen.generation.recipe_admission import RecipeFamily
from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError


def bound_advanced(task, mart):
    try:
        evidence, sources = read_binding(mart.plan.notes)
        if sources != tuple(sorted(t.name for t in task.tables)) or evidence not in advanced_candidates(task, mart.plan.template_id):
            raise ValueError("Evidence no longer supported by the source")
        if advanced_plan(task, evidence, mart.name, mart.plan.template_id) != mart:
            raise ValueError("Mart no longer matches its recipe")
    except (TypeError, ValueError, KeyError) as exc:
        raise SemanticDifficultyError(f"Invalid advanced recipe binding: {exc}") from exc
    return evidence


_FAMILY = RecipeFamily(
    name="advanced", recipes=ADVANCED_RECIPES, candidates=advanced_candidates,
    plan=advanced_plan, witness_rows=advanced_witness_rows,
    variants=advanced_variants, bind=bound_advanced,
)


def advanced_cases(e, mart, recipe):
    return _FAMILY.cases(e, mart, recipe)


materialize_advanced_mutation = _FAMILY.materialize_mutation


def certify_advanced(task, mart, e, rows):
    return _FAMILY.certify(task, mart, e, rows)


def apply_advanced_difficulty(task, *, recipe):
    return _FAMILY.apply(task, (recipe,))

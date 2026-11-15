"""Schema binding and recipe hooks for the dependent family."""
from dataclasses import fields
import json
from elt_taskgen.generation.recipe_evidence import DEPENDENT_RECIPES, RecipeEvidence, recipe_candidates
from elt_taskgen.generation.recipe_plan import recipe_plan, recipe_variants
from elt_taskgen.generation.recipe_witness import recipe_witness_rows
from elt_taskgen.generation.recipe_admission import RecipeFamily
from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError
from elt_taskgen.models import TaskIR, sha256_hex


def bound_recipe(task, mart):
    try:
        raw = json.loads(mart.plan.notes)
        if set(raw) != {f.name for f in fields(RecipeEvidence)}:
            raise ValueError("Incorrect evidence fields")
        raw["keys"], raw["links"] = tuple(raw["keys"]), tuple(raw["links"])
        evidence = RecipeEvidence(**raw)
    except (TypeError, ValueError, KeyError) as exc:
        raise SemanticDifficultyError("Invalid dependent recipe evidence") from exc
    if (evidence not in recipe_candidates(task, mart.plan.template_id)
            or recipe_plan(task, evidence, mart.name, mart.plan.template_id) != mart):
        raise SemanticDifficultyError("Dependent mart no longer matches its schema-bound recipe")
    return evidence


_FAMILY = RecipeFamily(
    name="dependent", recipes=DEPENDENT_RECIPES, candidates=recipe_candidates,
    plan=recipe_plan, witness_rows=recipe_witness_rows,
    variants=recipe_variants, bind=bound_recipe,
)


def recipe_cases(e, mart, recipe):
    return _FAMILY.cases(e, mart, recipe)


materialize_recipe_mutation = _FAMILY.materialize_mutation


def certify_recipe(task, mart, evidence, rows):
    return _FAMILY.certify(task, mart, evidence, rows)


def apply_dependent_difficulty(task: TaskIR, *, recipe: str | None = None) -> TaskIR:
    if recipe is not None and recipe not in DEPENDENT_RECIPES:
        raise ValueError(f"Unknown dependent recipe {recipe!r}")
    # Preserve the direct API's deterministic rotation when no recipe is selected.
    start = int(sha256_hex(task.family_id)[:8], 16) % len(DEPENDENT_RECIPES)
    recipes = (recipe,) if recipe else DEPENDENT_RECIPES[start:] + DEPENDENT_RECIPES[:start]
    return _FAMILY.apply(task, recipes)

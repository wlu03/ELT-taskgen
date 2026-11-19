"""Automatic capability-aware assignment, reserving rare recipes across a batch.

Schema capabilities only shortlist candidates. An assignment is usable only
after its recipe's normal executable admission succeeds. Matching can move an
already assigned recipe to another project to avoid wasting a rare capability.
"""
from collections import defaultdict

from elt_taskgen.generation.recipe_registry import RECIPE_REGISTRY, RecipeSelection
from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError, semantic_candidates
from elt_taskgen.models import canonical_json, sha256_hex


def capabilities(task, selection):
    from elt_taskgen.generation.composite_evidence import COMPOSITE_RECIPES, composite_candidates
    from elt_taskgen.generation.graph_evidence import GRAPH_RECIPES, graph_candidates
    from elt_taskgen.generation.advanced_evidence import ADVANCED_RECIPES, advanced_candidates
    from elt_taskgen.generation.recipe_evidence import DEPENDENT_RECIPES, recipe_candidates
    from elt_taskgen.generation.scenario_evidence import SCENARIO_RECIPES, scenario_candidates
    existing = {m.plan.template_id for m in task.marts if m.plan.template_id in RECIPE_REGISTRY}
    if existing:
        return existing & set(selection.enabled_recipes)
    basic = None
    result = set()
    for recipe in selection.enabled_recipes:
        if recipe in COMPOSITE_RECIPES:
            supported = bool(composite_candidates(task, recipe))
        elif recipe in GRAPH_RECIPES:
            supported = bool(graph_candidates(task, recipe))
        elif recipe in SCENARIO_RECIPES:
            supported = bool(scenario_candidates(task, recipe))
        elif recipe in ADVANCED_RECIPES:
            supported = bool(advanced_candidates(task, recipe))
        elif recipe in DEPENDENT_RECIPES:
            supported = bool(recipe_candidates(task, recipe))
        else:
            if basic is None:
                basic = bool(semantic_candidates(task))
            supported = basic
        if supported:
            result.add(recipe)
    return result


def specificity(recipe):
    # A semantic priority, not a claim of measured solver difficulty.
    if recipe in {"semantic_rollup_v1", "global_baseline_v1", "independent_winners_v1", "entity_total_baseline_v1", "aggregate_filter_rank_v1",
                  "composite_eligible_group_report_v1"}:
        return 1
    return 0


def supported_order(task, selection):
    supported = capabilities(task, selection)
    original = selection.ordered_for(task.family_id)
    return tuple(sorted((r for r in original if r in supported), key=specificity))


def assign_recipe_batch(tasks, selection=None):
    """Return tasks in input order, with one recipe per task and maximal feasible
    distinct-recipe matching across source families. No model calls or writes.
    """
    from elt_taskgen.generation.recipe_registry import apply_semantic_recipes
    selection = RecipeSelection.model_validate(selection) if selection is not None else RecipeSelection()
    tasks = tuple(tasks)
    owners = defaultdict(list)
    caps = [capabilities(t, selection) for t in tasks]
    for i, recipes in enumerate(caps):
        for recipe in recipes:
            owners[recipe].append(i)
    rank = lambda value: sha256_hex(canonical_json([selection.seed, value]))
    recipes = sorted(owners, key=lambda r: (len({tasks[i].family_id for i in owners[r]}), specificity(r), rank(r)))
    assigned, admitted = {}, {}

    def build(i, recipe):
        key = (i, recipe)
        if key not in admitted:
            try:
                # Apply through the registry to preserve idempotency and the
                # exactly-one-managed-recipe rule even for pre-enriched inputs.
                admitted[key] = apply_semantic_recipes(tasks[i], RecipeSelection(enabled_recipes=(recipe,), seed=selection.seed))
            except SemanticDifficultyError:
                admitted[key] = None
        return admitted[key]

    def match(recipe, visited):
        candidates = sorted(owners[recipe], key=lambda i: (len(caps[i]), rank(tasks[i].family_id), tasks[i].task_id))
        for i in candidates:
            family = tasks[i].family_id
            if family in visited or build(i, recipe) is None:
                continue
            visited.add(family)
            previous = assigned.get(family)
            if previous is None or match(previous[1], visited):
                assigned[family] = (i, recipe)
                return True
        return False

    for recipe in recipes:
        match(recipe, set())
    chosen = {i: recipe for i, recipe in assigned.values()}
    return tuple(build(i, chosen[i]) if i in chosen else apply_semantic_recipes(task, selection)
                 for i, task in enumerate(tasks))

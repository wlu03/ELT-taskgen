"""Select and admit discovered source-bound graphs through final-report comparison."""

from dataclasses import asdict, replace
from itertools import zip_longest

from elt_taskgen.generation.composition_search import MAX_GRAPHS, SEARCH_RECIPE, CompositionGraph, discover_graphs
from elt_taskgen.generation.discovered_plan import (
    DiscoveredEvidence, certifies_discovered, discovered_plan, discovered_variants, read_discovered_binding,
)
from elt_taskgen.generation.discovered_witness import discovered_witness_rows
from elt_taskgen.generation.recipe_admission import RecipeFamily, SemanticDifficultyError
from elt_taskgen.generation.recipe_evidence import AGGREGATE_RANK, recipe_candidates
from elt_taskgen.models import MartSpec, TaskIR, canonical_json, sha256_hex


def discovered_candidates(task: TaskIR, recipe: str = SEARCH_RECIPE, *, seed: int = 0) -> tuple[DiscoveredEvidence, ...]:
    """Interleave bindings so the automatic retry budget can sample different source roles."""
    if recipe != SEARCH_RECIPE:
        raise ValueError(f"Unknown discovered recipe {recipe!r}")
    by_binding = []
    for source in recipe_candidates(task, AGGREGATE_RANK):
        identity = sha256_hex(canonical_json([seed, task.family_id, asdict(source)]))
        graphs = sorted(discover_graphs(source, limit=MAX_GRAPHS),
                        key=lambda graph: sha256_hex(canonical_json([identity, graph.graph_id])))
        by_binding.append((identity, [DiscoveredEvidence(source, graph) for graph in graphs]))
    streams = [candidates for _, candidates in sorted(by_binding, key=lambda item: item[0])]
    return tuple(candidate for batch in zip_longest(*streams) for candidate in batch if candidate is not None)


def bound_discovered(task: TaskIR, mart: MartSpec) -> DiscoveredEvidence:
    """Reconstruct the exact graph and public output contract against the current schema."""
    try:
        candidate, names = read_discovered_binding(mart.plan.notes)
        if names != tuple(sorted(t.name for t in task.tables)):
            raise ValueError("Source tables have changed")
        if candidate.source not in recipe_candidates(task, AGGREGATE_RANK):
            raise ValueError("Source no longer supports the selected group, measure, or relationship")
        if candidate.graph not in discover_graphs(candidate.source, limit=MAX_GRAPHS):
            raise ValueError("Graph is outside the source's supported search space")
        if discovered_plan(task, candidate, mart.name, mart.plan.template_id) != mart:
            raise ValueError("Discovered report differs from its bound graph")
    except (TypeError, ValueError, KeyError) as exc:
        raise SemanticDifficultyError(f"Invalid discovered graph binding: {exc}") from exc
    return candidate


_FAMILY = RecipeFamily(
    name="discovered", recipes=(SEARCH_RECIPE,), candidates=discovered_candidates, plan=discovered_plan,
    witness_rows=discovered_witness_rows, variants=discovered_variants, bind=bound_discovered,
    witness_description="counterexamples for the selected graph's final report",
)

discovered_cases = _FAMILY.cases
materialize_discovered_mutation = _FAMILY.materialize_mutation


def apply_discovered_difficulty(task: TaskIR, *, recipe: str = SEARCH_RECIPE, seed: int = 0) -> TaskIR:
    family = replace(_FAMILY, candidates=lambda source, recipe: discovered_candidates(source, recipe, seed=seed))
    return family.apply(task, (recipe,))


def apply_bound_discovered(task: TaskIR, recipe: str, evidence: DiscoveredEvidence) -> TaskIR:
    canonical = replace(evidence, graph=CompositionGraph(evidence.graph.canonical_nodes))
    return _FAMILY.apply(task, (recipe,), required_evidence=canonical)

"""Schema binding and recipe hooks for the graph family."""
import duckdb
from elt_taskgen.generation.graph_evidence import GRAPH_RECIPES, graph_candidates
from elt_taskgen.generation.graph_plan import read_graph_binding, graph_plan, graph_variants
from elt_taskgen.generation.graph_witness import graph_witness_rows
from elt_taskgen.generation.recipe_admission import RecipeFamily
from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError


def bound_graph(task, mart):
    try:
        e, sources=read_graph_binding(mart.plan.notes)
        if sources != tuple(sorted(t.name for t in task.tables)) or e not in graph_candidates(task,mart.plan.template_id):
            raise ValueError("Source no longer supports graph evidence")
        if graph_plan(task,e,mart.name,mart.plan.template_id) != mart:
            raise ValueError("Graph mart no longer matches its public contract")
        return e
    except (TypeError,ValueError,KeyError) as exc:
        raise SemanticDifficultyError(f"Invalid graph binding: {exc}") from exc


_FAMILY = RecipeFamily(
    name="graph", recipes=GRAPH_RECIPES, candidates=graph_candidates,
    plan=graph_plan, witness_rows=graph_witness_rows,
    variants=graph_variants, bind=bound_graph,
    witness_description="executable counterexamples",
    retryable_errors=(SemanticDifficultyError, duckdb.Error),
)


def graph_cases(e, mart, recipe):
    return _FAMILY.cases(e, mart, recipe)


materialize_graph_mutation = _FAMILY.materialize_mutation


def certify_graph(task, mart, e, rows):
    return _FAMILY.certify(task, mart, e, rows)


def apply_graph_difficulty(task, *, recipe):
    return _FAMILY.apply(task, (recipe,))

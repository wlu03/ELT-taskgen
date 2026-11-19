"""Graph search must produce distinct, executable reports on the same bound source."""

from pathlib import Path
import unittest

from elt_taskgen.adapters import dlt
from elt_taskgen.generation.composite_evidence import ELIGIBLE_GROUP_REPORT, composite_candidates
from elt_taskgen.generation.composite_proposals import apply_composite_proposal, list_composite_proposals
from elt_taskgen.generation.composition_search import CompositionGraph, discover_graphs, validate_graph
from elt_taskgen.generation.discovered_recipes import bound_discovered
from elt_taskgen.generation.mart_plan import detect_semantic_patterns
from elt_taskgen.generation.recipe_admission import SemanticDifficultyError
from elt_taskgen.generation.source_data import generate_rows
from elt_taskgen.models import PopulationName, SemanticPattern
from elt_taskgen.verification.attacks import materialize_mutation
from elt_taskgen.verification.upstream_eval import compare_mart, rows_to_canonical_csv
from tests.test_advanced_recipes import query


def operators(graph):
    return tuple(node.op for node in graph.nodes)


def sample_rows(task):
    """The eligible totals on deal 1 are 48, 12, -3, -3, 6 (mean 12)."""
    rows = {t.name: [] for t in task.tables}
    rows["deals"] = [{"id": i, "update_time": "2024-01-01"} for i in range(1, 5)]
    flows = rows["deals_flow"]
    for deal, label, values in (
        (1, "A", (16, 16, 16)), (1, "B", (4, 4, 4)), (1, "C", (-1, -1, -1)),
        (1, "D", (-1, -1, -1)), (1, "E", (2, 2, 2)),
        (1, "ineligible", (200, 200, None)), (1, None, (200, 200, 200)),
        (2, "A", (2, 2, 2)), (2, "B", (2, 2, 2)), (4, "A", (1, 2)),
    ):
        for value in values:
            flows.append({"id": len(flows) + 1, "_deals_id": deal, "flow_value": value,
                          "flow_object_kind": label})
    return rows


class CompositionSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connector = Path(__file__).resolve().parents[1] / "config/dlt_connectors/pipedrive.yaml"
        cls.source = dlt.to_task_ir(dlt.load_connector(connector))
        cls.binding, = composite_candidates(cls.source, ELIGIBLE_GROUP_REPORT)

    def test_one_binding_generates_distinct_live_topologies_within_search_limits(self):
        graphs = discover_graphs(self.binding, limit=24, max_nodes=7)
        self.assertEqual(graphs, discover_graphs(self.binding, limit=24, max_nodes=7))
        self.assertEqual(len(graphs), len({g.graph_id for g in graphs}))
        shapes = set()
        for graph in graphs:
            validate_graph(graph)
            self.assertLessEqual(len(graph.nodes), 7)
            self.assertEqual(CompositionGraph.from_dict(graph.as_dict()).graph_id, graph.graph_id)
            ancestors = {len(graph.nodes) - 1}
            for i in range(len(graph.nodes) - 1, -1, -1):
                if i in ancestors:
                    self.assertTrue(all(j < i for j in graph.nodes[i].inputs))
                    ancestors.update(graph.nodes[i].inputs)
            self.assertEqual(ancestors, set(range(len(graph.nodes))))
            shapes.add(tuple((n.op, n.inputs) for n in graph.nodes))
        self.assertEqual(len(shapes), len(graphs))
        # Ignore operation labels entirely to distinguish new wiring from new policies.
        self.assertGreaterEqual(len({tuple(n.inputs for n in g.nodes) for g in graphs}), 3)
        paths = {operators(g) for g in graphs}
        common = ("records", "summarize", "qualify")
        self.assertIn((*common, "count_groups"), paths)
        self.assertIn((*common, "mean", "below_mean", "count_groups"), paths)
        self.assertIn((*common, "mean", "above_mean", "highest"), paths)
        limited = discover_graphs(self.binding, limit=2, max_nodes=5)
        self.assertEqual(len(limited), 2)
        self.assertTrue(all(len(g.nodes) <= 5 for g in limited))

    def test_branch_order_canonicalizes_to_a_shared_graph_identity(self):
        prefix = [{"op": "records", "inputs": []}, {"op": "summarize", "inputs": [0]},
                  {"op": "qualify", "inputs": [1]}]
        choose = {"op": "highest", "inputs": [2]}
        mean = {"op": "mean", "inputs": [2]}
        left = CompositionGraph.from_dict({"nodes": [*prefix, choose, mean, {"op": "compare", "inputs": [3, 4]}]})
        right = CompositionGraph.from_dict({"nodes": [*prefix, mean, choose, {"op": "compare", "inputs": [4, 3]}]})
        self.assertEqual(left.as_dict(), right.as_dict())
        self.assertEqual(left.graph_id, right.graph_id)

    def test_invalid_grains_dead_nodes_and_forward_edges_are_refused(self):
        prefix = [("records", ()), ("summarize", (0,)), ("qualify", (1,))]
        for suffix in (
            [("mean", (0,)), ("compare", (1, 3))],             # Mean of raw facts
            [("mean", (2,)), ("highest", (2,))],                # Mean never consumed
            [("below_mean", (2, 4)), ("mean", (2,)), ("lowest", (3,))],
            [("mean", (2,)), ("highest", (3,))],               # Mean has no grouped labels
        ):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                graph = CompositionGraph.from_dict({"nodes": [
                    {"op": op, "inputs": list(inputs)} for op, inputs in (*prefix, *suffix)
                ]})
                validate_graph(graph)

    def test_bypassing_eligibility_on_only_the_mean_edge_breaks_certification(self):
        proposal = next(p for p in list_composite_proposals(self.source) if p.graph and
                        operators(CompositionGraph.from_dict(p.graph))[-2:] == ("mean", "compare"))
        task = apply_composite_proposal(self.source, proposal)
        mart = task.marts[-1]
        ops = list(mart.plan.ops)
        i = next(i for i, op in enumerate(ops) if op.details["name"] == "group_mean")
        ops[i] = ops[i].model_copy(update={"tables": ("group_totals",)})
        broken_plan = mart.plan.model_copy(update={"ops": tuple(ops)})
        self.assertNotIn(SemanticPattern.DISCOVERED_GROUP_REPORT, detect_semantic_patterns(broken_plan))
        with self.assertRaises(SemanticDifficultyError):
            bound_discovered(task, mart.model_copy(update={"plan": broken_plan}))

    def test_pipedrive_can_produce_two_different_reports_on_the_same_fields(self):
        proposals = {operators(CompositionGraph.from_dict(p.graph)): p
                     for p in list_composite_proposals(self.source) if p.graph is not None}
        common = ("records", "summarize", "qualify", "mean")
        smaller = proposals[(*common, "below_mean", "count_groups")]
        winner = proposals[(*common, "above_mean", "highest", "compare")]
        self.assertEqual(smaller.binding, winner.binding)
        self.assertNotEqual(smaller.proposal_id, winner.proposal_id)
        for proposal, columns, expected, mistakes in (
            (smaller, ("id", "group_count"), [(1, 3), (2, 0), (3, 0), (4, 0)],
             {"wrong_group_count", "skip_threshold", "reverse_threshold", "skip_eligibility", "unfiltered_mean"}),
            (winner, ("id", "selected_group", "selected_group_total", "eligible_group_mean", "selected_minus_mean"),
             [(1, "A", 48, 12, 36), (2, None, None, 6, None), (3, None, None, None, None),
              (4, None, None, None, None)],
             {"reverse_choice", "reverse_tie", "reverse_comparison", "skip_threshold",
              "reverse_threshold", "skip_eligibility", "unfiltered_mean"}),
        ):
            with self.subTest(proposal=proposal.proposal_id, columns=columns):
                task = apply_composite_proposal(self.source, proposal)
                mart = task.marts[-1]
                self.assertEqual(task.marts[:-1], self.source.marts)
                self.assertIn(SemanticPattern.DISCOVERED_GROUP_REPORT, detect_semantic_patterns(mart.plan))
                actual = query(task, mart, sample_rows(task))
                self.assertEqual(sorted(tuple(r[c] for c in columns) for r in actual), expected)

                witness = generate_rows(task, PopulationName.COUNTERFACTUAL)
                gold = rows_to_canonical_csv(query(task, mart, witness), tuple(c.name for c in mart.columns))
                cases = task.attack_cases[len(self.source.attack_cases):]
                self.assertTrue({f"{mart.name}__{m}" for m in mistakes} <= {c.name for c in cases})
                for case in cases:
                    with self.subTest(mistake=case.name):
                        sql = materialize_mutation(task, case, object())[mart.name]
                        self.assertFalse(compare_mart(gold, query(task, mart, witness, sql), mart))

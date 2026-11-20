"""Verify the outputs of both source-derived reports independently of their plans."""

from pathlib import Path
import unittest

from elt_taskgen.adapters import dlt
from elt_taskgen.generation.composite_evidence import (
    COMPOSITE_RECIPES, DOCUMENT_BALANCE, ELIGIBLE_GROUP_REPORT,
)
from elt_taskgen.generation.composite_plan import FragmentGraph
from elt_taskgen.generation.difficulty_profiles import apply_generation_profile
from elt_taskgen.generation.mart_plan import detect_semantic_patterns
from elt_taskgen.generation.recipe_assignment import assign_recipe_batch, capabilities
from elt_taskgen.generation.recipe_registry import RecipeSelection
from elt_taskgen.generation.semantic_difficulty import SemanticDifficultyError
from elt_taskgen.generation.source_data import generate_rows
from elt_taskgen.models import PopulationName, SemanticPattern
from elt_taskgen.reference.solution import compile_plan_sql
from elt_taskgen.verification.attacks import materialize_mutation
from elt_taskgen.verification.upstream_eval import compare_mart, rows_to_canonical_csv
from tests.scenario_fixtures import scenario_source
from tests.test_advanced_recipes import query


class CompositeRecipeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The checked-in Pipedrive manifest includes curated flow fields and the
        # transformer-to-deal relationship from the pinned dlt connector.
        connector = Path(__file__).resolve().parents[1] / "config/dlt_connectors/pipedrive.yaml"
        cls.deals = dlt.to_task_ir(dlt.load_connector(connector))
        source = scenario_source(composite=True, text_keys=True)
        # Only lines and payments carry amounts in this version of the fixture.
        header = source.table("invoices")
        header = header.model_copy(update={
            "columns": tuple(c for c in header.columns if c.name != "amount_total"),
        })
        cls.invoices = source.model_copy(update={
            "tables": tuple(header if t.name == header.name else t for t in source.tables),
        })
        cls.group_report, cls.document_report = assign_recipe_batch(
            (cls.deals, cls.invoices), RecipeSelection(enabled_recipes=COMPOSITE_RECIPES),
        )

    def test_batch_assigns_reports_and_reuses_them(self):
        for source, task, recipe in (
            (self.deals, self.group_report, ELIGIBLE_GROUP_REPORT),
            (self.invoices, self.document_report, DOCUMENT_BALANCE),
        ):
            with self.subTest(recipe=recipe):
                self.assertEqual(task.marts[-1].plan.template_id, recipe)
                self.assertEqual(task.marts[:-1], source.marts)
                self.assertEqual(task.tables, source.tables)
                self.assertEqual(task.relationships, source.relationships)
                policy = RecipeSelection(enabled_recipes=(recipe,))
                self.assertEqual(apply_generation_profile(task, recipe_selection=policy), task)

    def test_reports_require_declared_source_relationships(self):
        for source, recipe, child in (
            (self.deals, ELIGIBLE_GROUP_REPORT, "deals_flow"),
            (self.invoices, DOCUMENT_BALANCE, "payments"),
        ):
            with self.subTest(recipe=recipe):
                disconnected = source.model_copy(update={
                    "relationships": tuple(r for r in source.relationships if r.child_table != child),
                })
                policy = RecipeSelection(enabled_recipes=(recipe,))
                self.assertFalse(capabilities(disconnected, policy))
                with self.assertRaises(SemanticDifficultyError):
                    apply_generation_profile(disconnected, recipe_selection=policy)

    def test_static_certification_requires_both_dependencies_to_be_live(self):
        for task, pattern, consumer, wrong_input in (
            (self.group_report, SemanticPattern.ELIGIBLE_GROUP_REPORT, "eligible_mean", "group_totals"),
            (self.document_report, SemanticPattern.DOCUMENT_BALANCE, "document_lines", "payment_totals"),
        ):
            with self.subTest(pattern=pattern):
                mart = task.marts[-1]
                plan = mart.plan
                self.assertIn(pattern, detect_semantic_patterns(plan))
                ops = list(plan.ops)
                i = next(i for i, op in enumerate(ops) if op.details["name"] == consumer)
                ops[i] = ops[i].model_copy(update={"tables": (*ops[i].tables[:-1], wrong_input)})
                broken_plan = plan.model_copy(update={"ops": tuple(ops)})
                self.assertNotIn(pattern, detect_semantic_patterns(broken_plan))
                changed = task.model_copy(update={
                    "marts": (*task.marts[:-1], mart.model_copy(update={"plan": broken_plan})),
                })
                with self.assertRaises(SemanticDifficultyError):
                    apply_generation_profile(
                        changed, recipe_selection=RecipeSelection(enabled_recipes=(plan.template_id,)),
                    )

    def test_fragment_attach_requires_unique_grain_and_available_columns(self):
        graph = FragmentGraph(self.deals)
        keys = ("deal",)
        parents = graph.source("deals", ("id",), keys, {}, "entities", unique=True)
        flows = graph.source("deals_flow", ("_deals_id",), keys,
                             {"value": ("flow_value", "number")}, "activity")
        with self.assertRaisesRegex(ValueError, "grain"):
            graph.attach(parents, flows, keys, ("value",), "fanout")
        totals = graph.aggregate(flows, keys, {"total": "SUM(value)"}, "totals")
        with self.assertRaisesRegex(ValueError, "value"):
            graph.attach(parents, totals, keys, ("value",), "missing_projection")
        result = graph.attach(parents, totals, keys, ("total",), "entity_totals")
        self.assertEqual(result.grain, keys)
        self.assertEqual(set(result.columns), {"deal", "total"})

    def test_ranking_and_mean_both_consume_eligible_group_totals(self):
        task = self.group_report
        mart = task.marts[-1]
        rows = {table.name: [] for table in task.tables}
        rows["deals"] = [{"id": i, "update_time": "2024-01-01"} for i in (1, 2, 3, 4)]
        for deal, label, values in (
            (1, "zeta", (4, 4, 4)), (1, "beta", (5, 5, 20)), (1, "alpha", (10, 10, 10)),
            (1, "high", (100, 100, None)), (1, None, (100, 100, 100)),
            (2, "alpha", (-3, -3, -3)), (2, "beta", (-1, -1, -1)),
            (4, "alpha", (1, 1)),
        ):
            for value in values:
                flows = rows["deals_flow"]
                flows.append({"id": len(flows) + 1, "_deals_id": deal,
                              "flow_object_kind": label, "flow_value": value})
        fields = ("id", "selected_group", "selected_group_total", "eligible_group_mean", "above_group_mean")
        actual = sorted(tuple(row[k] for k in fields) for row in query(task, mart, rows))
        self.assertEqual(actual, [
            (1, "alpha", 30, 24, 6), (2, "beta", -3, -6, 3),
            (3, None, None, None, None), (4, None, None, None, None),
        ])

    def test_document_branches_reconcile_at_the_full_parent_grain(self):
        task = self.document_report
        mart = task.marts[-1]
        rows = {table.name: [] for table in task.tables}
        keys = [("A", "1"), ("A", "2"), ("B", "1"), ("B", "2")]
        rows["invoices"] = [dict(zip(("tenant_id", "id"), key)) for key in keys]
        for table, identity, amount, entries in (
            ("invoice_lines", "line_id", "amount", [(0, 50), (0, 50), (1, 15), (1, None)]),
            ("payments", "payment_id", "paid_amount", [(0, 30), (0, 30), (2, 10)]),
        ):
            for index, (owner, value) in enumerate(entries):
                tenant, invoice = keys[owner]
                rows[table].append({identity: str(index), "owner_tenant": tenant,
                                    "owner_id": invoice, amount: value})
        fields = ("tenant_id", "id", "line_total", "paid_total", "line_payment_gap",
                  "mean_document_gap", "above_mean_gap")
        actual = sorted(tuple(row[k] for k in fields) for row in query(task, mart, rows))
        self.assertEqual(actual, [
            ("A", "1", 100, 60, 40, 11.25, 28.75), ("A", "2", 15, 0, 15, 11.25, 3.75),
            ("B", "1", 0, 10, -10, 11.25, -21.25), ("B", "2", 0, 0, 0, 11.25, -11.25),
        ])

    def test_generated_counterfactuals_distinguish_each_connection_error(self):
        for source, task, expected_variants in (
            (self.deals, self.group_report,
             {"rank_unfiltered", "baseline_unfiltered", "reverse_tie", "inner_parent", "distinct_group_values"}),
            (self.invoices, self.document_report,
             {"raw_fanout", "distinct_payments", "exclude_empty_baseline", "missing_key_0", "missing_key_1"}),
        ):
            mart = task.marts[-1]
            rows = generate_rows(task, PopulationName.COUNTERFACTUAL)
            gold = rows_to_canonical_csv(query(task, mart, rows), tuple(c.name for c in mart.columns))
            cases = task.attack_cases[len(source.attack_cases):]
            self.assertTrue({f"{mart.name}__{v}" for v in expected_variants} <= {c.name for c in cases})
            for case in cases:
                with self.subTest(mart=mart.name, variant=case.name):
                    sqls = materialize_mutation(task, case, object())
                    wrong = query(task, mart, rows, sqls[mart.name])
                    self.assertFalse(compare_mart(gold, wrong, mart))
                    for existing in source.marts:
                        self.assertEqual(sqls[existing.name], compile_plan_sql(task, existing))

    def test_small_balance_survives_large_independent_totals(self):
        task = self.document_report
        rows = {table.name: [] for table in task.tables}
        owner = {"owner_tenant": "A", "owner_id": "1"}
        rows["invoices"] = [{"tenant_id": "A", "id": "1"}]
        rows["invoice_lines"] = [
            {**owner, "line_id": "L1", "amount": 2**53},
            {**owner, "line_id": "L2", "amount": 1},
        ]
        rows["payments"] = [{**owner, "payment_id": "P1", "paid_amount": 2**53}]
        result, = query(task, task.marts[-1], rows)
        self.assertEqual(result["line_payment_gap"], 1)
        self.assertEqual(result["mean_document_gap"], 1)

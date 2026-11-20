import unittest

from elt_taskgen.destinations import Destination
from elt_taskgen.generation.difficulty_profiles import apply_generation_profile
from elt_taskgen.generation.lineage import effective_lineage
from elt_taskgen.generation.plan_validation import mart_inputs, validate_plan
from elt_taskgen.generation.recipe_registry import UPSTREAM_BASELINE, RecipeSelection
from elt_taskgen.models import MartOp, MartOpKind, MartPlan, PopulationName
from elt_taskgen.reference.duckdb_sandbox import sandboxed_memory_connection
from elt_taskgen.reference.solution import (
    PlanCompilationError, _insert_rows, build_reference, create_table, execute_mart,
)
from elt_taskgen.training.canonical import render_canonical_model
from elt_taskgen.training.dbt_runner import rewrite_model_sql
from elt_taskgen.verification.structural_completeness import check_structural_completeness
from tests.test_recipe_registry import source_task


class UpstreamBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        selection = RecipeSelection(enabled_recipes=(UPSTREAM_BASELINE,))
        cls.task = apply_generation_profile(source_task(), recipe_selection=selection)
        cls.upstream, cls.downstream = cls.task.marts

    def test_changed_upstream_value_propagates_through_three_models(self):
        qualified = self.downstream.model_copy(update={
            "name": "qualified_studios",
            "plan": MartPlan(mart="qualified_studios", ops=(
                MartOp(kind=MartOpKind.FILTER, tables=(self.downstream.name,),
                       columns=tuple(column.name for column in self.downstream.columns),
                       description="Keep studios above the population mean.",
                       predicate="above_population_mean", details={"name": "qualified"}),
            )),
        })
        task = self.task.model_copy(update={"marts": (qualified, self.downstream, self.upstream)})
        reference = build_reference(task)
        sql = dict(reference.sql_by_mart)
        sql[self.upstream.name] = (
            "SELECT * REPLACE (CASE WHEN studio_id = 901 THEN 12 ELSE films_count END AS films_count) "
            f"FROM ({sql[self.upstream.name]}) AS changed"
        )
        changed = task.model_copy(update={"reference": reference.model_copy(update={"sql_by_mart": sql})})
        rebuilt = build_reference(changed)
        rows = task.population(PopulationName.COUNTERFACTUAL).literal_rows
        with sandboxed_memory_connection() as con:
            for table in task.tables:
                create_table(con, table)
                _insert_rows(con, table, rows[table.name])
            before = execute_mart(con, qualified, reference.sql_by_mart[qualified.name])
            after = execute_mart(con, qualified, rebuilt.sql_by_mart[qualified.name])
        self.assertEqual([row["studio_id"] for row in before], [900, 902, 903, 904])
        self.assertEqual([row["studio_id"] for row in after], [901])

    def test_dependency_survives_validation_lineage_and_dbt_export(self):
        inputs = mart_inputs(self.task, self.downstream.plan)
        self.assertEqual(inputs, (self.upstream.name,))
        self.assertEqual(validate_plan(self.task, self.downstream.plan), [])
        self.assertEqual(check_structural_completeness(self.task), [])
        lineage = effective_lineage(self.task)
        self.assertEqual(lineage.by_mart[self.downstream.name], lineage.by_mart[self.upstream.name])
        for destination in Destination:
            with self.subTest(destination=destination):
                sql = render_canonical_model(
                    self.task.reference.sql_by_mart[self.downstream.name],
                    set(lineage.used_tables), destination, upstream_names=inputs,
                )
                self.assertIn("{{ ref('studio_activity') }}", sql)
                self.assertNotIn("{{ source(", sql)
                rewrite_model_sql(sql, destination)

    def test_cycle_is_rejected_even_with_saved_references(self):
        plan = self.upstream.plan
        first = plan.ops[0].model_copy(update={"tables": (self.downstream.name,)})
        upstream = self.upstream.model_copy(update={
            "plan": plan.model_copy(update={"ops": (first, *plan.ops[1:])}),
        })
        task = self.task.model_copy(update={"marts": (upstream, self.downstream)})
        with self.assertRaisesRegex(PlanCompilationError, "Cyclic mart dependency"):
            build_reference(task)

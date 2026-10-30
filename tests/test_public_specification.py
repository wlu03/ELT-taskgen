"""The solver receives output definitions, not the reference plan's topology."""

from __future__ import annotations

import unittest

import yaml

from elt_taskgen import demo_fixture
from elt_taskgen.export import eltbench
from elt_taskgen.generation import mart_plan
from elt_taskgen.models import MartOpKind
from elt_taskgen.specification import data_model
from tests.test_dataset_column_names import EVIDENCE, build_task


class PublicSpecificationTest(unittest.TestCase):
    def test_every_shape_uses_original_benchmark_schema(self):
        for name, builder in mart_plan.registered_shape_builders():
            with self.subTest(shape=name):
                built = builder(EVIDENCE, mart=f"{name}_mart")
                task = build_task(built, f"proof__public_{name}")
                document = data_model(task)
                model = document["models"][0]
                self.assertEqual(set(model), {"name", "description", "columns"})
                self.assertEqual(
                    [c["name"] for c in model["columns"]],
                    [c.name for c in built.columns],
                )
                for column in model["columns"]:
                    self.assertEqual(set(column), {"name", "description"})
                    self.assertTrue(column["description"].strip())
                serialized = eltbench._dump_yaml(document, sort_keys=False)
                self.assertEqual(yaml.safe_load(serialized), document)
                for token in ("join_preservation:", "carried_fields:", "operation:",
                              "transformation:", "source_inputs:", "semantic_parameters:",
                              "placeholder", "Deterministic output order"):
                    self.assertNotIn(token, serialized)

    def test_changing_internal_strategy_does_not_change_public_contract(self):
        task = demo_fixture.demo_task()
        mart = task.marts[0]
        ops = tuple(
            op.model_copy(update={"description": "PRIVATE STRATEGY DETAIL", "details": {
                **op.details, "name": "secret_intermediate", "sql": "SELECT secret_answer"
            }})
            if op.kind not in {MartOpKind.FILTER, MartOpKind.DEDUPE} else op
            for op in reversed(mart.plan.ops)
        )
        changed = task.model_copy(update={"marts": (
            mart.model_copy(update={"plan": mart.plan.model_copy(update={"ops": ops})}),
        ), "solver_prompt": "PRIVATE STRATEGY DETAIL"})
        # Reordering scope rules is immaterial; compare their presence separately.
        actual = data_model(changed)["models"][0]
        expected = data_model(task)["models"][0]
        self.assertEqual(actual["columns"], expected["columns"])
        self.assertIn("status = 'completed'", actual["description"])
        for surface in (str(actual), eltbench.public_documentation(changed),
                        eltbench.t_documentation(changed)):
            self.assertNotIn("PRIVATE STRATEGY DETAIL", surface)
            self.assertNotIn("secret_intermediate", surface)
            self.assertNotIn("secret_answer", surface)

    def test_threshold_is_a_model_requirement_without_per_column_repetition(self):
        built = mart_plan.aggregate_then_filter(EVIDENCE, mart="active_studios")
        task = build_task(built, "proof__public_threshold")
        model = data_model(task)["models"][0]
        self.assertIn(f"{built.column_named('link_count')} is at least 2", model["description"])
        for column in model["columns"]:
            self.assertNotIn("at least 2", column["description"])
        self.assertNotIn("after", model["description"])

    def test_independent_implementer_uses_the_same_public_contract(self):
        from elt_taskgen.reference.independent import implementer_view

        task = demo_fixture.demo_task().model_copy(update={
            "solver_prompt": "PRIVATE AUTHOR WALKTHROUGH: preaggregate then join",
        })
        view = implementer_view(task)
        self.assertIn(eltbench._dump_yaml(data_model(task), sort_keys=False).rstrip(), view)
        self.assertNotIn("PRIVATE AUTHOR WALKTHROUGH", view)
        self.assertNotIn("carried_fields", view)

    def test_diagnostic_filter_labels_do_not_become_public_requirements(self):
        from elt_taskgen.models import MartOp

        task = demo_fixture.demo_task()
        mart = task.marts[0]
        diagnostic = MartOp(
            kind=MartOpKind.FILTER, description="semantic known groups",
            tables=("orders",), details={"name": "semantic_known_groups"},
        )
        changed = task.model_copy(update={"marts": (
            mart.model_copy(update={"plan": mart.plan.model_copy(update={
                "ops": (*mart.plan.ops, diagnostic),
            })}),
        )})
        self.assertEqual(data_model(changed), data_model(task))
        self.assertIn("status = 'completed'", str(data_model(changed)))


if __name__ == "__main__":
    unittest.main()

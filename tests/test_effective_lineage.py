from __future__ import annotations

import unittest

from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.generation.lineage import (
    EffectiveLineageError,
    effective_lineage,
    prune_to_effective_lineage,
)
from elt_taskgen.models import (
    AttackCase,
    AttackKind,
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    MartOp,
    MartOpKind,
    PopulationName,
    TableSpec,
)


class EffectiveLineageTests(unittest.TestCase):
    def test_lineage_comes_from_executable_sql(self) -> None:
        task = demo_task()
        lineage = effective_lineage(task)
        self.assertEqual(
            lineage.used_tables, ("customers", "order_items", "orders")
        )
        self.assertEqual(lineage.unused_tables, ())

    def test_prunes_unrelated_table_population_and_noop_source_hint(self) -> None:
        task = demo_task()
        audit = TableSpec(
            name="audit_log",
            description="Unrelated package table.",
            primary_key=("event_id",),
            columns=(
                ColumnSpec(
                    name="event_id",
                    type=ColumnType.BIGINT,
                    nullable=False,
                    description="Audit event identifier.",
                ),
            ),
        )
        mart = task.marts[0]
        hint = MartOp(
            kind=MartOpKind.SOURCE,
            description="Read source table audit_log.",
            tables=("audit_log",),
        )
        populations = tuple(
            population.model_copy(
                update={
                    "scale": {**population.scale, "audit_log": 10},
                    "conditions": population.conditions
                    + (
                        "COVERAGE SHORTFALL: audit_log is keyed by a removed edge.",
                    ),
                    "literal_rows": {
                        **population.literal_rows,
                        "audit_log": ({"event_id": 1},),
                    },
                }
            )
            for population in task.populations
        )
        expanded = task.model_copy(
            update={
                "tables": task.tables + (audit,),
                "backends": task.backends
                + (BackendAssignment(table="audit_log", backend=Backend.FILES),),
                "marts": (
                    mart.model_copy(
                        update={
                            "plan": mart.plan.model_copy(
                                update={"ops": (hint,) + mart.plan.ops}
                            )
                        }
                    ),
                ),
                "populations": populations,
                "attack_cases": tuple(
                    case
                    for case in task.attack_cases
                    if case.kind is not AttackKind.HEADER_AS_ROW
                )
                + (
                    AttackCase(
                        name="header_as_row",
                        kind=AttackKind.HEADER_AS_ROW,
                        description=(
                            "Required because removed text-only audit_log is a "
                            "FILES table."
                        ),
                        mutation="directive:load:header_as_row",
                        expected_pass={PopulationName.PRIMARY: False},
                    ),
                ),
            }
        )

        compacted = prune_to_effective_lineage(expanded)

        self.assertEqual(
            {table.name for table in compacted.tables},
            {"customers", "orders", "order_items"},
        )
        self.assertNotIn("audit_log", {item.table for item in compacted.backends})
        self.assertTrue(
            all("audit_log" not in population.scale for population in compacted.populations)
        )
        self.assertTrue(
            all(
                "audit_log" not in population.literal_rows
                for population in compacted.populations
            )
        )
        self.assertTrue(
            all(
                "audit_log" not in condition
                for population in compacted.populations
                for condition in population.conditions
            )
        )
        self.assertFalse(
            any(
                "audit_log" in op.tables
                for op in compacted.marts[0].plan.ops
            )
        )
        header_cases = [
            case
            for case in compacted.attack_cases
            if case.kind is AttackKind.HEADER_AS_ROW
        ]
        self.assertEqual(len(header_cases), 1)
        self.assertFalse(header_cases[0].required)
        self.assertNotIn("audit_log", header_cases[0].description)
        self.assertEqual(effective_lineage(compacted).unused_tables, ())

    def test_rejects_constant_mart_with_no_source_lineage(self) -> None:
        task = demo_task()
        reference = task.reference.model_copy(
            update={"sql_by_mart": {task.marts[0].name: "SELECT 1 AS answer"}}
        )
        with self.assertRaisesRegex(EffectiveLineageError, "reads no declared"):
            effective_lineage(task.model_copy(update={"reference": reference}))

    def test_minimum_source_table_policy_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(EffectiveLineageError, "below required minimum"):
            prune_to_effective_lineage(demo_task(), minimum_source_tables=4)


if __name__ == "__main__":
    unittest.main()

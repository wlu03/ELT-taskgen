"""Policy-v6 coverage for optional standard fact-to-parent relationships."""

from __future__ import annotations

import unittest

import duckdb

from elt_taskgen.generation import mart_plan, populations, source_data
from elt_taskgen.models import (
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    MartSpec,
    Origin,
    PopulationName,
    Relationship,
    TableSpec,
    TaskIR,
)
from elt_taskgen.reference import solution as reference


def _fixture(
    *,
    required: bool = False,
    nullable: bool = True,
) -> tuple[TaskIR, mart_plan.StarShape]:
    accounts = TableSpec(
        name="accounts",
        columns=(
            ColumnSpec(name="account_id", type=ColumnType.INTEGER),
            ColumnSpec(name="account_name", type=ColumnType.TEXT),
        ),
        primary_key=("account_id",),
    )
    events = TableSpec(
        name="events",
        columns=(
            ColumnSpec(name="event_id", type=ColumnType.INTEGER),
            ColumnSpec(
                name="account_id",
                type=ColumnType.INTEGER,
                nullable=nullable,
            ),
            ColumnSpec(name="amount", type=ColumnType.INTEGER, nullable=True),
            ColumnSpec(name="event_label", type=ColumnType.TEXT),
        ),
        primary_key=("event_id",),
    )
    relationship = Relationship(
        child_table="events",
        child_columns=("account_id",),
        parent_table="accounts",
        parent_columns=("account_id",),
        required=required,
    )
    evidence = mart_plan.ChainEvidence(
        parent="accounts",
        parent_key="account_id",
        parent_key_type=ColumnType.INTEGER,
        parent_attr="account_name",
        bridge="events",
        bridge_key="event_id",
        bridge_key_type=ColumnType.INTEGER,
        bridge_key_is_unique=True,
        bridge_parent_fk="account_id",
        bridge_amount="amount",
        bridge_amount_type=ColumnType.INTEGER,
        bridge_amount_nullable=True,
        bridge_label="event_label",
    )
    built = mart_plan.measure_state_distribution(
        evidence, mart="account_event_states"
    )
    mart = MartSpec(
        name=built.plan.mart,
        description="One distribution row per account and measure state.",
        grain="One row per account and measure state.",
        key_columns=built.shape.key_columns,
        columns=built.columns,
        plan=built.plan,
    )
    tables = (accounts, events)
    relationships = (relationship,)
    generated, attacks = populations.derive_populations_and_attacks(
        task_id="proof__optional_owner_orphan",
        tables=tables,
        relationships=relationships,
        shapes=(built.shape,),
        scale_hint={"accounts": 10, "events": 20},
    )
    task = TaskIR(
        task_id="proof__optional_owner_orphan",
        family_id="proof__optional_owner_orphan",
        cluster_id="proof__optional_owner_orphan",
        origin=Origin.SYNTHETIC,
        license="CC0-1.0",
        tables=tables,
        relationships=relationships,
        backends=tuple(
            BackendAssignment(table=table.name, backend=Backend.FILES)
            for table in tables
        ),
        marts=(mart,),
        populations=generated,
        attack_cases=attacks,
    )
    return task, built.shape


def _counterfactual(task: TaskIR):
    return next(
        population
        for population in task.populations
        if population.name is PopulationName.COUNTERFACTUAL
    )


def _execute(
    task: TaskIR,
    sql: str,
    rows: dict[str, list[dict]],
) -> list[tuple]:
    connection = duckdb.connect(":memory:")
    try:
        for table in task.tables:
            reference.create_table(connection, table)
            if rows.get(table.name):
                reference._insert_rows(connection, table, rows[table.name])
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


class OptionalOwnerOrphanTest(unittest.TestCase):
    def test_nullable_optional_owner_gets_deterministic_non_null_orphan(self) -> None:
        task, shape = _fixture()
        again, _ = _fixture()
        counterfactual = _counterfactual(task)
        self.assertEqual(
            counterfactual.model_dump(mode="json"),
            _counterfactual(again).model_dump(mode="json"),
        )

        account_keys = {
            row["account_id"]
            for row in counterfactual.literal_rows["accounts"]
        }
        dangling = [
            row
            for row in counterfactual.literal_rows["events"]
            if row["account_id"] is not None
            and row["account_id"] not in account_keys
        ]
        self.assertEqual(len(dangling), 1, counterfactual.literal_rows)
        self.assertEqual(
            populations._relationship_key(
                populations._shape_owner_relationship(
                    shape, task.relationships
                )
            ),
            ("events", "accounts", ("account_id",), ("account_id",)),
        )
        optional_lines = tuple(
            line
            for line in counterfactual.conditions
            if line.startswith("OPTIONAL-LINK WITNESS:")
        )
        self.assertEqual(len(optional_lines), 1)
        self.assertIn("events.account_id", optional_lines[0])
        self.assertIn("accounts.account_id", optional_lines[0])
        self.assertTrue(source_data.declares_dangling(counterfactual.conditions))
        self.assertEqual(populations.validate_population_coverage(task), [])

    def test_child_preserving_outer_join_is_killed_only_when_orphan_exists(self) -> None:
        task, _shape = _fixture()
        population = _counterfactual(task)
        rows = {
            name: [dict(row) for row in table_rows]
            for name, table_rows in population.literal_rows.items()
        }
        account_keys = {row["account_id"] for row in rows["accounts"]}
        orphan = next(
            row
            for row in rows["events"]
            if row["account_id"] is not None
            and row["account_id"] not in account_keys
        )

        gold_sql = reference.compile_plan_sql(task, task.marts[0])
        wrong_sql = gold_sql.replace(
            'distribution_entities."entity_key" AS "entity_key"',
            'COALESCE(distribution_entities."entity_key", '
            'events."account_id") AS "entity_key"',
            1,
        ).replace(
            'FROM step_2 AS distribution_entities LEFT JOIN "events" AS events',
            'FROM step_2 AS distribution_entities FULL OUTER JOIN "events" AS events',
            1,
        )
        self.assertNotEqual(gold_sql, wrong_sql)

        without_orphan = {
            **rows,
            "events": [row for row in rows["events"] if row is not orphan],
        }
        self.assertEqual(
            _execute(task, gold_sql, without_orphan),
            _execute(task, wrong_sql, without_orphan),
        )
        self.assertNotEqual(
            _execute(task, gold_sql, rows),
            _execute(task, wrong_sql, rows),
        )

    def test_required_owner_never_gets_an_orphan_or_claim(self) -> None:
        task, shape = _fixture(required=True, nullable=False)
        counterfactual = _counterfactual(task)
        self.assertIsNone(
            populations._shape_owner_relationship(shape, task.relationships)
        )
        account_keys = {
            row["account_id"]
            for row in counterfactual.literal_rows["accounts"]
        }
        self.assertTrue(
            all(
                row["account_id"] in account_keys
                for row in counterfactual.literal_rows["events"]
            )
        )
        self.assertFalse(
            any(
                line.startswith("OPTIONAL-LINK WITNESS:")
                for line in counterfactual.conditions
            )
        )

    def test_overlapping_relationship_declines_the_owner_orphan(self) -> None:
        task, shape = _fixture()
        overlapping = Relationship(
            child_table="events",
            child_columns=("account_id",),
            parent_table="accounts",
            parent_columns=("account_id",),
            required=True,
        )
        self.assertIsNone(
            populations._shape_owner_relationship(
                shape, task.relationships + (overlapping,)
            )
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

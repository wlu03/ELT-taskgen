"""Policy-v5 counterfactual coverage for nullable bridge measures.

Row B proves the no-linked-row branch.  It cannot also prove what happens when
real linked rows exist but every measure is NULL: COUNT(link_key), argmax row
selection, and an absent-value cohort all behave differently in that state.
"""

from __future__ import annotations

import unittest

import duckdb

from elt_taskgen.generation import mart_plan, populations, source_data
from elt_taskgen.models import (
    AttackKind,
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
from elt_taskgen.verification import attacks


def fixture(nullable: bool, *, label_nullable: bool = False):
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
            ColumnSpec(name="account_id", type=ColumnType.INTEGER),
            ColumnSpec(
                name="amount",
                type=ColumnType.INTEGER,
                nullable=nullable,
            ),
            ColumnSpec(
                name="event_label",
                type=ColumnType.TEXT,
                nullable=label_nullable,
            ),
        ),
        primary_key=("event_id",),
    )
    relationship = Relationship(
        child_table="events",
        child_columns=("account_id",),
        parent_table="accounts",
        parent_columns=("account_id",),
        required=True,
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
        bridge_amount_nullable=nullable,
        bridge_label="event_label",
        bridge_label_nullable=label_nullable,
    )
    return (accounts, events), (relationship,), evidence


def task_for(built: mart_plan.BuiltPlan, tables, relationships, task_id: str) -> TaskIR:
    mart = MartSpec(
        name=built.plan.mart,
        description=f"{built.shape.shape_name} nullable-measure proof mart.",
        grain=f"One row per {built.shape.shape_name} key.",
        key_columns=built.shape.key_columns,
        columns=built.columns,
        plan=built.plan,
    )
    generated, attacks = populations.derive_populations_and_attacks(
        task_id=task_id,
        tables=tables,
        relationships=relationships,
        shapes=(built.shape,),
        # This helper is also reused by tests that add a dimension table.
        # Give every declared table a real scaled parent pool; otherwise a
        # required FK fixture is invalid for reasons unrelated to the witness
        # behavior under test.
        scale_hint={
            **{table.name: 10 for table in tables},
            "accounts": 10,
            "events": 20,
        },
        backends=1,
    )
    return TaskIR(
        task_id=task_id,
        family_id="proof__nullable_measure",
        cluster_id=task_id,
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


def run_counterfactual_sql(task: TaskIR, sql: str) -> list[dict]:
    rows = source_data.generate_rows(task, PopulationName.COUNTERFACTUAL)
    con = duckdb.connect(":memory:")
    try:
        for table in task.tables:
            reference.create_table(con, table)
            if rows.get(table.name):
                reference._insert_rows(con, table, rows[table.name])
        cursor = con.execute(sql)
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        con.close()


def run_counterfactual(task: TaskIR) -> list[dict]:
    return run_counterfactual_sql(
        task, reference.compile_plan_sql(task, task.marts[0])
    )


class NullableMeasureWitnessTest(unittest.TestCase):
    def test_measure_state_plan_describes_unique_values_behaviorally(self) -> None:
        tables, relationships, evidence = fixture(nullable=True)
        built = mart_plan.measure_state_distribution(
            evidence, mart="nullable_measure_states"
        )

        serialized = built.plan.model_dump_json()
        self.assertNotIn("distinct value count", serialized.lower())
        # "a count that includes each non-missing amount once even when rows
        # repeat" was read as COUNT(amount) per row against the column's
        # "unique values" (dlt__personio, batch10 2026-09-11); the rule now
        # says the same thing the column says.
        self.assertIn(
            "how many different non-missing amount values occur (each different "
            "value counted once, however many rows repeat it)",
            serialized,
        )
        distinct_amount = next(
            column
            for column in built.columns
            if column.name == built.column_named("distinct_amount_count")
        )
        # ZERO FOR TWO DIFFERENT CELLS. "0 for a no-activity absent cell"
        # named only one of them, so the absent cell of an entity whose rows
        # all have a missing amount was left undescribed (batch10 2026-09-11,
        # dlt__personio: the ambiguity critic filed it as a graded fork).
        self.assertEqual(
            distinct_amount.description,
            "Number of unique non-missing amount values in this cell; each unique "
            "non-missing value is counted once, however many rows repeat it; 0 "
            "whenever the cell holds no amount value at all — both for an "
            "accounts row with no linked events row and for an absent cell "
            "whose rows all have a missing amount.",
        )
        # row_count is the one the fork actually bit: it COUNTS the rows of an
        # absent cell that holds real rows, and gold agrees — the expression
        # is COUNT(link_key), which is 0 only for the LEFT-join placeholder.
        row_count = next(
            column
            for column in built.columns
            if column.name == built.column_named("row_count")
        )
        self.assertEqual(
            row_count.description,
            "Number of linked events rows in this entity/state cell; an absent "
            "cell holding real events rows whose amount is missing COUNTS "
            "those rows, and only the placeholder cell of an accounts row "
            "with no linked events row at all reports 0.",
        )

    def test_nullable_contract_retains_real_row_missing_measure_branches(self) -> None:
        _tables, _relationships, evidence = fixture(nullable=True)
        argmax = mart_plan.argmax_profile(evidence, mart="nullable_argmax")
        argmax_contract = argmax.plan.model_dump_json() + " ".join(
            column.description for column in argmax.columns
        )
        self.assertIn("no amount value", argmax_contract)
        self.assertIn(
            "none of its rows carries a amount value",
            argmax_contract,
        )
        tied_count = next(
            column
            for column in argmax.columns
            if column.name == argmax.column_named("tied_count")
        )
        self.assertIn(
            "0 when there are no rows or when none of the rows carries a amount value",
            tied_count.description,
        )
        # batch10 run F 2026-09-11 (dlt__workable, dlt__personio): "'empty'
        # = no row holds a maximum" beside "every row ranks, so a winner
        # exists" read as two rules; the winner picked by the tie-break alone
        # is named and explicitly not counted as tied.
        self.assertIn("is not counted here", tied_count.description)
        name = argmax.column_named
        tie_state = next(
            column for column in argmax.columns if column.name == name("tie_state")
        )
        self.assertIn("never holds the maximum", tie_state.description)
        self.assertIn(f"with {name('tied_count')} 0", tie_state.description)
        named = {c.name for c in argmax.columns}
        label, row_id = name("top_label"), name("top_row_id")
        if row_id in named:
            self.assertIn(f"{label} and {row_id} name", tie_state.description)
        else:
            self.assertIn(f"{label} names", tie_state.description)
            self.assertNotIn(row_id, tie_state.description)
        self.assertNotIn("when the winner is unique", tied_count.description)
        self.assertIn(
            "when no row carries a measure there is no maximum and "
            f"{name('tied_count')} is 0",
            argmax_contract,
        )

        distribution = mart_plan.measure_state_distribution(
            evidence, mart="nullable_distribution"
        )
        distribution_contract = distribution.plan.model_dump_json() + " ".join(
            column.description for column in distribution.columns
        )
        self.assertIn("amount is missing", distribution_contract)
        self.assertIn(
            "none of the cell's rows carries an amount value",
            distribution_contract,
        )

    def test_nullable_measure_appends_row_l_without_moving_a_b_or_f(self) -> None:
        nullable_tables, relationships, nullable_evidence = fixture(nullable=True)
        strict_tables, _relationships, strict_evidence = fixture(nullable=False)
        nullable_shape = mart_plan.argmax_profile(
            nullable_evidence, mart="nullable_argmax"
        ).shape
        strict_shape = mart_plan.argmax_profile(
            strict_evidence, mart="strict_argmax"
        ).shape

        nullable = populations.witness_literal_rows(
            nullable_tables, relationships, nullable_shape
        )
        strict = populations.witness_literal_rows(
            strict_tables, relationships, strict_shape
        )

        # L is after the legacy A--K rows: every old anchor and bridge row
        # retains its prior bytes and position, including A's winner-last/F's
        # winner-first opposition.
        self.assertEqual(nullable["accounts"][:3], strict["accounts"][:3])
        self.assertEqual(nullable["events"][:4], strict["events"][:4])
        self.assertEqual(
            [(row["account_id"], row["amount"]) for row in nullable["events"][:4]],
            [(900, 10), (900, 40), (902, 25), (902, 25)],
        )
        self.assertNotIn(901, {row["account_id"] for row in nullable["events"]})

        row_l = [row for row in nullable["events"] if row["account_id"] == 903]
        self.assertEqual(len(row_l), 2)
        self.assertEqual([row["amount"] for row in row_l], [None, None])
        self.assertEqual([row["event_label"] for row in row_l], ["alpha", "beta"])
        self.assertEqual(len({row["event_id"] for row in row_l}), 2)

        nullable_conditions = populations.witness_conditions(
            nullable_shape, tables=nullable_tables
        )
        strict_conditions = populations.witness_conditions(
            strict_shape, tables=strict_tables
        )
        self.assertTrue(any("row L:" in line for line in nullable_conditions))
        expected_scope = populations._witness_scope_prefix(nullable_shape)
        self.assertTrue(
            all(line.startswith(expected_scope) for line in nullable_conditions)
        )
        self.assertTrue(
            any("events.amount value is NULL" in line for line in nullable_conditions)
        )
        self.assertFalse(any("row L:" in line for line in strict_conditions))

    def test_argmax_reference_distinguishes_childless_and_all_null_groups(self) -> None:
        tables, relationships, evidence = fixture(nullable=True)
        built = mart_plan.argmax_profile(evidence, mart="nullable_argmax")
        task = task_for(
            built, tables, relationships, "proof__nullable_measure_argmax"
        )
        self.assertEqual(populations.validate_population_coverage(task), [])

        name = built.column_named
        rows = {row[name("parent_key")]: row for row in run_counterfactual(task)}
        self.assertTrue({900, 901, 902, 903}.issubset(rows))
        self.assertEqual(
            (
                rows[900][name("top_measure")],
                rows[900][name("top_label")],
                rows[900][name("tied_count")],
                rows[900][name("child_count")],
                rows[900][name("tie_state")],
            ),
            (40, "beta", 1, 2, "unique"),
        )
        self.assertEqual(
            (
                rows[901][name("top_measure")],
                rows[901][name("top_label")],
                rows[901][name("tied_count")],
                rows[901][name("child_count")],
                rows[901][name("tie_state")],
            ),
            (0, "(none)", 0, 0, "empty"),
        )
        self.assertEqual(
            (
                rows[902][name("top_measure")],
                rows[902][name("top_label")],
                rows[902][name("tied_count")],
                rows[902][name("child_count")],
                rows[902][name("tie_state")],
            ),
            (25, "alpha", 2, 2, "tied"),
        )
        # Same numeric defaults/state as B, but a genuine winning row and two
        # real links: this is the branch the old counterfactual never reached.
        self.assertEqual(
            (
                rows[903][name("top_measure")],
                rows[903][name("top_label")],
                rows[903][name("tied_count")],
                rows[903][name("child_count")],
                rows[903][name("tie_state")],
            ),
            (0, "alpha", 0, 2, "empty"),
        )

    def test_measure_state_reference_counts_real_null_rows_as_absent(self) -> None:
        tables, relationships, evidence = fixture(nullable=True)
        built = mart_plan.measure_state_distribution(
            evidence, mart="nullable_measure_states"
        )
        task = task_for(
            built, tables, relationships, "proof__nullable_measure_states"
        )
        self.assertEqual(populations.validate_population_coverage(task), [])

        name = built.column_named
        rows = {
            (row[name("entity_key")], row[name("measure_state")]): row
            for row in run_counterfactual(task)
        }
        childless = rows[(901, "absent")]
        all_null = rows[(902, "absent")]
        self.assertEqual(childless[name("row_count")], 0)
        self.assertEqual(all_null[name("row_count")], 2)
        self.assertEqual(all_null[name("distinct_amount_count")], 0)
        self.assertEqual(all_null[name("total_amount")], 0)
        self.assertEqual(all_null[name("max_amount")], 0)
        self.assertEqual(all_null[name("max_amount_share")], 0.0)

    def test_measure_state_has_executable_distinct_measure_witness(self) -> None:
        tables, relationships, evidence = fixture(nullable=True)
        built = mart_plan.measure_state_distribution(
            evidence, mart="nullable_measure_states"
        )
        task = task_for(
            built, tables, relationships, "proof__distinct_measure_states"
        )
        counterfactual = task.population(PopulationName.COUNTERFACTUAL)
        self.assertTrue(
            any("row O:" in condition for condition in counterfactual.conditions)
        )

        grouped: dict[tuple[object, object], list[dict]] = {}
        for row in counterfactual.literal_rows["events"]:
            if row["amount"] is not None:
                grouped.setdefault((row["account_id"], row["amount"]), []).append(row)
        repeated = [rows for rows in grouped.values() if len(rows) > 1]
        self.assertEqual(len(repeated), 1)
        witness = repeated[0]
        self.assertEqual(len({row["event_id"] for row in witness}), 2)

        no_dedup = next(case for case in task.attack_cases if case.name == "no_dedup")
        self.assertEqual(
            no_dedup.expected_pass,
            {PopulationName.COUNTERFACTUAL: False},
        )
        reference_sql = reference.compile_plan_sql(task, task.marts[0])
        mutant_sql = attacks._apply_kind(
            AttackKind.NO_DEDUP, reference_sql, task.marts[0]
        )
        self.assertIsNotNone(mutant_sql)
        name = built.column_named
        reference_rows = {
            (row[name("entity_key")], row[name("measure_state")]): row
            for row in run_counterfactual_sql(task, reference_sql)
        }
        mutant_rows = {
            (row[name("entity_key")], row[name("measure_state")]): row
            for row in run_counterfactual_sql(task, mutant_sql or "")
        }
        witness_key = (witness[0]["account_id"], "present")
        distinct = name("distinct_amount_count")
        self.assertEqual(reference_rows[witness_key][distinct], 1)
        self.assertEqual(mutant_rows[witness_key][distinct], 2)

    def test_nonnullable_measure_has_no_row_l_and_still_validates(self) -> None:
        tables, relationships, evidence = fixture(nullable=False)
        for builder in (
            mart_plan.argmax_profile,
            mart_plan.measure_state_distribution,
        ):
            with self.subTest(builder=builder.__name__):
                built = builder(evidence, mart=f"strict_{builder.__name__}")
                task = task_for(
                    built,
                    tables,
                    relationships,
                    f"proof__strict_{builder.__name__}",
                )
                counterfactual = task.population(PopulationName.COUNTERFACTUAL)
                self.assertFalse(
                    any("row L:" in line for line in counterfactual.conditions)
                )
                self.assertFalse(
                    any(row["amount"] is None for row in counterfactual.literal_rows["events"])
                )
                self.assertEqual(populations.validate_population_coverage(task), [])

    def test_nonnullable_contract_omits_dead_missing_branches_but_attack_survives(
        self,
    ) -> None:
        tables, relationships, evidence = fixture(nullable=False)
        nullable_tables, nullable_relationships, nullable_evidence = fixture(
            nullable=True
        )
        for builder in (
            mart_plan.argmax_profile,
            mart_plan.measure_state_distribution,
        ):
            with self.subTest(builder=builder.__name__):
                built = builder(evidence, mart=f"strict_{builder.__name__}")
                task = task_for(
                    built,
                    tables,
                    relationships,
                    f"proof__strict_contract_{builder.__name__}",
                )
                contract = built.plan.model_dump_json() + " ".join(
                    column.description for column in built.columns
                )
                self.assertNotIn("no amount value", contract)
                self.assertNotIn("none of its rows carries a amount value", contract)
                self.assertNotIn("amount is missing", contract)
                if builder is mart_plan.argmax_profile:
                    self.assertIn("ordering measure is required", contract)
                    self.assertIn("no matching rows", contract)
                else:
                    self.assertIn("amount is required", contract)
                    self.assertIn("no-activity absent cell", contract)

                sql = reference.compile_plan_sql(task, task.marts[0])
                if builder is mart_plan.argmax_profile:
                    self.assertIn('"f_measure" DESC NULLS LAST', sql)
                else:
                    # Defensive SQL is unchanged: under a NOT NULL source
                    # contract this predicate selects only the LEFT-join
                    # placeholder for the childless parent.
                    self.assertIn('"link_amount" IS NULL', sql)

                # Nullability specializes only the public contract. Keep the
                # defensive SQL byte-for-byte identical so unknown/dirty source
                # data remains deterministic and childless groups retain their
                # defaults.
                nullable_built = builder(
                    nullable_evidence,
                    mart=built.plan.mart,
                )
                nullable_task = task_for(
                    nullable_built,
                    nullable_tables,
                    nullable_relationships,
                    f"proof__nullable_sql_{builder.__name__}",
                )
                self.assertEqual(
                    sql,
                    reference.compile_plan_sql(
                        nullable_task,
                        nullable_task.marts[0],
                    ),
                )
                mutant = attacks._apply_kind(
                    AttackKind.NO_NULL_DEFAULT,
                    sql,
                    task.marts[0],
                )
                self.assertIsNotNone(mutant)
                self.assertNotEqual(
                    run_counterfactual_sql(task, sql),
                    run_counterfactual_sql(task, mutant),
                    "the childless row must keep no_null_default attackable",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

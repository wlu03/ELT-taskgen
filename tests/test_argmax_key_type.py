"""The ARGMAX key's TYPE, and the two ways a hard-coded one lied.

WHY THIS FILE EXISTS
`argmax_profile` projects the extremal row's identifier (`top_row_id`) and
declared it ``ColumnType.BIGINT`` unconditionally. Nothing guaranteed that:
`ChainEvidence` carried no type for `bridge_key`, and `_countable_key` PREFERS
a numeric key but falls back to a non-numeric single-column primary key rather
than dropping the chain (dropping it deletes every chain on a text-keyed pool —
WikiDBs declares no primary key on any of its 12 reference-stage databases, and
SynSQL ships TEXT primary keys). The declared type selects the empty-group
COALESCE literal, so the lie showed up twice:

  * TEXT key  -> ``COALESCE(<varchar>, 0)`` -> DuckDB BinderException at bind
    time. That is the REFERENCE stage failing after generation, measured on
    ``synsql__3d_coordinate_system…`` and ``schemapile__…openbis…schema_186``,
    each after exhausting the 3-round repair budget. No repair round can fix a
    type the shape hard-codes.
  * DECIMAL/FLOAT/DATE key -> no entry in `_EXTREMUM_DEFAULTS` -> the builder
    emitted NO COALESCE at all, so the column silently shipped NULL while the
    shipped prose promised "0 when there are no rows".

THE BAR IS EXECUTION. Structure is not enough here: the TEXT defect is
invisible until DuckDB binds the COALESCE. Every case below compiles the
reference through the ONE compiler and RUNS it, then asserts the value in the
empty group is the value the shipped column DESCRIPTION promises.
"""

from __future__ import annotations

import dataclasses
import unittest

import duckdb

from elt_taskgen.generation import mart_plan as mp
from elt_taskgen.generation import populations as pops
from elt_taskgen.generation.source_data import generate_rows
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
from elt_taskgen.reference import solution as ref

SUB_STATUS = ("paid", "pending", "failed")

TABLES = (
    TableSpec(
        name="accounts",
        description="Customer accounts.",
        columns=(
            ColumnSpec(name="account_id", type=ColumnType.BIGINT),
            ColumnSpec(name="account_name", type=ColumnType.TEXT),
        ),
        primary_key=("account_id",),
    ),
    TableSpec(
        name="subscriptions",
        description="One row per subscription of an account.",
        columns=(
            # THREE candidate keys of three different types, which is the whole
            # point: a real pool hands us whichever one its DDL declares.
            ColumnSpec(name="subscription_id", type=ColumnType.BIGINT),
            ColumnSpec(name="subscription_ref", type=ColumnType.TEXT),
            ColumnSpec(name="subscription_seq", type=ColumnType.DECIMAL),
            ColumnSpec(name="signed_on", type=ColumnType.DATE),
            ColumnSpec(name="account_id", type=ColumnType.BIGINT, nullable=True),
            ColumnSpec(name="sub_status", type=ColumnType.TEXT, enum_values=SUB_STATUS),
            ColumnSpec(name="sub_label", type=ColumnType.TEXT),
            ColumnSpec(name="amount", type=ColumnType.INTEGER),
        ),
        primary_key=("subscription_id",),
    ),
)

RELATIONSHIPS = (
    Relationship(
        child_table="subscriptions",
        child_columns=("account_id",),
        parent_table="accounts",
        parent_columns=("account_id",),
        required=False,
    ),
)

BACKENDS = (
    BackendAssignment(table="accounts", backend=Backend.POSTGRES),
    BackendAssignment(table="subscriptions", backend=Backend.FILES),
)

EVIDENCE = mp.ChainEvidence(
    parent="accounts",
    parent_key="account_id",
    parent_attr="account_name",
    bridge="subscriptions",
    bridge_key="subscription_id",
    bridge_key_type=ColumnType.BIGINT,
    bridge_key_is_unique=True,
    bridge_parent_fk="account_id",
    bridge_status="sub_status",
    bridge_status_pass=("paid",),
    bridge_status_fail=("pending", "failed"),
    bridge_amount="amount",
    bridge_label="sub_label",
    owner_link_optional=True,
)

SCALE = {"accounts": 12, "subscriptions": 40}


def _task(built: mp.BuiltPlan, task_id: str) -> TaskIR:
    mart = MartSpec(
        name=built.plan.mart,
        description="argmax key-type proof mart.",
        grain="one row per account",
        key_columns=built.shape.key_columns,
        columns=built.columns,
        plan=built.plan,
    )
    populations, cases = pops.derive_populations_and_attacks(
        task_id=task_id,
        tables=TABLES,
        relationships=RELATIONSHIPS,
        shapes=(built.shape,),
        scale_hint=SCALE,
        backends=2,
    )
    return TaskIR(
        task_id=task_id,
        family_id="proof__argmax_key_type",
        cluster_id=task_id,
        origin=Origin.SYNTHETIC,
        license="CC0-1.0",
        tables=TABLES,
        relationships=RELATIONSHIPS,
        backends=BACKENDS,
        marts=(mart,),
        populations=populations,
        attack_cases=cases,
    )


def _run(key: str, key_type: ColumnType) -> tuple[str, str, list[dict]]:
    """(compiled SQL, top_row_id description, EXECUTED rows) for one key type."""
    evidence = dataclasses.replace(EVIDENCE, bridge_key=key, bridge_key_type=key_type)
    built = mp.argmax_profile(evidence, mart=f"argmax_{key_type.value}")
    task = _task(built, f"proof__argmax_{key_type.value}")
    sql = ref.compile_plan_sql(task, task.marts[0])
    description = next(c.description for c in built.columns if c.name == "top_row_id")
    con = duckdb.connect()
    # PRIMARY, not populations[0] (development): the empty-group literal is
    # only observable on a childless parent, and development's parent-first
    # coverage schedule gives EVERY parent a child by design; primary carves
    # childless parents out on purpose (childless_frac).
    rows = generate_rows(task, PopulationName.PRIMARY)
    for table in task.tables:
        ref.create_table(con, table)
        if rows.get(table.name):
            ref._insert_rows(con, table, rows[table.name])
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return sql, description, [dict(zip(cols, r)) for r in cur.fetchall()]


class ArgmaxKeyType(unittest.TestCase):
    def test_argmax_text_row_id_publishes_and_executes_exact_third_order(self) -> None:
        """The row-id tie-break has its own text collation contract.

        The label and measure are deliberately equal, so only the mixed-case
        text identifier can select the winner.  Under the stated stored-text
        order ``Zulu`` precedes ``alpha``; a case-insensitive interpretation
        would choose the other row.
        """
        evidence = dataclasses.replace(
            EVIDENCE,
            bridge_key="subscription_ref",
            bridge_key_type=ColumnType.TEXT,
        )
        built = mp.argmax_profile(evidence, mart="text_id_argmax")
        top_label = next(
            column for column in built.columns if column.name == "top_label"
        )
        extrema = next(
            op for op in built.plan.ops if op.kind.value == "extrema"
        )
        exact_order = f"the smallest subscription_ref {mp.TEXT_ORDER_PROSE}"
        self.assertIn(f"resolved by {exact_order}", top_label.description)
        self.assertIn(f"then {exact_order}", extrema.description)

        task = _task(built, "proof__argmax_text_id_order")
        sql = ref.compile_plan_sql(task, task.marts[0])
        self.assertIn('"f_id" ASC NULLS LAST', sql)
        rows = {
            "accounts": [{"account_id": 1, "account_name": "Account"}],
            "subscriptions": [
                {
                    "subscription_id": 1,
                    "subscription_ref": "alpha",
                    "subscription_seq": 1,
                    "signed_on": "2026-01-01",
                    "account_id": 1,
                    "sub_status": "paid",
                    "sub_label": "same",
                    "amount": 10,
                },
                {
                    "subscription_id": 2,
                    "subscription_ref": "Zulu",
                    "subscription_seq": 2,
                    "signed_on": "2026-01-02",
                    "account_id": 1,
                    "sub_status": "paid",
                    "sub_label": "same",
                    "amount": 10,
                },
            ],
        }
        con = duckdb.connect()
        try:
            for table in task.tables:
                ref.create_table(con, table)
                ref._insert_rows(con, table, rows[table.name])
            cursor = con.execute(sql)
            columns = [description[0] for description in cursor.description]
            result = [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            con.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["top_row_id"], "Zulu")

    def test_snapshot_text_key_publishes_its_exact_tie_break_order(self) -> None:
        evidence = dataclasses.replace(
            EVIDENCE,
            bridge_key="subscription_ref",
            bridge_key_type=ColumnType.TEXT,
            bridge_timestamp="signed_on",
        )
        built = mp.latest_snapshot(evidence, mart="text_snapshot")
        latest = next(
            column for column in built.columns if column.name == "latest_row_id"
        )
        self.assertIn(mp.TEXT_ORDER_PROSE, latest.description)
        extrema = next(
            op for op in built.plan.ops if op.kind.value == "extrema"
        )
        self.assertIn(mp.TEXT_ORDER_PROSE, extrema.description)

    def test_a_text_bridge_key_binds_and_reports_the_literal_it_promises(self) -> None:
        sql, description, rows = _run("subscription_ref", ColumnType.TEXT)
        self.assertIn("COALESCE(\"top_row_id\", '(none)')", sql)
        self.assertIn("the literal '(none)' when there are no rows", description)
        empty = [r for r in rows if r["child_count"] == 0]
        self.assertTrue(empty, "the population must contain a childless parent")
        for row in empty:
            self.assertEqual(row["top_row_id"], "(none)")

    def test_a_decimal_bridge_key_emits_a_coalesce_instead_of_a_null(self) -> None:
        sql, description, rows = _run("subscription_seq", ColumnType.DECIMAL)
        self.assertIn('COALESCE("top_row_id", 0)', sql)
        self.assertIn("0 when there are no rows", description)
        empty = [r for r in rows if r["child_count"] == 0]
        self.assertTrue(empty, "the population must contain a childless parent")
        for row in empty:
            self.assertIsNotNone(row["top_row_id"])
            self.assertEqual(float(row["top_row_id"]), 0.0)

    def test_the_numeric_key_still_reports_zero(self) -> None:
        """The pre-existing behaviour, pinned: the fix must not move it."""
        sql, description, rows = _run("subscription_id", ColumnType.BIGINT)
        self.assertIn('COALESCE("top_row_id", 0)', sql)
        self.assertIn("0 when there are no rows", description)
        for row in (r for r in rows if r["child_count"] == 0):
            self.assertEqual(row["top_row_id"], 0)

    def test_the_declared_mart_column_type_is_the_bridge_keys_own_type(self) -> None:
        """What the solver is SHOWN, not only what the SQL binds.

        `top_row_id` was declared `bigint` for every key, so the exported
        schema told the solver "bigint" for a column that ships VARCHAR.
        """
        for key, key_type in (
            ("subscription_ref", ColumnType.TEXT),
            ("subscription_seq", ColumnType.DECIMAL),
            ("subscription_id", ColumnType.BIGINT),
        ):
            with self.subTest(type=key_type.value):
                evidence = dataclasses.replace(
                    EVIDENCE, bridge_key=key, bridge_key_type=key_type
                )
                built = mp.argmax_profile(evidence, mart="argmax_types")
                column = next(c for c in built.columns if c.name == "top_row_id")
                self.assertEqual(column.type, key_type)

    def test_the_description_is_derived_from_the_emitted_default(self) -> None:
        """The prose and the literal come from ONE table, so they cannot drift."""
        for key, key_type in (
            ("subscription_ref", ColumnType.TEXT),
            ("subscription_seq", ColumnType.DECIMAL),
            ("subscription_id", ColumnType.BIGINT),
        ):
            with self.subTest(type=key_type.value):
                sql, description, _rows = _run(key, key_type)
                literal = mp.extremum_default(key_type)
                self.assertIn(f'COALESCE("top_row_id", {literal})', sql)
                self.assertIn(mp._default_prose(key_type), description)

    def test_a_key_type_with_no_declared_default_fails_closed(self) -> None:
        """A DATE key has no honest zero, so the shape declines the chain.

        The old behaviour was to emit no COALESCE and ship a NULL column under
        a description promising a value — strictly worse than refusing.
        """
        with self.assertRaises(ValueError) as ctx:
            _run("signed_on", ColumnType.DATE)
        self.assertIn("no declared empty-group default", str(ctx.exception))

    def test_an_extremum_cannot_be_constructed_with_an_undefaulted_type(self) -> None:
        with self.assertRaises(ValueError):
            mp.Extremum(
                column="top_row_id",
                source="f_id",
                type=ColumnType.DATE,
                description="no default exists for this type",
            )


class BridgeKeyTypeEvidence(unittest.TestCase):
    """The type must travel WITH the key, from the schema, per pool."""

    def test_chain_candidates_read_the_key_type_off_the_schema(self) -> None:
        from elt_taskgen.adapters.evidence import chain_candidates

        text_keyed = (
            TableSpec(
                name="components",
                description="Components, keyed by a TEXT id (SynSQL ships these).",
                columns=(
                    ColumnSpec(name="component_id", type=ColumnType.TEXT),
                    ColumnSpec(name="component_name", type=ColumnType.TEXT),
                    ColumnSpec(name="project_id", type=ColumnType.BIGINT, nullable=True),
                    ColumnSpec(name="weight", type=ColumnType.INTEGER),
                ),
                primary_key=("component_id",),
            ),
            TableSpec(
                name="projects",
                description="Projects.",
                columns=(
                    ColumnSpec(name="project_id", type=ColumnType.BIGINT),
                    ColumnSpec(name="project_name", type=ColumnType.TEXT),
                ),
                primary_key=("project_id",),
            ),
        )
        rels = (
            Relationship(
                child_table="components",
                child_columns=("project_id",),
                parent_table="projects",
                parent_columns=("project_id",),
                required=False,
            ),
        )
        candidates = chain_candidates(text_keyed, rels)
        self.assertTrue(candidates)
        chain = candidates[0]
        # The declared TEXT PK `component_id` beats the NOT NULL measure
        # `weight` as the countable key (a measure is never the row key) —
        # and the point stands: whatever it picks, the TYPE is read off that
        # column rather than assumed.
        self.assertEqual(chain.bridge_key, "component_id")
        expected = text_keyed[0].column(chain.bridge_key).type
        self.assertEqual(chain.bridge_key_type, expected)
        self.assertEqual(chain.bridge_amount, "weight")

    def test_a_text_pk_chain_is_kept_and_typed_rather_than_dropped(self) -> None:
        """Declining the chain would delete the tasks the fix exists to recover."""
        from elt_taskgen.adapters.evidence import chain_candidates

        tables = (
            TableSpec(
                name="components",
                description="No numeric column at all: the key can only be TEXT.",
                columns=(
                    ColumnSpec(name="component_id", type=ColumnType.TEXT),
                    ColumnSpec(name="component_name", type=ColumnType.TEXT),
                    ColumnSpec(name="project_id", type=ColumnType.TEXT, nullable=True),
                ),
                primary_key=("component_id",),
            ),
            TableSpec(
                name="projects",
                description="Projects.",
                columns=(
                    ColumnSpec(name="project_id", type=ColumnType.TEXT),
                    ColumnSpec(name="project_name", type=ColumnType.TEXT),
                ),
                primary_key=("project_id",),
            ),
        )
        rels = (
            Relationship(
                child_table="components",
                child_columns=("project_id",),
                parent_table="projects",
                parent_columns=("project_id",),
                required=False,
            ),
        )
        candidates = chain_candidates(tables, rels)
        self.assertTrue(candidates, "a text-keyed chain must survive selection")
        self.assertEqual(candidates[0].bridge_key, "component_id")
        self.assertEqual(candidates[0].bridge_key_type, ColumnType.TEXT)


class StarGrainMustBeAKey(unittest.TestCase):
    """Track A, second line of defence: a star mart grains on a KEY.

    The grain is projected without DISTINCT, so a non-key grain duplicates, the
    LEFT JOIN multiplies the fact rows, and GROUP BY hides the duplication in
    the output while every measure stays inflated — measured on 16 of 17
    wikidbs marts, whose grain column the adapter's OWN uniqueness derivation
    had already declined to call a key.
    """

    def _build(self, *, parent_key_columns):
        return mp.build_star(
            mart="star_mart",
            parent="accounts",
            parent_keys=("account_name",),
            key_columns=("account_name",),
            joins=(
                mp.StarJoin(
                    table="subscriptions",
                    on_pairs=(("account_name", "account_id"),),
                    carry=(("amount", "f_amount"),),
                    rel_columns=("account_id", "account_id"),
                    description="LEFT JOIN subscriptions onto accounts.",
                ),
            ),
            measures=(
                mp.Measure(column="sub_count", expr='COUNT("f_amount")'),
            ),
            parent_key_columns=parent_key_columns,
        )

    def test_a_non_key_grain_is_refused_and_the_error_names_the_join(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._build(parent_key_columns=frozenset({"account_id"}))
        message = str(ctx.exception)
        self.assertIn("account_name", message)
        self.assertIn("not a key", message)
        self.assertIn("subscriptions", message)

    def test_a_key_grain_still_builds(self) -> None:
        built = self._build(parent_key_columns=frozenset({"account_id", "account_name"}))
        self.assertEqual(built.plan.mart, "star_mart")

    def test_no_declared_keys_means_no_check(self) -> None:
        """Back-compatible: a caller that cannot answer the question is not lied to."""
        built = self._build(parent_key_columns=None)
        self.assertEqual(built.plan.mart, "star_mart")


if __name__ == "__main__":
    unittest.main()

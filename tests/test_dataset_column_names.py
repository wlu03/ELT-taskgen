"""Mart columns are named after the chain they are built from.

The plan library named every mart column after its ROLE in the template
(`parent_key`, `link_count`, `total_amount`), so 50 generated tasks shared 81
column names over 1030 columns — 8% distinct, against 65% in the ELT-Bench
anchors — and the descriptions repeated because they quote those names
(batch50, 2026-09-19). A name is built from the chain's own nouns instead, and
the rename reaches the ops and the compiled SQL, not just the contract.
"""

from __future__ import annotations

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

RELEASE_STATE = ("released", "delayed", "shelved")
STUDIO_TIER = ("independent", "major", "boutique", "defunct")

TABLES = (
    TableSpec(
        name="studios",
        description="Film studios.",
        # A key spelled `id` is the case the naming rule exists for: it names
        # nothing on its own, and every such task would share the name.
        columns=(
            ColumnSpec(name="id", type=ColumnType.BIGINT),
            ColumnSpec(name="name", type=ColumnType.TEXT),
            ColumnSpec(
                name="studio_tier", type=ColumnType.TEXT, enum_values=STUDIO_TIER
            ),
        ),
        primary_key=("id",),
    ),
    TableSpec(
        name="films",
        description="One row per film a studio released.",
        columns=(
            ColumnSpec(name="film_id", type=ColumnType.BIGINT),
            ColumnSpec(name="studio_id", type=ColumnType.BIGINT, nullable=True),
            ColumnSpec(name="genre_id", type=ColumnType.BIGINT, nullable=True),
            ColumnSpec(
                name="release_state", type=ColumnType.TEXT, enum_values=RELEASE_STATE
            ),
            ColumnSpec(name="title", type=ColumnType.TEXT),
            ColumnSpec(name="runtime_minutes", type=ColumnType.INTEGER),
            ColumnSpec(name="released_on", type=ColumnType.DATE),
        ),
        primary_key=("film_id",),
    ),
    TableSpec(
        name="genres",
        description="Film genres.",
        columns=(
            ColumnSpec(name="genre_id", type=ColumnType.BIGINT),
            ColumnSpec(name="genre_name", type=ColumnType.TEXT),
        ),
        primary_key=("genre_id",),
    ),
)

RELATIONSHIPS = (
    Relationship(
        child_table="films",
        child_columns=("studio_id",),
        parent_table="studios",
        parent_columns=("id",),
        required=False,
    ),
    Relationship(
        child_table="films",
        child_columns=("genre_id",),
        parent_table="genres",
        parent_columns=("genre_id",),
        required=False,
    ),
)

BACKENDS = (
    BackendAssignment(table="studios", backend=Backend.POSTGRES),
    BackendAssignment(table="films", backend=Backend.POSTGRES),
    BackendAssignment(table="genres", backend=Backend.FILES),
)

EVIDENCE = mp.ChainEvidence(
    parent="studios",
    parent_key="id",
    parent_attr="name",
    parent_domain_column="studio_tier",
    domain=STUDIO_TIER,
    out_of_domain="defunct",
    bridge="films",
    bridge_key="film_id",
    bridge_key_is_unique=True,
    bridge_parent_fk="studio_id",
    bridge_child_fk="genre_id",
    bridge_status="release_state",
    bridge_status_pass=("released",),
    bridge_status_fail=("delayed", "shelved"),
    bridge_amount="runtime_minutes",
    bridge_label="title",
    bridge_timestamp="released_on",
    child="genres",
    child_key="genre_id",
    child_label="genre_name",
    child_link_optional=True,
    owner_link_optional=True,
)

SCALE = {"studios": 12, "films": 40, "genres": 4}

#: Names that say only what the TEMPLATE did. None of them may survive on a
#: chain that supplies the parts, whichever shape built the mart.
TEMPLATE_NAMES = frozenset(
    {
        "parent_key",
        "parent_name",
        "entity_key",
        "entity_name",
        "owner_name",
        "link_count",
        "event_count",
        "child_count",
        "row_count",
        "matched_count",
        "orphan_count",
        "active_link_count",
        "active_event_count",
        "distinct_child_count",
        "distinct_status_count",
        "distinct_amount_count",
        "tied_count",
        "total_amount",
        "total_measure",
        "lifetime_amount",
        "period_amount",
        "max_amount",
        "active_amount",
        "matched_amount",
        "latest_amount",
        "top_measure",
        "running_amount",
        "prev_period_amount",
        "top_label",
        "top_row_id",
        "latest_label",
        "latest_row_id",
        "latest_status",
        "status_group",
        "measure_state",
        "cohort",
        "tie_state",
        "active_amount_ratio",
        "top_measure_share",
        "latest_amount_share",
        "max_amount_share",
        "period_share",
        "passing_ratio",
        "match_rate",
        "size_band",
        "adoption_band",
        "coverage_band",
        "trend",
        "has_links",
    }
)

SHAPES = mp.registered_shape_builders()


def build_task(built: mp.BuiltPlan, task_id: str) -> TaskIR:
    mart = MartSpec(
        name=built.plan.mart,
        description=f"{built.shape.shape_name} mart.",
        grain=built.shape.shape_name + " grain",
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
        family_id="proof__dataset_names",
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


class EveryShapeNamesItsColumnsAfterTheChain(unittest.TestCase):
    def setUp(self) -> None:
        self.built = {
            name: builder(EVIDENCE, mart=f"{name}_mart") for name, builder in SHAPES
        }

    def test_no_mart_column_keeps_a_template_name(self) -> None:
        for name, built in self.built.items():
            with self.subTest(shape=name):
                kept = [
                    column.name
                    for column in built.columns
                    if column.name in TEMPLATE_NAMES
                ]
                self.assertEqual([], kept)

    def test_the_names_come_from_this_chain(self) -> None:
        """The nouns are the chain's: tables, the measure, the status column."""
        names = {column.name for column in self.built["fan_out_rollup"].columns}
        self.assertIn("studio_id", names)  # `studios.id` qualified by its table
        self.assertIn("studio_name", names)
        self.assertIn("films_count", names)
        self.assertIn("distinct_genres_count", names)
        self.assertIn("total_runtime_minutes", names)
        self.assertIn("release_state_films_count", names)
        self.assertIn("has_films", names)

    def test_a_union_shape_is_renamed_too(self) -> None:
        """These two assemble their own ops instead of calling `build_rollup`,
        and they were 54 of the corpus's 116 marts."""
        cohorts = {column.name for column in self.built["status_cohort_union"].columns}
        self.assertIn("studio_id", cohorts)
        self.assertIn("release_state_cohort", cohorts)
        self.assertIn("total_runtime_minutes", cohorts)
        distribution = {
            column.name for column in self.built["measure_state_distribution"].columns
        }
        self.assertIn("runtime_minutes_state", distribution)
        self.assertIn("films_count", distribution)

    def test_every_mart_column_name_is_unique(self) -> None:
        for name, built in self.built.items():
            with self.subTest(shape=name):
                names = [column.name for column in built.columns]
                self.assertEqual(sorted(set(names)), sorted(names))

    def test_the_contract_and_the_ops_agree(self) -> None:
        """A rename that moved the contract but not the ops would compile to a
        mart whose columns are the old names."""
        for name, built in self.built.items():
            with self.subTest(shape=name):
                task = build_task(built, f"proof__{name}_names")
                self.assertEqual([], mp.validate_plan(task, built.plan))
                sql = ref.compile_plan_sql(task, task.marts[0])
                for column in built.columns:
                    self.assertIn(f'"{column.name}"', sql)

    def test_the_renamed_sql_executes_and_returns_the_new_columns(self) -> None:
        for name, built in self.built.items():
            with self.subTest(shape=name):
                task = build_task(built, f"proof__{name}_exec")
                sql = ref.compile_plan_sql(task, task.marts[0])
                rows = generate_rows(task, PopulationName.PRIMARY)
                con = duckdb.connect(":memory:")
                try:
                    for table in task.tables:
                        ref.create_table(con, table)
                        payload = rows.get(table.name) or []
                        if payload:
                            ref._insert_rows(con, table, payload)
                    cursor = con.execute(sql)
                    produced = [d[0] for d in cursor.description]
                    cursor.fetchall()
                finally:
                    con.close()
                self.assertEqual([c.name for c in built.columns], produced)

    def test_a_shape_reports_what_it_named_each_template_column(self) -> None:
        built = self.built["fan_out_rollup"]
        self.assertEqual("films_count", built.column_named("link_count"))
        self.assertEqual("studio_id", built.column_named("parent_key"))
        # An unknown template name is returned unchanged, never guessed at.
        self.assertEqual("not_a_column", built.column_named("not_a_column"))


class NamesDegradeToTheTemplate(unittest.TestCase):
    """A half-named column reads worse than a plain one: when the chain does
    not supply a part, the template name stands."""

    def test_a_measure_already_named_amount_keeps_total_amount(self) -> None:
        evidence = mp.ChainEvidence(
            parent="studios",
            parent_key="studio_id",
            parent_attr="studio_name",
            parent_domain_column="studio_tier",
            domain=STUDIO_TIER,
            out_of_domain="defunct",
            bridge="films",
            bridge_key="film_id",
            bridge_parent_fk="studio_id",
            bridge_status="release_state",
            bridge_status_pass=("released",),
            bridge_status_fail=("delayed",),
            bridge_amount="amount",
        )
        built = mp.categorical_ladder(evidence, mart="ladder")
        names = {column.name for column in built.columns}
        # "total_" + "amount" IS the template name, so nothing was gained.
        self.assertIn("studio_id", names)
        self.assertEqual("studio_id", built.column_named("parent_key"))
        self.assertEqual("total_amount", built.column_named("total_amount"))

    def test_a_chain_whose_nouns_are_missing_is_left_alone(self) -> None:
        names = mp._dataset_column_names(
            ["link_count", "total_amount"], mp._NameEvidence(parent="studios")
        )
        self.assertEqual({}, names)

    def test_a_generic_column_is_qualified_only_when_it_is_generic(self) -> None:
        generic = mp._GENERIC_KEY_COLUMNS
        self.assertEqual("studio_id", mp._qualified("id", "studios", generic))
        self.assertEqual("company_id", mp._qualified("id", "companies", generic))
        self.assertEqual("account_id", mp._qualified("account_id", "accounts", generic))
        # Already carrying its table's name: no `studio_studio_id`.
        self.assertEqual("studio_id", mp._qualified("studio_id", "studios", generic))


if __name__ == "__main__":
    unittest.main()

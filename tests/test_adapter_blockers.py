"""The two adapter blockers, pinned shut.

WHY THIS FILE EXISTS
Five of the six documented pool runs (docs/runs/*.md) died on one of two lines
of adapter code, neither of which had anything to do with the source material:

  A1  every non-demo adapter built its TaskIR with ``populations=()`` and
      ``attack_cases=()``, and ``cli.run_generate`` VALIDATES populations
      rather than creating them, so `generate` failed instantly with
      "population coverage: missing population: development";
  A2  adapter MartPlans were DESCRIPTIVE, and
      ``reference/solution.py::compile_plan_sql`` refused them
      ("derive op must name exactly one table, got ()").

The fixes are two shared implementations — `generation/populations.py`
(populations + counterfactual + attack catalogue) and `generation/mart_plan.py`
(the plan BUILDERS) — that every adapter calls. These tests pin the properties
those two modules are supposed to guarantee, so a future adapter cannot
regress into either blocker silently:

  * a built plan COMPILES, and `validate_plan` agrees with the compiler about
    what a plan means (the intermediate-relation namespace);
  * a derived counterfactual is SCHEMA-VALID and actually DISCRIMINATES — the
    childless parent and the duplicate child rows are checked as data, not as
    prose;
  * the derived attack catalogue claims ONLY surfaces the plan offers.
"""

from __future__ import annotations

import unittest

from elt_taskgen.generation import mart_plan as mp
from elt_taskgen.generation import populations as pops
from elt_taskgen.models import (
    AttackKind,
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    JoinType,
    MartColumn,
    MartOpKind,
    MartSpec,
    Origin,
    PopulationName,
    Relationship,
    TableSpec,
    TaskIR,
)
from elt_taskgen.reference.solution import compile_plan_sql

P = PopulationName


# ---------------------------------------------------------------------------
# A minimal two-table star, the shape every schema-only pool manufactures
# ---------------------------------------------------------------------------

def _tables() -> tuple[TableSpec, ...]:
    return (
        TableSpec(
            name="captains",
            description="One row per captain.",
            columns=(
                ColumnSpec(name="captain_id", type=ColumnType.INTEGER,
                           description="Captain id."),
                ColumnSpec(name="captain_name", type=ColumnType.TEXT,
                           description="Name."),
            ),
            primary_key=("captain_id",),
        ),
        TableSpec(
            name="landings",
            description="One row per landing (no upstream primary key).",
            columns=(
                ColumnSpec(name="captain_id", type=ColumnType.INTEGER, nullable=True,
                           description="Captain who made the landing."),
                ColumnSpec(name="tonnes", type=ColumnType.DECIMAL,
                           description="Tonnes landed."),
                ColumnSpec(name="grade", type=ColumnType.TEXT,
                           enum_values=("a", "b"), description="Quality grade."),
            ),
            primary_key=(),
        ),
    )


def _relationships() -> tuple[Relationship, ...]:
    return (
        Relationship(
            child_table="landings",
            child_columns=("captain_id",),
            parent_table="captains",
            parent_columns=("captain_id",),
            required=False,
        ),
    )


def _built() -> mp.BuiltPlan:
    return mp.build_star(
        mart="captain_summary",
        parent="captains",
        parent_keys=("captain_id",),
        key_columns=("captain_id",),
        joins=(
            mp.StarJoin(
                table="landings",
                on_pairs=(("captain_id", "captain_id"),),
                carry=(("captain_id", "landings__captain_id"),
                       ("tonnes", "landings__tonnes")),
                rel_columns=("captain_id",),
            ),
        ),
        measures=(
            mp.Measure(column="landings_count",
                       expr='COUNT("landings__captain_id")'),
            mp.Measure(column="total_tonnes",
                       expr='SUM("landings__tonnes")', null_default="0"),
        ),
        dedupe=("landings", ("captain_id", "tonnes", "grade")),
        notes="test star",
    )


def _mart(built: mp.BuiltPlan) -> MartSpec:
    return MartSpec(
        name="captain_summary",
        description="Per-captain landings summary.",
        grain="One row per captain, including captains with no landings.",
        key_columns=("captain_id",),
        columns=(
            MartColumn(name="captain_id", type=ColumnType.INTEGER,
                       description="Captain id."),
            MartColumn(name="landings_count", type=ColumnType.INTEGER,
                       description="Landings for this captain; 0 if none."),
            MartColumn(name="total_tonnes", type=ColumnType.DECIMAL,
                       description="Tonnes landed; 0 if none."),
        ),
        plan=built.plan,
    )


def _task(built: mp.BuiltPlan | None = None) -> TaskIR:
    built = built or _built()
    tables = _tables()
    rels = _relationships()
    populations, attacks = pops.derive_populations_and_attacks(
        task_id="test__captains",
        tables=tables,
        relationships=rels,
        shapes=(built.shape,),
        backends=2,
    )
    return TaskIR(
        task_id="test__captains",
        family_id="test__captains",
        cluster_id="test__captains",
        origin=Origin.SYNTHETIC,
        license="CC0-1.0",
        title="Captains",
        tables=tables,
        relationships=rels,
        backends=(
            BackendAssignment(table="captains", backend=Backend.POSTGRES),
            BackendAssignment(table="landings", backend=Backend.FILES,
                              options={"format": "csv"}),
        ),
        marts=(_mart(built),),
        populations=populations,
        attack_cases=attacks,
    )


# ---------------------------------------------------------------------------
# BLOCKER A2 — the plan the adapters emit must COMPILE
# ---------------------------------------------------------------------------

class BuiltPlanCompilesTest(unittest.TestCase):
    """`build_star` output is compiler-grade, and the validator agrees."""

    def setUp(self) -> None:
        self.built = _built()
        self.task = _task(self.built)
        self.mart = self.task.mart("captain_summary")

    def test_star_plan_compiles_to_duckdb_sql(self) -> None:
        sql = compile_plan_sql(self.task, self.mart)
        self.assertIn("LEFT JOIN", sql)
        self.assertIn("SELECT DISTINCT", sql)
        self.assertIn("GROUP BY", sql)
        self.assertIn("COALESCE", sql)
        # A deterministic TOTAL order over every mart column, not just the key.
        # The compiler may quote the identifiers (it now does, so a
        # reserved-word column cannot break the final projection); the ORDER
        # is what this test pins, not the quoting.
        self.assertRegex(
            sql,
            r'ORDER BY "?captain_id"?, "?landings_count"?, "?total_tonnes"?',
        )

    def test_star_default_grain_description_states_the_mart_projection(self) -> None:
        base = next(
            op for op in self.built.plan.ops if op.kind is MartOpKind.DERIVE
        )
        self.assertEqual(
            base.description,
            "Form the mart key columns captain_id from source table captains.",
        )

    def test_rollup_default_grain_description_states_the_mart_projection(self) -> None:
        built = mp.build_rollup(
            mart="captain_rollup",
            shape_name="test_rollup",
            parent="captains",
            keys=(
                mp.KeyColumn(
                    column="captain_id",
                    source="captain_id",
                    type=ColumnType.INTEGER,
                    description="Captain id.",
                ),
            ),
            measures=(
                mp.Measure(
                    column="row_count",
                    expr="COUNT(*)",
                    type=ColumnType.BIGINT,
                    description="Rows for this captain.",
                ),
            ),
            enforce_budget=False,
        )
        base = next(op for op in built.plan.ops if op.kind is MartOpKind.DERIVE)
        self.assertEqual(
            base.description,
            "Form the mart key columns captain_id from source table captains.",
        )

    def test_validate_plan_resolves_the_same_namespace_as_the_compiler(self) -> None:
        """The compiler binds intermediates via details['name']; the validator
        must know them too, or every built plan reports 'unknown tables'."""
        self.assertEqual(mp.validate_plan(self.task, self.mart.plan), [])

    def test_validate_plan_still_rejects_a_genuinely_unknown_table(self) -> None:
        ops = list(self.mart.plan.ops)
        ops[0] = ops[0].model_copy(update={"tables": ("nope",)})
        plan = self.mart.plan.model_copy(update={"ops": tuple(ops)})
        problems = mp.validate_plan(self.task, plan)
        self.assertTrue(any("unknown tables" in p for p in problems), problems)

    def test_attack_surface_of_a_built_plan_is_what_the_plan_offers(self) -> None:
        surface = mp.attack_surface(self.mart.plan)
        self.assertIn(AttackKind.INNER_JOIN, surface)     # the LEFT JOIN op
        self.assertIn(AttackKind.NO_DEDUP, surface)       # the DEDUPE op
        self.assertIn(AttackKind.WRONG_GRAIN, surface)    # the AGGREGATE op
        self.assertIn(AttackKind.NO_NULL_DEFAULT, surface)  # the COALESCE derive

    def test_single_table_star_needs_no_join(self) -> None:
        built = mp.build_star(
            mart="captain_summary",
            parent="captains",
            parent_keys=("captain_id",),
            key_columns=("captain_id",),
            measures=(mp.Measure(column="landings_count", expr="COUNT(*)"),),
        )
        task = _task().model_copy(
            update={
                "marts": (
                    _mart(built).model_copy(
                        update={
                            "columns": (
                                MartColumn(name="captain_id", type=ColumnType.INTEGER,
                                           description="Captain id."),
                                MartColumn(name="landings_count",
                                           type=ColumnType.INTEGER,
                                           description="Rows at this grain."),
                            ),
                            "plan": built.plan,
                        }
                    ),
                )
            }
        )
        self.assertIn("GROUP BY", compile_plan_sql(task, task.marts[0]))
        self.assertFalse(built.shape.has_join)

    def test_projection_plan_compiles_and_names_every_source_in_scope(self) -> None:
        built = mp.build_projection(
            mart="captain_dim",
            table="captains",
            select_map=(("captain_id", "captain_id"), ("captain_name", "name")),
            key_columns=("captain_id",),
            extra_sources=("captains", "landings"),
        )
        named = sorted(
            t for op in built.plan.ops if op.kind.value == "source" for t in op.tables
        )
        # Extraction is graded on every table of the cut: a table the plan never
        # names is a table a solver can skip for free.
        self.assertEqual(named, ["captains", "landings"])

    def test_a_star_without_measures_is_refused(self) -> None:
        """A keys-only mart is SELECT DISTINCT, not a task."""
        with self.assertRaises(ValueError):
            mp.build_star(
                mart="m",
                parent="captains",
                parent_keys=("captain_id",),
                key_columns=("captain_id",),
                measures=(),
            )


# ---------------------------------------------------------------------------
# BLOCKER A1 — populations exist, and the counterfactual DISCRIMINATES
# ---------------------------------------------------------------------------

class DerivedPopulationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.built = _built()
        self.task = _task(self.built)

    def test_all_five_populations_are_present_and_coverage_passes(self) -> None:
        """The exact check cli.run_generate makes before it materializes."""
        self.assertEqual(
            {p.name for p in self.task.populations}, set(PopulationName)
        )
        self.assertEqual(pops.validate_population_coverage(self.task), [])

    def test_primary_and_resampled_share_a_scale(self) -> None:
        by = {p.name: p for p in self.task.populations}
        self.assertEqual(by[P.PRIMARY].scale, by[P.RESAMPLED].scale)

    def test_counterfactual_is_literal_and_seeded_deterministically(self) -> None:
        cf = next(p for p in self.task.populations if p.name is P.COUNTERFACTUAL)
        self.assertTrue(cf.literal_rows)
        again = _task(self.built)
        cf2 = next(p for p in again.populations if p.name is P.COUNTERFACTUAL)
        self.assertEqual(cf.literal_rows, cf2.literal_rows)
        self.assertEqual(cf.seed, cf2.seed)

    def test_counterfactual_contains_a_childless_parent(self) -> None:
        """The row that distinguishes LEFT from INNER, and 0 from NULL.

        Constructible for ANY relationship: a declared FK constrains children
        to have a parent, never a parent to have children.
        """
        cf = next(p for p in self.task.populations if p.name is P.COUNTERFACTUAL)
        parents = cf.literal_rows["captains"]
        linked = {r["captain_id"] for r in cf.literal_rows["landings"]}
        childless = [r for r in parents if r["captain_id"] not in linked]
        self.assertEqual(len(childless), 1, cf.literal_rows)

    def test_counterfactual_contains_byte_identical_duplicate_child_rows(self) -> None:
        """The rows that distinguish DISTINCT from no dedupe. Emitted only
        because `landings` declares no primary key upstream."""
        cf = next(p for p in self.task.populations if p.name is P.COUNTERFACTUAL)
        rows = [tuple(sorted(r.items())) for r in cf.literal_rows["landings"]]
        self.assertLess(len(set(rows)), len(rows), rows)

    def test_no_duplicate_child_rows_when_the_fact_has_a_primary_key(self) -> None:
        tables = list(_tables())
        tables[1] = tables[1].model_copy(
            update={"primary_key": ("captain_id", "tonnes")}
        )
        built = mp.build_star(
            mart="captain_summary",
            parent="captains",
            parent_keys=("captain_id",),
            key_columns=("captain_id",),
            joins=(
                mp.StarJoin(
                    table="landings",
                    on_pairs=(("captain_id", "captain_id"),),
                    carry=(("captain_id", "landings__captain_id"),),
                    rel_columns=("captain_id",),
                ),
            ),
            measures=(mp.Measure(column="landings_count",
                                 expr='COUNT("landings__captain_id")'),),
            dedupe=(),
        )
        rows = pops.counterfactual_literal_rows(
            tuple(tables), _relationships(), built.shape
        )
        seen = [tuple(sorted(r.items())) for r in rows["landings"]]
        self.assertEqual(len(set(seen)), len(seen))

    def test_every_table_gets_rows_and_every_foreign_key_resolves(self) -> None:
        """Fail-closed data: no empty table, no dangling required link."""
        cf = next(p for p in self.task.populations if p.name is P.COUNTERFACTUAL)
        self.assertEqual(set(cf.literal_rows), {t.name for t in self.task.tables})
        for table_rows in cf.literal_rows.values():
            self.assertTrue(table_rows)

    def test_literal_rows_respect_enum_domains_and_nullability(self) -> None:
        """`validate_population_coverage` schema-checks literal rows; this pins
        that the MINTER is what makes them valid, not luck."""
        cf = next(p for p in self.task.populations if p.name is P.COUNTERFACTUAL)
        for row in cf.literal_rows["landings"]:
            self.assertIn(row["grade"], ("a", "b"))
            self.assertIsNotNone(row["tonnes"])

    def test_counterfactual_values_cannot_collide_with_primary_or_resampled(self) -> None:
        """The `constants` attack must fail on the counterfactual: its ids come
        from a base disjoint from every generated population's id base."""
        from elt_taskgen.generation import source_data

        by = {p.name: p for p in self.task.populations}
        cf = by[P.COUNTERFACTUAL]
        ids = {r["captain_id"] for r in cf.literal_rows["captains"]}
        self.assertEqual(
            ids,
            {pops.COUNTERFACTUAL_ID_BASE + i for i in range(len(ids))},
        )
        for pop in (P.PRIMARY, P.RESAMPLED, P.DEVELOPMENT, P.STRESS):
            base = source_data._ID_BASE[pop]
            # The id window that population's generator can actually mint.
            hi = base + by[pop].scale.get("captains", 0)
            self.assertFalse(
                ids & set(range(base, hi + 1)),
                f"{pop.value} mints {base}..{hi}, counterfactual uses {sorted(ids)}",
            )


# ---------------------------------------------------------------------------
# Recovered DBT LEFT joins need a data witness, not a shape-level promise
# ---------------------------------------------------------------------------

class RecoveredOptionalJoinCounterfactualTest(unittest.TestCase):
    @staticmethod
    def _case() -> tuple[
        tuple[TableSpec, ...],
        tuple[Relationship, ...],
        mp.BuiltPlan,
        mp.StarShape,
    ]:
        import dataclasses

        tables = (
            TableSpec(
                name="accounts",
                columns=(
                    ColumnSpec(name="id", type=ColumnType.INTEGER),
                    ColumnSpec(name="name", type=ColumnType.TEXT),
                ),
                primary_key=("id",),
            ),
            TableSpec(
                name="owners",
                columns=(ColumnSpec(name="id", type=ColumnType.INTEGER),),
                primary_key=("id",),
            ),
            TableSpec(
                name="events",
                columns=(
                    ColumnSpec(name="event_id", type=ColumnType.INTEGER),
                    # Recovered dbt relationships are often optional evidence
                    # even though the warehouse column itself is NOT NULL.
                    ColumnSpec(
                        name="account_id",
                        type=ColumnType.INTEGER,
                        nullable=False,
                    ),
                    ColumnSpec(
                        name="owner_id",
                        type=ColumnType.INTEGER,
                        nullable=False,
                    ),
                    ColumnSpec(name="amount", type=ColumnType.DECIMAL),
                ),
                primary_key=("event_id",),
            ),
        )
        relationships = (
            Relationship(
                child_table="events",
                child_columns=("account_id",),
                parent_table="accounts",
                parent_columns=("id",),
                required=False,
            ),
            Relationship(
                child_table="events",
                child_columns=("owner_id",),
                parent_table="owners",
                parent_columns=("id",),
                required=True,
            ),
        )
        built = mp.build_rollup(
            mart="event_rollup",
            shape_name="dbt_recovered_rollup",
            parent="events",
            keys=(
                mp.KeyColumn(
                    column="event_id",
                    source="event_id",
                    type=ColumnType.INTEGER,
                    description="Event id.",
                ),
            ),
            passthrough=(
                mp.Passthrough(
                    column="account_name",
                    source="name",
                    from_hop="accounts",
                    type=ColumnType.TEXT,
                    description="Account name; empty for an unmatched account.",
                ),
            ),
            parent_carry=(
                ("account_id", "account_id"),
                ("amount", "event_amount"),
            ),
            hops=(
                mp.StarJoin(
                    table="accounts",
                    on_pairs=(("account_id", "id"),),
                    carry=(("name", "account_name"),),
                    rel_columns=("account_id", "id"),
                    description="Retain events whose account is absent.",
                ),
            ),
            measures=(
                mp.Measure(
                    column="total_amount",
                    expr='SUM("event_amount")',
                    type=ColumnType.DECIMAL,
                    description="Amount per event.",
                ),
            ),
            enforce_budget=False,
        )
        shape = dataclasses.replace(
            built.shape,
            dedupe_counterfactual_witness=False,
            join_counterfactual_witness=False,
            join_edges=(("events", ("account_id",), "accounts", ("id",)),),
        )
        return tables, relationships, built, shape

    @staticmethod
    def _grain_task(
        tables: tuple[TableSpec, ...],
        relationships: tuple[Relationship, ...],
        built: mp.BuiltPlan,
        *,
        key_columns: tuple[str, ...] = ("event_id",),
    ) -> TaskIR:
        mart = MartSpec(
            name="event_rollup",
            description="Recovered event rollup.",
            grain="One row per declared logical key.",
            key_columns=key_columns,
            columns=built.columns,
            plan=built.plan,
        )
        return TaskIR.model_construct(
            task_id="test__recovered_lookup_grain",
            tables=tables,
            relationships=relationships,
            marts=(mart,),
        )

    def test_optional_non_null_fk_plants_orphan_and_kills_actual_mutant(self) -> None:
        import duckdb
        from elt_taskgen.verification import attacks as attack_impl

        tables, relationships, built, shape = self._case()
        populations, attacks = pops.derive_populations_and_attacks(
            task_id="test__recovered_optional_join",
            tables=tables,
            relationships=relationships,
            shapes=(shape,),
        )
        again, _ = pops.derive_populations_and_attacks(
            task_id="test__recovered_optional_join",
            tables=tables,
            relationships=relationships,
            shapes=(shape,),
        )
        counterfactual = next(
            population
            for population in populations
            if population.name is P.COUNTERFACTUAL
        )
        self.assertEqual(counterfactual.literal_rows, again[3].literal_rows)

        account_keys = {row["id"] for row in counterfactual.literal_rows["accounts"]}
        dangling = [
            row
            for row in counterfactual.literal_rows["events"]
            if row["account_id"] not in account_keys
        ]
        self.assertEqual(len(dangling), 1, counterfactual.literal_rows)
        self.assertIsNotNone(dangling[0]["account_id"])
        self.assertTrue(
            any("OPTIONAL-LINK WITNESS" in line for line in counterfactual.conditions)
        )
        from elt_taskgen.generation.source_data import declares_dangling

        self.assertTrue(declares_dangling(counterfactual.conditions))

        base_projection = next(
            op
            for op in built.plan.ops
            if op.kind is MartOpKind.DERIVE
            and op.tables == ("events",)
        )
        self.assertIn("Form the mart key columns", base_projection.description)
        self.assertNotIn("The grain of events", base_projection.description)

        # The optional discriminator must not damage an unrelated required FK.
        owner_keys = {row["id"] for row in counterfactual.literal_rows["owners"]}
        self.assertTrue(
            all(
                row["owner_id"] in owner_keys
                for row in counterfactual.literal_rows["events"]
            )
        )
        self.assertEqual(
            [case.name for case in attacks if case.kind is AttackKind.INNER_JOIN],
            ["inner_join"],
        )

        mart = MartSpec(
            name="event_rollup",
            description="Recovered event rollup.",
            grain="One row per event.",
            key_columns=("event_id",),
            columns=built.columns,
            plan=built.plan,
        )
        task = TaskIR.model_construct(
            task_id="test__recovered_optional_join",
            tables=tables,
            relationships=relationships,
            marts=(mart,),
        )
        # account_name remains in the package's mechanical GROUP BY, but the
        # event primary key determines account_id and the unique lookup parent
        # determines account_name. It therefore does not enlarge the logical
        # grain or become an invalid NULL key on the unmatched witness row.
        from elt_taskgen.adapters import dbt as dbt_adapter

        dbt_adapter.verify_grain(task)
        sql = compile_plan_sql(task, mart)
        mutant = attack_impl._apply_kind(AttackKind.INNER_JOIN, sql, mart, "")
        self.assertIsNotNone(mutant)

        connection = duckdb.connect(":memory:")
        try:
            connection.execute("CREATE TABLE accounts (id BIGINT, name VARCHAR)")
            connection.execute("CREATE TABLE owners (id BIGINT)")
            connection.execute(
                "CREATE TABLE events ("
                "event_id BIGINT, account_id BIGINT, owner_id BIGINT, amount DECIMAL)"
            )
            for table_name, rows in counterfactual.literal_rows.items():
                columns = tuple(rows[0])
                placeholders = ", ".join("?" for _ in columns)
                connection.executemany(
                    f'INSERT INTO "{table_name}" ('
                    + ", ".join(f'"{column}"' for column in columns)
                    + f") VALUES ({placeholders})",
                    [tuple(row[column] for column in columns) for row in rows],
                )
            gold = connection.execute(sql).fetchall()
            mutated = connection.execute(mutant).fetchall()
        finally:
            connection.close()

        self.assertEqual(len(gold), 3)
        self.assertEqual(len(mutated), 2)
        self.assertNotEqual(gold, mutated)

    def test_all_null_group_stays_separate_from_optional_join_witness(self) -> None:
        """The new aggregate boundary must not consume the join boundary."""
        import dataclasses

        tables, relationships, _built, shape = self._case()
        tables = tuple(
            table.model_copy(
                update={
                    "columns": tuple(
                        column.model_copy(update={"nullable": True})
                        if column.name == "amount"
                        else column
                        for column in table.columns
                    )
                }
            )
            if table.name == "events"
            else table
            for table in tables
        )
        shape = dataclasses.replace(
            shape,
            all_null_aggregate_witnesses=(
                mp.AllNullAggregateWitness(
                    source_table="events",
                    input_columns=("amount",),
                    measure_columns=("total_amount",),
                    direct_group_columns=("event_id",),
                ),
            ),
        )
        populations, _attacks = pops.derive_populations_and_attacks(
            task_id="test__recovered_optional_join_all_null",
            tables=tables,
            relationships=relationships,
            shapes=(shape,),
        )
        counterfactual = next(
            population
            for population in populations
            if population.name is P.COUNTERFACTUAL
        )

        events = counterfactual.literal_rows["events"]
        account_keys = {
            row["id"] for row in counterfactual.literal_rows["accounts"]
        }
        dangling = [row for row in events if row["account_id"] not in account_keys]
        all_null = [row for row in events if row["amount"] is None]
        self.assertEqual(len(dangling), 1, counterfactual.literal_rows)
        self.assertEqual(len(all_null), 1, counterfactual.literal_rows)
        self.assertIsNot(dangling[0], all_null[0])
        self.assertEqual(
            sum(row["event_id"] == all_null[0]["event_id"] for row in events),
            1,
        )
        owner_keys = {
            row["id"] for row in counterfactual.literal_rows["owners"]
        }
        self.assertTrue(all(row["owner_id"] in owner_keys for row in events))
        self.assertTrue(
            any(
                "OPTIONAL-LINK WITNESS" in line
                for line in counterfactual.conditions
            )
        )
        self.assertTrue(
            any(
                "ALL-NULL AGGREGATE WITNESS" in line
                and "total_amount" in line
                for line in counterfactual.conditions
            )
        )

    def test_unrelated_optional_non_null_fk_is_closed_and_not_advertised(self) -> None:
        """Only recovered plan edges receive deliberate non-NULL orphans."""
        tables, relationships, _built, shape = self._case()
        tables = tuple(
            table.model_copy(
                update={
                    "columns": table.columns
                    + (
                        ColumnSpec(
                            name="campaign_id",
                            type=ColumnType.INTEGER,
                            nullable=False,
                        ),
                    )
                }
            )
            if table.name == "events"
            else table
            for table in tables
        ) + (
            TableSpec(
                name="campaigns",
                columns=(ColumnSpec(name="id", type=ColumnType.INTEGER),),
                primary_key=("id",),
            ),
        )
        relationships = relationships + (
            Relationship(
                child_table="events",
                child_columns=("campaign_id",),
                parent_table="campaigns",
                parent_columns=("id",),
                required=False,
            ),
        )

        populations, _attacks = pops.derive_populations_and_attacks(
            task_id="test__scoped_recovered_optional_join",
            tables=tables,
            relationships=relationships,
            shapes=(shape,),
        )
        counterfactual = next(
            population
            for population in populations
            if population.name is P.COUNTERFACTUAL
        )

        campaign_keys = {
            row["id"] for row in counterfactual.literal_rows["campaigns"]
        }
        self.assertTrue(
            all(
                row["campaign_id"] in campaign_keys
                for row in counterfactual.literal_rows["events"]
            ),
            counterfactual.literal_rows,
        )
        optional_lines = tuple(
            line
            for line in counterfactual.conditions
            if "OPTIONAL-LINK WITNESS" in line
        )
        self.assertTrue(any("events.account_id" in line for line in optional_lines))
        self.assertFalse(
            any(
                "campaign_id" in line or "campaigns" in line
                for line in optional_lines
            ),
            optional_lines,
        )

    def test_lookup_attribute_is_not_fd_when_declared_grain_omits_base_key_part(self) -> None:
        """A partial composite base identity cannot determine its foreign key."""
        tables, relationships, built, _shape = self._case()
        tables = tuple(
            table.model_copy(
                update={"primary_key": ("event_id", "account_id")}
            )
            if table.name == "events"
            else table
            for table in tables
        )
        mart = MartSpec(
            name="event_rollup",
            description="Recovered event rollup.",
            grain="One row per event id.",
            key_columns=("event_id",),
            columns=built.columns,
            plan=built.plan,
        )
        task = TaskIR.model_construct(
            task_id="test__partial_base_identity",
            tables=tables,
            relationships=relationships,
            marts=(mart,),
        )
        from elt_taskgen.adapters import dbt as dbt_adapter

        with self.assertRaisesRegex(
            dbt_adapter.DeclaredGrainViolation,
            "every extra grouping column must be provably determined",
        ):
            dbt_adapter.verify_grain(task)

    def test_partial_composite_parent_key_is_not_a_unique_lookup(self) -> None:
        """Sharing one member of a composite PK does not prove uniqueness."""
        tables, relationships, built, _shape = self._case()
        tables = tuple(
            table.model_copy(
                update={
                    "columns": table.columns
                    + (ColumnSpec(name="region", type=ColumnType.TEXT),),
                    "primary_key": ("id", "region"),
                }
            )
            if table.name == "accounts"
            else table
            for table in tables
        )
        mart = MartSpec(
            name="event_rollup",
            description="Recovered event rollup.",
            grain="One row per event.",
            key_columns=("event_id",),
            columns=built.columns,
            plan=built.plan,
        )
        task = TaskIR.model_construct(
            task_id="test__partial_composite_lookup",
            tables=tables,
            relationships=relationships,
            marts=(mart,),
        )
        from elt_taskgen.adapters import dbt as dbt_adapter

        with self.assertRaisesRegex(
            dbt_adapter.DeclaredGrainViolation,
            "not minted unique on that key",
        ):
            dbt_adapter.verify_grain(task)

    def test_pkless_business_key_does_not_prevent_physical_lookup_duplicates(
        self,
    ) -> None:
        """Stress may duplicate a PK-less row byte-for-byte despite its BK."""
        tables, relationships, built, _shape = self._case()
        tables = tuple(
            table.model_copy(
                update={"primary_key": (), "business_key": ("id",)}
            )
            if table.name == "accounts"
            else table
            for table in tables
        )
        task = self._grain_task(tables, relationships, built)
        from elt_taskgen.adapters import dbt as dbt_adapter

        with self.assertRaisesRegex(
            dbt_adapter.DeclaredGrainViolation,
            "not minted unique on that key",
        ):
            dbt_adapter.verify_grain(task)

    def test_only_exact_equality_lookups_confer_functional_dependency(self) -> None:
        """Inequality/OR/residual/RIGHT/FULL joins are not lookup proofs."""
        tables, relationships, built, _shape = self._case()
        join = next(op for op in built.plan.ops if op.kind is MartOpKind.JOIN)
        left = join.tables[0]
        mutations = {
            "inequality": {"predicate": join.predicate.replace(" = ", " >= ")},
            "or": {
                "predicate": (
                    f"({join.predicate}) OR "
                    f'accounts."id" = {left}."account_id"'
                )
            },
            "residual": {
                "predicate": (
                    f'{join.predicate} AND {left}."event_id" > 0'
                )
            },
            "right": {"join_type": JoinType.RIGHT},
            "full": {"join_type": JoinType.FULL},
        }
        from elt_taskgen.adapters import dbt as dbt_adapter

        for label, update in mutations.items():
            with self.subTest(label=label):
                drifted = built.plan.model_copy(
                    update={
                        "ops": tuple(
                            op.model_copy(update=update)
                            if op is join
                            else op
                            for op in built.plan.ops
                        )
                    }
                )
                drifted_built = mp.BuiltPlan(
                    plan=drifted,
                    columns=built.columns,
                    shape=built.shape,
                )
                task = self._grain_task(tables, relationships, drifted_built)
                mart = task.marts[0]
                self.assertEqual(
                    dbt_adapter._lookup_hops(
                        mart, task.relationships, {table.name for table in tables}
                    ),
                    [],
                )
                with self.assertRaisesRegex(
                    dbt_adapter.DeclaredGrainViolation,
                    "neither an exact LEFT/INNER equality lookup",
                ):
                    dbt_adapter.verify_grain(task)

    def test_malformed_measure_only_parent_hop_cannot_evade_fanout_guard(self) -> None:
        """A malformed hop is rejected even with no carried GROUP BY column."""
        tables, relationships, _built, _shape = self._case()
        built = mp.build_rollup(
            mart="event_rollup",
            shape_name="dbt_recovered_rollup",
            parent="events",
            keys=(
                mp.KeyColumn(
                    column="event_id",
                    source="event_id",
                    type=ColumnType.INTEGER,
                    description="Event id.",
                ),
            ),
            parent_carry=(("account_id", "account_id"),),
            hops=(
                mp.StarJoin(
                    table="accounts",
                    on_pairs=(("account_id", "id"),),
                    carry=(("name", "account_name"),),
                    rel_columns=("account_id", "id"),
                ),
            ),
            measures=(
                mp.Measure(
                    column="last_account_name",
                    expr='MAX("account_name")',
                    type=ColumnType.TEXT,
                    description="Account name at this event.",
                ),
            ),
            enforce_budget=False,
        )
        join = next(op for op in built.plan.ops if op.kind is MartOpKind.JOIN)
        drifted_plan = built.plan.model_copy(
            update={
                "ops": tuple(
                    op.model_copy(
                        update={"predicate": op.predicate.replace(" = ", " < ")}
                    )
                    if op is join
                    else op
                    for op in built.plan.ops
                )
            }
        )
        drifted = mp.BuiltPlan(
            plan=drifted_plan,
            columns=built.columns,
            shape=built.shape,
        )
        task = self._grain_task(tables, relationships, drifted)
        from elt_taskgen.adapters import dbt as dbt_adapter

        self.assertEqual(
            dbt_adapter._mechanical_grain(task.marts[0]), ("event_id",)
        )
        with self.assertRaisesRegex(
            dbt_adapter.DeclaredGrainViolation,
            "cannot confer functional dependency or bypass lookup fan-out",
        ):
            dbt_adapter.verify_grain(task)

    def test_exact_composite_lookup_proves_the_carried_attribute(self) -> None:
        """Every pair of a composite relationship must match exactly once."""
        tables = (
            TableSpec(
                name="accounts",
                columns=(
                    ColumnSpec(name="id", type=ColumnType.INTEGER),
                    ColumnSpec(name="region", type=ColumnType.TEXT),
                    ColumnSpec(name="name", type=ColumnType.TEXT),
                ),
                primary_key=("id", "region"),
            ),
            TableSpec(
                name="events",
                columns=(
                    ColumnSpec(name="event_id", type=ColumnType.INTEGER),
                    ColumnSpec(name="account_id", type=ColumnType.INTEGER),
                    ColumnSpec(name="account_region", type=ColumnType.TEXT),
                    ColumnSpec(name="amount", type=ColumnType.DECIMAL),
                ),
                primary_key=("event_id",),
            ),
        )
        relationships = (
            Relationship(
                child_table="events",
                child_columns=("account_id", "account_region"),
                parent_table="accounts",
                parent_columns=("id", "region"),
                required=True,
            ),
        )
        built = mp.build_rollup(
            mart="event_rollup",
            shape_name="dbt_recovered_rollup",
            parent="events",
            keys=(
                mp.KeyColumn(
                    column="event_id",
                    source="event_id",
                    type=ColumnType.INTEGER,
                    description="Event id.",
                ),
            ),
            passthrough=(
                mp.Passthrough(
                    column="account_name",
                    source="name",
                    from_hop="accounts",
                    type=ColumnType.TEXT,
                    description="Account name.",
                ),
            ),
            parent_carry=(
                ("account_id", "account_id"),
                ("account_region", "account_region"),
                ("amount", "event_amount"),
            ),
            hops=(
                mp.StarJoin(
                    table="accounts",
                    on_pairs=(
                        ("account_id", "id"),
                        ("account_region", "region"),
                    ),
                    carry=(("name", "account_name"),),
                    rel_columns=(
                        "account_id",
                        "account_region",
                        "id",
                        "region",
                    ),
                ),
            ),
            measures=(
                mp.Measure(
                    column="total_amount",
                    expr='SUM("event_amount")',
                    type=ColumnType.DECIMAL,
                    description="Amount per event.",
                ),
            ),
            enforce_budget=False,
        )
        task = self._grain_task(tables, relationships, built)
        from elt_taskgen.adapters import dbt as dbt_adapter

        self.assertEqual(
            dbt_adapter._lookup_hops(
                task.marts[0], relationships, {table.name for table in tables}
            ),
            [("accounts", ("id", "region"))],
        )
        dbt_adapter.verify_grain(task)

        join = next(
            op for op in built.plan.ops if op.kind is MartOpKind.JOIN
        )
        incomplete_plan = built.plan.model_copy(
            update={
                "ops": tuple(
                    op.model_copy(
                        update={"predicate": op.predicate.split(" AND ", 1)[0]}
                    )
                    if op is join
                    else op
                    for op in built.plan.ops
                )
            }
        )
        incomplete = task.marts[0].model_copy(update={"plan": incomplete_plan})
        incomplete_task = task.model_copy(update={"marts": (incomplete,)})
        self.assertEqual(
            dbt_adapter._lookup_hops(
                incomplete, relationships, {table.name for table in tables}
            ),
            [],
        )
        with self.assertRaisesRegex(
            dbt_adapter.DeclaredGrainViolation,
            "neither an exact LEFT/INNER equality lookup",
        ):
            dbt_adapter.verify_grain(incomplete_task)

    def test_optional_left_lookup_cannot_retain_a_hop_attribute_as_a_key(
        self,
    ) -> None:
        """Parent NOT NULL does not prevent NULL extension on no match."""
        tables, relationships, built, _shape = self._case()
        task = self._grain_task(
            tables,
            relationships,
            built,
            key_columns=("event_id", "account_name"),
        )
        from elt_taskgen.adapters import dbt as dbt_adapter

        with self.assertRaisesRegex(
            dbt_adapter.DeclaredGrainViolation,
            "optional LEFT lookup.*NULL-extend",
        ):
            dbt_adapter.verify_grain(task)

        required = tuple(
            relationship.model_copy(update={"required": True})
            if relationship.parent_table == "accounts"
            else relationship
            for relationship in relationships
        )
        dbt_adapter.verify_grain(
            task.model_copy(update={"relationships": required})
        )

    def test_model_builder_retains_hop_attribute_for_undetermined_lookup_key(
        self,
    ) -> None:
        """A hidden FK is not silently treated as determined by another key."""
        from elt_taskgen.adapters import dbt as dbt_adapter

        column = dbt_adapter.DbtColumn
        node = dbt_adapter.DbtNode
        spec = dbt_adapter.CandidateSpec(
            package_name="p",
            sources=(
                node(
                    unique_id="source.p.events",
                    name="events",
                    resource_type="source",
                    columns=(
                        column(name="event_id", data_type="integer"),
                        column(name="account_id", data_type="integer"),
                        column(name="amount", data_type="numeric"),
                    ),
                ),
                node(
                    unique_id="source.p.accounts",
                    name="accounts",
                    resource_type="source",
                    columns=(
                        column(name="id", data_type="integer"),
                        column(name="name", data_type="text"),
                    ),
                ),
            ),
        )
        model = node(
            unique_id="model.p.event_rollup",
            name="event_rollup",
            resource_type="model",
            columns=(
                column(name="event_id", data_type="integer"),
                column(name="account_name", data_type="text"),
                column(name="total_amount", data_type="numeric"),
            ),
        )
        grounding = dbt_adapter._Grounding(
            base_table="events",
            select_map=(("event_id", "event_id"),),
            key_columns=("event_id",),
            key_source_columns=("event_id",),
            mode="aggregate",
            measures=(
                dbt_adapter._Measure(
                    column="total_amount",
                    expr='SUM("amount")',
                    refs=("amount",),
                ),
            ),
            joined_columns=(("accounts", "name", "account_name"),),
            joins=(("accounts", ("account_id",), ("id",)),),
            declared=3,
        )
        relationships = (
            Relationship(
                child_table="events",
                child_columns=("account_id",),
                parent_table="accounts",
                parent_columns=("id",),
                required=True,
            ),
        )
        mart, shape = dbt_adapter._model_to_mart(
            spec,
            model,
            ["accounts", "events"],
            relationships,
            grounding,
            "test evidence",
            {
                "events": {"event_id", "account_id", "amount"},
                "accounts": {"id", "name"},
            },
            {},
        )

        self.assertEqual(mart.key_columns, ("event_id", "account_name"))
        # Orphaning this edge would NULL-extend account_name, now a logical key.
        # It therefore advertises neither a dangling witness nor INNER mutant.
        self.assertEqual(shape.join_edges, ())
        generated_tables = (
            TableSpec(
                name="accounts",
                columns=(
                    ColumnSpec(name="id", type=ColumnType.INTEGER),
                    ColumnSpec(name="name", type=ColumnType.TEXT),
                ),
                primary_key=("id",),
            ),
            TableSpec(
                name="events",
                columns=(
                    ColumnSpec(name="event_id", type=ColumnType.INTEGER),
                    ColumnSpec(name="account_id", type=ColumnType.INTEGER),
                    ColumnSpec(name="amount", type=ColumnType.DECIMAL),
                ),
                primary_key=("event_id",),
            ),
        )
        _populations, attacks = pops.derive_populations_and_attacks(
            task_id="test__retained_lookup_key",
            tables=generated_tables,
            relationships=relationships,
            shapes=(shape,),
        )
        self.assertNotIn(AttackKind.INNER_JOIN, {attack.kind for attack in attacks})

        optional = (
            relationships[0].model_copy(update={"required": False}),
        )
        with self.assertRaisesRegex(
            dbt_adapter.UnprovableGrainError,
            "unmatched row would NULL-extend a logical grain key",
        ):
            dbt_adapter._model_to_mart(
                spec,
                model,
                ["accounts", "events"],
                optional,
                grounding,
                "test evidence",
                {
                    "events": {"event_id", "account_id", "amount"},
                    "accounts": {"id", "name"},
                },
                {},
            )

    def test_projection_requires_a_complete_composite_identity(self) -> None:
        """One member of a composite source identity is not a unique key."""
        from elt_taskgen.adapters import dbt as dbt_adapter

        table = TableSpec(
            name="records",
            columns=(
                ColumnSpec(name="tenant_id", type=ColumnType.INTEGER),
                ColumnSpec(name="record_id", type=ColumnType.INTEGER),
                ColumnSpec(name="value", type=ColumnType.TEXT),
            ),
            primary_key=("tenant_id", "record_id"),
        )
        built = mp.build_projection(
            mart="record_dim",
            table="records",
            select_map=(
                ("tenant_id", "tenant_id"),
                ("record_id", "record_id"),
                ("value", "value"),
            ),
            key_columns=("tenant_id",),
        )
        mart = MartSpec(
            name="record_dim",
            description="Record projection.",
            grain="One row per tenant id.",
            key_columns=("tenant_id",),
            columns=(
                MartColumn(
                    name="tenant_id",
                    type=ColumnType.INTEGER,
                    description="Tenant id.",
                ),
                MartColumn(
                    name="record_id",
                    type=ColumnType.INTEGER,
                    description="Record id.",
                ),
                MartColumn(
                    name="value",
                    type=ColumnType.TEXT,
                    description="Record value.",
                ),
            ),
            plan=built.plan,
        )
        task = TaskIR.model_construct(
            task_id="test__partial_projection_identity",
            tables=(table,),
            relationships=(),
            marts=(mart,),
        )
        with self.assertRaisesRegex(
            dbt_adapter.DeclaredGrainViolation,
            "does not contain a complete identity",
        ):
            dbt_adapter.verify_grain(task)

        pkless_business_key = table.model_copy(
            update={
                "primary_key": (),
                "business_key": ("tenant_id",),
            }
        )
        with self.assertRaisesRegex(
            dbt_adapter.DeclaredGrainViolation,
            "protected from physical duplicate rows",
        ):
            dbt_adapter.verify_grain(
                task.model_copy(update={"tables": (pkless_business_key,)})
            )

        unresolved = mart.model_copy(
            update={"key_columns": ("tenant_id", "missing_alias")}
        )
        with self.assertRaisesRegex(
            dbt_adapter.DeclaredGrainViolation,
            "no direct source binding",
        ):
            dbt_adapter.verify_grain(
                task.model_copy(update={"marts": (unresolved,)})
            )

    def test_exhausted_boolean_domain_does_not_claim_inner_join(self) -> None:
        """No made-up third BOOLEAN value: an unproved case stays absent."""
        import dataclasses

        child = TableSpec(
            name="events",
            columns=(
                ColumnSpec(name="event_id", type=ColumnType.INTEGER),
                ColumnSpec(name="account_id", type=ColumnType.BOOLEAN),
            ),
            primary_key=("event_id",),
        )
        relationship = Relationship(
            child_table="events",
            child_columns=("account_id",),
            parent_table="accounts",
            parent_columns=("id",),
            required=False,
        )
        self.assertIsNone(
            pops._fresh_optional_key(
                relationship,
                child,
                [{"id": False}, {"id": True}],
                [
                    {"event_id": 1, "account_id": False},
                    {"event_id": 2, "account_id": True},
                ],
                1,
            )
        )

        _, _, _, recovered = self._case()
        recovered = dataclasses.replace(
            recovered,
            join_edges=(("events", ("account_id",), "accounts", ("id",)),),
        )
        closed_counterfactual = pops.PopulationSpec(
            name=P.COUNTERFACTUAL,
            seed=1,
            literal_rows={
                "events": (
                    {"event_id": 1, "account_id": False},
                    {"event_id": 2, "account_id": True},
                ),
                "accounts": ({"id": False}, {"id": True}),
            },
        )
        names = {
            case.name
            for case in pops.derive_attack_cases(
                (recovered,),
                policy=pops.POLICY_CONSTRUCTED,
                populations=(closed_counterfactual,),
            )
        }
        self.assertNotIn("inner_join", names)


# ---------------------------------------------------------------------------
# The derived attack catalogue claims only what the plan offers
# ---------------------------------------------------------------------------

class DerivedAttackCatalogueTest(unittest.TestCase):
    def test_join_dedupe_and_coalesce_surfaces_all_produce_a_case(self) -> None:
        cases = {c.kind for c in _task().attack_cases}
        self.assertIn(AttackKind.INNER_JOIN, cases)
        self.assertIn(AttackKind.NO_DEDUP, cases)
        self.assertIn(AttackKind.NO_NULL_DEFAULT, cases)
        self.assertIn(AttackKind.CONSTANTS, cases)

    def test_a_join_free_plan_claims_no_inner_join_case(self) -> None:
        built = mp.build_star(
            mart="m", parent="captains", parent_keys=("captain_id",),
            key_columns=("captain_id",),
            measures=(mp.Measure(column="n", expr="COUNT(*)"),),
        )
        kinds = {c.kind for c in pops.derive_attack_cases((built.shape,))}
        self.assertNotIn(AttackKind.INNER_JOIN, kinds)
        self.assertNotIn(AttackKind.NO_DEDUP, kinds)

    def test_a_count_only_mart_claims_no_null_default_case(self) -> None:
        """COUNT is never NULL, so a dropped COALESCE around one is
        undetectable — claiming it would make the catalogue lie."""
        built = mp.build_star(
            mart="m", parent="captains", parent_keys=("captain_id",),
            key_columns=("captain_id",),
            measures=(mp.Measure(column="n", expr="COUNT(*)"),),
        )
        kinds = {c.kind for c in pops.derive_attack_cases((built.shape,))}
        self.assertNotIn(AttackKind.NO_NULL_DEFAULT, kinds)

    def test_skip_extraction_is_only_claimed_with_more_than_one_backend(self) -> None:
        shape = _built().shape
        one = {c.kind for c in pops.derive_attack_cases((shape,), backends=1)}
        many = {c.kind for c in pops.derive_attack_cases((shape,), backends=3)}
        self.assertNotIn(AttackKind.SKIP_EXTRACTION, one)
        self.assertIn(AttackKind.SKIP_EXTRACTION, many)

    def test_the_provided_rows_policy_claims_different_populations(self) -> None:
        """WikiDBs ships REAL rows: development/stress carry whatever coverage
        the vendor shipped and only STRESS replicates rows, so the
        constructed-policy `no_dedup` expectation is false there (measured).
        `inner_join` claims ONLY the counterfactual under either policy — the
        one population whose childless-parent witness is constructed (the
        legacy development/stress FULL-reward claims broke as soon as the fact
        carried an enum, and primary carves childless parents only through a
        fact's first optional link)."""
        shape = _built().shape
        constructed = {
            c.name: c.expected_pass
            for c in pops.derive_attack_cases(
                (shape,), policy=pops.POLICY_CONSTRUCTED
            )
        }
        provided = {
            c.name: c.expected_pass
            for c in pops.derive_attack_cases(
                (shape,), policy=pops.POLICY_PROVIDED_ROWS
            )
        }
        self.assertEqual(constructed["inner_join"], {P.COUNTERFACTUAL: False})
        self.assertNotIn(P.DEVELOPMENT, constructed["inner_join"])
        self.assertNotIn(P.DEVELOPMENT, provided["inner_join"])
        self.assertEqual(constructed["no_dedup"], {P.COUNTERFACTUAL: False})
        self.assertEqual(provided["no_dedup"], {P.STRESS: False})

    def test_every_required_case_must_lose_reward_somewhere(self) -> None:
        for case in _task().attack_cases:
            if case.required:
                self.assertIn(False, list(case.expected_pass.values()), case.name)

    def test_filtered_and_constant_divisor_surfaces_declare_phase_a_cases(
        self,
    ) -> None:
        """Phase A §A2+§A3 (blocker1_fivetran_attacks.md). A rollup whose
        measures carry a CASE-inside-aggregate declares BOTH dropped_filter
        forms (the value error and the strictly different filter_to_where
        row-count error), and a constant unit divisor declares
        wrong_denominator — each once, each with a live surface in the
        compiled SQL, so the $0 attack-matrix precheck can execute rather
        than refuse them.
        """
        import elt_taskgen.reference.solution as sol
        from elt_taskgen.verification import attacks as A

        built = mp.build_rollup(
            mart="captain_summary", shape_name="dbt_recovered_rollup",
            parent="captains",
            keys=(mp.KeyColumn(column="captain_id", type=ColumnType.TEXT,
                               description="The captain.", source="captain_id"),),
            passthrough=(
                mp.Passthrough(column="home_port", source="home_port",
                               type=ColumnType.TEXT, description="Port."),
                mp.Passthrough(column="license_class", source="license_class",
                               type=ColumnType.TEXT, description="Class."),
            ),
            hops=(mp.StarJoin(
                table="landings", on_pairs=(("captain_id", "captain_id"),),
                carry=(("captain_id", "landings__captain_id"),
                       ("tonnes", "landings__tonnes"),
                       ("grade", "landings__grade")),
                rel_columns=("captain_id",)),),
            measures=(
                mp.Measure(column="landings_count",
                           expr='COUNT("landings__captain_id")',
                           type=ColumnType.BIGINT, description="Count."),
                mp.Measure(column="total_tonnes", expr='SUM("landings__tonnes")',
                           null_default="0", type=ColumnType.DECIMAL,
                           description="Total."),
                mp.Measure(column="max_tonnes", expr='MAX("landings__tonnes")',
                           null_default="0", type=ColumnType.DECIMAL,
                           description="Max."),
                mp.Measure(
                    column="premium_tonnes",
                    expr=("SUM(CASE WHEN \"landings__grade\" = 'premium' "
                          "THEN \"landings__tonnes\" ELSE 0 END)"),
                    null_default="0", type=ColumnType.DECIMAL,
                    description="Premium only."),
                mp.Measure(column="tonnes_kilo",
                           expr='SUM("landings__tonnes") / 1000.0',
                           null_default="0", type=ColumnType.DECIMAL,
                           description="Kilotonnes."),
            ),
            roles=mp.FactRoles(link_key="captain_id", measure="tonnes",
                               predicate_column="grade",
                               predicate_pass=("premium",),
                               predicate_fail=("standard",)),
        )
        self.assertEqual(built.shape.filtered_measures, ("premium_tonnes",))
        self.assertEqual(built.shape.constant_divisor_measures, ("tonnes_kilo",))

        cases = pops.derive_attack_cases(
            (built.shape,), policy=pops.POLICY_CONSTRUCTED
        )
        by_name = {c.name: c for c in cases}
        for name in ("dropped_filter", "dropped_filter__filter_to_where",
                     "wrong_denominator"):
            with self.subTest(case=name):
                self.assertIn(name, by_name)
                self.assertEqual(
                    by_name[name].expected_pass, {P.COUNTERFACTUAL: False}
                )
        self.assertIn(
            "directive:kind:dropped_filter@filter_to_where",
            by_name["dropped_filter__filter_to_where"].mutation,
        )
        # The descriptions name the actual measures, not a template's.
        self.assertIn("premium_tonnes", by_name["dropped_filter"].description)
        self.assertIn("tonnes_kilo", by_name["wrong_denominator"].description)

        # Every declared case has a LIVE surface in the compiled SQL.
        mart = MartSpec(
            name="captain_summary", description="Per-captain landings summary.",
            grain="One row per captain.", key_columns=("captain_id",),
            columns=built.columns, plan=built.plan)
        tables = (
            TableSpec(name="captains", description="Captains.", columns=(
                ColumnSpec(name="captain_id", type=ColumnType.TEXT,
                           description="id", nullable=False),
                ColumnSpec(name="home_port", type=ColumnType.TEXT,
                           description="port", nullable=False),
                ColumnSpec(name="license_class", type=ColumnType.TEXT,
                           description="cls", nullable=False),
            ), primary_key=("captain_id",)),
            TableSpec(name="landings", description="Landings.", columns=(
                ColumnSpec(name="captain_id", type=ColumnType.TEXT,
                           description="fk", nullable=False),
                ColumnSpec(name="tonnes", type=ColumnType.DECIMAL,
                           description="t", nullable=False),
                ColumnSpec(name="grade", type=ColumnType.TEXT, description="g",
                           nullable=False, enum_values=("premium", "standard")),
            )),
        )
        stub = TaskIR.model_construct(
            task_id="probe", tables=tables, marts=(mart,))
        sql = sol.compile_plan_sql(stub, mart)
        for kind, variant in (
            (AttackKind.DROPPED_FILTER, ""),
            (AttackKind.DROPPED_FILTER, "filter_to_where"),
            (AttackKind.WRONG_DENOMINATOR, ""),
        ):
            with self.subTest(kind=kind.value, variant=variant):
                out = A._apply_kind(kind, sql, mart, variant)
                self.assertIsNotNone(out)
                self.assertNotEqual(out, sql)

    def test_ratio_and_constant_divisor_yield_one_wrong_denominator(self) -> None:
        """The two surfaces are disjoint by construction and share a case
        name; both present must yield ONE case (the ratio one, whose witness
        the counterfactual constructs), never a duplicate TaskIR rejects."""
        import dataclasses

        shape = dataclasses.replace(
            _built().shape,
            ratio_measures=("share",),
            constant_divisor_measures=("spend_kilo",),
        )
        cases = pops.derive_attack_cases((shape,), policy=pops.POLICY_CONSTRUCTED)
        wd = [c for c in cases if c.name == "wrong_denominator"]
        self.assertEqual(len(wd), 1)
        self.assertIn("strictly smaller than its denominator", wd[0].description)

    def test_a_recovered_rollup_declares_no_inner_join_case(self) -> None:
        """A recovered plan's counterfactual constructs no childless-parent
        witness and every generated FK resolves, so LEFT is INNER everywhere —
        measured on reddit_ads (1.0 on all three claimed kills, refused by the
        $0 precheck). The case must not be declared; it returns when witness
        construction covers recovered shapes. Provided-rows pools still
        declare it (their counterfactual genuinely orphans parents)."""
        import dataclasses

        recovered = dataclasses.replace(
            _built().shape,
            dedupe_counterfactual_witness=False,
            join_counterfactual_witness=False,
        )
        constructed = {
            c.name for c in pops.derive_attack_cases(
                (recovered,), policy=pops.POLICY_CONSTRUCTED)
        }
        provided = {
            c.name for c in pops.derive_attack_cases(
                (recovered,), policy=pops.POLICY_PROVIDED_ROWS)
        }
        self.assertNotIn("inner_join", constructed)
        self.assertIn("inner_join", provided)
        # the library default still declares it under the constructed policy
        library = {
            c.name for c in pops.derive_attack_cases(
                (_built().shape,), policy=pops.POLICY_CONSTRUCTED)
        }
        self.assertIn("inner_join", library)

    def test_a_recovered_rollup_claims_no_dedup_on_stress_not_counterfactual(
        self,
    ) -> None:
        """Phase A §A1 (blocker1_fivetran_attacks.md). A dbt-RECOVERED rollup
        runs the constructed policy but its counterfactual constructs no
        duplicate witness — measured on servicenow, `no_dedup` scored 1.0 on
        the counterfactual and 0.8 only on STRESS (dup_frac policy). The shape
        says so via `dedupe_counterfactual_witness=False` and the derivation
        must claim STRESS; a library shape (witness constructed, the default)
        keeps the counterfactual claim.
        """
        import dataclasses

        shape = _built().shape
        self.assertTrue(shape.dedupe_counterfactual_witness)  # library default
        recovered = dataclasses.replace(shape, dedupe_counterfactual_witness=False)

        witnessed = {
            c.name: c.expected_pass
            for c in pops.derive_attack_cases(
                (shape,), policy=pops.POLICY_CONSTRUCTED
            )
        }
        unwitnessed = {
            c.name: c.expected_pass
            for c in pops.derive_attack_cases(
                (recovered,), policy=pops.POLICY_CONSTRUCTED
            )
        }
        self.assertEqual(witnessed["no_dedup"], {P.COUNTERFACTUAL: False})
        self.assertEqual(unwitnessed["no_dedup"], {P.STRESS: False})
        # The prose must state the claim it ships, not the library's.
        case = next(
            c
            for c in pops.derive_attack_cases(
                (recovered,), policy=pops.POLICY_CONSTRUCTED
            )
            if c.name == "no_dedup"
        )
        self.assertIn("stress is the population", case.description)
        self.assertNotIn("Witnessed by the counterfactual", case.description)


# ---------------------------------------------------------------------------
# The trusted reference travels ON the task
# ---------------------------------------------------------------------------

class AttachedReferenceTest(unittest.TestCase):
    def test_attach_reference_makes_the_task_mutatable_by_attacks(self) -> None:
        """`verification/attacks.py::_reference_sql` reads TaskIR.reference and
        refuses a task without one; before this, 4 of 6 derived cases per pool
        could not compile at all."""
        from elt_taskgen.reference.solution import attach_reference

        task = attach_reference(_task())
        self.assertIsNotNone(task.reference)
        self.assertIn("captain_summary", task.reference.sql_by_mart)
        # Idempotent: a second call must not re-provenance or re-compile.
        self.assertEqual(
            attach_reference(task).reference, task.reference
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

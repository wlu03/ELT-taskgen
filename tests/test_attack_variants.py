"""Round-6 mutation rules: the wrong_grain fix, and the attack VARIANTS.

WHY THIS FILE EXISTS
Every rule here is one that the corpus audit found declared-but-inert, or one
that had no construction site at all. The point of testing them at the AST
level is that a mutation rule which returns None ("not applicable") reads
exactly like a mutation rule that never fires, and the corpus shipped 88 marts
advertising a `wrong_grain` surface on that ambiguity. These tests pin the
difference: applicable-and-changed, applicable-and-correct, or explicitly
not applicable.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import duckdb
import sqlglot
from sqlglot import exp

from elt_taskgen.models import (
    AttackKind,
    ColumnSpec,
    ColumnType,
    MartColumn,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    PopulationName,
    SemanticPattern,
    TableSpec,
)
from elt_taskgen.verification import attacks

#: A compiled plan in the shape `generation/mart_plan.py::build_rollup` emits:
#: the GROUP BY lives in a CTE and the OUTER select is a bare projection over
#: it. That is the shape on which the OLD wrong_grain rule was inert.
CTE_PLAN = """
WITH j1 AS (
  SELECT p.k AS parent_key, b.id AS link_key, b.cid AS link_child_fk,
         b.status AS link_status, b.amt AS link_amount
  FROM p LEFT JOIN b ON b.k = p.k
),
j2 AS (
  SELECT j1.parent_key AS parent_key, j1.link_key AS link_key,
         j1.link_status AS link_status, j1.link_amount AS link_amount,
         c.cid AS dim_key, c.label AS dim_label
  FROM j1 LEFT JOIN c ON c.cid = j1.link_child_fk
),
g AS (
  SELECT parent_key,
    COUNT(link_key) AS m_0,
    COUNT(DISTINCT dim_key) AS m_1,
    COUNT(CASE WHEN link_status IN ('open') THEN link_key END) AS m_2,
    SUM(link_amount) AS m_3,
    ARG_MAX(dim_label, ROW(link_amount, dim_label)) AS m_4
  FROM j2 GROUP BY parent_key
),
r AS (
  SELECT parent_key, m_0 AS link_count, m_1 AS distinct_child_count,
         m_2 AS open_count, COALESCE(m_3, 0) AS total_amount,
         m_4 AS top_label,
         ROUND(CAST(m_2 AS DOUBLE) / NULLIF(m_0, 0), 4) AS open_ratio,
         CASE WHEN m_0 = 0 THEN 'none' WHEN m_0 <= 2 THEN 'small' ELSE 'big' END
           AS size_band,
         DENSE_RANK() OVER (ORDER BY m_0 DESC, parent_key ASC) AS rank_pos
  FROM g
)
SELECT parent_key, link_count, distinct_child_count, open_count, total_amount,
       top_label, open_ratio, size_band, rank_pos
FROM r ORDER BY parent_key
"""

_MART = MartSpec(
    name="m",
    grain="one row per parent_key",
    key_columns=("parent_key",),
    columns=(
        MartColumn(name="parent_key", type=ColumnType.INTEGER, description="k"),
        MartColumn(name="link_count", type=ColumnType.BIGINT, description="n"),
    ),
    plan=MartPlan(
        mart="m",
        ops=(MartOp(kind=MartOpKind.SOURCE, description="read p", tables=("p",)),),
    ),
)

HAVING_PLAN = CTE_PLAN.replace(
    "FROM r ORDER BY parent_key",
    "FROM r WHERE link_count >= 2 ORDER BY parent_key",
)

_HAVING_MART = _MART.model_copy(
    update={
        "plan": MartPlan(
            mart="m",
            ops=(
                MartOp(
                    kind=MartOpKind.SOURCE,
                    description="read p",
                    tables=("p",),
                ),
                MartOp(
                    kind=MartOpKind.FILTER,
                    description="retain groups with at least two links",
                    tables=("r",),
                    columns=("parent_key", "link_count"),
                    predicate="link_count >= 2",
                    details={"name": "filtered"},
                ),
            ),
            template_id="aggregate_then_filter",
            semantic_patterns=(SemanticPattern.AGGREGATE_THEN_FILTER,),
        )
    }
)


def _apply(kind: AttackKind, variant: str = "", sql: str = CTE_PLAN) -> str | None:
    return attacks._apply_kind(kind, sql, _MART, variant)


def _apply_having() -> str | None:
    return attacks._apply_kind(
        AttackKind.WRONG_AGG_STAGE,
        HAVING_PLAN,
        _HAVING_MART,
        "filter_before_aggregate",
    )


class WrongGrainFixTest(unittest.TestCase):
    """The measured defect: the rule read the OUTER select's GROUP BY, which is
    None on every plan the builder emits because the GROUP BY is in a CTE."""

    def test_outer_select_of_a_built_plan_has_no_group_by(self) -> None:
        ast = sqlglot.parse_one(CTE_PLAN, read="duckdb")
        self.assertIsInstance(ast, exp.Select)
        self.assertIsNone(ast.args.get("group"))

    def test_wrong_grain_mutates_the_select_that_owns_the_group_by(self) -> None:
        mutated = _apply(AttackKind.WRONG_GRAIN)
        self.assertIsNotNone(mutated, "wrong_grain must be applicable to a CTE plan")
        groups = [
            s.args["group"]
            for s in sqlglot.parse_one(mutated, read="duckdb").find_all(exp.Select)
            if s.args.get("group")
        ]
        self.assertEqual(len(groups), 1)
        names = {c.name for g in groups for c in g.find_all(exp.Column)}
        self.assertIn("parent_key", names)
        self.assertGreater(len(names), 1, "no child column was added to the grain")

    def test_wrong_grain_changes_the_answer_when_the_bridge_fans_out(self) -> None:
        con = duckdb.connect()
        con.execute("CREATE TABLE p(k INT)")
        con.execute("CREATE TABLE b(id INT, k INT, cid INT, status VARCHAR, amt INT)")
        con.execute("CREATE TABLE c(cid INT, label VARCHAR)")
        con.execute("INSERT INTO p VALUES (1),(2)")
        con.execute(
            "INSERT INTO b VALUES (10,1,100,'open',5),(11,1,100,'shut',7),"
            "(12,1,101,'open',9)"
        )
        con.execute("INSERT INTO c VALUES (100,'aa'),(101,'bb')")
        gold = con.execute(CTE_PLAN).fetchall()
        mutant = con.execute(_apply(AttackKind.WRONG_GRAIN)).fetchall()
        self.assertNotEqual(gold, mutant)
        self.assertEqual(len(gold), 2)  # one row per parent
        self.assertGreater(len(mutant), len(gold))  # the grain broke apart


class VariantCatalogueTest(unittest.TestCase):
    def test_unknown_variant_raises_rather_than_falling_back(self) -> None:
        with self.assertRaises(ValueError):
            attacks.split_kind_directive("wrong_grain@not_a_variant")

    def test_known_variants_round_trip(self) -> None:
        kind, variant = attacks.split_kind_directive("dropped_filter@filter_to_where")
        self.assertIs(kind, AttackKind.DROPPED_FILTER)
        self.assertEqual(variant, "filter_to_where")

    def test_kind_without_default_requires_its_named_variant(self) -> None:
        with self.assertRaisesRegex(ValueError, "no variant ''"):
            attacks.split_kind_directive("wrong_agg_stage")
        kind, variant = attacks.split_kind_directive(
            "wrong_agg_stage@filter_before_aggregate"
        )
        self.assertIs(kind, AttackKind.WRONG_AGG_STAGE)
        self.assertEqual(variant, "filter_before_aggregate")

    def test_every_catalogued_variant_has_a_rule_that_fires(self) -> None:
        """A variant nobody can apply is a variant that proves nothing."""
        skip = {
            # Plan-level kinds are materialized without an AST edit, and
            # `drop_frame` needs an explicit ROWS frame, which this plan (a
            # DENSE_RANK) deliberately does not carry — see the running-total
            # rejection note in generation/mart_plan.py.
            (AttackKind.CONSTANTS, ""),
            (AttackKind.KEYS_ONLY, ""),
            (AttackKind.WRONG_WINDOW, "drop_frame"),
            (AttackKind.INNER_JOIN, "second_hop"),
        }
        for kind, variants in attacks.KIND_VARIANTS.items():
            for variant in sorted(variants):
                if (kind, variant) in skip:
                    continue
                with self.subTest(kind=kind.value, variant=variant):
                    mutated = (
                        _apply_having()
                        if kind is AttackKind.WRONG_AGG_STAGE
                        else _apply(kind, variant)
                    )
                    self.assertIsNotNone(
                        mutated,
                        f"{kind.value}@{variant} is inert on a full library plan",
                    )


class ReservedIdentifierRenderingTest(unittest.TestCase):
    def test_hardcoded_output_quotes_reserved_and_escaped_column_names(self) -> None:
        mart = _MART.model_copy(
            update={
                "columns": (
                    MartColumn(
                        name="group", type=ColumnType.INTEGER, description="k"
                    ),
                    MartColumn(
                        name='a"b', type=ColumnType.INTEGER, description="v"
                    ),
                )
            }
        )
        sql = attacks._literal_select(mart, 'group,"a""b"\n1,2\n')
        con = duckdb.connect(":memory:")
        try:
            result = con.execute(sql)
            self.assertEqual(
                [column[0] for column in result.description], ["group", 'a"b']
            )
            self.assertEqual(result.fetchall(), [(1, 2)])
        finally:
            con.close()

    def test_reserved_source_table_and_column_can_be_created_and_filled(self) -> None:
        table = TableSpec(
            name="group",
            columns=(
                ColumnSpec(name="order", type=ColumnType.INTEGER),
            ),
        )
        con = duckdb.connect(":memory:")
        try:
            attacks._create_and_fill(con, table, [{"order": 7}])
            self.assertEqual(
                con.execute('SELECT "order" FROM "group"').fetchall(), [(7,)]
            )
        finally:
            con.close()

    def test_reserved_omitted_table_is_dropped_from_a_load_mutation(self) -> None:
        from elt_taskgen import demo_fixture

        task = demo_fixture.demo_task()
        plan = attacks.LoadMutationPlan(
            name="skip_tables",
            source_population={
                PopulationName.DEVELOPMENT: PopulationName.DEVELOPMENT
            },
            omit_tables=("group",),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            rendered = (
                workspace
                / "tasks"
                / task.task_id
                / "populations"
                / PopulationName.DEVELOPMENT.value
                / "rendered"
            )
            rendered.mkdir(parents=True)
            con = duckdb.connect(":memory:")
            try:
                con.execute('CREATE TABLE "group" ("order" INTEGER)')
                with mock.patch(
                    "elt_taskgen.reference.solution.load_sources_duckdb",
                    return_value=SimpleNamespace(counts={"group": 1}),
                ):
                    counts = attacks.apply_load_mutation(
                        task,
                        plan,
                        PopulationName.DEVELOPMENT,
                        workspace,
                        con,
                    )
                self.assertEqual(counts, {})
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_name = 'group'"
                    ).fetchone()[0],
                    0,
                )
            finally:
                con.close()


class WrongAggregateStageVariantTest(unittest.TestCase):
    def test_filter_before_aggregate_moves_the_group_predicate(self) -> None:
        mutated = _apply_having()
        self.assertIsNotNone(mutated)
        ast = sqlglot.parse_one(mutated, read="duckdb")
        wheres = list(ast.find_all(exp.Where))
        self.assertEqual(1, len(wheres))
        self.assertIn("NOT link_key IS NULL", wheres[0].sql(dialect="duckdb"))
        self.assertNotIn("link_count >= 2", mutated)


class FilteredAggregateVariantTest(unittest.TestCase):
    def test_dropped_filter_unwraps_the_case_inside_the_aggregate(self) -> None:
        """A filtered aggregate has no WHERE to delete."""
        mutated = _apply(AttackKind.DROPPED_FILTER)
        self.assertNotIn("link_status IN ('open')", mutated)

    def test_filter_to_where_hoists_the_predicate_out_of_the_aggregate(self) -> None:
        mutated = _apply(AttackKind.DROPPED_FILTER, "filter_to_where")
        ast = sqlglot.parse_one(mutated, read="duckdb")
        wheres = list(ast.find_all(exp.Where))
        self.assertEqual(len(wheres), 1)
        self.assertIn("link_status", wheres[0].sql(dialect="duckdb"))

    def test_the_two_filter_variants_are_different_mutants(self) -> None:
        self.assertNotEqual(
            _apply(AttackKind.DROPPED_FILTER),
            _apply(AttackKind.DROPPED_FILTER, "filter_to_where"),
        )


class RatioVariantTest(unittest.TestCase):
    def test_wrong_denominator_replaces_the_divisor_with_one(self) -> None:
        ast = sqlglot.parse_one(_apply(AttackKind.WRONG_DENOMINATOR), read="duckdb")
        divisors = [d.expression.sql(dialect="duckdb") for d in ast.find_all(exp.Div)]
        self.assertEqual(divisors, ["1"])

    def test_filtered_denominator_divides_the_numerator_by_itself(self) -> None:
        ast = sqlglot.parse_one(
            _apply(AttackKind.WRONG_DENOMINATOR, "filtered_denominator"), read="duckdb"
        )
        div = next(ast.find_all(exp.Div))
        numerator = div.this.this if isinstance(div.this, exp.Cast) else div.this
        target = div.expression.this  # NULLIF(<denominator>, 0)
        self.assertEqual(
            numerator.sql(dialect="duckdb"), target.sql(dialect="duckdb")
        )

    def test_filtered_denominator_is_not_applicable_when_already_degenerate(
        self,
    ) -> None:
        sql = "SELECT k, CAST(a AS DOUBLE) / NULLIF(a, 0) AS r FROM t"
        self.assertIsNone(
            _apply(AttackKind.WRONG_DENOMINATOR, "filtered_denominator", sql)
        )


class ExtremaAndBoundaryVariantTest(unittest.TestCase):
    def test_argmax_as_max_handles_the_aggregate_spelling(self) -> None:
        mutated = _apply(AttackKind.CUSTOM, "argmax_as_max")
        self.assertNotIn("ARG_MAX", mutated.upper())
        self.assertIn("MAX(dim_label)", mutated)

    def test_argmax_as_max_handles_the_window_spelling(self) -> None:
        """`generation/mart_plan.py::extrema_op` emits FIRST_VALUE OVER (...)."""
        sql = (
            "SELECT k, FIRST_VALUE(label) OVER (PARTITION BY k ORDER BY m DESC, "
            "label ASC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) "
            "AS top_label FROM t"
        )
        mutated = _apply(AttackKind.CUSTOM, "argmax_as_max", sql)
        self.assertIsNotNone(mutated)
        self.assertNotIn("FIRST_VALUE", mutated.upper())
        self.assertIn("MAX(label)", mutated)
        # The window survives: dropping it is what `wrong_window` does, and two
        # mutants that compile to the same SQL prove one thing between them.
        self.assertIn("OVER", mutated.upper())

    def test_argmax_as_max_really_reports_a_different_label(self) -> None:
        con = duckdb.connect()
        con.execute("CREATE TABLE t(k INT, label VARCHAR, m INT)")
        # The largest measure sits on the alphabetically SMALLEST label.
        con.execute("INSERT INTO t VALUES (1,'aa',9),(1,'zz',1)")
        sql = (
            "SELECT k, ARG_MAX(label, ROW(m, label)) AS top_label "
            "FROM t GROUP BY k"
        )
        self.assertEqual(con.execute(sql).fetchall(), [(1, "aa")])
        mutated = _apply(AttackKind.CUSTOM, "argmax_as_max", sql)
        self.assertEqual(con.execute(mutated).fetchall(), [(1, "zz")])

    def test_wrong_boundary_else_drops_the_mandatory_else(self) -> None:
        ast = sqlglot.parse_one(
            _apply(AttackKind.CUSTOM, "wrong_boundary_else"), read="duckdb"
        )
        for case in ast.find_all(exp.Case):
            self.assertIsNone(case.args.get("default"))

    def test_wrong_boundary_inclusive_flips_every_threshold(self) -> None:
        mutated = _apply(AttackKind.CUSTOM, "wrong_boundary_inclusive")
        self.assertIn("m_0 < 2", mutated)  # `<= 2` became `< 2`
        self.assertNotIn("m_0 <= 2", mutated)


class SecondHopTest(unittest.TestCase):
    def test_second_hop_flips_only_the_later_join(self) -> None:
        mutated = _apply(AttackKind.INNER_JOIN, "second_hop")
        self.assertIsNotNone(mutated)
        sides = [
            (j.side or "INNER")
            for j in sqlglot.parse_one(mutated, read="duckdb").find_all(exp.Join)
        ]
        self.assertEqual(sides, ["LEFT", "INNER"])

    def test_second_hop_is_not_applicable_to_a_one_hop_plan(self) -> None:
        sql = "SELECT p.k, b.id FROM p LEFT JOIN b ON b.k = p.k"
        self.assertIsNone(_apply(AttackKind.INNER_JOIN, "second_hop", sql))

    def test_plain_inner_join_flips_both_hops(self) -> None:
        mutated = _apply(AttackKind.INNER_JOIN)
        sides = [
            (j.side or "INNER")
            for j in sqlglot.parse_one(mutated, read="duckdb").find_all(exp.Join)
        ]
        self.assertEqual(sides, ["INNER", "INNER"])


class WindowVariantTest(unittest.TestCase):
    def test_wrong_window_drops_the_declared_tie_break(self) -> None:
        mutated = _apply(AttackKind.WRONG_WINDOW)
        ast = sqlglot.parse_one(mutated, read="duckdb")
        for window in ast.find_all(exp.Window):
            self.assertIsNone(window.args.get("order"))

    def test_drop_frame_removes_only_the_frame(self) -> None:
        sql = (
            "SELECT k, SUM(v) OVER (ORDER BY k ASC ROWS BETWEEN UNBOUNDED "
            "PRECEDING AND CURRENT ROW) AS running FROM t"
        )
        mutated = _apply(AttackKind.WRONG_WINDOW, "drop_frame", sql)
        self.assertIsNotNone(mutated)
        self.assertNotIn("ROWS BETWEEN", mutated.upper())
        self.assertIn("ORDER BY", mutated.upper())


if __name__ == "__main__":
    unittest.main()


ONE_HOP_ARGMAX = """
WITH step_2 AS (
    SELECT p."id" AS "parent_key" FROM "parents" AS p
),
step_3 AS (
    SELECT b."parent_key", c."id" AS "f_id", c."amount" AS "f_measure", c."label" AS "f_label"
    FROM step_2 AS b LEFT JOIN "children" AS c ON c."parent_id" = b."parent_key"
),
step_5 AS (
    SELECT "parent_key", COUNT("f_id") AS "m_2" FROM step_3 GROUP BY "parent_key"
),
step_6 AS (
    SELECT "parent_key", "f_label" AS "top_label" FROM step_3
    QUALIFY ROW_NUMBER() OVER (PARTITION BY "parent_key" ORDER BY "f_measure" DESC NULLS LAST, "f_label" ASC NULLS LAST) = 1
),
step_7 AS (
    SELECT g."parent_key", g.m_2, t."top_label" FROM step_5 AS g LEFT JOIN step_6 AS t ON t."parent_key" = g."parent_key"
)
SELECT "parent_key", m_2 AS "child_count", COALESCE("top_label", '(none)') AS "top_label", COALESCE(m_2, 0) AS "n"
FROM step_7
"""


class InnerJoinFlipsOnlyHopsTest(unittest.TestCase):
    """dlt__personio, batch10 run L (2026-09-11): `inner_join@second_hop` on
    a one-hop argmax mart flipped the LEFT join that attaches the extremal
    row (a QUALIFY CTE over the group's own rows, so every group matches),
    kept FULL reward on all five populations, and was reported as a live
    exploit. The attach is not a hop: the default variant leaves it alone
    and `second_hop` has no target on a one-hop mart."""

    def test_second_hop_has_no_target_on_a_one_hop_mart(self) -> None:
        self.assertIsNone(_apply(AttackKind.INNER_JOIN, "second_hop", ONE_HOP_ARGMAX))

    def test_the_default_variant_flips_the_source_join_and_keeps_the_attach(self) -> None:
        mutated = _apply(AttackKind.INNER_JOIN, "", ONE_HOP_ARGMAX)
        self.assertIsNotNone(mutated)
        joins = [(j.side or "", j.this.name) for j in sqlglot.parse_one(mutated, read="duckdb").find_all(exp.Join)]
        self.assertEqual(joins, [("", "children"), ("LEFT", "step_6")])

    def test_zero_is_missing_leaves_a_text_default_alone(self) -> None:
        """The same task, next run: NULLIF(top_label, 0) on a TEXT column was
        a bind error that scored the mart 0 on every population."""
        mutated = _apply(AttackKind.NO_NULL_DEFAULT, "zero_is_missing", ONE_HOP_ARGMAX)
        self.assertIsNotNone(mutated)
        self.assertIn('COALESCE("top_label", \'(none)\')', mutated)
        self.assertIn("NULLIF(m_2, 0)", mutated)

"""Group A fix tests: lookup-hop grain honesty (I4b), the fan-out guard that
accompanies lineage-aware grounding (C1), and the small typed helpers behind
staging types (I4a) — everything the reddit_ads manifest cannot exercise
because no hop survives there.

Fixture: an ads package whose report table joins an `account` dimension on
`account_id -> id` (rung 1 of the join recovery) and carries `currency` from
it, so a LOOKUP HOP survives into the plan.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from elt_taskgen.adapters import dbt as dbt_adapter
from elt_taskgen.generation.source_data import generate_rows
from elt_taskgen.models import ColumnType, MartOpKind, PopulationName, Relationship


def _col(name: str, data_type: str | None = None, description: str = "") -> dict:
    return {"name": name, "data_type": data_type, "description": description}


def _lookup_manifest(*, org: bool = False) -> dict:
    """report (fact-like) -> account (dimension) via `account_id = id`.

    The mart carries `max(accounts.currency)` from the dimension (an
    EXTREMA over the hop — a row-level fact, not a fan-out), so the lookup
    hop survives into the plan. With `org=True` the account's `id` is ALSO a
    foreign key to `org.org_id` (a declared relationships test), so the parent
    key cannot be minted unique and the hop must be refused.
    """
    manifest = {
        "metadata": {"project_name": "ads_pkg_integration_tests"},
        "sources": {
            "source.ads_pkg.ads.report": {
                "name": "report",
                "package_name": "ads_pkg",
                "columns": {
                    "account_id": _col("account_id"),
                    "date": _col("date"),
                    "clicks": _col("clicks"),
                    "spend": _col("spend"),
                },
            },
            "source.ads_pkg.ads.account": {
                "name": "account",
                "package_name": "ads_pkg",
                "columns": {
                    "id": _col("id"),
                    "currency": _col("currency"),
                    "name": _col("name"),
                },
            },
        },
        "nodes": {
            "model.ads_pkg.stg_ads__report": {
                "resource_type": "model",
                "name": "stg_ads__report",
                "package_name": "ads_pkg",
                "depends_on": {"nodes": ["source.ads_pkg.ads.report"]},
                "compiled_code": (
                    'with base as (select * from "db"."s"."report"), fields as ('
                    "select cast(null as text) as account_id, cast(null as date) as date, "
                    "cast(null as integer) as clicks, cast(null as float) as spend from base) "
                    "select account_id, date as date_day, clicks, spend from fields"
                ),
                "columns": {},
            },
            "model.ads_pkg.stg_ads__account": {
                "resource_type": "model",
                "name": "stg_ads__account",
                "package_name": "ads_pkg",
                "depends_on": {"nodes": ["source.ads_pkg.ads.account"]},
                "compiled_code": (
                    'select id as account_id, currency, name as account_name from "db"."s"."account"'
                ),
                "columns": {},
            },
            "model.ads_pkg.ads__account_report": {
                "resource_type": "model",
                "name": "ads__account_report",
                "package_name": "ads_pkg",
                "depends_on": {
                    "nodes": ["model.ads_pkg.stg_ads__report", "model.ads_pkg.stg_ads__account"]
                },
                "compiled_code": (
                    'with report as (select * from "db"."s"."stg_ads__report"), '
                    'accounts as (select * from "db"."s"."stg_ads__account") '
                    "select report.account_id, report.date_day, "
                    "max(accounts.currency) as currency, "
                    "sum(report.clicks) as clicks, sum(report.spend) as spend "
                    "from report left join accounts on report.account_id = accounts.account_id "
                    "group by 1, 2"
                ),
                "columns": {
                    "account_id": _col("account_id"),
                    "date_day": _col("date_day"),
                    "currency": _col("currency"),
                    "clicks": _col("clicks"),
                    "spend": _col("spend"),
                },
            },
            "test.ads_pkg.not_null_account_id": {
                "resource_type": "test",
                "test_metadata": {"name": "not_null", "kwargs": {"column_name": "account_id"}},
                "attached_node": "model.ads_pkg.ads__account_report",
                "depends_on": {"nodes": ["model.ads_pkg.ads__account_report"]},
            },
            "test.ads_pkg.not_null_date_day": {
                "resource_type": "test",
                "test_metadata": {"name": "not_null", "kwargs": {"column_name": "date_day"}},
                "attached_node": "model.ads_pkg.ads__account_report",
                "depends_on": {"nodes": ["model.ads_pkg.ads__account_report"]},
            },
        },
    }
    if org:
        manifest["sources"]["source.ads_pkg.ads.org"] = {
            "name": "org",
            "package_name": "ads_pkg",
            "columns": {"org_id": _col("org_id"), "region": _col("region")},
        }
        # The mart (not the single-source staging model) reads org, so the
        # `accounts` CTE still resolves to exactly one source table.
        manifest["nodes"]["model.ads_pkg.ads__account_report"]["depends_on"]["nodes"].append(
            "source.ads_pkg.ads.org"
        )
        manifest["nodes"]["test.ads_pkg.rel_account_org"] = {
            "resource_type": "test",
            "test_metadata": {
                "name": "relationships",
                "kwargs": {"column_name": "id", "to": "source('ads', 'org')", "field": "org_id"},
            },
            "attached_node": "source.ads_pkg.ads.account",
            "depends_on": {"nodes": ["source.ads_pkg.ads.org", "source.ads_pkg.ads.account"]},
        }
    return manifest


class LookupHopGrainHonestyTest(unittest.TestCase):
    """I4(b): a lookup hop's parent is minted unique on the join key, so the
    plan's "every base row appears exactly once" is TRUE in generated data;
    a parent keyed on foreign keys alone is refused with a reason."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _task(self, manifest: dict, name: str):
        path = Path(self.tmp.name) / name
        path.write_text(json.dumps(manifest), encoding="utf-8")
        result = dbt_adapter.extract_candidates(dbt_adapter.load_manifest(path))
        return result

    def test_the_hop_survives_and_its_parent_is_minted_unique(self) -> None:
        result = self._task(_lookup_manifest(), "lookup.json")
        self.assertEqual(len(result.tasks), 1, [s.reason for s in result.skipped])
        task = result.tasks[0]
        self.assertEqual(task.family_id, "dbt__ads_pkg")   # I6-D suffix stripped
        mart = task.marts[0]
        joins = [op for op in mart.plan.ops if op.kind is MartOpKind.JOIN]
        self.assertEqual(len(joins), 1)
        self.assertEqual(joins[0].tables[-1], "account")
        by_name = {t.name: t for t in task.tables}
        account = by_name["account"]
        self.assertEqual(account.primary_key, ("id",))
        self.assertFalse(account.column("id").nullable)
        self.assertEqual(by_name["report"].primary_key, ())
        # `verify_grain` (invariant 4) is quiet on the built cut...
        dbt_adapter.verify_grain(task)
        # ...and the hops it sees are exactly the plan's lookup joins.
        self.assertEqual(
            dbt_adapter._lookup_hops(mart, task.relationships, set(by_name)),
            [("account", ("id",))],
        )
        names = {c.name for c in mart.columns}
        self.assertEqual(names, {"account_id", "date_day", "currency", "clicks", "spend"})
        types = {c.name: c.type for c in mart.columns}
        self.assertEqual(types["date_day"], ColumnType.DATE)
        self.assertEqual(types["clicks"], ColumnType.BIGINT)   # INTEGER promoted
        self.assertEqual(types["spend"], ColumnType.FLOAT)     # staging float kept a SUM

    def test_stress_rows_never_duplicate_the_lookup_parent_key(self) -> None:
        task = self._task(_lookup_manifest(), "stress.json").tasks[0]
        for population in (PopulationName.PRIMARY, PopulationName.STRESS):
            rows = generate_rows(task, population)
            keys = [r["id"] for r in rows["account"]]
            self.assertEqual(len(keys), len(set(keys)), population)
            self.assertNotIn(None, keys)

    def test_verify_grain_refuses_a_lookup_onto_an_unminted_parent(self) -> None:
        task = self._task(_lookup_manifest(), "unminted.json").tasks[0]
        account = next(t for t in task.tables if t.name == "account")
        broken = task.model_copy(
            update={
                "tables": tuple(
                    t.model_copy(update={"primary_key": (), "business_key": ()})
                    if t.name == "account"
                    else t
                    for t in task.tables
                )
            }
        )
        with self.assertRaises(dbt_adapter.DeclaredGrainViolation) as ctx:
            dbt_adapter.verify_grain(broken)
        self.assertIn("account", str(ctx.exception))
        self.assertIn("not minted unique", str(ctx.exception))
        # a business-key-only minting also satisfies the invariant
        bk_only = task.model_copy(
            update={
                "tables": tuple(
                    t.model_copy(update={"primary_key": ("name",), "business_key": ("id",)})
                    if t.name == "account"
                    else t
                    for t in task.tables
                )
            }
        )
        dbt_adapter.verify_grain(bk_only)
        self.assertEqual(account.primary_key, ("id",))

    def test_an_all_foreign_key_composite_parent_is_refused_with_a_reason(self) -> None:
        result = self._task(_lookup_manifest(org=True), "allfk.json")
        self.assertEqual(len(result.tasks), 1, [s.reason for s in result.skipped])
        task = result.tasks[0]
        mart = task.marts[0]
        self.assertEqual([op for op in mart.plan.ops if op.kind is MartOpKind.JOIN], [])
        self.assertNotIn("currency", {c.name for c in mart.columns})
        self.assertIn("all foreign keys drawn with repetition", mart.plan.notes)
        self.assertIn("not provably one row per key", mart.plan.notes)
        account = next(t for t in task.tables if t.name == "account")
        self.assertEqual(account.primary_key, ())   # never declared unique on a FK
        dbt_adapter.verify_grain(task)

    def test_lookup_joins_returns_offers_and_refusals(self) -> None:
        rels = (
            Relationship(child_table="report", child_columns=("account_id",),
                         parent_table="account", parent_columns=("id",), required=False),
            Relationship(child_table="account", child_columns=("id",),
                         parent_table="org", parent_columns=("org_id",), required=False),
            Relationship(child_table="report", child_columns=("date", "account_id"),
                         parent_table="daily", parent_columns=("date", "account_id"),
                         required=False),
            Relationship(child_table="daily", child_columns=("account_id",),
                         parent_table="account", parent_columns=("id",), required=False),
        )
        offers, refused = dbt_adapter._lookup_joins("report", {"account", "daily", "org"}, rels)
        # account.id is a foreign key -> refused; daily's (date, account_id)
        # has a non-FK column (date) -> offered.
        self.assertEqual(sorted(offers), ["daily"])
        self.assertEqual(offers["daily"], (("date", "account_id"), ("date", "account_id")))
        self.assertEqual(sorted(refused), ["account"])
        self.assertIn("all foreign keys", refused["account"])
        # a second, mintable relationship to the same parent clears the refusal
        more = rels + (
            Relationship(child_table="report", child_columns=("account_id",),
                         parent_table="account", parent_columns=("name",), required=False),
        )
        offers, refused = dbt_adapter._lookup_joins("report", {"account"}, more)
        self.assertEqual(sorted(offers), ["account"])
        self.assertEqual(refused, {})


class FanOutGuardTest(unittest.TestCase):
    """C1 companion: an ADDITIVE aggregate whose every column lies on a lookup
    hop (by known lineage) is refused — summing a parent column once per base
    row is not what the package computes."""

    def _projection(self, expr: str, lineage, kind=dbt_adapter.ProjectionKind.AGGREGATE):
        return dbt_adapter._Projection(
            column="m", expr=expr, refs=tuple(sorted({n for n, _ in lineage})),
            kind=kind, is_aggregate=True, lineage=tuple(lineage),
        )

    def test_sum_over_hop_only_columns_is_refused(self) -> None:
        p = self._projection("SUM(report.clicks)", (("clicks", ("report",)),))
        reason = dbt_adapter._fans_out(p, "conv", ("report",))
        self.assertIn("fan-out", reason)
        self.assertIn("report", reason)

    def test_max_and_count_distinct_over_the_hop_are_row_level_facts(self) -> None:
        for expr, kind in (
            ("MAX(report.clicks)", dbt_adapter.ProjectionKind.EXTREMA),
            ("COUNT(DISTINCT report.clicks)", dbt_adapter.ProjectionKind.DISTINCT_AGGREGATE),
        ):
            p = self._projection(expr, (("clicks", ("report",)),), kind)
            self.assertEqual(dbt_adapter._fans_out(p, "conv", ("report",)), "", expr)

    def test_a_base_column_in_the_aggregate_or_unknown_lineage_keeps_it(self) -> None:
        mixed = self._projection(
            "SUM(conv.value * report.rate)",
            (("rate", ("report",)), ("value", ("conv",))),
        )
        self.assertEqual(dbt_adapter._fans_out(mixed, "conv", ("report",)), "")
        unknown = self._projection("SUM(x.clicks)", (("clicks", ("?x",)),))
        self.assertEqual(dbt_adapter._fans_out(unknown, "conv", ("report",)), "")
        no_hops = self._projection("SUM(report.clicks)", (("clicks", ("report",)),))
        self.assertEqual(dbt_adapter._fans_out(no_hops, "conv", ()), "")
        count_star = self._projection("COUNT(*)", ())
        self.assertEqual(dbt_adapter._fans_out(count_star, "conv", ("report",)), "")


class TypeHelperTest(unittest.TestCase):
    """I4(a) helpers: adopted-type precedence and the measure result type."""

    def test_adopt_column_type_precedence(self) -> None:
        declared = dbt_adapter.DbtColumn(name="x", data_type="varchar")
        undeclared = dbt_adapter.DbtColumn(name="x")
        A = dbt_adapter._adopt_column_type
        T = ColumnType
        self.assertEqual(A(declared, T.DATE, T.BIGINT), T.TEXT)     # declared wins
        self.assertEqual(A(undeclared, T.DATE, T.BIGINT), T.DATE)   # staging non-TEXT
        self.assertEqual(A(undeclared, T.TEXT, T.BIGINT), T.BIGINT) # use beats staging TEXT
        self.assertEqual(A(undeclared, T.TEXT, None), T.TEXT)
        self.assertEqual(A(undeclared, None, T.BOOLEAN), T.BOOLEAN)
        self.assertEqual(A(undeclared, None, None), T.TEXT)

    def test_measure_result_type(self) -> None:
        M = dbt_adapter._measure_result_type
        T = ColumnType
        bound = {"clicks": T.INTEGER, "spend": T.FLOAT, "amount": T.DECIMAL,
                 "flag": T.BOOLEAN, "event_name": T.TEXT, "ts": T.TIMESTAMP}
        self.assertEqual(M("COUNT(clicks)", bound), T.BIGINT)
        self.assertEqual(M("COUNT(DISTINCT clicks)", bound), T.BIGINT)
        self.assertEqual(M("SUM(clicks)", bound), T.BIGINT)          # INTEGER promoted
        self.assertEqual(M("SUM(spend)", bound), T.FLOAT)
        self.assertEqual(M("MIN(amount)", bound), T.DECIMAL)
        self.assertEqual(M("MAX(ts)", bound), T.TIMESTAMP)
        self.assertEqual(M("AVG(clicks)", bound), T.FLOAT)
        self.assertEqual(M("SUM(clicks) / NULLIF(SUM(spend), 0)", bound), T.FLOAT)
        self.assertEqual(
            M("SUM(CASE WHEN event_name = 'lead' THEN clicks ELSE 0 END)", bound), T.BIGINT
        )
        self.assertEqual(M("SUM(COALESCE(clicks, spend, 0))", bound), T.FLOAT)
        self.assertEqual(M("MAX(CASE WHEN flag THEN TRUE ELSE FALSE END)", bound), T.BOOLEAN)
        self.assertEqual(M("SUM(unknown_col)", bound), T.TEXT)
        self.assertEqual(M("not sql at all ((", bound), T.TEXT)

    def test_ground_column_requires_the_aliased_source_to_exist(self) -> None:
        """A package-wide alias entry cannot bind to a table that does not
        declare the renamed column (same-named source in another component)."""
        source_columns = {"orders": {"order_id", "amount"}}
        aliases = {"sales_order_id": {"orders": "order_id"}, "who": {"orders": "customer"}}
        self.assertEqual(
            dbt_adapter._ground_column("sales_order_id", "orders", source_columns, aliases),
            "order_id",
        )
        self.assertIsNone(dbt_adapter._ground_column("who", "orders", source_columns, aliases))
        self.assertEqual(
            dbt_adapter._ground_column("amount", "orders", source_columns, aliases), "amount"
        )


class SelectScopeTest(unittest.TestCase):
    """`_select_scope` / `_source_resolver` mechanics on a bare AST."""

    def test_scope_maps_aliases_and_subqueries(self) -> None:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(
            "select a.x, s.y from base as a left join (select y from other) as s "
            "on a.id = s.id join third on third.k = a.k",
            read="duckdb",
        )
        resolver = {
            "base": frozenset({"t_base"}),
            "other": frozenset({"t_other"}),
            "third": frozenset({"t_third"}),
        }
        scope = dbt_adapter._select_scope(tree, lambda n: resolver.get(n, frozenset({f"?{n}"})))
        self.assertEqual(scope["a"], frozenset({"t_base"}))
        self.assertEqual(scope["s"], frozenset({"t_other"}))
        self.assertEqual(scope["third"], frozenset({"t_third"}))
        # a subquery column's lineage is the inner select's scope
        inner = next(sq for sq in tree.find_all(exp.Subquery)).this
        self.assertEqual(
            dbt_adapter._select_scope(inner, lambda n: resolver.get(n, frozenset({f"?{n}"}))),
            {"other": frozenset({"t_other"})},
        )

    def test_output_select_chain_follows_select_star(self) -> None:
        import sqlglot

        tree = sqlglot.parse_one(
            "with a as (select 1 as x from t), b as (select * from a), "
            "c as (select x from b) select * from c",
            read="duckdb",
        )
        chain = dbt_adapter._output_select_chain(tree)
        self.assertEqual(len(chain), 2)          # root, then `c` (not `select *`)
        self.assertEqual(chain[1].sql(dialect="duckdb"), "SELECT x FROM b")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

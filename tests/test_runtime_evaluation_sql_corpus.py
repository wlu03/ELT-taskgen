"""Executable corpus for the AST-based evaluation-SQL guard and rewriter.

Every valid entry is executed against real populated DuckDB relations laid
out per destination, so the corpus pins semantics rather than byte layout.
"""

from __future__ import annotations

import logging
import unittest
from decimal import Decimal
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

from elt_taskgen.destinations import Destination
from elt_taskgen.runtime.evaluation import EvaluationError, prepare_evaluation_sql

DATABASE = "demo_database"

ALPHA_ROWS: list[tuple[Any, ...]] = [
    (1, Decimal("10.00"), "first"),
    (2, Decimal("20.50"), "copy of two"),
    (3, Decimal("30.00"), "third"),
]
BETA_ROWS: list[tuple[Any, ...]] = [
    (1, "one"),
    (2, "please JOIN demo_database.gamma; DROP TABLE x"),
]

# (label, stored answer-key SQL, rows expected from every destination layout)
VALID_CORPUS: list[tuple[str, str, list[tuple[Any, ...]]]] = [
    (
        "canonical exporter shape",
        "select * from demo_database.alpha order by id, total;",
        ALPHA_ROWS,
    ),
    (
        "cte consumed by from and join",
        "with totals as (select id, sum(total) as s from demo_database.alpha "
        "group by id) select t.id, t.s from totals t "
        "join demo_database.beta b on t.id = b.id order by t.id",
        [(1, Decimal("10.00")), (2, Decimal("20.50"))],
    ),
    (
        "quoted relation",
        'select * from "demo_database"."alpha" order by id',
        ALPHA_ROWS,
    ),
    (
        "mutation words inside a string literal",
        "select id, note from demo_database.beta "
        "where note <> 'DROP TABLE x; DELETE FROM y' order by id",
        BETA_ROWS,
    ),
    (
        "semicolon inside a string literal",
        "select id from demo_database.beta "
        "where note = 'please JOIN demo_database.gamma; DROP TABLE x' order by id",
        [(2,)],
    ),
    (
        "join text in a line comment",
        "select a.id from demo_database.alpha a -- join demo_database.gamma\n"
        "join demo_database.beta b on a.id = b.id order by a.id",
        [(1,), (2,)],
    ),
    (
        "block comment with mutation words",
        "select id /* COPY INSERT UPDATE */ from demo_database.alpha order by id",
        [(1,), (2,), (3,)],
    ),
    (
        "comma cross-join of two relations",
        "select a.id, b.id from demo_database.alpha a, demo_database.beta b "
        "order by a.id, b.id",
        [(1, 1), (1, 2), (2, 1), (2, 2), (3, 1), (3, 2)],
    ),
    (
        "table function alongside a real relation",
        "select a.id, r.range from demo_database.alpha a, range(2) r "
        "order by a.id, r.range",
        [(1, 0), (1, 1), (2, 0), (2, 1), (3, 0), (3, 1)],
    ),
    (
        "lateral derived table",
        "select a.id, u.x from demo_database.alpha a, "
        "lateral (select a.id + 1 as x) u order by a.id",
        [(1, 2), (2, 3), (3, 4)],
    ),
    (
        "derived-table subquery in from",
        "select s.id from (select id from demo_database.alpha) s order by s.id",
        [(1,), (2,), (3,)],
    ),
    (
        "in-subquery in where",
        "select id from demo_database.alpha "
        "where id in (select id from demo_database.beta) order by id",
        [(1,), (2,)],
    ),
    (
        "union all of two relations",
        "select id from demo_database.alpha union all "
        "select id from demo_database.beta order by id",
        [(1,), (1,), (2,), (2,), (3,)],
    ),
    (
        "leading comment before select",
        "-- preamble\nselect id from demo_database.alpha order by id",
        [(1,), (2,), (3,)],
    ),
]

_SNOWFLAKE_ONLY: tuple[dict[str, Any], ...] = ({},)
_ALL_DESTINATIONS: tuple[dict[str, Any], ...] = (
    {},
    {
        "destination": Destination.DATABRICKS,
        "physical_container": "benchmark_catalog",
    },
    {"destination": "redshift"},
)

# (stored SQL, EvaluationError regex, extra kwargs, destination variants)
INVALID_CORPUS: list[tuple[str, str, dict[str, Any], tuple[dict[str, Any], ...]]] = [
    ("delete from demo_database.alpha", "read-only SELECT", {}, _ALL_DESTINATIONS),
    (
        "insert into demo_database.gamma select id from demo_database.alpha",
        "read-only SELECT",
        {},
        _ALL_DESTINATIONS,
    ),
    (
        "create table demo_database.gamma as select id from demo_database.alpha",
        "read-only SELECT",
        {},
        _ALL_DESTINATIONS,
    ),
    ("drop table demo_database.alpha", "read-only SELECT", {}, _ALL_DESTINATIONS),
    ("truncate table demo_database.alpha", "read-only SELECT", {}, _ALL_DESTINATIONS),
    (
        "merge into demo_database.alpha a using demo_database.beta b "
        "on a.id = b.id when matched then update set note = 'x'",
        "read-only SELECT",
        {},
        _SNOWFLAKE_ONLY,
    ),
    ("alter session set timezone = 'UTC'", "read-only SELECT", {}, _SNOWFLAKE_ONLY),
    (
        "copy into demo_database.alpha from @stage1",
        "read-only SELECT",
        {},
        _SNOWFLAKE_ONLY,
    ),
    ("put file:///tmp/data.csv @stage1", "read-only SELECT", {}, _SNOWFLAKE_ONLY),
    ("remove @stage1", "read-only SELECT", {}, _SNOWFLAKE_ONLY),
    ("call demo_database.refresh_all()", "read-only SELECT", {}, _SNOWFLAKE_ONLY),
    (
        "explain select * from demo_database.alpha",
        "read-only SELECT",
        {},
        _SNOWFLAKE_ONLY,
    ),
    (
        "select id into gamma from demo_database.alpha",
        "read-only SELECT",
        {},
        _SNOWFLAKE_ONLY,
    ),
    (
        "select * from demo_database.alpha for update",
        "read-only SELECT",
        {},
        _SNOWFLAKE_ONLY,
    ),
    (
        "select * from demo_database.alpha; drop table demo_database.alpha",
        "exactly one statement",
        {},
        _ALL_DESTINATIONS,
    ),
    (
        "select * from other_db.alpha",
        "expected 'demo_database'",
        {},
        _ALL_DESTINATIONS,
    ),
    (
        "select * from cat.demo_database.alpha",
        "must be database.table",
        {},
        _ALL_DESTINATIONS,
    ),
    ("select * from alpha", "must be database.table", {}, _ALL_DESTINATIONS),
    (
        # The legacy regex silently forwarded the stage unrewritten.
        "select * from @stage1 join demo_database.alpha on true",
        "unsupported relation",
        {},
        _SNOWFLAKE_ONLY,
    ),
    ("select 1", "no database.table relation", {}, _ALL_DESTINATIONS),
    (
        "select * from demo_database.alpha order by id",
        "does not reference that mart",
        {"expected_mart": "summary"},
        _ALL_DESTINATIONS,
    ),
    (
        "select * from demo_database.alpha order by",
        "does not parse",
        {},
        _ALL_DESTINATIONS,
    ),
]


def _seed(con: duckdb.DuckDBPyConnection, prefix: str) -> None:
    con.execute(
        f"CREATE TABLE {prefix}.alpha (id INTEGER, total DECIMAL(10,2), note VARCHAR)"
    )
    con.execute(f"CREATE TABLE {prefix}.beta (id INTEGER, note VARCHAR)")
    con.executemany(f"INSERT INTO {prefix}.alpha VALUES (?, ?, ?)", ALPHA_ROWS)
    con.executemany(f"INSERT INTO {prefix}.beta VALUES (?, ?)", BETA_ROWS)


def _snowflake_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS demo_database")
    con.execute("CREATE SCHEMA demo_database.AIRBYTE_SCHEMA")
    _seed(con, "demo_database.AIRBYTE_SCHEMA")
    return con


def _redshift_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE SCHEMA demo_database")
    _seed(con, "demo_database")
    return con


def _databricks_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS benchmark_catalog")
    con.execute("CREATE SCHEMA benchmark_catalog.demo_database")
    _seed(con, "benchmark_catalog.demo_database")
    return con


class EvaluationSqlCorpusTests(unittest.TestCase):
    def _execute(
        self,
        con: duckdb.DuckDBPyConnection,
        prepared: str,
        expected_rows: list[tuple[Any, ...]],
    ) -> None:
        try:
            self.assertEqual(con.execute(prepared).fetchall(), expected_rows)
        finally:
            con.close()

    @staticmethod
    def _relation_tables(prepared: str, read: str) -> list[exp.Table]:
        """Real relation nodes of one prepared statement, CTE aliases skipped."""

        tree = sqlglot.parse_one(prepared.rstrip(";"), read=read)
        aliases = {cte.alias_or_name.casefold() for cte in tree.find_all(exp.CTE)}
        tables: list[exp.Table] = []
        for table in tree.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                continue
            if not table.db and table.name.casefold() in aliases:
                continue
            tables.append(table)
        return tables

    def test_valid_corpus_executes_on_snowflake_layout(self) -> None:
        for label, stored, expected_rows in VALID_CORPUS:
            with self.subTest(label=label):
                prepared = prepare_evaluation_sql(
                    stored,
                    DATABASE,
                    destination=Destination.SNOWFLAKE,
                )
                self._execute(_snowflake_connection(), prepared, expected_rows)

    def test_valid_corpus_executes_on_redshift_layout(self) -> None:
        for label, stored, expected_rows in VALID_CORPUS:
            with self.subTest(label=label):
                prepared = prepare_evaluation_sql(
                    stored,
                    DATABASE,
                    destination="redshift",
                    physical_container="connection_database",
                )
                self._execute(_redshift_connection(), prepared, expected_rows)

    def test_valid_corpus_rewrites_and_executes_for_databricks(self) -> None:
        for label, stored, expected_rows in VALID_CORPUS:
            with self.subTest(label=label):
                prepared = prepare_evaluation_sql(
                    stored,
                    DATABASE,
                    destination=Destination.DATABRICKS,
                    physical_container="benchmark_catalog",
                )
                for table in self._relation_tables(prepared, "databricks"):
                    self.assertEqual(
                        (table.catalog, table.db),
                        ("benchmark_catalog", "demo_database"),
                    )
                    self.assertIn(table.name, {"alpha", "beta"})
                # DuckDB rejects backtick quoting, so the databricks-form
                # statement is executed through a dialect transpile.
                transpiled = sqlglot.transpile(
                    prepared,
                    read="databricks",
                    write="duckdb",
                )[0]
                self._execute(_databricks_connection(), transpiled, expected_rows)

    def test_prepared_sql_leaves_no_unqualified_relations(self) -> None:
        expectations = [
            (Destination.SNOWFLAKE, {}, "snowflake", "DEMO_DATABASE", "AIRBYTE_SCHEMA"),
            ("redshift", {"destination": "redshift"}, "redshift", "", "demo_database"),
        ]
        for label, stored, _ in VALID_CORPUS:
            for destination, kwargs, read, catalog, db in expectations:
                with self.subTest(label=label, destination=str(destination)):
                    prepared = prepare_evaluation_sql(stored, DATABASE, **kwargs)
                    tables = self._relation_tables(prepared, read)
                    for table in tables:
                        self.assertEqual((table.catalog, table.db), (catalog, db))

    def test_digit_leading_namespace_parses_rewrites_and_executes(self) -> None:
        stored = "select * from 1demo.summary order by id"
        snowflake = prepare_evaluation_sql(stored, "1demo", expected_mart="summary")
        self.assertEqual(
            snowflake,
            'SELECT * FROM "1DEMO"."AIRBYTE_SCHEMA"."SUMMARY" ORDER BY id',
        )
        redshift = prepare_evaluation_sql(
            stored,
            "1demo",
            expected_mart="summary",
            destination="redshift",
        )
        con = duckdb.connect()
        con.execute('CREATE SCHEMA "1demo"')
        con.execute('CREATE TABLE "1demo".summary (id INTEGER)')
        con.executemany('INSERT INTO "1demo".summary VALUES (?)', [(2,), (1,)])
        self._execute(con, redshift, [(1,), (2,)])

    def test_invalid_corpus_is_refused(self) -> None:
        # Unsupported statements (PUT, REMOVE, CALL, EXPLAIN) log a sqlglot
        # Command-fallback warning before being refused; keep the run quiet.
        sqlglot_logger = logging.getLogger("sqlglot")
        previous_level = sqlglot_logger.level
        sqlglot_logger.setLevel(logging.ERROR)
        self.addCleanup(sqlglot_logger.setLevel, previous_level)

        for stored, pattern, extra_kwargs, variants in INVALID_CORPUS:
            for destination_kwargs in variants:
                with self.subTest(
                    sql=stored,
                    destination=str(destination_kwargs.get("destination", "snowflake")),
                ):
                    with self.assertRaisesRegex(EvaluationError, pattern):
                        prepare_evaluation_sql(
                            stored,
                            DATABASE,
                            **extra_kwargs,
                            **destination_kwargs,
                        )


if __name__ == "__main__":
    unittest.main()

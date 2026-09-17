"""Reserved-word identifiers remain usable across the trusted SQL lifecycle."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import duckdb

from elt_taskgen.destinations import Destination
from elt_taskgen.export.eltbench import (
    evaluation_sql,
    materialize_warehouse,
    warehouse_census,
)
from elt_taskgen.generation.mart_plan import build_projection
from elt_taskgen.generation.source_data import render_postgres
from elt_taskgen.models import (
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    MartColumn,
    MartSpec,
    Origin,
    TableSpec,
    TaskIR,
)
from elt_taskgen.reference.solution import (
    compile_plan_sql,
    load_sources_duckdb,
)
from elt_taskgen.runtime.evaluation import _table_relation, prepare_evaluation_sql
from elt_taskgen.sql_identifiers import (
    identifier_needs_quoting,
    quote_sql_identifier,
    quote_sql_path,
)
from elt_taskgen.verification.filters import run_intake_filters
from elt_taskgen.verification.strict_diagnostic import load_sources_duckdb_strict


def _reserved_task() -> TaskIR:
    table = TableSpec(
        name="group",
        description="A source whose name is a SQL reserved word.",
        columns=(
            ColumnSpec(
                name="select",
                type=ColumnType.INTEGER,
                description="A reserved-word integer column.",
            ),
            ColumnSpec(
                name="label",
                type=ColumnType.TEXT,
                description="A label for the value.",
            ),
        ),
        primary_key=("select",),
    )
    companion = TableSpec(
        name="members",
        description="A second source keeps the task intake-eligible.",
        columns=(
            ColumnSpec(
                name="member_id",
                type=ColumnType.INTEGER,
                description="A member identifier.",
            ),
        ),
        primary_key=("member_id",),
    )
    built = build_projection(
        mart="group_projection",
        table=table.name,
        select_map=(("select", "select"), ("label", "label")),
        key_columns=("select",),
    )
    mart = MartSpec(
        name="group_projection",
        description="The source projected unchanged.",
        grain="One row per select value.",
        key_columns=("select",),
        columns=(
            MartColumn(
                name="select",
                type=ColumnType.INTEGER,
                description="The source value.",
            ),
            MartColumn(
                name="label",
                type=ColumnType.TEXT,
                description="The source label.",
            ),
        ),
        plan=built.plan,
    )
    return TaskIR(
        task_id="test__reserved_group",
        family_id="test__reserved_group",
        cluster_id="test__reserved_group",
        origin=Origin.SYNTHETIC,
        license="CC0-1.0",
        tables=(table, companion),
        backends=tuple(
            BackendAssignment(table=source.name, backend=Backend.POSTGRES)
            for source in (table, companion)
        ),
        marts=(mart,),
    )


class SqlIdentifierTest(unittest.TestCase):
    def test_quotes_keywords_by_dialect_and_preserves_plain_names(self) -> None:
        self.assertFalse(identifier_needs_quoting("customers"))
        self.assertTrue(identifier_needs_quoting("group"))
        self.assertEqual(quote_sql_identifier("customers"), "customers")
        self.assertEqual(quote_sql_identifier("values"), "values")
        self.assertEqual(quote_sql_identifier("group"), '"group"')
        self.assertEqual(
            quote_sql_identifier("group", dialect="databricks"), "`group`"
        )
        self.assertEqual(
            quote_sql_path(("main", "group"), dialect="duckdb"),
            'main."group"',
        )
        self.assertEqual(
            quote_sql_identifier('a"b', force=True),
            '"a""b"',
        )

    def test_group_table_round_trips_loading_reference_and_export(self) -> None:
        task = _reserved_task()
        table = task.tables[0]
        rows = [
            {"select": 2, "label": "two"},
            {"select": 1, "label": "one"},
        ]
        intake = run_intake_filters(task, ())
        self.assertTrue(all(report.passed for report in intake))
        self.assertNotIn(
            "rule:reserved-word-relation-name",
            intake[0].evidence,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = render_postgres(table, rows, root / Backend.POSTGRES.value)
            render_postgres(
                task.tables[1],
                [{"member_id": 1}],
                root / Backend.POSTGRES.value,
            )
            rendered = script.read_text(encoding="utf-8")
            self.assertIn('CREATE TABLE "group"', rendered)
            self.assertIn(
                'INSERT INTO "group" ("select", "label")',
                rendered,
            )

            connection = duckdb.connect(":memory:")
            try:
                loaded = load_sources_duckdb(task, root, connection)
                self.assertEqual(loaded.counts, {"group": 2, "members": 1})
                sql = compile_plan_sql(task, task.marts[0])
                self.assertIn('"group"."select"', sql)
                self.assertIn('FROM "group" AS "group"', sql)
                self.assertEqual(
                    connection.execute(sql).fetchall(),
                    [(1, "one"), (2, "two")],
                )
            finally:
                connection.close()

            strict = duckdb.connect(":memory:")
            try:
                self.assertEqual(
                    load_sources_duckdb_strict(task, root, strict),
                    {"group": 2, "members": 1},
                )
            finally:
                strict.close()

            warehouse = root / "primary.duckdb"
            materialize_warehouse(
                task,
                SimpleNamespace(
                    stage1={"primary": {"group": 2, "members": 1}}
                ),
                "primary",
                root,
                warehouse,
            )
            census = warehouse_census(warehouse)
            self.assertEqual(census["tables"]["group"]["row_count"], 2)

    def test_evaluator_quotes_group_for_every_destination(self) -> None:
        self.assertEqual(
            _table_relation(
                "bench",
                "group",
                destination=Destination.SNOWFLAKE,
                physical_container=None,
            ),
            '"BENCH"."AIRBYTE_SCHEMA"."GROUP"',
        )
        self.assertEqual(
            _table_relation(
                "bench",
                "group",
                destination=Destination.DATABRICKS,
                physical_container="catalog",
            ),
            "`catalog`.`bench`.`group`",
        )
        self.assertEqual(
            _table_relation(
                "bench",
                "group",
                destination=Destination.REDSHIFT,
                physical_container=None,
            ),
            '"bench"."group"',
        )

        mart = _reserved_task().marts[0].model_copy(update={"name": "group"})
        self.assertEqual(
            evaluation_sql(mart, database="bench"),
            'select * from bench."group" order by "select", label;\n',
        )
        self.assertEqual(
            prepare_evaluation_sql(
                evaluation_sql(mart, database="bench"),
                "bench",
                expected_mart="group",
                destination=Destination.DATABRICKS,
                physical_container="catalog",
            ),
            "SELECT * FROM `catalog`.`bench`.`group` "
            "ORDER BY `select` NULLS LAST, label NULLS LAST;",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

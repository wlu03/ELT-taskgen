from __future__ import annotations

import datetime
import decimal
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import duckdb
import yaml

from elt_taskgen.destinations import Destination
from elt_taskgen.models import task_from_json
from elt_taskgen.runtime.evaluation import (
    CountMismatch,
    EvaluationError,
    evaluate_end_to_end_release,
    evaluate_stage1,
    evaluate_stage1_release,
    evaluate_stage2,
    evaluate_stage2_release,
    prepare_evaluation_sql,
)
from elt_taskgen.verification import strict_diagnostic, upstream_eval
from tests.workspace_proxy_fixture import (
    TASK_ID as PORTABLE_TASK_ID,
    portable_five_backend_release,
)


@dataclass
class _Response:
    sql: str
    rows: list[tuple[Any, ...]]
    description: Any = (("VALUE",),)
    error: Exception | None = None


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection
        self.description: Any = None
        self._rows: list[tuple[Any, ...]] = []
        self.closed = False

    def execute(self, sql: str) -> None:
        self.connection.executed.append(sql)
        if not self.connection.responses:
            raise AssertionError(f"unexpected SQL: {sql}")
        response = self.connection.responses.pop(0)
        if sql != response.sql:
            raise AssertionError(f"SQL mismatch:\nwant: {response.sql}\n got: {sql}")
        if response.error is not None:
            raise response.error
        self.description = response.description
        self._rows = response.rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self, responses: list[_Response] | None = None) -> None:
        self.responses = list(responses or [])
        self.executed: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def assert_consumed(self, case: unittest.TestCase) -> None:
        case.assertEqual(self.responses, [])


class RuntimeSnowflakeEvaluationTests(unittest.TestCase):
    TASK = "demo_task"
    DATABASE = "demo_database"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.public = self.root / "public" / self.TASK
        self.answer_key = self.root / "private" / self.TASK / "answer_key"
        self.public.mkdir(parents=True)
        self.answer_key.mkdir(parents=True)
        (self.public / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "snowflake": {
                        "config": {
                            "database": self.DATABASE,
                            "schema": "AIRBYTE_SCHEMA",
                        }
                    }
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_json(self, relative: str, payload: Any) -> Path:
        path = self.answer_key / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    def _write_stage1_key(self, counts: dict[str, int]) -> None:
        self._write_json("table.json", {self.DATABASE: counts})

    def _write_stage2_bundle(
        self,
        marts: list[dict[str, Any]],
        *,
        population: str = "primary",
        use_gt: bool = False,
        gold: dict[str, str] | None = None,
    ) -> None:
        (self.public / "data_model.yaml").write_text(
            yaml.safe_dump({"models": marts}, sort_keys=False), encoding="utf-8"
        )
        sort_keys = {
            mart["name"]: list(mart["key_columns"])
            for mart in marts
        }
        self._write_json("sort_key.json", {self.DATABASE: sort_keys})
        for mart in marts:
            name = mart["name"]
            sql_path = self.answer_key / "evaluation" / "sql" / f"{name}.sql"
            sql_path.parent.mkdir(parents=True, exist_ok=True)
            sql_path.write_text(
                f"select * from {self.DATABASE}.{name} order by "
                f"{', '.join(mart['key_columns'])};\n",
                encoding="utf-8",
            )
            if gold is not None:
                base = self.answer_key / ("gt" if use_gt else f"gold/{population}")
                base.mkdir(parents=True, exist_ok=True)
                (base / f"{name}.csv").write_text(gold[name], encoding="utf-8")

    @staticmethod
    def _mart(name: str) -> dict[str, Any]:
        return {
            "name": name,
            "description": f"The {name} mart",
            "grain": "one row per id",
            "key_columns": ["id"],
            "columns": [
                {"name": "id", "type": "integer", "description": "Identifier"},
                {"name": "total", "type": "decimal", "description": "Total"},
            ],
        }

    @property
    def _list_tables_sql(self) -> str:
        return (
            'SELECT TABLE_NAME FROM "DEMO_DATABASE"."INFORMATION_SCHEMA"."TABLES" '
            "WHERE TABLE_SCHEMA = 'AIRBYTE_SCHEMA' AND TABLE_TYPE = 'BASE TABLE' "
            "ORDER BY TABLE_NAME"
        )

    @staticmethod
    def _count_sql(table: str) -> str:
        return f'SELECT COUNT(*) FROM "DEMO_DATABASE"."AIRBYTE_SCHEMA"."{table.upper()}"'

    @staticmethod
    def _semantic_gate_stage1_connection(
        release: Path,
        *,
        corrupt_table: str | None = None,
        unexpected_table: bool = False,
        additional_tables: tuple[str, ...] = (),
        include_source_metadata: bool = False,
        type_invalid_table: str | None = None,
        repetitions: int = 1,
    ) -> _Connection:
        task_id = PORTABLE_TASK_ID
        private = release / "private" / task_id
        task = task_from_json(
            (private / "semantic" / "task_ir.json").read_text(encoding="utf-8")
        )
        counts_payload = json.loads(
            (private / "answer_key" / "table.json").read_text(encoding="utf-8")
        )
        counts = counts_payload[task_id]
        listed = [(name.upper(),) for name in counts]
        if unexpected_table:
            listed.append(("UNEXPECTED_RAW",))
        listed.extend((name.upper(),) for name in additional_tables)
        responses = [
            _Response(
                'SELECT TABLE_NAME FROM "GATE__FIVE_BACKEND_PROBE".'
                '"INFORMATION_SCHEMA"."TABLES" '
                "WHERE TABLE_SCHEMA = 'AIRBYTE_SCHEMA' "
                "AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME",
                listed,
            )
        ]
        for name in sorted(counts, key=str.casefold):
            responses.append(
                _Response(
                    'SELECT COUNT(*) FROM "GATE__FIVE_BACKEND_PROBE".'
                    f'"AIRBYTE_SCHEMA"."{name.upper()}"',
                    [(counts[name] * repetitions,)],
                )
            )

        local = duckdb.connect(":memory:")
        try:
            strict_diagnostic.load_sources_duckdb_strict(
                task,
                private / "populations" / "primary" / "rendered",
                local,
            )
            tables = {table.name: table for table in task.tables}
            for name in counts:
                table = tables[name]
                columns = tuple(column.name for column in table.columns)
                projected = ", ".join(f'"{column}"' for column in columns)
                raw_rows = [
                    tuple(row)
                    for row in local.execute(
                        f'SELECT {projected} FROM "{name}"'
                    ).fetchall()
                ]
                if name == corrupt_table:
                    changed = list(raw_rows[0])
                    text_index = next(
                        index
                        for index, column in enumerate(table.columns)
                        if column.type.value == "text"
                    )
                    changed[text_index] = str(changed[text_index]) + "-wrong"
                    raw_rows[0] = tuple(changed)
                if name == type_invalid_table:
                    changed = list(raw_rows[0])
                    integer_index = next(
                        index
                        for index, column in enumerate(table.columns)
                        if column.type.value == "integer"
                    )
                    changed[integer_index] = "not-an-integer"
                    raw_rows[0] = tuple(changed)
                metadata_columns = ["_AIRBYTE_RAW_ID"]
                if include_source_metadata:
                    backend = task.backend_for(name).backend.value
                    if backend == "mongodb":
                        metadata_columns.extend(
                            (
                                "_ID",
                                "_AB_CDC_CURSOR",
                                "_AB_CDC_DELETED_AT",
                                "_AB_CDC_UPDATED_AT",
                            )
                        )
                    elif backend in {"files", "s3"}:
                        metadata_columns.extend(
                            (
                                "_AB_SOURCE_FILE_URL",
                                "_AB_SOURCE_FILE_LAST_MODIFIED",
                            )
                        )
                rows = [
                    row + tuple("ignored-metadata" for _ in metadata_columns)
                    for row in raw_rows * repetitions
                ]
                description = tuple((column.upper(),) for column in columns) + tuple(
                    (column,) for column in metadata_columns
                )
                responses.append(
                    _Response(
                        'SELECT * FROM "GATE__FIVE_BACKEND_PROBE".'
                        f'"AIRBYTE_SCHEMA"."{name.upper()}" LIMIT '
                        f'{counts[name] * repetitions + 1}',
                        rows,
                        description=description,
                    )
                )
        finally:
            local.close()
        return _Connection(responses)

    @staticmethod
    def _semantic_gate_stage2_responses(release: Path) -> list[_Response]:
        private = release / "private" / PORTABLE_TASK_ID / "answer_key"
        responses: list[_Response] = []
        for mart in ("customer_rollup", "event_wide"):
            stored_sql = (
                private / "evaluation" / "sql" / f"{mart}.sql"
            ).read_text(encoding="utf-8")
            sql = prepare_evaluation_sql(
                stored_sql,
                PORTABLE_TASK_ID,
                expected_mart=mart,
            )
            gold_csv = (
                private / "gold" / "primary" / f"{mart}.csv"
            ).read_text(encoding="utf-8")
            columns, rows = upstream_eval.parse_canonical_csv(gold_csv)
            responses.append(
                _Response(
                    sql,
                    [tuple(row[column] for column in columns) for row in rows],
                    description=tuple((column,) for column in columns),
                )
            )
        return responses

    def test_stage1_release_reports_unexpected_without_penalizing_upstream_reward(self) -> None:
        self._write_stage1_key({"customers": 2, "orders": 1})
        connection = _Connection(
            [
                _Response(
                    self._list_tables_sql,
                    [("CUSTOMERS",), ("orders",), ("dbt_artifact",)],
                ),
                _Response(self._count_sql("customers"), [(2,)]),
                _Response(self._count_sql("orders"), [(1,)]),
            ]
        )

        result = evaluate_stage1_release(self.root, self.TASK, connection)

        self.assertTrue(result.stage1_pass)
        self.assertEqual(result.reward, 1.0)
        self.assertEqual(result.missing_tables, ())
        self.assertEqual(result.unexpected_tables, ("dbt_artifact",))
        self.assertEqual(result.count_mismatches, {})
        self.assertEqual(result.errors, {})
        self.assertEqual(result.actual_counts, {"customers": 2, "orders": 1})
        self.assertEqual(result.physical_container, "")
        connection.assert_consumed(self)

    def test_stage1_certification_detects_same_count_wrong_content(self) -> None:
        with portable_five_backend_release() as release:
            connection = self._semantic_gate_stage1_connection(
                release, corrupt_table="customers"
            )
            result = evaluate_stage1_release(
                release,
                PORTABLE_TASK_ID,
                connection,
                certification_strict=True,
            )

        self.assertTrue(result.passed)
        self.assertEqual(result.reward, 1.0)
        self.assertFalse(result.certification_passed)
        self.assertEqual(result.canonical_mismatch_codes["customers"], "values")
        self.assertFalse(result.canonical_table_scores["customers"])
        self.assertTrue(
            all(
                matched
                for table, matched in result.canonical_table_scores.items()
                if table != "customers"
            )
        )
        connection.assert_consumed(self)

    def test_strict_end_to_end_gates_stage2_on_same_count_wrong_raw_data(
        self,
    ) -> None:
        with portable_five_backend_release() as release:
            connection = self._semantic_gate_stage1_connection(
                release, corrupt_table="customers"
            )
            result = evaluate_end_to_end_release(
                release,
                PORTABLE_TASK_ID,
                connection,
                certification_strict=True,
            )

        self.assertTrue(result.stage1.passed)
        self.assertFalse(result.stage1.certification_passed)
        self.assertIsNone(result.stage2)
        self.assertFalse(result.certification_passed)
        self.assertEqual(result.reward, 0.0)
        connection.assert_consumed(self)

    def test_strict_end_to_end_allows_only_declared_marts_after_stage2(
        self,
    ) -> None:
        with portable_five_backend_release() as release:
            connection = self._semantic_gate_stage1_connection(
                release,
                additional_tables=("customer_rollup", "event_wide"),
            )
            connection.responses.extend(
                self._semantic_gate_stage2_responses(release)
            )
            result = evaluate_end_to_end_release(
                release,
                PORTABLE_TASK_ID,
                connection,
                certification_strict=True,
            )

        self.assertTrue(result.stage1.certification_passed)
        self.assertEqual(result.stage1.unexpected_tables, ())
        self.assertIsNotNone(result.stage2)
        self.assertTrue(result.certification_passed)
        self.assertEqual(result.reward, 1.0)
        connection.assert_consumed(self)

    def test_stage1_certification_passes_exact_content_and_rejects_extra_table(
        self,
    ) -> None:
        with portable_five_backend_release() as release:
            exact = self._semantic_gate_stage1_connection(release)
            exact_result = evaluate_stage1_release(
                release,
                PORTABLE_TASK_ID,
                exact,
                certification_strict=True,
            )
            self.assertTrue(exact_result.certification_passed)
            self.assertTrue(all(exact_result.canonical_table_scores.values()))
            exact.assert_consumed(self)

            extra = self._semantic_gate_stage1_connection(
                release, unexpected_table=True
            )
            extra_result = evaluate_stage1_release(
                release,
                PORTABLE_TASK_ID,
                extra,
                certification_strict=True,
            )
            self.assertTrue(extra_result.passed)
            self.assertTrue(all(extra_result.canonical_table_scores.values()))
            self.assertFalse(extra_result.certification_passed)
            self.assertEqual(extra_result.unexpected_tables, ("UNEXPECTED_RAW",))
            extra.assert_consumed(self)

    def test_stage1_certification_allows_backend_scoped_source_metadata(
        self,
    ) -> None:
        with portable_five_backend_release() as release:
            connection = self._semantic_gate_stage1_connection(
                release,
                include_source_metadata=True,
            )
            result = evaluate_stage1_release(
                release,
                PORTABLE_TASK_ID,
                connection,
                certification_strict=True,
            )

        self.assertTrue(result.certification_passed)
        self.assertTrue(all(result.canonical_table_scores.values()))
        connection.assert_consumed(self)

    def test_stage1_type_failure_is_not_misreported_as_schema_drift(self) -> None:
        with portable_five_backend_release() as release:
            connection = self._semantic_gate_stage1_connection(
                release,
                type_invalid_table="customers",
            )
            result = evaluate_stage1_release(
                release,
                PORTABLE_TASK_ID,
                connection,
                certification_strict=True,
            )

        self.assertEqual(
            result.canonical_mismatch_codes["customers"],
            "values:type",
        )
        self.assertFalse(result.canonical_table_scores["customers"])
        connection.assert_consumed(self)

    def test_stage1_certification_proves_two_full_refresh_append_copies(
        self,
    ) -> None:
        with portable_five_backend_release() as release:
            connection = self._semantic_gate_stage1_connection(
                release, repetitions=2
            )
            result = evaluate_stage1_release(
                release,
                PORTABLE_TASK_ID,
                connection,
                certification_strict=True,
                expected_repetitions=2,
            )

        self.assertEqual(result.expected_repetitions, 2)
        self.assertTrue(result.passed)
        self.assertTrue(result.certification_passed)
        self.assertTrue(all(result.canonical_table_scores.values()))
        connection.assert_consumed(self)

    def test_stage1_repetition_count_must_be_positive_integer(self) -> None:
        self._write_stage1_key({"customers": 1})
        for invalid in (0, -1, True):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    EvaluationError, "expected_repetitions"
                ):
                    evaluate_stage1(
                        _Connection(),
                        self.answer_key,
                        expected_repetitions=invalid,
                    )

    def test_stage1_separates_missing_unexpected_and_count_mismatch(self) -> None:
        self._write_stage1_key({"customers": 2, "orders": 1})
        connection = _Connection(
            [
                _Response(self._list_tables_sql, [("customers",), ("MART",)]),
                _Response(self._count_sql("customers"), [(3,)]),
            ]
        )

        result = evaluate_stage1(connection, self.answer_key)

        self.assertFalse(result.passed)
        self.assertEqual(result.reward, 0.0)
        self.assertEqual(result.missing_tables, ("orders",))
        self.assertEqual(result.unexpected_tables, ("MART",))
        self.assertEqual(
            result.count_mismatches,
            {"customers": CountMismatch(expected=2, actual=3)},
        )
        self.assertEqual(result.detail["customers"], "expected 2 rows, got 3")
        self.assertEqual(result.detail["orders"], "table not found")
        connection.assert_consumed(self)

    def test_stage1_uses_selected_non_primary_population_counts(self) -> None:
        self._write_stage1_key({"customers": 10})
        self._write_json("gold/development/stage1_counts.json", {"customers": 2})
        connection = _Connection(
            [
                _Response(self._list_tables_sql, [("customers",)]),
                _Response(self._count_sql("customers"), [(2,)]),
            ]
        )

        result = evaluate_stage1(
            connection, self.answer_key, population="development"
        )

        self.assertEqual(result.population, "development")
        self.assertEqual(result.expected_counts, {"customers": 2})
        self.assertEqual(result.reward, 1.0)
        connection.assert_consumed(self)

    def test_stage1_fails_closed_on_conflicting_primary_answer_keys(self) -> None:
        self._write_stage1_key({"customers": 10})
        self._write_json("gold/primary/stage1_counts.json", {"customers": 9})
        connection = _Connection()

        with self.assertRaisesRegex(EvaluationError, "counts disagree"):
            evaluate_stage1(connection, self.answer_key)
        self.assertEqual(connection.executed, [])

    def test_end_to_end_reward_skips_transform_when_stage1_fails(self) -> None:
        self._write_stage1_key({"customers": 1})
        connection = _Connection(
            [_Response(self._list_tables_sql, [])]
        )

        result = evaluate_end_to_end_release(
            self.root, self.TASK, connection
        )

        self.assertFalse(result.stage1.passed)
        self.assertIsNone(result.stage2)
        self.assertEqual(result.reward, 0.0)
        self.assertEqual(connection.executed, [self._list_tables_sql])

    def test_prepare_evaluation_sql_maps_every_from_and_join_relation(self) -> None:
        sql = (
            "select a.id from demo_database.alpha a "
            "join DEMO_DATABASE.beta b on a.id = b.id;"
        )

        rewritten = prepare_evaluation_sql(sql, self.DATABASE, expected_mart="alpha")

        self.assertEqual(
            rewritten,
            'SELECT a.id FROM "DEMO_DATABASE"."AIRBYTE_SCHEMA"."ALPHA" AS a '
            'JOIN "DEMO_DATABASE"."AIRBYTE_SCHEMA"."BETA" AS b ON a.id = b.id;',
        )

        rewritten_cte = prepare_evaluation_sql(
            "with totals as (select id from demo_database.alpha) "
            "select t.id from totals t join demo_database.beta b on t.id = b.id",
            self.DATABASE,
        )
        self.assertIn('FROM "DEMO_DATABASE"."AIRBYTE_SCHEMA"."ALPHA"', rewritten_cte)
        self.assertIn("FROM totals", rewritten_cte)
        self.assertIn('JOIN "DEMO_DATABASE"."AIRBYTE_SCHEMA"."BETA"', rewritten_cte)

        literal = prepare_evaluation_sql(
            "select id from demo_database.alpha where note <> 'DROP TABLE x'",
            self.DATABASE,
        )
        self.assertIn("'DROP TABLE x'", literal)

        with self.assertRaisesRegex(EvaluationError, "expected 'demo_database'"):
            prepare_evaluation_sql("select * from other.alpha", self.DATABASE)
        with self.assertRaisesRegex(EvaluationError, "exactly one statement"):
            prepare_evaluation_sql(
                "select * from demo_database.alpha; drop table alpha", self.DATABASE
            )
        with self.assertRaisesRegex(EvaluationError, "invalid Snowflake database"):
            prepare_evaluation_sql("select 1", 'demo";drop')

    def test_prepare_evaluation_sql_maps_databricks_and_redshift_namespaces(self) -> None:
        stored = (
            "select a.id from demo_database.alpha a "
            "join DEMO_DATABASE.beta b on a.id = b.id;"
        )

        databricks = prepare_evaluation_sql(
            stored,
            self.DATABASE,
            expected_mart="alpha",
            destination=Destination.DATABRICKS,
            physical_container="benchmark_catalog",
        )
        self.assertEqual(
            databricks,
            "SELECT a.id FROM `benchmark_catalog`.`demo_database`.`alpha` AS a "
            "JOIN `benchmark_catalog`.`demo_database`.`beta` AS b ON a.id = b.id;",
        )

        redshift = prepare_evaluation_sql(
            stored,
            self.DATABASE,
            expected_mart="alpha",
            destination="redshift",
            physical_container="connection_database",
        )
        self.assertEqual(
            redshift,
            'SELECT a.id FROM "demo_database"."alpha" AS a '
            'JOIN "demo_database"."beta" AS b ON a.id = b.id;',
        )

    def test_databricks_requires_a_safe_physical_catalog_before_queries(self) -> None:
        self._write_stage1_key({"customers": 1})
        connection = _Connection()
        with self.assertRaisesRegex(EvaluationError, "requires physical_container"):
            evaluate_stage1(
                connection,
                self.answer_key,
                destination=Destination.DATABRICKS,
            )
        self.assertEqual(connection.executed, [])

        with self.assertRaisesRegex(
            EvaluationError, "invalid Databricks physical container"
        ):
            prepare_evaluation_sql(
                f"select * from {self.DATABASE}.customers",
                self.DATABASE,
                destination=Destination.DATABRICKS,
                physical_container="catalog`; DROP CATALOG prod",
            )

    def test_stage1_databricks_lists_catalog_schema_and_counts_three_part_relation(
        self,
    ) -> None:
        self._write_stage1_key({"customers": 2})
        list_sql = (
            "SELECT table_name FROM `benchmark_catalog`.information_schema.tables "
            "WHERE table_schema = 'demo_database' "
            "AND table_type IN ('MANAGED', 'EXTERNAL') ORDER BY table_name"
        )
        count_sql = (
            "SELECT COUNT(*) FROM "
            "`benchmark_catalog`.`demo_database`.`customers`"
        )
        connection = _Connection(
            [
                _Response(list_sql, [("customers",)]),
                _Response(count_sql, [(2,)]),
            ]
        )

        result = evaluate_stage1(
            connection,
            self.answer_key,
            destination=Destination.DATABRICKS,
            physical_container="benchmark_catalog",
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.database, self.DATABASE)
        self.assertEqual(result.physical_container, "benchmark_catalog")
        self.assertEqual(connection.executed, [list_sql, count_sql])
        connection.assert_consumed(self)

    def test_stage1_redshift_uses_db_scoped_information_schema_and_lowercase_schema(
        self,
    ) -> None:
        self._write_stage1_key({"customers": 2})
        list_sql = (
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'demo_database' "
            "AND table_type = 'BASE TABLE' ORDER BY table_name"
        )
        count_sql = 'SELECT COUNT(*) FROM "demo_database"."customers"'
        connection = _Connection(
            [
                _Response("SELECT current_database()", [("connection_database",)]),
                _Response(list_sql, [("CUSTOMERS",)]),
                _Response(count_sql, [(2,)]),
            ]
        )

        result = evaluate_stage1(
            connection,
            self.answer_key,
            destination=Destination.REDSHIFT,
            # This is checked against the session-selected database, but stays
            # absent from relation SQL because Redshift cannot cross databases.
            physical_container="connection_database",
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.physical_container, "connection_database")
        self.assertEqual(
            connection.executed,
            ["SELECT current_database()", list_sql, count_sql],
        )
        connection.assert_consumed(self)

    def test_stage1_redshift_rejects_requested_database_that_is_not_connected(
        self,
    ) -> None:
        self._write_stage1_key({"customers": 2})
        connection = _Connection(
            [_Response("SELECT current_database()", [("actual_database",)])]
        )

        with self.assertRaisesRegex(
            EvaluationError, "does not match the connection database"
        ):
            evaluate_stage1(
                connection,
                self.answer_key,
                destination=Destination.REDSHIFT,
                physical_container="requested_database",
            )

        self.assertEqual(connection.executed, ["SELECT current_database()"])
        connection.assert_consumed(self)

    def test_end_to_end_release_threads_databricks_catalog_through_both_stages(
        self,
    ) -> None:
        (self.public / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "databricks": {
                        "config": {
                            "database": "benchmark_catalog",
                            "schema": self.DATABASE,
                        }
                    }
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self._write_stage1_key({"customers": 1})
        marts = [self._mart("summary")]
        self._write_stage2_bundle(
            marts,
            gold={"summary": "id,total\n1,10\n"},
        )
        list_sql = (
            "SELECT table_name FROM `benchmark_catalog`.information_schema.tables "
            "WHERE table_schema = 'demo_database' "
            "AND table_type IN ('MANAGED', 'EXTERNAL') ORDER BY table_name"
        )
        count_sql = (
            "SELECT COUNT(*) FROM "
            "`benchmark_catalog`.`demo_database`.`customers`"
        )
        mart_sql = prepare_evaluation_sql(
            f"select * from {self.DATABASE}.summary order by id;\n",
            self.DATABASE,
            expected_mart="summary",
            destination=Destination.DATABRICKS,
            physical_container="benchmark_catalog",
        )
        connection = _Connection(
            [
                _Response(list_sql, [("customers",)]),
                _Response(count_sql, [(1,)]),
                _Response(
                    mart_sql,
                    [(1, 10)],
                    description=(("id",), ("total",)),
                ),
            ]
        )

        result = evaluate_end_to_end_release(
            self.root,
            self.TASK,
            connection,
            destination=Destination.DATABRICKS,
            physical_container="benchmark_catalog",
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.reward, 1.0)
        self.assertIsNotNone(result.stage2)
        self.assertEqual(result.stage1.physical_container, "benchmark_catalog")
        self.assertEqual(result.stage2.physical_container, "benchmark_catalog")
        self.assertEqual(connection.executed, [list_sql, count_sql, mart_sql])
        connection.assert_consumed(self)

    def test_stage2_redshift_records_the_connection_selected_database(self) -> None:
        marts = [self._mart("summary")]
        self._write_stage2_bundle(
            marts,
            gold={"summary": "id,total\n1,10\n"},
        )
        mart_sql = prepare_evaluation_sql(
            f"select * from {self.DATABASE}.summary order by id;\n",
            self.DATABASE,
            expected_mart="summary",
            destination=Destination.REDSHIFT,
        )
        connection = _Connection(
            [
                _Response("SELECT current_database()", [("connection_database",)]),
                _Response(
                    mart_sql,
                    [(1, 10)],
                    description=(("id",), ("total",)),
                ),
            ]
        )

        result = evaluate_stage2(
            connection,
            self.public,
            self.answer_key,
            destination=Destination.REDSHIFT,
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.physical_container, "connection_database")
        self.assertEqual(
            connection.executed,
            ["SELECT current_database()", mart_sql],
        )
        connection.assert_consumed(self)

    def test_release_destination_is_inferred_and_explicit_mismatch_is_rejected(
        self,
    ) -> None:
        self._write_stage1_key({"customers": 1})
        with self.assertRaisesRegex(EvaluationError, "does not match"):
            evaluate_stage1_release(
                self.root,
                self.TASK,
                _Connection(),
                destination=Destination.REDSHIFT,
            )

    def test_stage2_release_executes_one_namespaced_query_per_mart_and_fractions(self) -> None:
        marts = [self._mart("good"), self._mart("bad")]
        gold = {
            "good": "id,total\n2,20\n1,10\n",
            "bad": "id,total\n1,10\n",
        }
        self._write_stage2_bundle(marts, gold=gold)
        good_sql = prepare_evaluation_sql(
            f"select * from {self.DATABASE}.good order by id;\n",
            self.DATABASE,
            expected_mart="good",
        )
        bad_sql = prepare_evaluation_sql(
            f"select * from {self.DATABASE}.bad order by id;\n",
            self.DATABASE,
            expected_mart="bad",
        )
        connection = _Connection(
            [
                _Response(
                    good_sql,
                    [(1, 10), (2, 20)],
                    description=(("ID",), ("TOTAL",)),
                ),
                _Response(
                    bad_sql,
                    [(1, 999)],
                    description=(("ID",), ("TOTAL",)),
                ),
            ]
        )

        result = evaluate_stage2_release(self.root, self.TASK, connection)

        self.assertEqual(result.mart_scores, {"good": True, "bad": False})
        self.assertEqual(result.reward, 0.5)
        self.assertFalse(result.passed)
        self.assertEqual(result.errors, {})
        self.assertEqual(connection.executed, [good_sql, bad_sql])
        connection.assert_consumed(self)

    def test_stage2_primary_uses_gt_fallback(self) -> None:
        marts = [self._mart("summary")]
        self._write_stage2_bundle(
            marts,
            use_gt=True,
            gold={"summary": "id,total\n1,10\n"},
        )
        sql = prepare_evaluation_sql(
            f"select * from {self.DATABASE}.summary order by id;\n",
            self.DATABASE,
            expected_mart="summary",
        )
        connection = _Connection(
            [_Response(sql, [(1, 10)], description=(("id",), ("total",)))]
        )

        result = evaluate_stage2(connection, self.public, self.answer_key)

        self.assertTrue(result.passed)
        self.assertEqual(result.reward, 1.0)
        self.assertEqual(result.physical_container, "")
        connection.assert_consumed(self)

    def test_stage2_non_primary_never_falls_back_to_gt(self) -> None:
        marts = [self._mart("summary")]
        self._write_stage2_bundle(
            marts,
            use_gt=True,
            gold={"summary": "id,total\n1,10\n"},
        )
        connection = _Connection()

        with self.assertRaisesRegex(EvaluationError, "missing development gold CSV"):
            evaluate_stage2(
                connection,
                self.public,
                self.answer_key,
                population="development",
            )
        self.assertEqual(connection.executed, [])

    def test_stage2_empty_result_preserves_cursor_schema(self) -> None:
        marts = [self._mart("summary")]
        self._write_stage2_bundle(
            marts,
            gold={"summary": "id,total\n"},
        )
        sql = prepare_evaluation_sql(
            f"select * from {self.DATABASE}.summary order by id;\n",
            self.DATABASE,
            expected_mart="summary",
        )
        missing_column = _Connection(
            [_Response(sql, [], description=(("ID",),))]
        )
        result = evaluate_stage2(
            missing_column, self.public, self.answer_key
        )
        self.assertEqual(result.mart_scores, {"summary": False})
        self.assertEqual(result.reward, 0.0)

        complete_schema = _Connection(
            [_Response(sql, [], description=(("ID",), ("TOTAL",)))]
        )
        result = evaluate_stage2(
            complete_schema, self.public, self.answer_key
        )
        self.assertEqual(result.mart_scores, {"summary": True})
        self.assertEqual(result.reward, 1.0)

    def test_stage2_rejects_private_gold_mart_omitted_from_model(self) -> None:
        marts = [self._mart("summary")]
        self._write_stage2_bundle(
            marts,
            gold={"summary": "id,total\n1,10\n"},
        )
        extra = self.answer_key / "gold" / "primary" / "hidden.csv"
        extra.write_text("id,total\n1,10\n", encoding="utf-8")

        with self.assertRaisesRegex(EvaluationError, "data-model/gold mart mismatch"):
            evaluate_stage2(_Connection(), self.public, self.answer_key)

    def test_stage2_fails_closed_before_query_when_public_and_private_keys_differ(self) -> None:
        marts = [self._mart("summary")]
        self._write_stage2_bundle(
            marts,
            gold={"summary": "id,total\n1,10\n"},
        )
        self._write_json(
            "sort_key.json", {self.DATABASE: {"summary": ["total"]}}
        )
        connection = _Connection()

        with self.assertRaisesRegex(EvaluationError, "public key_columns disagree"):
            evaluate_stage2(connection, self.public, self.answer_key)
        self.assertEqual(connection.executed, [])

    # -- IR-002: strict typed diagnostic next to the unchanged reward --------

    @staticmethod
    def _typed_mart(name: str, columns: list[tuple[str, str]]) -> dict[str, Any]:
        return {
            "name": name,
            "description": f"The {name} mart",
            "grain": "one row per id",
            "key_columns": ["id"],
            "columns": [
                {"name": col, "type": ctype, "description": f"{col} column"}
                for col, ctype in columns
            ],
        }

    def _mart_sql(self, name: str, key_columns: str = "id") -> str:
        return prepare_evaluation_sql(
            f"select * from {self.DATABASE}.{name} order by {key_columns};\n",
            self.DATABASE,
            expected_mart=name,
        )

    def test_stage2_strict_diagnostic_flags_each_erasure_class(self) -> None:
        """Legacy reward stays byte-identical while strict codes name the drift."""
        marts = [
            self._typed_mart("m_tol", [("id", "integer"), ("total", "decimal")]),
            self._typed_mart("m_bigint", [("id", "integer"), ("total", "decimal")]),
            self._typed_mart("m_null", [("id", "integer"), ("note", "text")]),
            self._typed_mart("m_case", [("id", "integer"), ("note", "text")]),
            self._typed_mart("m_tz", [("id", "integer"), ("seen_at", "timestamp")]),
        ]
        gold = {
            "m_tol": "id,total\n1,10.5\n",
            "m_bigint": f"id,total\n1,{2**53 + 1}\n",
            "m_null": "id,note\n1,\n",
            "m_case": "id,note\n1,acme\n",
            "m_tz": "id,seen_at\n1,2020-01-01 00:00:00+00:00\n",
        }
        self._write_stage2_bundle(marts, gold=gold)
        aware = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        responses = [
            _Response(
                self._mart_sql("m_tol"),
                [(1, decimal.Decimal("10.55"))],  # inside the 1% tolerance
                description=(("ID", 0), ("TOTAL", 3)),
            ),
            _Response(
                self._mart_sql("m_bigint"),
                [(1, 2**53)],  # collapses onto gold in double precision
                description=(("ID", 0), ("TOTAL", 0)),
            ),
            _Response(
                self._mart_sql("m_null"),
                [(1, "null")],  # literal text vs gold NULL (an NA token)
                description=(("ID", 0), ("NOTE", 2)),
            ),
            _Response(
                self._mart_sql("m_case"),
                [(1, "ACME")],  # case-folded equal
                description=(("ID", 0), ("NOTE", 2)),
            ),
            _Response(
                self._mart_sql("m_tz"),
                [(1, aware)],  # str() form: what gold and every path spell
                description=(("ID", 0), ("SEEN_AT", 7)),
            ),
        ]
        connection = _Connection(responses)

        result = evaluate_stage2(connection, self.public, self.answer_key)

        # The legacy reward is untouched by every strict mismatch.
        self.assertEqual(
            result.mart_scores,
            {name: True for name in gold},
        )
        self.assertEqual(result.reward, 1.0)
        self.assertTrue(result.passed)
        self.assertEqual(result.errors, {})
        # The driver type fingerprint the legacy path discards is captured.
        self.assertEqual(
            result.column_fingerprints["m_tol"], (("ID", "0"), ("TOTAL", "3"))
        )
        self.assertEqual(
            result.column_fingerprints["m_tz"], (("ID", "0"), ("SEEN_AT", "7"))
        )
        # Every erasure class carries its exact stable strict code. A UTC
        # timestamp is not an erasure: strict spells it as str() does, which
        # is how gold was frozen, so the aware value matches exactly.
        self.assertEqual(
            result.strict_mart_scores,
            {name: name == "m_tz" for name in gold},
        )
        self.assertEqual(
            result.strict_mismatch_codes,
            {
                "m_tol": "values:total",
                "m_bigint": "values:total",
                "m_null": "values:note",
                "m_case": "values:note",
                "m_tz": "",
            },
        )
        # Logical canonicalization still catches every real value erasure, but
        # correctly treats the two UTC timestamp spellings as equivalent.
        self.assertEqual(
            result.canonical_mart_scores,
            {
                "m_tol": False,
                "m_bigint": False,
                "m_null": False,
                "m_case": False,
                "m_tz": True,
            },
        )
        self.assertFalse(result.certification_passed)
        connection.assert_consumed(self)

    def test_stage2_strict_pass_reports_match_without_changing_reward(self) -> None:
        marts = [self._mart("summary")]
        self._write_stage2_bundle(marts, gold={"summary": "id,total\n1,10\n"})
        connection = _Connection(
            [
                _Response(
                    self._mart_sql("summary"),
                    [(1, 10)],
                    description=(("ID", 0), ("TOTAL", 0)),
                )
            ]
        )

        result = evaluate_stage2(connection, self.public, self.answer_key)

        self.assertEqual(result.mart_scores, {"summary": True})
        self.assertEqual(result.strict_mart_scores, {"summary": True})
        self.assertEqual(result.strict_mismatch_codes, {"summary": ""})
        self.assertEqual(
            result.column_fingerprints["summary"], (("ID", "0"), ("TOTAL", "0"))
        )
        self.assertEqual(result.canonical_mart_scores, {"summary": True})
        self.assertEqual(result.canonical_mismatch_codes, {"summary": ""})
        self.assertTrue(result.certification_passed)

    def test_stage2_certification_rejects_extra_business_column(self) -> None:
        marts = [self._mart("summary")]
        self._write_stage2_bundle(marts, gold={"summary": "id,total\n1,10\n"})
        connection = _Connection(
            [
                _Response(
                    self._mart_sql("summary"),
                    [(1, 10, "not-declared")],
                    description=(("ID", 0), ("TOTAL", 0), ("EXTRA", 2)),
                )
            ]
        )

        result = evaluate_stage2(connection, self.public, self.answer_key)

        self.assertEqual(result.mart_scores, {"summary": True})
        self.assertEqual(result.reward, 1.0)
        self.assertFalse(result.certification_passed)
        self.assertEqual(result.canonical_mart_scores, {"summary": False})
        self.assertEqual(result.canonical_mismatch_codes, {"summary": "schema"})

    def test_stage2_strict_bug_never_touches_the_reward(self) -> None:
        marts = [self._mart("summary")]
        self._write_stage2_bundle(marts, gold={"summary": "id,total\n1,10\n"})
        connection = _Connection(
            [
                _Response(
                    self._mart_sql("summary"),
                    [(1, 10)],
                    description=(("ID", 0), ("TOTAL", 0)),
                )
            ]
        )

        with mock.patch(
            "elt_taskgen.runtime.evaluation.strict_diagnostic.strict_text_compare",
            side_effect=RuntimeError("injected strict bug"),
        ):
            result = evaluate_stage2(connection, self.public, self.answer_key)

        self.assertEqual(result.mart_scores, {"summary": True})
        self.assertEqual(result.reward, 1.0)
        self.assertTrue(result.passed)
        self.assertEqual(result.errors, {})
        self.assertEqual(result.strict_mart_scores, {"summary": False})
        self.assertEqual(
            result.strict_mismatch_codes, {"summary": "error:strict_failed"}
        )

    def test_stage2_query_error_records_strict_query_failed(self) -> None:
        marts = [self._mart("summary")]
        self._write_stage2_bundle(marts, gold={"summary": "id,total\n1,10\n"})
        connection = _Connection(
            [
                _Response(
                    self._mart_sql("summary"),
                    [],
                    error=RuntimeError("warehouse offline"),
                )
            ]
        )

        result = evaluate_stage2(connection, self.public, self.answer_key)

        self.assertEqual(result.mart_scores, {"summary": False})
        self.assertIn("summary", result.errors)
        self.assertEqual(result.strict_mart_scores, {"summary": False})
        self.assertEqual(
            result.strict_mismatch_codes, {"summary": "error:query_failed"}
        )
        self.assertEqual(result.column_fingerprints, {})
        self.assertEqual(result.canonical_mart_scores, {"summary": False})
        self.assertEqual(
            result.canonical_mismatch_codes, {"summary": "error:query_failed"}
        )
        self.assertFalse(result.certification_passed)


if __name__ == "__main__":
    unittest.main()

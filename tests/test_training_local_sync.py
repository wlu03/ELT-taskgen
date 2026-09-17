"""Focused Milestone-2 tests for selected-stream DuckDB execution."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import duckdb

from elt_taskgen.destinations import Destination
from elt_taskgen.training.local_sync import (
    LocalSyncErrorCode,
    LocalSyncHarnessError,
    raw_state_immutable,
    run_local_sync,
    verify_raw_state,
)
from elt_taskgen.training.namespace import project_namespace
from elt_taskgen.training.package import load_workspace_package
from elt_taskgen.training.terraform_intent import expected_terraform_graph

try:
    from workspace_proxy_fixture import TASK_ID, portable_five_backend_release
except ImportError:  # running as tests.test_training_local_sync
    from tests.workspace_proxy_fixture import TASK_ID, portable_five_backend_release


_ROUTES = {
    "postgres": ("postgres", "postgres"),
    "mongodb": ("mongodb", "mongodb"),
    "rest": ("custom_api", "custom_api"),
    "s3": ("aws_s3", "aws_s3"),
}


def _selection(package, table_name: str, **updates: object):
    backend = package.task.backend_for(table_name).backend.value
    if backend == "files":
        source_key, connector_kind = f"file_{table_name}", "file"
    else:
        source_key, connector_kind = _ROUTES[backend]
    values = {
        "source_key": source_key,
        "connector_kind": connector_kind,
        "stream_name": table_name,
        "sync_mode": "full_refresh_append",
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _intent(package, streams=None, **destination_updates: object):
    destination = {
        "kind": package.destination.value,
        "logical_namespace": package.logical_namespace,
        "schema": (
            package.airbyte_contract["destination"]["configuration"]["schema"]
        ),
    }
    destination.update(destination_updates)
    if streams is None:
        streams = tuple(_selection(package, table.name) for table in package.task.tables)
    return SimpleNamespace(
        selected_streams=tuple(streams),
        destination=SimpleNamespace(**destination),
    )


class LocalSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.release_context = portable_five_backend_release()
        cls.release_dir = cls.release_context.__enter__()
        cls.addClassCleanup(cls.release_context.__exit__, None, None, None)
        cls.package = load_workspace_package(cls.release_dir, TASK_ID)

    def _run(self, root: Path, *, intent=None, name="attempt.duckdb"):
        return run_local_sync(
            self.package,
            "primary",
            intent if intent is not None else _intent(self.package),
            root / name,
        )

    def test_mixed_five_backend_sync_passes_strict_and_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            execution = self._run(Path(directory))
            self.assertTrue(execution.sync_lifecycle)
            self.assertTrue(execution.upstream_stage1)
            self.assertEqual(execution.strict_raw_tables, 1.0)
            self.assertEqual(execution.error_codes, ())
            self.assertEqual(
                {item.table for item in execution.raw_state.tables},
                {"customers", "orders", "order_items", "events", "metrics"},
            )
            self.assertTrue(all(item.matched for item in execution.raw_state.tables))
            self.assertEqual(execution.namespace.raw_schema, "AIRBYTE_SCHEMA")
            self.assertEqual(execution.namespace.mart_schema, TASK_ID)

    def test_consumes_the_real_normalized_terraform_graph_api(self) -> None:
        graph = expected_terraform_graph(self.package)
        with tempfile.TemporaryDirectory() as directory:
            execution = self._run(Path(directory), intent=graph)
        self.assertTrue(execution.sync_lifecycle)
        self.assertTrue(execution.upstream_stage1)
        self.assertEqual(execution.strict_raw_tables, 1.0)

    def test_each_trusted_backend_reader_loads_only_selected_stream(self) -> None:
        tables = {
            "customers": "postgres",
            "orders": "mongodb",
            "events": "rest",
            "metrics": "s3",
            "order_items": "files",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for table, backend in tables.items():
                with self.subTest(backend=backend):
                    execution = self._run(
                        root,
                        intent=_intent(self.package, (_selection(self.package, table),)),
                        name=f"{backend}.duckdb",
                    )
                    self.assertTrue(execution.sync_lifecycle)
                    self.assertFalse(execution.upstream_stage1)
                    self.assertEqual(execution.strict_raw_tables, 0.2)
                    self.assertIn(
                        LocalSyncErrorCode.MISSING_STREAM, execution.error_codes
                    )
                    selected = next(
                        item for item in execution.raw_state.tables if item.table == table
                    )
                    self.assertTrue(selected.matched)

    def test_count_correct_content_and_schema_corruption_fail_only_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            execution = self._run(Path(directory))
            con = duckdb.connect(str(execution.database_path))
            try:
                con.execute(
                    "UPDATE AIRBYTE_SCHEMA.customers "
                    "SET customer_name = 'count-preserving-corruption' "
                    "WHERE customer_id = "
                    "(SELECT min(customer_id) FROM AIRBYTE_SCHEMA.customers)"
                )
            finally:
                con.close()
            after = verify_raw_state(self.package, execution)
            self.assertTrue(after.upstream_stage1)
            self.assertLess(after.strict_raw_tables, 1.0)
            self.assertIn(LocalSyncErrorCode.RAW_CONTENT_MISMATCH, after.error_codes)
            self.assertFalse(raw_state_immutable(execution.raw_state, after))

        with tempfile.TemporaryDirectory() as directory:
            execution = self._run(Path(directory))
            con = duckdb.connect(str(execution.database_path))
            try:
                con.execute(
                    "ALTER TABLE AIRBYTE_SCHEMA.customers "
                    "ADD COLUMN unexpected_business_column VARCHAR"
                )
            finally:
                con.close()
            after = verify_raw_state(self.package, execution)
            self.assertTrue(after.upstream_stage1)
            self.assertLess(after.strict_raw_tables, 1.0)
            self.assertIn(LocalSyncErrorCode.RAW_SCHEMA_MISMATCH, after.error_codes)

    def test_airbyte_metadata_is_ignored_but_raw_identity_remains_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            execution = self._run(Path(directory))
            con = duckdb.connect(str(execution.database_path))
            try:
                con.execute(
                    'ALTER TABLE AIRBYTE_SCHEMA.customers '
                    'ADD COLUMN "_AIRBYTE_RAW_ID" VARCHAR'
                )
                con.execute(
                    'UPDATE AIRBYTE_SCHEMA.customers SET "_AIRBYTE_RAW_ID" = '
                    "customer_id::VARCHAR"
                )
            finally:
                con.close()
            after = verify_raw_state(self.package, execution)
            self.assertTrue(after.upstream_stage1)
            self.assertEqual(after.strict_raw_tables, 1.0)
            self.assertTrue(after.strict_pass)
            self.assertTrue(raw_state_immutable(execution.raw_state, after))

    def test_selection_mutations_have_stable_results(self) -> None:
        all_streams = list(_intent(self.package).selected_streams)
        cases = {
            "missing": (
                _intent(self.package, all_streams[:-1]),
                LocalSyncErrorCode.MISSING_STREAM,
                True,
            ),
            "extra": (
                _intent(
                    self.package,
                    (*all_streams, _selection(self.package, "orders", stream_name="ghost")),
                ),
                LocalSyncErrorCode.EXTRA_STREAM,
                False,
            ),
            "duplicate": (
                _intent(self.package, (*all_streams, all_streams[0])),
                LocalSyncErrorCode.DUPLICATE_STREAM,
                False,
            ),
            "wrong_file_selection": (
                _intent(
                    self.package,
                    (
                        *all_streams[1:],
                        _selection(
                            self.package,
                            "customers",
                            source_key="file_order_items",
                            connector_kind="file",
                        ),
                    ),
                ),
                LocalSyncErrorCode.BACKEND_MISMATCH,
                False,
            ),
            "wrong_mode": (
                _intent(
                    self.package,
                    (
                        _selection(
                            self.package,
                            "customers",
                            sync_mode="incremental_append",
                        ),
                        *all_streams[1:],
                    ),
                ),
                LocalSyncErrorCode.SYNC_MODE_UNSUPPORTED,
                False,
            ),
            "wrong_destination": (
                _intent(self.package, kind="redshift"),
                LocalSyncErrorCode.DESTINATION_MISMATCH,
                False,
            ),
            "wrong_namespace": (
                _intent(self.package, logical_namespace="other"),
                LocalSyncErrorCode.NAMESPACE_MISMATCH,
                False,
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (name, (intent, code, lifecycle)) in enumerate(cases.items()):
                with self.subTest(case=name):
                    execution = self._run(
                        root, intent=intent, name=f"mutation_{index}.duckdb"
                    )
                    self.assertEqual(execution.sync_lifecycle, lifecycle)
                    self.assertFalse(execution.upstream_stage1)
                    self.assertLess(execution.strict_raw_tables, 1.0)
                    self.assertIn(code, execution.error_codes)

    def test_empty_table_and_null_bearing_tables_are_supported(self) -> None:
        source = self.package.source_root("primary") / "files" / "order_items.csv"
        original = source.read_bytes()
        source.chmod(0o600)
        source.write_text(
            "order_id,quantity,unit_price,discount\n", encoding="utf-8"
        )
        try:
            stage1 = {
                name: dict(counts)
                for name, counts in self.package.gold.stage1.items()
            }
            stage1["primary"]["order_items"] = 0
            gold = self.package.gold.model_copy(update={"stage1": stage1})
            semantic = self.package.semantic.model_copy(update={"gold": gold})
            package = dataclasses.replace(self.package, semantic=semantic)
            with tempfile.TemporaryDirectory() as directory:
                execution = run_local_sync(
                    package,
                    "primary",
                    _intent(package),
                    Path(directory) / "empty.duckdb",
                )
                self.assertTrue(execution.upstream_stage1)
                self.assertEqual(execution.strict_raw_tables, 1.0)
                empty = next(
                    item
                    for item in execution.raw_state.tables
                    if item.table == "order_items"
                )
                self.assertEqual(empty.fingerprint.relation.row_count, 0)
                # The same mixed population includes nullable fields across
                # MongoDB, REST, S3, and files; a perfect strict score proves
                # SQL NULL survived those readers.
                self.assertTrue(execution.raw_state.strict_pass)
        finally:
            source.write_bytes(original)

    def test_malformed_json_rest_page_and_s3_prefix_are_task_defects(self) -> None:
        mutations = {
            "malformed_mongodb": (
                self.package.source_root("primary") / "mongodb" / "orders.jsonl",
                "append",
            ),
            "missing_rest_page": (
                self.package.source_root("primary")
                / "rest"
                / "events"
                / "page_0003.json",
                "remove",
            ),
            "extra_s3_object": (
                self.package.source_root("primary")
                / "s3"
                / "metrics"
                / "part-99999.jsonl",
                "create",
            ),
        }
        for name, (path, operation) in mutations.items():
            with self.subTest(case=name):
                parent_mode = path.parent.stat().st_mode
                path.parent.chmod(0o700)
                existed = path.exists()
                original = path.read_bytes() if existed else b""
                if existed:
                    path.chmod(0o600)
                try:
                    if operation == "append":
                        path.write_bytes(original + b"{malformed-json\n")
                    elif operation == "remove":
                        path.unlink()
                    else:
                        path.write_text(
                            '{"metric_id":999,"event_id":999,"value":1,"big_note":"extra"}\n',
                            encoding="utf-8",
                        )
                    with tempfile.TemporaryDirectory() as directory:
                        with self.assertRaises(LocalSyncHarnessError) as raised:
                            run_local_sync(
                                self.package,
                                "primary",
                                _intent(self.package),
                                Path(directory) / f"{name}.duckdb",
                            )
                    self.assertEqual(
                        raised.exception.code,
                        LocalSyncErrorCode.PRIVATE_SOURCE_INVALID,
                    )
                    self.assertNotIn(str(path), str(raised.exception))
                finally:
                    if path.exists():
                        path.chmod(0o600)
                        path.unlink()
                    if existed:
                        path.write_bytes(original)
                    path.parent.chmod(parent_mode & 0o777)

    def test_database_freshness_and_deterministic_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._run(root, name="first.duckdb")
            second = self._run(root, name="second.duckdb")
            first_digests = {
                table: value.fingerprint_sha256
                for table, value in first.expected_fingerprints.items()
            }
            second_digests = {
                table: value.fingerprint_sha256
                for table, value in second.expected_fingerprints.items()
            }
            self.assertEqual(first_digests, second_digests)
            with self.assertRaises(LocalSyncHarnessError) as raised:
                self._run(root, name="first.duckdb")
            self.assertEqual(
                raised.exception.code, LocalSyncErrorCode.DATABASE_NOT_FRESH
            )
            (root / "stale.duckdb.wal").write_bytes(b"stale")
            with self.assertRaises(LocalSyncHarnessError) as raised:
                self._run(root, name="stale.duckdb")
            self.assertEqual(
                raised.exception.code, LocalSyncErrorCode.DATABASE_NOT_FRESH
            )


class NamespaceProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.release_context = portable_five_backend_release()
        cls.release_dir = cls.release_context.__enter__()
        cls.addClassCleanup(cls.release_context.__exit__, None, None, None)
        cls.package = load_workspace_package(cls.release_dir, TASK_ID)

    def _retarget(self, destination: Destination, configuration: dict):
        contract = {
            "destination": {
                "configuration": configuration,
            }
        }
        logical_namespace = configuration[
            "database" if destination is Destination.SNOWFLAKE else "schema"
        ]
        return dataclasses.replace(
            self.package,
            destination=destination,
            logical_namespace=logical_namespace,
            airbyte_contract=contract,
        )

    def test_snowflake_databricks_and_redshift_namespace_projection(self) -> None:
        cases = (
            (
                self._retarget(
                    Destination.SNOWFLAKE,
                    {"database": TASK_ID, "schema": "AIRBYTE_SCHEMA"},
                ),
                TASK_ID,
                None,
                "AIRBYTE_SCHEMA",
            ),
            (
                self._retarget(
                    Destination.DATABRICKS,
                    {"database": "training_catalog", "schema": TASK_ID},
                ),
                None,
                "training_catalog",
                TASK_ID,
            ),
            (
                self._retarget(
                    Destination.REDSHIFT,
                    {"database": "training_database", "schema": TASK_ID},
                ),
                "training_database",
                None,
                TASK_ID,
            ),
        )
        for package, logical_database, logical_catalog, raw_schema in cases:
            with self.subTest(destination=package.destination.value):
                projection = project_namespace(package, Path("attempt.duckdb"))
                self.assertEqual(projection.logical_database, logical_database)
                self.assertEqual(projection.logical_catalog, logical_catalog)
                self.assertEqual(projection.raw_schema, raw_schema)
                self.assertEqual(projection.mart_schema, TASK_ID)
                self.assertEqual(projection.dbt_target_schema, TASK_ID)
                self.assertEqual(projection.evaluator_schema, TASK_ID)
                self.assertEqual(
                    projection.raw_relation("orders"),
                    f'"{raw_schema}"."orders"',
                )


if __name__ == "__main__":
    unittest.main()

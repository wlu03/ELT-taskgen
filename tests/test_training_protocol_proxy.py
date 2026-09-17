"""Destination-profile and deterministic Airbyte protocol-proxy tests."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import duckdb

from elt_taskgen.training.airbyte_proxy import (
    AirbyteJobStatus,
    AirbyteProtocolProxy,
    AirbyteProxyErrorCode,
)
from elt_taskgen.training.package import load_workspace_package
from elt_taskgen.training.terraform_intent import expected_terraform_graph
from elt_taskgen.training.warehouse_profiles import warehouse_profile

try:
    from workspace_proxy_fixture import TASK_ID, portable_five_backend_release
except ImportError:
    from tests.workspace_proxy_fixture import TASK_ID, portable_five_backend_release


class TestWarehouseBehaviorProfiles(unittest.TestCase):
    def test_profiles_encode_calibrated_empty_string_behavior(self) -> None:
        self.assertIsNone(warehouse_profile("snowflake").normalize_scalar(""))
        self.assertIsNone(warehouse_profile("redshift").normalize_scalar(""))
        self.assertEqual(warehouse_profile("databricks").normalize_scalar(""), "")

    def test_databricks_json_scalar_is_json_quoted_text(self) -> None:
        profile = warehouse_profile("databricks")
        self.assertEqual(profile.normalize_scalar('"note"', logical_type="json"), '"note"')
        self.assertEqual(profile.normalize_scalar("3", logical_type="json"), "3")

    def test_identifiers_and_unsupported_behavior_fail_closed(self) -> None:
        self.assertEqual(warehouse_profile("snowflake").fold_identifier("raw"), "RAW")
        self.assertEqual(warehouse_profile("redshift").fold_identifier("RAW"), "raw")
        self.assertEqual(warehouse_profile("databricks").fold_identifier("Raw"), "Raw")
        with self.assertRaisesRegex(ValueError, "unsupported_destination"):
            warehouse_profile("bigquery")
        with self.assertRaisesRegex(ValueError, "unsupported_behavior"):
            warehouse_profile("snowflake").error("optimizer_hint")


class TestAirbyteProtocolProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.release = portable_five_backend_release()
        cls.release_dir = cls.release.__enter__()
        cls.package = load_workspace_package(cls.release_dir, TASK_ID)
        cls.graph = expected_terraform_graph(cls.package)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.release.__exit__(None, None, None)

    def _sync(self, package, population, graph, database_path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(database_path))
        con.execute('CREATE SCHEMA "AIRBYTE_SCHEMA"')
        for stream in graph.selected_streams:
            con.execute(
                f'CREATE TABLE "AIRBYTE_SCHEMA"."{stream.stream_name}" (value INTEGER)'
            )
            con.execute(
                f'INSERT INTO "AIRBYTE_SCHEMA"."{stream.stream_name}" VALUES (1)'
            )
        con.close()
        return SimpleNamespace(
            sync_lifecycle=True,
            database_path=database_path,
            namespace=SimpleNamespace(raw_schema="AIRBYTE_SCHEMA"),
            selected_streams=graph.selected_streams,
        )

    def test_ids_transitions_json_and_isolation_are_deterministic(self) -> None:
        snapshots = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("a", "b"):
                proxy = AirbyteProtocolProxy(
                    self.package, "primary", self.graph, root / name / "airbyte", sync=self._sync
                )
                with mock.patch("socket.create_connection", side_effect=AssertionError("network")):
                    job = proxy.trigger_sync(root / name / "attempt.duckdb")
                self.assertEqual(job.status, AirbyteJobStatus.SUCCEEDED)
                self.assertEqual(proxy.poll_job(job.id), job)
                snapshots.append(proxy.state_path.read_bytes())
            self.assertEqual(snapshots[0], snapshots[1])
            self.assertNotEqual((root / "a" / "airbyte").resolve(), (root / "b" / "airbyte").resolve())

    def test_duplicate_connection_fails_without_fabricated_success(self) -> None:
        graph = dataclasses.replace(
            self.graph, connections=(*self.graph.connections, self.graph.connections[0])
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proxy = AirbyteProtocolProxy(self.package, "primary", graph, root / "state", sync=self._sync)
            validations = proxy.validate_connections()
            self.assertEqual(validations[-1].error_code, AirbyteProxyErrorCode.DUPLICATE_CONNECTION)
            job = proxy.trigger_sync(root / "attempt.duckdb")
            self.assertEqual(job.status, AirbyteJobStatus.FAILED)
            self.assertIsNone(proxy.execution)

    def test_wrong_stream_namespace_and_mode_fail_validation(self) -> None:
        base = self.graph.connections[0]
        mutations = (
            (dataclasses.replace(base, streams=(("wrong", "full_refresh_append"),)), AirbyteProxyErrorCode.INVALID_STREAM),
            (dataclasses.replace(base, namespace_definition="source"), AirbyteProxyErrorCode.WRONG_NAMESPACE),
            (dataclasses.replace(base, streams=((base.streams[0][0], "overwrite"),)), AirbyteProxyErrorCode.INVALID_SYNC_MODE),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (connection, code) in enumerate(mutations):
                graph = dataclasses.replace(self.graph, connections=(connection,))
                proxy = AirbyteProtocolProxy(self.package, "primary", graph, root / str(index), sync=self._sync)
                self.assertEqual(proxy.validate_connections()[0].error_code, code)
                self.assertEqual(proxy.trigger_sync(root / f"{index}.duckdb").status, AirbyteJobStatus.FAILED)

    def test_pending_job_can_be_cancelled_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proxy = AirbyteProtocolProxy(self.package, "primary", self.graph, Path(directory) / "state", sync=self._sync)
            pending = proxy.queue_sync()
            cancelled = proxy.cancel_job(pending.id)
            self.assertEqual(cancelled.status, AirbyteJobStatus.CANCELLED)
            self.assertEqual(cancelled.updated_tick, pending.updated_tick + 1)

    def test_full_refresh_append_produces_exactly_two_copies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "attempt.duckdb"
            proxy = AirbyteProtocolProxy(self.package, "primary", self.graph, root / "state", sync=self._sync)
            self.assertEqual(proxy.trigger_sync(database).status, AirbyteJobStatus.SUCCEEDED)
            self.assertEqual(proxy.trigger_sync(database).status, AirbyteJobStatus.SUCCEEDED)
            con = duckdb.connect(str(database), read_only=True)
            try:
                for stream in self.graph.selected_streams:
                    count = con.execute(
                        f'SELECT count(*) FROM "AIRBYTE_SCHEMA"."{stream.stream_name}"'
                    ).fetchone()[0]
                    self.assertEqual(count, 2, stream.stream_name)
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()

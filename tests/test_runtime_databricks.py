from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from elt_taskgen.runtime.databricks import (
    DatabricksRuntimeError,
    UnityCatalogScope,
    assert_current_principal,
    connect,
    load_credentials,
    provision_attempt,
    quote_identifier,
    quote_principal,
)


def token_credentials(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "hostname": "workspace.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc123",
        "access_token": "solver-token",
    }
    values.update(overrides)
    return values


def oauth_credentials(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "hostname": "workspace.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc123",
        "client_id": "solver-client",
        "secret": "solver-secret",
    }
    values.update(overrides)
    return values


class FakeCursor:
    def __init__(
        self,
        *,
        fail_on: str | None = None,
        result: object = None,
    ) -> None:
        self.statements: list[str] = []
        self.closed = False
        self.fail_on = fail_on
        self.result = result

    def execute(self, statement: str) -> None:
        self.statements.append(statement)
        if self.fail_on is not None and self.fail_on in statement:
            raise RuntimeError("connector detail containing admin-secret")

    def close(self) -> None:
        self.closed = True

    def fetchone(self) -> object:
        return self.result


class FakeConnection:
    def __init__(
        self,
        *,
        fail_on: str | None = None,
        result: object = None,
    ) -> None:
        self.value = FakeCursor(fail_on=fail_on, result=result)
        self.cursor_calls = 0

    def cursor(self) -> FakeCursor:
        self.cursor_calls += 1
        return self.value


class RuntimeDatabricksTests(unittest.TestCase):
    def test_solver_credential_is_bound_to_granted_principal(self) -> None:
        connection = FakeConnection(result=("solver-id",))
        self.assertEqual(
            assert_current_principal(connection, "solver-id"), "solver-id"
        )
        self.assertEqual(connection.value.statements, ["SELECT session_user()"])
        self.assertTrue(connection.value.closed)

        mismatch = FakeConnection(result=("different-principal",))
        with self.assertRaisesRegex(
            DatabricksRuntimeError, "does not match --solver-principal"
        ):
            assert_current_principal(mismatch, "solver-id")
        self.assertTrue(mismatch.value.closed)

    def test_solver_principal_check_sanitizes_driver_failure(self) -> None:
        connection = FakeConnection(fail_on="session_user")
        with self.assertRaises(DatabricksRuntimeError) as raised:
            assert_current_principal(connection, "solver-id")
        self.assertEqual(
            str(raised.exception),
            "could not verify the Databricks solver principal",
        )
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(connection.value.closed)

    def test_identifiers_and_principals_are_validated_and_quoted(self) -> None:
        self.assertEqual(quote_identifier("bench_1"), "`bench_1`")
        self.assertEqual(
            quote_principal("12ab-service@example.com"),
            "`12ab-service@example.com`",
        )
        with self.assertRaisesRegex(ValueError, "unsafe Databricks identifier"):
            quote_identifier("bench; DROP CATALOG prod")
        with self.assertRaisesRegex(ValueError, "unsafe Databricks principal"):
            quote_principal("solver` GRANT ALL")

    def test_credential_loader_accepts_upstream_oauth_shape_and_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "databricks.json"
            path.write_text(
                json.dumps(
                    {
                        "server_hostname": "workspace.cloud.databricks.com",
                        "http_path": "/sql/1.0/warehouses/abc123",
                        "client_id": "solver-client",
                        "client_secret": "solver-secret",
                        "catalog": "bench_catalog",
                        "schema": "attempt_1",
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_credentials(path),
                {
                    "hostname": "workspace.cloud.databricks.com",
                    "http_path": "/sql/1.0/warehouses/abc123",
                    "client_id": "solver-client",
                    "secret": "solver-secret",
                    "database": "bench_catalog",
                    "schema": "attempt_1",
                },
            )

    def test_credential_loader_fails_closed_without_echoing_secrets(self) -> None:
        cases = (
            token_credentials(hostname="https://workspace.cloud.databricks.com"),
            token_credentials(http_path="sql/warehouse"),
            token_credentials(access_token=""),
            token_credentials(client_id="also-oauth", secret="secret"),
            oauth_credentials(secret=None),
            token_credentials(unexpected="not-allowed"),
            token_credentials(schema="attempt_1"),
            token_credentials(database="bad;catalog"),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "databricks.json"
            for values in cases:
                with self.subTest(values=tuple(values)):
                    path.write_text(json.dumps(values), encoding="utf-8")
                    with self.assertRaises(DatabricksRuntimeError) as raised:
                        load_credentials(path)
                    self.assertNotIn("solver-token", str(raised.exception))
                    self.assertNotIn("solver-secret", str(raised.exception))

            path.write_text('{"access_token":"literal-secret",', encoding="utf-8")
            with self.assertRaises(DatabricksRuntimeError) as raised:
                load_credentials(path)
            self.assertNotIn("literal-secret", str(raised.exception))

    def test_credential_loader_rejects_conflicting_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "databricks.json"
            path.write_text(
                json.dumps(
                    token_credentials(
                        hostname="first.cloud.databricks.com",
                        server_hostname="second.cloud.databricks.com",
                    )
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DatabricksRuntimeError, "conflicting"):
                load_credentials(path)

    def test_connect_lazily_passes_pat_credentials_to_dbapi(self) -> None:
        calls: list[dict[str, object]] = []
        expected = object()
        sql_module = types.ModuleType("databricks.sql")
        sql_module.connect = lambda **kwargs: calls.append(kwargs) or expected
        databricks_module = types.ModuleType("databricks")
        databricks_module.__path__ = []  # type: ignore[attr-defined]
        databricks_module.sql = sql_module

        with patch.dict(
            sys.modules,
            {"databricks": databricks_module, "databricks.sql": sql_module},
        ):
            result = connect(
                token_credentials(database="bench_catalog", schema="attempt_1")
            )

        self.assertIs(result, expected)
        self.assertEqual(
            calls,
            [
                {
                    "server_hostname": "workspace.cloud.databricks.com",
                    "http_path": "/sql/1.0/warehouses/abc123",
                    "_socket_timeout": 15,
                    "_retry_stop_after_attempts_count": 3,
                    "_retry_stop_after_attempts_duration": 30,
                    "catalog": "bench_catalog",
                    "schema": "attempt_1",
                    "access_token": "solver-token",
                }
            ],
        )

    def test_connect_lazily_builds_oauth_credentials_provider(self) -> None:
        connect_calls: list[dict[str, object]] = []
        config_calls: list[dict[str, object]] = []
        oauth_calls: list[object] = []
        expected = object()

        class FakeConfig:
            def __init__(self, **kwargs: object) -> None:
                config_calls.append(kwargs)

        def fake_oauth(config: object) -> str:
            oauth_calls.append(config)
            return "oauth-header-provider"

        sql_module = types.ModuleType("databricks.sql")
        sql_module.connect = lambda **kwargs: connect_calls.append(kwargs) or expected
        core_module = types.ModuleType("databricks.sdk.core")
        core_module.Config = FakeConfig
        core_module.oauth_service_principal = fake_oauth
        sdk_module = types.ModuleType("databricks.sdk")
        sdk_module.__path__ = []  # type: ignore[attr-defined]
        sdk_module.core = core_module
        databricks_module = types.ModuleType("databricks")
        databricks_module.__path__ = []  # type: ignore[attr-defined]
        databricks_module.sql = sql_module
        databricks_module.sdk = sdk_module

        with patch.dict(
            sys.modules,
            {
                "databricks": databricks_module,
                "databricks.sql": sql_module,
                "databricks.sdk": sdk_module,
                "databricks.sdk.core": core_module,
            },
        ):
            result = connect(oauth_credentials())
            provider = connect_calls[0]["credentials_provider"]
            self.assertEqual(provider(), "oauth-header-provider")  # type: ignore[operator]

        self.assertIs(result, expected)
        self.assertEqual(
            config_calls,
            [
                {
                    "host": "https://workspace.cloud.databricks.com",
                    "client_id": "solver-client",
                    "client_secret": "solver-secret",
                    "http_timeout_seconds": 10,
                    "retry_timeout_seconds": 15,
                }
            ],
        )
        self.assertEqual(len(oauth_calls), 1)
        self.assertNotIn("access_token", connect_calls[0])

    def test_oauth_discovery_failure_has_secret_safe_user_message(self) -> None:
        leaked = "solver-secret"

        class FailingConfig:
            def __init__(self, **_kwargs: object) -> None:
                raise RuntimeError(f"host discovery echoed {leaked}")

        sql_module = types.ModuleType("databricks.sql")
        core_module = types.ModuleType("databricks.sdk.core")
        core_module.Config = FailingConfig
        core_module.oauth_service_principal = lambda _config: object()
        sdk_module = types.ModuleType("databricks.sdk")
        sdk_module.__path__ = []  # type: ignore[attr-defined]
        sdk_module.core = core_module
        databricks_module = types.ModuleType("databricks")
        databricks_module.__path__ = []  # type: ignore[attr-defined]
        databricks_module.sql = sql_module
        databricks_module.sdk = sdk_module

        with patch.dict(
            sys.modules,
            {
                "databricks": databricks_module,
                "databricks.sql": sql_module,
                "databricks.sdk": sdk_module,
                "databricks.sdk.core": core_module,
            },
        ):
            with self.assertRaises(DatabricksRuntimeError) as raised:
                connect(oauth_credentials())

        self.assertEqual(str(raised.exception), "could not connect to Databricks")
        self.assertNotIn(leaked, str(raised.exception))

    def test_connect_failure_has_secret_safe_user_message(self) -> None:
        def fail_connect(**_kwargs: object) -> object:
            raise RuntimeError("transport echoed solver-token")

        sql_module = types.ModuleType("databricks.sql")
        sql_module.connect = fail_connect
        databricks_module = types.ModuleType("databricks")
        databricks_module.__path__ = []  # type: ignore[attr-defined]
        databricks_module.sql = sql_module

        with patch.dict(
            sys.modules,
            {"databricks": databricks_module, "databricks.sql": sql_module},
        ):
            with self.assertRaises(DatabricksRuntimeError) as raised:
                connect(token_credentials())
        self.assertEqual(str(raised.exception), "could not connect to Databricks")
        self.assertNotIn("solver-token", str(raised.exception))

    def test_provision_attempt_stays_inside_existing_catalog_scope(self) -> None:
        connection = FakeConnection()
        credentials = provision_attempt(
            connection,
            "attempt_1",
            scope=UnityCatalogScope(
                "bench_catalog", existing_dedicated_catalog=True
            ),
            solver_principal="1234-service-principal",
            solver_credentials=oauth_credentials(),
            attempt_dedicated_principal=True,
        )

        self.assertEqual(
            connection.value.statements,
            [
                "CREATE SCHEMA `bench_catalog`.`attempt_1`",
                (
                    "GRANT USE CATALOG, CREATE SCHEMA ON CATALOG `bench_catalog` "
                    "TO `1234-service-principal`"
                ),
                (
                    "GRANT USE SCHEMA, CREATE TABLE, CREATE VOLUME, SELECT, "
                    "MODIFY ON SCHEMA "
                    "`bench_catalog`.`attempt_1` TO `1234-service-principal`"
                ),
            ],
        )
        self.assertTrue(connection.value.closed)
        self.assertEqual(
            credentials,
            oauth_credentials(database="bench_catalog", schema="attempt_1"),
        )
        self.assertNotIn("admin_secret", credentials)

    def test_catalog_creation_requires_explicit_scope_grant(self) -> None:
        existing_catalog = FakeConnection()
        provision_attempt(
            existing_catalog,
            "attempt_1",
            scope=UnityCatalogScope(
                "bench_catalog", existing_dedicated_catalog=True
            ),
            solver_principal="solver-id",
            solver_credentials=token_credentials(),
            attempt_dedicated_principal=True,
        )
        self.assertFalse(
            any(
                statement.startswith("CREATE CATALOG")
                for statement in existing_catalog.value.statements
            )
        )

        new_catalog = FakeConnection()
        provision_attempt(
            new_catalog,
            "attempt_1",
            scope=UnityCatalogScope("bench_catalog", allow_create_catalog=True),
            solver_principal="solver-id",
            solver_credentials=token_credentials(),
            attempt_dedicated_principal=True,
        )
        self.assertEqual(
            new_catalog.value.statements[0], "CREATE CATALOG `bench_catalog`"
        )

    def test_provision_refuses_out_of_scope_credentials_before_sql(self) -> None:
        for credentials in (
            token_credentials(database="other_catalog"),
            token_credentials(database="bench_catalog", schema="other_attempt"),
        ):
            with self.subTest(credentials=credentials):
                connection = FakeConnection()
                with self.assertRaises(ValueError):
                    provision_attempt(
                        connection,
                        "attempt_1",
                        scope=UnityCatalogScope(
                            "bench_catalog", existing_dedicated_catalog=True
                        ),
                        solver_principal="solver-id",
                        solver_credentials=credentials,
                        attempt_dedicated_principal=True,
                    )
                self.assertEqual(connection.cursor_calls, 0)

    def test_provision_failure_closes_cursor_and_hides_secrets(self) -> None:
        connection = FakeConnection(fail_on="CREATE SCHEMA")
        with self.assertRaises(DatabricksRuntimeError) as raised:
            provision_attempt(
                connection,
                "attempt_1",
                scope=UnityCatalogScope(
                    "bench_catalog", allow_create_catalog=True
                ),
                solver_principal="solver-id",
                solver_credentials=oauth_credentials(),
                attempt_dedicated_principal=True,
            )
        self.assertTrue(connection.value.closed)
        self.assertNotIn("solver-secret", str(raised.exception))
        self.assertNotIn("admin-secret", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIn(
            "DROP CATALOG IF EXISTS `bench_catalog` CASCADE",
            connection.value.statements,
        )

    def test_provision_requires_attempt_dedicated_principal_assertion(self) -> None:
        connection = FakeConnection()
        with self.assertRaisesRegex(ValueError, "attempt-dedicated"):
            provision_attempt(
                connection,
                "attempt_1",
                scope=UnityCatalogScope(
                    "bench_catalog", allow_create_catalog=True
                ),
                solver_principal="solver-id",
                solver_credentials=token_credentials(),
            )
        self.assertEqual(connection.cursor_calls, 0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from elt_taskgen.runtime.redshift import (
    RedshiftRuntimeError,
    connect,
    load_credentials,
    provision_attempt,
    quote_identifier,
)


def base_credentials(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "host": "cluster.example.us-west-2.redshift.amazonaws.com",
        "port": 5439,
        "database": "elt_bench",
        "username": "runtime_admin",
        "password": "Admin-Secret-1",
        "s3_bucket_name": "elt-bench-staging",
        "s3_bucket_region": "us-west-2",
        "access_key_id": "AKIAEXAMPLE",
        "secret_access_key": "staging/secret+value",
    }
    values.update(updates)
    return values


class FakeCursor:
    def __init__(
        self,
        *,
        database: str = "elt_bench",
        user_exists: bool = False,
        database_exists: bool = False,
        fail_on: str | None = None,
        failure_secret: str = "",
        results: list[tuple[str] | tuple[int] | None] | None = None,
    ) -> None:
        self.database = database
        self.fail_on = fail_on
        self.failure_secret = failure_secret
        self.statements: list[str] = []
        self.closed = False
        self.results: list[tuple[str] | tuple[int] | None] = (
            list(results)
            if results is not None
            else [
                (database,),
                (1,) if user_exists else None,
                (1,) if database_exists else None,
            ]
        )

    def execute(self, statement: str) -> None:
        self.statements.append(statement)
        if self.fail_on and self.fail_on in statement:
            raise RuntimeError(f"driver leaked {self.failure_secret}")

    def fetchone(self) -> tuple[str] | tuple[int] | None:
        return self.results.pop(0)

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: FakeCursor | None = None) -> None:
        self.value = cursor or FakeCursor()
        self.commits = 0
        self.rollbacks = 0
        self.autocommit = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.value

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class RuntimeRedshiftTests(unittest.TestCase):
    def test_identifiers_are_validated_quoted_and_length_bounded(self) -> None:
        self.assertEqual(quote_identifier("attempt_1"), '"attempt_1"')
        for unsafe in ("attempt; DROP SCHEMA public", "has-hyphen", "x" * 128):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(ValueError, "unsafe Redshift identifier"):
                    quote_identifier(unsafe)

    def test_credential_loader_is_strict_and_rejects_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "redshift.json"
            path.write_text(json.dumps(base_credentials()), encoding="utf-8")
            loaded = load_credentials(path)
            self.assertEqual(loaded, base_credentials())

            path.write_text(
                json.dumps(base_credentials(secret_access_key="<redshift_secret>")),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RedshiftRuntimeError, "placeholder field"):
                load_credentials(path)

            path.write_text(
                json.dumps(base_credentials(port=True)), encoding="utf-8"
            )
            with self.assertRaisesRegex(RedshiftRuntimeError, "invalid port"):
                load_credentials(path)

            path.write_text(
                json.dumps(base_credentials(extra="must-not-pass")), encoding="utf-8"
            )
            with self.assertRaisesRegex(RedshiftRuntimeError, "unsupported fields"):
                load_credentials(path)

    def test_credential_loader_rejects_duplicate_keys_without_echoing_values(self) -> None:
        secret = "do-not-echo-this-secret"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "redshift.json"
            path.write_text(
                '{"host":"cluster.example","host":"%s"}' % secret,
                encoding="utf-8",
            )
            with self.assertRaises(RedshiftRuntimeError) as raised:
                load_credentials(path)
            self.assertNotIn(secret, str(raised.exception))

    def test_connect_prefers_redshift_connector_and_omits_staging_secrets(self) -> None:
        calls: list[dict[str, object]] = []
        sentinel = object()

        def fake_connect(**kwargs: object) -> object:
            calls.append(kwargs)
            return sentinel

        module = types.SimpleNamespace(connect=fake_connect)
        with mock.patch.dict(sys.modules, {"redshift_connector": module}):
            result = connect(base_credentials(schema="attempt_1_airbyte"))

        self.assertIs(result, sentinel)
        self.assertEqual(
            calls,
            [
                {
                    "host": "cluster.example.us-west-2.redshift.amazonaws.com",
                    "port": 5439,
                    "database": "elt_bench",
                    "user": "runtime_admin",
                    "password": "Admin-Secret-1",
                    "timeout": 15,
                }
            ],
        )
        self.assertNotIn("secret_access_key", calls[0])
        self.assertNotIn("schema", calls[0])

    def test_connect_sanitizes_driver_failures(self) -> None:
        leaked = "Admin-Secret-1"
        calls = 0

        def fake_connect(**_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            raise RuntimeError(f"authentication failed for {leaked}")

        module = types.SimpleNamespace(connect=fake_connect)
        with mock.patch.dict(sys.modules, {"redshift_connector": module}), mock.patch(
            "elt_taskgen.runtime.redshift.time.sleep"
        ) as sleeper:
            with self.assertRaises(RedshiftRuntimeError) as raised:
                connect(base_credentials())
        self.assertEqual(calls, 3)
        self.assertEqual(sleeper.call_count, 2)
        self.assertEqual(str(raised.exception), "could not connect to Redshift")
        self.assertNotIn(leaked, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_connect_retries_a_cold_start_then_succeeds(self) -> None:
        sentinel = object()
        connector = mock.Mock(
            side_effect=(TimeoutError("starting"), OSError("resuming"), sentinel)
        )
        module = types.SimpleNamespace(connect=connector)
        with mock.patch.dict(sys.modules, {"redshift_connector": module}), mock.patch(
            "elt_taskgen.runtime.redshift.time.sleep"
        ) as sleeper:
            self.assertIs(connect(base_credentials()), sentinel)
        self.assertEqual(connector.call_count, 3)
        self.assertEqual([call.args for call in sleeper.call_args_list], [(2.0,), (2.0,)])
        self.assertTrue(
            all(call.kwargs["timeout"] == 15 for call in connector.call_args_list)
        )

    def test_provision_attempt_scopes_user_schema_and_solver_mapping(self) -> None:
        connection = FakeConnection()
        solver_connection = FakeConnection(
            FakeCursor(
                database="task_7_db",
                results=[
                    ("task_7_db",),
                    ("elt_c55424493ddc8f0e1920_user",),
                ],
            )
        )
        salt = b"s" * 32
        with mock.patch(
            "elt_taskgen.runtime.redshift.secrets.token_bytes", return_value=salt
        ):
            result = provision_attempt(
                connection,
                "task_7",
                credentials=base_credentials(),
                database="task_7_db",
                password="Safe-Pass1!",
                attempt_dedicated_deployment=True,
                attempt_connector=lambda _credentials: solver_connection,
            )

        self.assertEqual(
            result,
            {
                "host": "cluster.example.us-west-2.redshift.amazonaws.com",
                "port": 5439,
                "database": "task_7_db",
                "username": "elt_c55424493ddc8f0e1920_user",
                "password": "Safe-Pass1!",
                "schema": "task_7",
                "s3_bucket_name": "elt-bench-staging",
                "s3_bucket_region": "us-west-2",
                "access_key_id": "AKIAEXAMPLE",
                "secret_access_key": "staging/secret+value",
                "s3_bucket_path": "elt-bench/task_7",
            },
        )
        statements = connection.value.statements
        self.assertEqual(statements[0], "SELECT current_database()")
        self.assertIn(
            'REVOKE TEMPORARY ON DATABASE "task_7_db" FROM PUBLIC', statements
        )
        digest = hashlib.sha256(b"Safe-Pass1!" + salt).hexdigest()
        self.assertIn(
            'CREATE USER "elt_c55424493ddc8f0e1920_user" '
            f"PASSWORD 'sha256|{digest}|{salt.hex()}'",
            statements,
        )
        self.assertFalse(
            any("Safe-Pass1!" in statement for statement in statements)
        )
        self.assertIn(
            'CREATE DATABASE "task_7_db" '
            'OWNER "elt_c55424493ddc8f0e1920_user"',
            statements,
        )
        self.assertIn(
            'GRANT CREATE, TEMPORARY ON DATABASE "task_7_db" '
            'TO "elt_c55424493ddc8f0e1920_user"',
            statements,
        )
        self.assertEqual(
            solver_connection.value.statements,
            [
                "SELECT current_database()",
                "SELECT current_user",
                'CREATE SCHEMA "task_7" AUTHORIZATION '
                '"elt_c55424493ddc8f0e1920_user"',
            ],
        )
        self.assertEqual(solver_connection.rollbacks, 1)
        self.assertFalse(solver_connection.autocommit)
        self.assertTrue(solver_connection.closed)
        self.assertNotEqual(result["password"], base_credentials()["password"])
        self.assertNotEqual(result["username"], base_credentials()["username"])
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertFalse(connection.autocommit)
        self.assertTrue(connection.value.closed)

    def test_provision_attempt_checks_database_before_ddl_and_rolls_back(self) -> None:
        connection = FakeConnection(FakeCursor(database="wrong_database"))
        with self.assertRaisesRegex(
            RedshiftRuntimeError, "could not provision isolated Redshift database"
        ):
            provision_attempt(
                connection,
                "task_7",
                credentials=base_credentials(),
                database="task_7_db",
                password="SafePass1!",
                attempt_dedicated_deployment=True,
            )
        self.assertEqual(connection.value.statements, ["SELECT current_database()"])
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertFalse(connection.autocommit)
        self.assertTrue(connection.value.closed)

    def test_provision_attempt_refuses_reusing_cluster_global_user(self) -> None:
        connection = FakeConnection(FakeCursor(user_exists=True))
        with self.assertRaisesRegex(
            RedshiftRuntimeError, "could not provision isolated Redshift database"
        ):
            provision_attempt(
                connection,
                "task_7",
                credentials=base_credentials(),
                database="task_7_db",
                password="SafePass1!",
                attempt_dedicated_deployment=True,
            )
        self.assertEqual(
            len(connection.value.statements),
            2,
            "collision must fail before schema checks or mutating DDL",
        )
        self.assertFalse(
            any(
                statement.startswith(("CREATE ", "DROP ", "GRANT ", "REVOKE "))
                for statement in connection.value.statements
            )
        )
        self.assertEqual(connection.rollbacks, 1)
        self.assertFalse(connection.autocommit)

    def test_provision_attempt_rejects_server_invalid_password_before_sql(self) -> None:
        for password in ("Unsafe'Pass1!", "Unsafe@Pass1!"):
            with self.subTest(password=password):
                connection = FakeConnection()
                with self.assertRaisesRegex(ValueError, "safety requirements"):
                    provision_attempt(
                        connection,
                        "task_7",
                        credentials=base_credentials(),
                        database="task_7_db",
                        password=password,
                        attempt_dedicated_deployment=True,
                    )
                self.assertEqual(connection.value.statements, [])

    def test_provision_attempt_sanitizes_ddl_failures(self) -> None:
        secret = "SafePass1!"
        connection = FakeConnection(
            FakeCursor(fail_on="CREATE USER", failure_secret=secret)
        )
        with self.assertRaises(RedshiftRuntimeError) as raised:
            provision_attempt(
                connection,
                "task_7",
                credentials=base_credentials(),
                database="task_7_db",
                password=secret,
                attempt_dedicated_deployment=True,
            )
        self.assertNotIn(secret, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertFalse(connection.autocommit)
        self.assertTrue(connection.value.closed)

    def test_schema_initialization_failure_removes_created_database_and_user(
        self,
    ) -> None:
        secret = "SafePass1!"
        admin = FakeConnection()
        solver = FakeConnection(
            FakeCursor(
                database="task_7_db",
                fail_on="CREATE SCHEMA",
                failure_secret=secret,
                results=[
                    ("task_7_db",),
                    ("elt_c55424493ddc8f0e1920_user",),
                ],
            )
        )

        with self.assertRaises(RedshiftRuntimeError) as raised:
            provision_attempt(
                admin,
                "task_7",
                credentials=base_credentials(),
                database="task_7_db",
                password=secret,
                attempt_dedicated_deployment=True,
                attempt_connector=lambda _credentials: solver,
            )

        self.assertNotIn(secret, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIn(
            'DROP DATABASE IF EXISTS "task_7_db"', admin.value.statements
        )
        self.assertIn(
            'DROP USER IF EXISTS "elt_c55424493ddc8f0e1920_user"',
            admin.value.statements,
        )
        self.assertTrue(solver.closed)
        self.assertFalse(admin.autocommit)

    def test_provision_requires_attempt_dedicated_deployment_assertion(self) -> None:
        connection = FakeConnection()
        with self.assertRaisesRegex(ValueError, "attempt-dedicated"):
            provision_attempt(
                connection,
                "task_7",
                credentials=base_credentials(),
                database="task_7_db",
                password="SafePass1!",
            )
        self.assertEqual(connection.value.statements, [])


if __name__ == "__main__":
    unittest.main()

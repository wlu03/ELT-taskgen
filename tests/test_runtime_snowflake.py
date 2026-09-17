from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from elt_taskgen.runtime.snowflake import (
    SnowflakeOwnershipError,
    SnowflakeRuntimeError,
    connect,
    load_credentials,
    provision_attempt,
    quote_identifier,
    reset_database,
)


MARKER = "elt-taskgen/attempt/v1:DEMO_1"

PRECHECK = [
    "USE ROLE SYSADMIN",
    "SHOW DATABASES LIKE 'DEMO_1'",
    "SHOW WAREHOUSES LIKE 'DEMO_1_AIRBYTE_WH'",
    "USE ROLE SECURITYADMIN",
    "SHOW USERS LIKE 'DEMO_1_AIRBYTE_USER'",
    "SHOW ROLES LIKE 'DEMO_1_AIRBYTE_ROLE'",
]

CREATE_SEQUENCE = [
    f"CREATE ROLE \"DEMO_1_AIRBYTE_ROLE\" COMMENT = '{MARKER}'",
    'GRANT ROLE "DEMO_1_AIRBYTE_ROLE" TO ROLE SYSADMIN',
    (
        'CREATE USER "DEMO_1_AIRBYTE_USER" PASSWORD = \'Safe\'\'Pass1!\' '
        "DEFAULT_ROLE = 'DEMO_1_AIRBYTE_ROLE' "
        "DEFAULT_WAREHOUSE = 'DEMO_1_AIRBYTE_WH' "
        f"MUST_CHANGE_PASSWORD = FALSE COMMENT = '{MARKER}'"
    ),
    'GRANT ROLE "DEMO_1_AIRBYTE_ROLE" TO USER "DEMO_1_AIRBYTE_USER"',
    "USE ROLE SYSADMIN",
    (
        'CREATE WAREHOUSE "DEMO_1_AIRBYTE_WH" WAREHOUSE_SIZE = XSMALL '
        "WAREHOUSE_TYPE = STANDARD AUTO_SUSPEND = 60 AUTO_RESUME = TRUE "
        f"INITIALLY_SUSPENDED = TRUE COMMENT = '{MARKER}'"
    ),
    'GRANT USAGE ON WAREHOUSE "DEMO_1_AIRBYTE_WH" TO ROLE "DEMO_1_AIRBYTE_ROLE"',
    f"CREATE DATABASE \"DEMO_1\" COMMENT = '{MARKER}'",
    'GRANT OWNERSHIP ON DATABASE "DEMO_1" TO ROLE "DEMO_1_AIRBYTE_ROLE"',
]

UNDO_ROLE = ["USE ROLE SECURITYADMIN", 'DROP ROLE IF EXISTS "DEMO_1_AIRBYTE_ROLE"']
UNDO_USER = ["USE ROLE SECURITYADMIN", 'DROP USER IF EXISTS "DEMO_1_AIRBYTE_USER"']
UNDO_WAREHOUSE = ["USE ROLE SYSADMIN", 'DROP WAREHOUSE IF EXISTS "DEMO_1_AIRBYTE_WH"']
UNDO_DATABASE = ["USE ROLE SYSADMIN", 'DROP DATABASE IF EXISTS "DEMO_1"']


class FakeCursor:
    def __init__(
        self,
        *,
        show_results: dict[str, list[tuple[str, str]]] | None = None,
        fail_on: str | tuple[str, ...] | None = None,
        failure_secret: str = "",
    ) -> None:
        self.show_results = show_results or {}
        self.fail_on = (fail_on,) if isinstance(fail_on, str) else (fail_on or ())
        self.failure_secret = failure_secret
        self.statements: list[str] = []
        self.closed = False
        self._pending_rows: list[tuple[str, str]] = []

    @property
    def description(self) -> tuple[tuple[str, None], tuple[str, None]]:
        return (("name", None), ("comment", None))

    def execute(self, statement: str) -> None:
        self.statements.append(statement)
        if statement.startswith("SHOW "):
            self._pending_rows = self.show_results.get(statement, [])
        if any(needle in statement for needle in self.fail_on):
            raise RuntimeError(f"driver leaked {self.failure_secret}")

    def fetchall(self) -> list[tuple[str, str]]:
        return self._pending_rows

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: FakeCursor | None = None) -> None:
        self.value = cursor or FakeCursor()

    def cursor(self) -> FakeCursor:
        return self.value


class RuntimeSnowflakeTests(unittest.TestCase):
    def test_identifiers_are_validated_and_quoted(self) -> None:
        self.assertEqual(quote_identifier("demo_1"), '"DEMO_1"')
        with self.assertRaisesRegex(ValueError, "unsafe Snowflake identifier"):
            quote_identifier("demo; DROP DATABASE prod")

    def test_credential_loader_fails_closed_on_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snowflake.json"
            path.write_text(
                json.dumps({"account": "", "user": "u", "password": "p"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SnowflakeRuntimeError, "account"):
                load_credentials(path)
            path.write_text(
                json.dumps({"account": "a", "user": "u", "password": "p"}),
                encoding="utf-8",
            )
            self.assertEqual(load_credentials(path)["account"], "a")

            path.write_text(
                json.dumps(
                    {"account": "org-account", "user": "u", "password": "<secret>"}
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SnowflakeRuntimeError, "placeholder"):
                load_credentials(path)

    def test_credential_loader_rejects_duplicates_and_unknowns_without_echoing(
        self,
    ) -> None:
        secret = "do-not-echo-this-secret"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snowflake.json"
            path.write_text(
                '{"account":"org-account","account":"%s"}' % secret,
                encoding="utf-8",
            )
            with self.assertRaises(SnowflakeRuntimeError) as raised:
                load_credentials(path)
            self.assertNotIn(secret, str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)

            path.write_text(
                json.dumps(
                    {
                        "account": "org-account",
                        "user": "u",
                        "password": "p",
                        secret: "value",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(SnowflakeRuntimeError) as raised:
                load_credentials(path)
            self.assertNotIn(secret, str(raised.exception))

    def test_connect_enforces_bounded_driver_timeouts(self) -> None:
        calls: list[dict[str, object]] = []
        expected = object()
        connector_module = types.ModuleType("snowflake.connector")
        connector_module.connect = lambda **kwargs: calls.append(kwargs) or expected
        snowflake_module = types.ModuleType("snowflake")
        snowflake_module.__path__ = []  # type: ignore[attr-defined]
        snowflake_module.connector = connector_module

        with mock.patch.dict(
            sys.modules,
            {
                "snowflake": snowflake_module,
                "snowflake.connector": connector_module,
            },
        ):
            result = connect(
                {
                    "account": "org-account",
                    "user": "runtime-user",
                    "password": "runtime-secret",
                    "login_timeout": 999,
                }
            )

        self.assertIs(result, expected)
        self.assertEqual(calls[0]["login_timeout"], 30)
        self.assertEqual(calls[0]["network_timeout"], 30)
        self.assertEqual(calls[0]["socket_timeout"], 30)

    def test_connect_failure_has_no_secret_bearing_exception_chain(self) -> None:
        connector_module = types.ModuleType("snowflake.connector")

        def fail_connect(**_kwargs: object) -> object:
            raise RuntimeError("driver echoed runtime-secret")

        connector_module.connect = fail_connect
        snowflake_module = types.ModuleType("snowflake")
        snowflake_module.__path__ = []  # type: ignore[attr-defined]
        snowflake_module.connector = connector_module

        with mock.patch.dict(
            sys.modules,
            {
                "snowflake": snowflake_module,
                "snowflake.connector": connector_module,
            },
        ):
            with self.assertRaises(SnowflakeRuntimeError) as raised:
                connect(
                    {
                        "account": "org-account",
                        "user": "runtime-user",
                        "password": "runtime-secret",
                    }
                )

        self.assertEqual(str(raised.exception), "could not connect to Snowflake")
        self.assertIsNone(raised.exception.__cause__)

    def test_provision_attempt_precheck_then_create_on_fresh_names(self) -> None:
        connection = FakeConnection()
        credentials = provision_attempt(
            connection,
            "demo_1",
            account="org-account",
            password="Safe'Pass1!",
        )
        self.assertEqual(
            credentials,
            {
                "account": "org-account",
                "user": "DEMO_1_AIRBYTE_USER",
                "password": "Safe'Pass1!",
                "role": "DEMO_1_AIRBYTE_ROLE",
                "warehouse": "DEMO_1_AIRBYTE_WH",
            },
        )
        self.assertEqual(connection.value.statements, PRECHECK + CREATE_SEQUENCE)
        for statement in connection.value.statements:
            self.assertFalse(statement.startswith("DROP "), statement)
        self.assertTrue(connection.value.closed)

    def test_provision_attempt_reprovisions_marker_owned_names(self) -> None:
        show_results = {
            "SHOW DATABASES LIKE 'DEMO_1'": [("DEMO_1", MARKER)],
            "SHOW WAREHOUSES LIKE 'DEMO_1_AIRBYTE_WH'": [("DEMO_1_AIRBYTE_WH", MARKER)],
            "SHOW USERS LIKE 'DEMO_1_AIRBYTE_USER'": [("DEMO_1_AIRBYTE_USER", MARKER)],
            "SHOW ROLES LIKE 'DEMO_1_AIRBYTE_ROLE'": [("DEMO_1_AIRBYTE_ROLE", MARKER)],
        }
        connection = FakeConnection(FakeCursor(show_results=show_results))
        provision_attempt(
            connection,
            "demo_1",
            account="org-account",
            password="Safe'Pass1!",
        )
        statements = connection.value.statements
        self.assertEqual(statements[: len(PRECHECK)], PRECHECK)
        self.assertEqual(
            statements[len(PRECHECK) : len(PRECHECK) + 2],
            [
                'DROP USER IF EXISTS "DEMO_1_AIRBYTE_USER"',
                'DROP ROLE IF EXISTS "DEMO_1_AIRBYTE_ROLE"',
            ],
        )
        sysadmin_index = statements.index("USE ROLE SYSADMIN", len(PRECHECK))
        self.assertEqual(
            statements[sysadmin_index + 1 : sysadmin_index + 3],
            [
                'DROP WAREHOUSE IF EXISTS "DEMO_1_AIRBYTE_WH"',
                'DROP DATABASE IF EXISTS "DEMO_1"',
            ],
        )
        for drop, create_prefix in (
            ('DROP USER IF EXISTS "DEMO_1_AIRBYTE_USER"', 'CREATE USER "DEMO_1_AIRBYTE_USER"'),
            ('DROP ROLE IF EXISTS "DEMO_1_AIRBYTE_ROLE"', 'CREATE ROLE "DEMO_1_AIRBYTE_ROLE"'),
            (
                'DROP WAREHOUSE IF EXISTS "DEMO_1_AIRBYTE_WH"',
                'CREATE WAREHOUSE "DEMO_1_AIRBYTE_WH"',
            ),
            ('DROP DATABASE IF EXISTS "DEMO_1"', 'CREATE DATABASE "DEMO_1"'),
        ):
            with self.subTest(drop=drop):
                create_index = next(
                    index
                    for index, statement in enumerate(statements)
                    if statement.startswith(create_prefix)
                )
                self.assertLess(statements.index(drop), create_index)

    def test_provision_attempt_fails_closed_on_unowned_collision(self) -> None:
        show_results = {
            "SHOW DATABASES LIKE 'DEMO_1'": [("DEMO_1", "")],
            "SHOW USERS LIKE 'DEMO_1_AIRBYTE_USER'": [
                ("DEMO_1_AIRBYTE_USER", "someone-else")
            ],
        }
        connection = FakeConnection(FakeCursor(show_results=show_results))
        with self.assertRaises(SnowflakeOwnershipError) as caught:
            provision_attempt(connection, "demo_1", account="org-account")
        message = str(caught.exception)
        self.assertIn('database "DEMO_1"', message)
        self.assertIn('user "DEMO_1_AIRBYTE_USER"', message)
        for statement in connection.value.statements:
            self.assertFalse(
                statement.startswith(("DROP ", "CREATE ", "GRANT ")), statement
            )
        self.assertTrue(connection.value.closed)

    def test_provision_attempt_rolls_back_created_objects_on_each_transition(self) -> None:
        cases = (
            ("CREATE ROLE ", []),
            ('GRANT ROLE "DEMO_1_AIRBYTE_ROLE" TO ROLE SYSADMIN', UNDO_ROLE),
            ("CREATE USER ", UNDO_ROLE),
            ('TO USER "DEMO_1_AIRBYTE_USER"', UNDO_USER + UNDO_ROLE),
            ("CREATE WAREHOUSE ", UNDO_USER + UNDO_ROLE),
            ("GRANT USAGE ON WAREHOUSE ", UNDO_WAREHOUSE + UNDO_USER + UNDO_ROLE),
            ("CREATE DATABASE ", UNDO_WAREHOUSE + UNDO_USER + UNDO_ROLE),
            (
                "GRANT OWNERSHIP ON DATABASE ",
                UNDO_DATABASE + UNDO_WAREHOUSE + UNDO_USER + UNDO_ROLE,
            ),
        )
        for fail_on, expected_cleanup_tail in cases:
            with self.subTest(fail_on=fail_on):
                cursor = FakeCursor(fail_on=fail_on, failure_secret="admin-token")
                connection = FakeConnection(cursor)
                with self.assertRaises(SnowflakeRuntimeError) as caught:
                    provision_attempt(
                        connection,
                        "demo_1",
                        account="org-account",
                        password="Safe'Pass1!",
                    )
                failing_index = next(
                    index
                    for index, statement in enumerate(cursor.statements)
                    if fail_on in statement
                )
                self.assertEqual(
                    cursor.statements[failing_index + 1 :], expected_cleanup_tail
                )
                self.assertNotIn("admin-token", str(caught.exception))
                self.assertNotIn("Safe'Pass1!", str(caught.exception))
                self.assertTrue(cursor.closed)

    def test_provision_attempt_cleanup_is_fail_safe_and_reports_aggregate(self) -> None:
        cursor = FakeCursor(
            fail_on=("GRANT OWNERSHIP ON DATABASE ", "DROP WAREHOUSE"),
            failure_secret="admin-token",
        )
        connection = FakeConnection(cursor)
        with self.assertRaises(SnowflakeRuntimeError) as caught:
            provision_attempt(
                connection,
                "demo_1",
                account="org-account",
                password="Safe'Pass1!",
            )
        failing_index = next(
            index
            for index, statement in enumerate(cursor.statements)
            if "GRANT OWNERSHIP ON DATABASE " in statement
        )
        tail = cursor.statements[failing_index + 1 :]
        self.assertIn('DROP DATABASE IF EXISTS "DEMO_1"', tail)
        warehouse_index = tail.index('DROP WAREHOUSE IF EXISTS "DEMO_1_AIRBYTE_WH"')
        self.assertIn(
            'DROP USER IF EXISTS "DEMO_1_AIRBYTE_USER"', tail[warehouse_index + 1 :]
        )
        self.assertIn(
            'DROP ROLE IF EXISTS "DEMO_1_AIRBYTE_ROLE"', tail[warehouse_index + 1 :]
        )
        self.assertIn("cleanup incomplete for: warehouse", str(caught.exception))
        self.assertNotIn("admin-token", str(caught.exception))

    def test_reset_database_prechecks_marker_before_drop(self) -> None:
        with self.subTest(path="fresh"):
            connection = FakeConnection()
            reset_database(connection, "demo_1")
            self.assertEqual(
                connection.value.statements,
                [
                    "SHOW DATABASES LIKE 'DEMO_1'",
                    f"CREATE DATABASE \"DEMO_1\" COMMENT = '{MARKER}'",
                    'GRANT OWNERSHIP ON DATABASE "DEMO_1" TO ROLE "AIRBYTE_ROLE"',
                ],
            )
            self.assertTrue(connection.value.closed)
        with self.subTest(path="owned"):
            cursor = FakeCursor(
                show_results={"SHOW DATABASES LIKE 'DEMO_1'": [("DEMO_1", MARKER)]}
            )
            reset_database(FakeConnection(cursor), "demo_1")
            self.assertEqual(
                cursor.statements,
                [
                    "SHOW DATABASES LIKE 'DEMO_1'",
                    'DROP DATABASE IF EXISTS "DEMO_1"',
                    f"CREATE DATABASE \"DEMO_1\" COMMENT = '{MARKER}'",
                    'GRANT OWNERSHIP ON DATABASE "DEMO_1" TO ROLE "AIRBYTE_ROLE"',
                ],
            )
        with self.subTest(path="unowned"):
            cursor = FakeCursor(
                show_results={"SHOW DATABASES LIKE 'DEMO_1'": [("DEMO_1", "legacy")]}
            )
            with self.assertRaises(SnowflakeOwnershipError):
                reset_database(FakeConnection(cursor), "demo_1")
            for statement in cursor.statements:
                self.assertFalse(
                    statement.startswith(("DROP ", "CREATE ", "GRANT ")), statement
                )
            self.assertTrue(cursor.closed)

    def test_reset_database_drops_created_database_when_grant_fails(self) -> None:
        cursor = FakeCursor(fail_on="GRANT OWNERSHIP")
        with self.assertRaises(SnowflakeRuntimeError):
            reset_database(FakeConnection(cursor), "demo_1")
        self.assertEqual(cursor.statements[-1], 'DROP DATABASE IF EXISTS "DEMO_1"')
        self.assertTrue(cursor.closed)


if __name__ == "__main__":
    unittest.main()

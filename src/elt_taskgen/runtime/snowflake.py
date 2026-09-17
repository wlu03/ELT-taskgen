"""Snowflake connection and isolated-database helpers for runtime attempts."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from elt_taskgen.sql_identifiers import quote_sql_identifier


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_PLACEHOLDER = re.compile(r"<[^<>]+>")
_CONNECT_TIMEOUT_SECONDS = 30
_KNOWN_CREDENTIAL_FIELDS = frozenset(
    ("account", "user", "password", "role", "warehouse", "database", "schema")
)


class SnowflakeRuntimeError(RuntimeError):
    """Snowflake runtime setup or connectivity failed."""


class SnowflakeOwnershipError(SnowflakeRuntimeError):
    """A derived attempt name already exists and is not attempt-owned."""


class _DuplicateCredentialField(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateCredentialField
        value[key] = item
    return value


def quote_identifier(value: str) -> str:
    """Validate exporter-owned names and quote them for Snowflake."""
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsafe Snowflake identifier: {value!r}")
    return quote_sql_identifier(value.upper(), dialect="snowflake", force=True)


def _quote_literal(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("unsafe empty Snowflake string literal")
    return "'" + value.replace("'", "''") + "'"


def _attempt_principal_names(database: str) -> tuple[str, str, str]:
    base = database.upper()
    return (
        f"{base}_AIRBYTE_ROLE",
        f"{base}_AIRBYTE_USER",
        f"{base}_AIRBYTE_WH",
    )


_ATTEMPT_MARKER_PREFIX = "elt-taskgen/attempt/v1:"


def attempt_marker(database: str) -> str:
    """Ownership marker set as the COMMENT on every attempt-created object.

    Harness-initiated drops are gated on an exact match: an existing object
    whose comment is anything else is never dropped by this module.
    """
    quote_identifier(database)  # reuse identifier validation
    return _ATTEMPT_MARKER_PREFIX + database.upper()


def _show_comment(cursor, kind: str, name: str) -> tuple[bool, str | None]:
    """Return (exists, comment) for the exactly named object of ``kind``.

    ``kind`` is one of DATABASES/USERS/ROLES/WAREHOUSES; ``name`` is the
    upper-cased exact object name.
    """
    cursor.execute(f"SHOW {kind} LIKE {_quote_literal(name)}")
    columns = [str(column[0]).lower() for column in cursor.description]
    for row in cursor.fetchall():
        record = dict(zip(columns, row))
        # Exact-name filter is mandatory: LIKE treats "_" as a
        # single-character wildcard, so sibling names can match the pattern.
        if str(record.get("name", "")).upper() == name:
            comment = record.get("comment")
            return True, comment if isinstance(comment, str) and comment else None
    return False, None


def _existing_owned(cursor, database: str) -> dict[str, bool]:
    """Report which derived attempt names already exist and are attempt-owned.

    Fails closed before any mutation: every colliding name whose comment is
    not the exact attempt marker is collected into one
    :class:`SnowflakeOwnershipError`. Leaves the session in SECURITYADMIN.
    """
    marker = attempt_marker(database)
    role_name, user_name, warehouse_name = _attempt_principal_names(database)
    owned: dict[str, bool] = {}
    unowned: list[str] = []
    checks = (
        (
            "USE ROLE SYSADMIN",
            (
                ("database", "DATABASES", database.upper()),
                ("warehouse", "WAREHOUSES", warehouse_name),
            ),
        ),
        (
            "USE ROLE SECURITYADMIN",
            (
                ("user", "USERS", user_name),
                ("role", "ROLES", role_name),
            ),
        ),
    )
    for role_statement, kinds in checks:
        cursor.execute(role_statement)
        for label, kind, name in kinds:
            exists, comment = _show_comment(cursor, kind, name)
            owned[label] = exists and comment == marker
            if exists and comment != marker:
                unowned.append(f'{label} "{name}"')
    if unowned:
        raise SnowflakeOwnershipError(
            "Snowflake attempt names already exist and are not owned by this "
            "harness: " + ", ".join(unowned)
            + "; drop them manually or choose another attempt name"
        )
    return owned


def load_credentials(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, _DuplicateCredentialField):
        raise SnowflakeRuntimeError(
            f"invalid Snowflake credential file: {path}"
        ) from None
    if not isinstance(value, dict):
        raise SnowflakeRuntimeError("Snowflake credential file must contain an object")
    if set(value) - _KNOWN_CREDENTIAL_FIELDS:
        # Field names are attacker-controlled and may themselves contain a
        # secret, so do not echo them.
        raise SnowflakeRuntimeError(
            "Snowflake credential file contains unsupported fields"
        )
    missing = [
        key
        for key in ("account", "user", "password")
        if not isinstance(value.get(key), str) or not value.get(key)
    ]
    if missing:
        raise SnowflakeRuntimeError(
            "Snowflake credential file has empty/missing fields: " + ", ".join(missing)
        )
    for key, item in value.items():
        if not isinstance(item, str) or not item:
            raise SnowflakeRuntimeError(
                f"Snowflake credential file has an invalid field: {key}"
            )
        stripped = item.strip()
        if _PLACEHOLDER.fullmatch(stripped) or stripped.casefold() in {
            "changeme",
            "replace-me",
            "replace_me",
        }:
            raise SnowflakeRuntimeError(
                f"Snowflake credential file contains a placeholder field: {key}"
            )
    account = value["account"]
    if not _ACCOUNT.fullmatch(account):
        raise SnowflakeRuntimeError(
            "Snowflake credential account must not contain a URL scheme or path"
        )
    return dict(value)


def connect(credentials: Mapping[str, Any]):
    """Open a DB-API connection with a lazy optional dependency."""
    try:
        import snowflake.connector
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise SnowflakeRuntimeError(
            "Snowflake runtime requires the optional snowflake-connector-python package"
        ) from exc
    connect_args = dict(credentials)
    # Bound authentication and subsequent network operations. User-supplied
    # credential documents cannot silently disable these certification bounds.
    connect_args["login_timeout"] = _CONNECT_TIMEOUT_SECONDS
    connect_args["network_timeout"] = _CONNECT_TIMEOUT_SECONDS
    connect_args["socket_timeout"] = _CONNECT_TIMEOUT_SECONDS
    try:
        return snowflake.connector.connect(**connect_args)
    except Exception:  # pragma: no cover - transport/account-specific
        # Connector errors can contain connection parameters or passwords.
        raise SnowflakeRuntimeError("could not connect to Snowflake") from None


def reset_database(connection, database: str, *, owner_role: str = "AIRBYTE_ROLE") -> None:
    """Create a per-attempt database owned by the Airbyte loading role.

    Replace an existing database only when its marker matches the attempt.
    """
    db = quote_identifier(database)
    role = quote_identifier(owner_role)
    marker = attempt_marker(database)
    cursor = connection.cursor()
    database_created = False
    try:
        exists, comment = _show_comment(cursor, "DATABASES", database.upper())
        if exists and comment != marker:
            raise SnowflakeOwnershipError(
                f"existing Snowflake database is not owned by this harness: {db}"
            )
        if exists:
            cursor.execute(f"DROP DATABASE IF EXISTS {db}")
        cursor.execute(f"CREATE DATABASE {db} COMMENT = {_quote_literal(marker)}")
        database_created = True
        cursor.execute(f"GRANT OWNERSHIP ON DATABASE {db} TO ROLE {role}")
    except SnowflakeOwnershipError:
        raise
    except Exception:
        message = f"could not provision isolated Snowflake database {database!r}"
        if database_created:
            try:
                cursor.execute(f"DROP DATABASE IF EXISTS {db}")
            except Exception:
                message += " (cleanup incomplete for: database)"
        raise SnowflakeRuntimeError(message) from None
    finally:
        cursor.close()


def provision_attempt(
    connection,
    database: str,
    *,
    account: str,
    password: str | None = None,
) -> dict[str, str]:
    """Create a task-scoped Snowflake principal and objects.

    Return only scoped solver credentials. Existing names may be replaced only
    when their ownership marker matches the attempt. Other collisions fail
    before mutation, and partial failures remove objects created by this call.
    """

    if not isinstance(account, str) or not account.strip():
        raise ValueError("Snowflake account is required")
    db = quote_identifier(database)
    role_name, user_name, warehouse_name = _attempt_principal_names(database)
    role = quote_identifier(role_name)
    user = quote_identifier(user_name)
    warehouse = quote_identifier(warehouse_name)
    generated_password = password or ("Aa1!" + secrets.token_urlsafe(24))
    password_sql = _quote_literal(generated_password)
    role_default = _quote_literal(role_name)
    warehouse_default = _quote_literal(warehouse_name)
    marker_sql = _quote_literal(attempt_marker(database))

    cursor = connection.cursor()
    undo: list[tuple[str, str, str]] = []
    try:
        owned = _existing_owned(cursor, database)
        # Record undo only after a statement succeeds. GRANTs need none because
        # dropping created objects removes their grants; precheck leaves SECURITYADMIN.
        plan: list[tuple[str, tuple[str, str, str] | None]] = []
        if owned["user"]:
            plan.append((f"DROP USER IF EXISTS {user}", None))
        if owned["role"]:
            plan.append((f"DROP ROLE IF EXISTS {role}", None))
        plan.extend(
            [
                (
                    f"CREATE ROLE {role} COMMENT = {marker_sql}",
                    ("role", "SECURITYADMIN", f"DROP ROLE IF EXISTS {role}"),
                ),
                (f"GRANT ROLE {role} TO ROLE SYSADMIN", None),
                (
                    (
                        f"CREATE USER {user} PASSWORD = {password_sql} "
                        f"DEFAULT_ROLE = {role_default} "
                        f"DEFAULT_WAREHOUSE = {warehouse_default} "
                        f"MUST_CHANGE_PASSWORD = FALSE COMMENT = {marker_sql}"
                    ),
                    ("user", "SECURITYADMIN", f"DROP USER IF EXISTS {user}"),
                ),
                (f"GRANT ROLE {role} TO USER {user}", None),
                ("USE ROLE SYSADMIN", None),
            ]
        )
        if owned["warehouse"]:
            plan.append((f"DROP WAREHOUSE IF EXISTS {warehouse}", None))
        if owned["database"]:
            plan.append((f"DROP DATABASE IF EXISTS {db}", None))
        plan.extend(
            [
                (
                    (
                        f"CREATE WAREHOUSE {warehouse} WAREHOUSE_SIZE = XSMALL "
                        "WAREHOUSE_TYPE = STANDARD AUTO_SUSPEND = 60 AUTO_RESUME = TRUE "
                        f"INITIALLY_SUSPENDED = TRUE COMMENT = {marker_sql}"
                    ),
                    ("warehouse", "SYSADMIN", f"DROP WAREHOUSE IF EXISTS {warehouse}"),
                ),
                (f"GRANT USAGE ON WAREHOUSE {warehouse} TO ROLE {role}", None),
                (
                    f"CREATE DATABASE {db} COMMENT = {marker_sql}",
                    ("database", "SYSADMIN", f"DROP DATABASE IF EXISTS {db}"),
                ),
                (f"GRANT OWNERSHIP ON DATABASE {db} TO ROLE {role}", None),
            ]
        )
        for statement, undo_entry in plan:
            cursor.execute(statement)
            if undo_entry is not None:
                undo.append(undo_entry)
    except SnowflakeOwnershipError:
        raise
    except Exception:
        failed_cleanup: list[str] = []
        for label, undo_role, drop_sql in reversed(undo):
            try:
                # USE ROLE stays inside the try: a dead session must not
                # abort the remainder of the sweep.
                cursor.execute(f"USE ROLE {undo_role}")
                cursor.execute(drop_sql)
            except Exception:
                failed_cleanup.append(label)
        message = f"could not provision isolated Snowflake attempt {database!r}"
        if failed_cleanup:
            message += " (cleanup incomplete for: " + ", ".join(failed_cleanup) + ")"
        raise SnowflakeRuntimeError(message) from None
    finally:
        cursor.close()
    return {
        "account": account,
        "user": user_name,
        "password": generated_password,
        "role": role_name,
        "warehouse": warehouse_name,
    }

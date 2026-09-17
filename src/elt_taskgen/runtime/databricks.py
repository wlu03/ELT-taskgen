"""Provision task-scoped Databricks SQL and Unity Catalog access.

The harness owns the administrator connection. The caller creates and reserves
the solver principal. This module grants only the declared scope and returns
only solver credentials.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from elt_taskgen.sql_identifiers import quote_sql_identifier


_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")
_HOSTNAME = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\."
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)
_PRINCIPAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]*")
_CONNECT_SOCKET_TIMEOUT_SECONDS = 15
_CONNECT_RETRY_LIMIT = 3
_CONNECT_RETRY_WINDOW_SECONDS = 30
_OAUTH_HTTP_TIMEOUT_SECONDS = 10
_OAUTH_RETRY_WINDOW_SECONDS = 15
_KNOWN_CREDENTIAL_FIELDS = {
    "hostname",
    "server_hostname",
    "http_path",
    "access_token",
    "client_id",
    "secret",
    "client_secret",
    "database",
    "catalog",
    "schema",
}


class DatabricksRuntimeError(RuntimeError):
    """Databricks runtime setup or connectivity failed."""


@dataclass(frozen=True)
class UnityCatalogScope:
    """Unity Catalog boundary reserved for one attempt.

    Airbyte requires ``CREATE SCHEMA``. The catalog must be newly created or
    explicitly dedicated to this attempt.
    """

    catalog: str
    allow_create_catalog: bool = False
    existing_dedicated_catalog: bool = False

    def __post_init__(self) -> None:
        if self.allow_create_catalog and self.existing_dedicated_catalog:
            raise ValueError(
                "Databricks catalog cannot be both newly created and existing"
            )


def quote_identifier(value: str) -> str:
    """Validate and quote an exporter-owned catalog or schema identifier."""

    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsafe Databricks identifier: {value!r}")
    return quote_sql_identifier(value, dialect="databricks", force=True)


def quote_principal(value: str) -> str:
    """Validate and quote a service-principal UUID/name for a GRANT target."""

    if not isinstance(value, str) or not _PRINCIPAL.fullmatch(value):
        raise ValueError("unsafe Databricks principal")
    return f"`{value}`"


def _required_text(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DatabricksRuntimeError(
            f"Databricks credential has an empty/missing field: {key}"
        )
    return value


def _alias(
    values: Mapping[str, object],
    first: str,
    second: str,
    *,
    required: bool = False,
) -> str | None:
    left = values.get(first)
    right = values.get(second)
    supplied = [value for value in (left, right) if value is not None]
    for value in supplied:
        if not isinstance(value, str) or not value.strip():
            raise DatabricksRuntimeError(
                f"Databricks credential has an empty/missing field: {first}"
            )
    if len(supplied) == 2 and supplied[0] != supplied[1]:
        raise DatabricksRuntimeError(
            f"Databricks credential has conflicting {first}/{second} fields"
        )
    if supplied:
        return supplied[0]
    if required:
        raise DatabricksRuntimeError(
            f"Databricks credential has an empty/missing field: {first}"
        )
    return None


def _normalize_credentials(values: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise DatabricksRuntimeError("Databricks credential must contain an object")
    unknown = sorted(str(key) for key in values if key not in _KNOWN_CREDENTIAL_FIELDS)
    if unknown:
        raise DatabricksRuntimeError(
            "Databricks credential contains unsupported fields: "
            + ", ".join(unknown)
        )

    hostname = _alias(values, "hostname", "server_hostname", required=True)
    assert hostname is not None
    if not _HOSTNAME.fullmatch(hostname):
        raise DatabricksRuntimeError(
            "Databricks credential hostname must not contain a URL scheme or path"
        )
    http_path = _required_text(values, "http_path")
    if not http_path.startswith("/") or any(
        character.isspace() or ord(character) < 32 for character in http_path
    ):
        raise DatabricksRuntimeError("Databricks credential has an invalid http_path")

    token = values.get("access_token")
    if token is not None and (not isinstance(token, str) or not token.strip()):
        raise DatabricksRuntimeError(
            "Databricks credential has an empty/missing field: access_token"
        )
    client_id = values.get("client_id")
    if client_id is not None and (
        not isinstance(client_id, str) or not client_id.strip()
    ):
        raise DatabricksRuntimeError(
            "Databricks credential has an empty/missing field: client_id"
        )
    secret = _alias(values, "secret", "client_secret")
    oauth_supplied = client_id is not None or secret is not None
    if token is not None and oauth_supplied:
        raise DatabricksRuntimeError(
            "Databricks credential must choose access_token or OAuth client fields"
        )
    if token is None and not oauth_supplied:
        raise DatabricksRuntimeError(
            "Databricks credential has no supported authentication fields"
        )
    if oauth_supplied and (client_id is None or secret is None):
        raise DatabricksRuntimeError(
            "Databricks OAuth credential requires both client_id and secret"
        )

    catalog = _alias(values, "database", "catalog")
    schema = values.get("schema")
    if schema is not None and (not isinstance(schema, str) or not schema.strip()):
        raise DatabricksRuntimeError(
            "Databricks credential has an empty/missing field: schema"
        )
    if schema is not None and catalog is None:
        raise DatabricksRuntimeError(
            "Databricks credential cannot select a schema without a catalog"
        )
    try:
        if catalog is not None:
            quote_identifier(catalog)
        if isinstance(schema, str):
            quote_identifier(schema)
    except ValueError:
        # Credential parsing has one public error type and never echoes values.
        raise DatabricksRuntimeError(
            "Databricks credential has an invalid catalog/schema identifier"
        ) from None

    normalized = {"hostname": hostname, "http_path": http_path}
    if isinstance(token, str):
        normalized["access_token"] = token
    else:
        assert isinstance(client_id, str) and secret is not None
        normalized["client_id"] = client_id
        normalized["secret"] = secret
    if catalog is not None:
        normalized["database"] = catalog
    if isinstance(schema, str):
        normalized["schema"] = schema
    return normalized


def load_credentials(path: Path) -> dict[str, str]:
    """Load one non-placeholder Databricks credential JSON document."""

    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        # Parser details and exception chains can quote credential contents.
        raise DatabricksRuntimeError(
            f"invalid Databricks credential file: {path}"
        ) from None
    if not isinstance(value, dict):
        raise DatabricksRuntimeError(
            "Databricks credential file must contain an object"
        )
    return _normalize_credentials(value)


def connect(credentials: Mapping[str, object]):
    """Open a Databricks SQL DB-API connection with lazy dependencies."""

    normalized = _normalize_credentials(credentials)
    try:
        from databricks import sql as dbsql
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise DatabricksRuntimeError(
            "Databricks runtime requires the optional databricks-sql-connector package"
        ) from exc

    connect_args: dict[str, Any] = {
        "server_hostname": normalized["hostname"],
        "http_path": normalized["http_path"],
        # The pinned connector otherwise permits a 15-minute socket timeout
        # and retry window. Certification must fail closed in bounded time
        # when SQL compute is stopped, unreachable, or out of credits.
        "_socket_timeout": _CONNECT_SOCKET_TIMEOUT_SECONDS,
        "_retry_stop_after_attempts_count": _CONNECT_RETRY_LIMIT,
        "_retry_stop_after_attempts_duration": _CONNECT_RETRY_WINDOW_SECONDS,
    }
    if "database" in normalized:
        connect_args["catalog"] = normalized["database"]
    if "schema" in normalized:
        connect_args["schema"] = normalized["schema"]
    if "access_token" in normalized:
        connect_args["access_token"] = normalized["access_token"]
    else:
        try:
            from databricks.sdk.core import Config, oauth_service_principal
        except ImportError as exc:  # pragma: no cover - environment-specific
            raise DatabricksRuntimeError(
                "Databricks OAuth runtime requires the optional databricks-sdk package"
            ) from exc
        try:
            # Config construction performs live OAuth host discovery before
            # dbsql.connect. Bound and sanitize that separate network phase as
            # well as the SQL connector below.
            config = Config(
                host=f"https://{normalized['hostname']}",
                client_id=normalized["client_id"],
                client_secret=normalized["secret"],
                http_timeout_seconds=_OAUTH_HTTP_TIMEOUT_SECONDS,
                retry_timeout_seconds=_OAUTH_RETRY_WINDOW_SECONDS,
            )
            connect_args["credentials_provider"] = lambda: oauth_service_principal(
                config
            )
        except Exception:
            raise DatabricksRuntimeError("could not connect to Databricks") from None
    try:
        return dbsql.connect(**connect_args)
    except Exception:  # pragma: no cover - transport/account-specific
        # Driver failures can echo the hostname, token, or OAuth fields.
        raise DatabricksRuntimeError("could not connect to Databricks") from None


def assert_current_principal(connection: Any, expected_principal: str) -> str:
    """Bind a solver SQL credential to the principal that receives grants.

    The caller opens ``connection`` with the solver credential, not the harness
    administrator credential.  Provisioning must stop before administrator DDL
    if Databricks reports any other current user.
    """

    quote_principal(expected_principal)
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT session_user()")
        row = cursor.fetchone()
    except Exception:
        # Driver diagnostics can contain OAuth fields or access tokens.
        raise DatabricksRuntimeError(
            "could not verify the Databricks solver principal"
        ) from None
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass

    if isinstance(row, Mapping):
        values = list(row.values())
    elif isinstance(row, Sequence) and not isinstance(
        row, (str, bytes, bytearray)
    ):
        values = list(row)
    else:
        values = []
    if (
        len(values) != 1
        or not isinstance(values[0], str)
        or values[0].casefold() != expected_principal.casefold()
    ):
        raise DatabricksRuntimeError(
            "Databricks solver credential does not match --solver-principal"
        )
    return values[0]


def provision_attempt(
    connection: Any,
    schema: str,
    *,
    scope: UnityCatalogScope,
    solver_principal: str,
    solver_credentials: Mapping[str, object],
    attempt_dedicated_principal: bool = False,
) -> dict[str, str]:
    """Provision one fresh schema inside an explicitly granted catalog scope.

    The helper never drops or reuses a schema: ``CREATE SCHEMA`` fails if the
    attempt name already exists.  It creates the catalog only when the caller's
    scope explicitly grants that operation.  ``connection`` is harness-admin
    state; only the separately supplied solver credential is returned.
    """

    if attempt_dedicated_principal is not True:
        raise ValueError(
            "Databricks provisioning requires an explicit attempt-dedicated "
            "solver-principal assertion"
        )

    catalog_sql = quote_identifier(scope.catalog)
    schema_sql = quote_identifier(schema)
    principal_sql = quote_principal(solver_principal)
    normalized = _normalize_credentials(solver_credentials)

    if not scope.allow_create_catalog and not scope.existing_dedicated_catalog:
        raise ValueError(
            "Databricks Airbyte attempts require a fresh or explicitly "
            "dedicated Unity Catalog"
        )

    selected_catalog = normalized.get("database")
    selected_schema = normalized.get("schema")
    if selected_catalog is not None and selected_catalog != scope.catalog:
        raise ValueError("solver credential selects a catalog outside its granted scope")
    if selected_schema is not None and selected_schema != schema:
        raise ValueError("solver credential selects a different attempt schema")

    statements: list[str] = []
    if scope.allow_create_catalog:
        statements.append(f"CREATE CATALOG {catalog_sql}")
    statements.extend(
        (
            f"CREATE SCHEMA {catalog_sql}.{schema_sql}",
            (
                "GRANT USE CATALOG, CREATE SCHEMA ON CATALOG "
                f"{catalog_sql} TO {principal_sql}"
            ),
            (
                "GRANT USE SCHEMA, CREATE TABLE, CREATE VOLUME, SELECT, MODIFY "
                "ON SCHEMA "
                f"{catalog_sql}.{schema_sql} TO {principal_sql}"
            ),
        )
    )

    cursor = connection.cursor()
    catalog_created = False
    schema_created = False
    try:
        for statement in statements:
            cursor.execute(statement)
            if statement.startswith("CREATE CATALOG "):
                catalog_created = True
            elif statement.startswith("CREATE SCHEMA "):
                schema_created = True
    except Exception:
        # DDL is not transactionally reversible in the runtime contract. Clean
        # up only objects this call proved it created, preserving a caller-owned
        # dedicated catalog while still removing its failed attempt schema.
        try:
            if catalog_created:
                cursor.execute(f"DROP CATALOG IF EXISTS {catalog_sql} CASCADE")
            elif schema_created:
                cursor.execute(
                    f"DROP SCHEMA IF EXISTS {catalog_sql}.{schema_sql} CASCADE"
                )
        except Exception:
            pass
        raise DatabricksRuntimeError(
            f"could not provision isolated Databricks schema "
            f"{scope.catalog}.{schema}"
        ) from None
    finally:
        cursor.close()

    return normalized | {"database": scope.catalog, "schema": schema}


__all__ = [
    "DatabricksRuntimeError",
    "UnityCatalogScope",
    "assert_current_principal",
    "connect",
    "load_credentials",
    "provision_attempt",
    "quote_identifier",
    "quote_principal",
]

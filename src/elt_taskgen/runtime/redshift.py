"""Provision an isolated Redshift user, database, and schema per attempt.

Each attempt requires a dedicated cluster or workgroup because users are
cluster-wide. Provisioning creates the schema through a verified solver
connection. S3 staging fields are returned to the connector but are not passed
to the DB-API driver or included in diagnostics.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from elt_taskgen.sql_identifiers import quote_sql_identifier


_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_PLACEHOLDER = re.compile(r"<[^<>]+>")
_MAX_IDENTIFIER_BYTES = 127
_CONNECT_TIMEOUT_SECONDS = 15
_CONNECT_ATTEMPTS = 3
_CONNECT_RETRY_SECONDS = 2.0

_CONNECTION_FIELDS = ("host", "port", "database", "username", "password")
_STAGING_FIELDS = (
    "s3_bucket_name",
    "s3_bucket_region",
    "access_key_id",
    "secret_access_key",
)
_KNOWN_FIELDS = frozenset(
    (*_CONNECTION_FIELDS, *_STAGING_FIELDS, "s3_bucket_path", "schema")
)
_SECRET_FIELDS = frozenset(("password", "secret_access_key"))


class RedshiftRuntimeError(RuntimeError):
    """Redshift runtime setup or connectivity failed without exposing secrets."""


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
    """Validate and quote an exporter-owned Redshift identifier."""

    if (
        not isinstance(value, str)
        or not _IDENTIFIER.fullmatch(value)
        or len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES
    ):
        raise ValueError(f"unsafe Redshift identifier: {value!r}")
    return quote_sql_identifier(value, dialect="redshift", force=True)


def _quote_literal(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("unsafe empty Redshift string literal")
    return "'" + value.replace("'", "''") + "'"


def _password_hash(value: str) -> str:
    """Build Redshift's documented salted SHA-256 password representation."""

    salt = secrets.token_bytes(32)
    digest = hashlib.sha256(value.encode("utf-8") + salt).hexdigest()
    return f"sha256|{digest}|{salt.hex()}"


def _required_text(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise RedshiftRuntimeError(
            f"Redshift credential has an empty/missing field: {key}"
        )
    stripped = value.strip()
    if _PLACEHOLDER.fullmatch(stripped) or stripped.casefold() in {
        "changeme",
        "replace-me",
        "replace_me",
    }:
        raise RedshiftRuntimeError(
            f"Redshift credential contains a placeholder field: {key}"
        )
    # Passwords and secret keys are opaque: whitespace may be intentional.
    return value if key in _SECRET_FIELDS else stripped


def _staging_prefix(value: str) -> str:
    """Validate one attempt-scoped S3 key prefix without normalizing it."""

    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 900
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("Redshift S3 staging path must be a safe relative prefix")
    return value


def _normalize_credentials(
    values: Mapping[str, object],
    *,
    require_staging: bool = True,
) -> dict[str, Any]:
    if not isinstance(values, Mapping):
        raise RedshiftRuntimeError("Redshift credential must contain an object")
    if any(not isinstance(key, str) for key in values):
        raise RedshiftRuntimeError(
            "Redshift credential contains an invalid field name"
        )
    if set(values) - _KNOWN_FIELDS:
        # Do not print attacker-controlled field names: they may themselves hold
        # secret material in a malformed credential document.
        raise RedshiftRuntimeError("Redshift credential contains unsupported fields")

    normalized: dict[str, Any] = {}
    required_fields = list(_CONNECTION_FIELDS)
    if require_staging:
        required_fields.extend(_STAGING_FIELDS)
    for key in required_fields:
        if key == "port":
            continue
        normalized[key] = _required_text(values, key)
    if not require_staging:
        for key in _STAGING_FIELDS:
            if key in values:
                normalized[key] = _required_text(values, key)

    port = values.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RedshiftRuntimeError("Redshift credential has an invalid port")
    normalized["port"] = port

    if not _HOST.fullmatch(normalized["host"]):
        raise RedshiftRuntimeError(
            "Redshift credential host must not contain a URL scheme, port, or path"
        )
    if "s3_bucket_name" in normalized and not _BUCKET.fullmatch(
        normalized["s3_bucket_name"]
    ):
        raise RedshiftRuntimeError("Redshift credential has an invalid S3 bucket name")
    try:
        quote_identifier(normalized["database"])
    except ValueError:
        raise RedshiftRuntimeError(
            "Redshift credential has an invalid database identifier"
        ) from None

    schema = values.get("schema")
    if schema is not None:
        if not isinstance(schema, str) or not schema.strip():
            raise RedshiftRuntimeError(
                "Redshift credential has an empty/missing field: schema"
            )
        schema = schema.strip()
        try:
            quote_identifier(schema)
        except ValueError:
            raise RedshiftRuntimeError(
                "Redshift credential has an invalid schema identifier"
            ) from None
        normalized["schema"] = schema
    staging_path = values.get("s3_bucket_path")
    if staging_path is not None:
        try:
            normalized["s3_bucket_path"] = _staging_prefix(staging_path)
        except ValueError as exc:
            raise RedshiftRuntimeError(str(exc)) from None
    return normalized


def _load_credential_object(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, _DuplicateCredentialField):
        # Parser details and exception chains can quote credential contents.
        raise RedshiftRuntimeError(
            f"invalid Redshift credential file: {path}"
        ) from None
    if not isinstance(value, dict):
        raise RedshiftRuntimeError(
            "Redshift credential file must contain an object"
        )
    return value


def load_credentials(path: Path) -> dict[str, Any]:
    """Load a complete Redshift destination credential including S3 staging."""

    return _normalize_credentials(_load_credential_object(path))


def load_connection_credentials(path: Path) -> dict[str, Any]:
    """Load only fields needed by a read-only verifier DB connection."""

    return _normalize_credentials(
        _load_credential_object(path), require_staging=False
    )


def connect(credentials: Mapping[str, object]):
    """Open a Redshift DB-API connection with a bounded cold-start retry.

    Serverless workgroups and newly resumed clusters can reject or time out an
    initial connection while becoming ready.  Each driver call retains the
    short TCP timeout and the complete operation makes only a fixed number of
    attempts with fixed sleeps; driver details are never surfaced.
    """

    normalized = _normalize_credentials(credentials, require_staging=False)
    try:
        import redshift_connector
    except ImportError:  # pragma: no cover - environment-specific
        raise RedshiftRuntimeError(
            "Redshift runtime requires the optional redshift-connector package"
        ) from None

    # Deliberately pass only database connection fields.  S3 staging keys and an
    # installed attempt schema are connector configuration, not DB-API options.
    for attempt in range(_CONNECT_ATTEMPTS):
        try:
            return redshift_connector.connect(
                host=normalized["host"],
                port=normalized["port"],
                database=normalized["database"],
                user=normalized["username"],
                password=normalized["password"],
                timeout=_CONNECT_TIMEOUT_SECONDS,
            )
        except Exception:  # pragma: no cover - transport/account-specific
            if attempt + 1 < _CONNECT_ATTEMPTS:
                time.sleep(_CONNECT_RETRY_SECONDS)
    raise RedshiftRuntimeError("could not connect to Redshift") from None


def _attempt_principal_names(database: str, schema: str) -> tuple[str, str]:
    try:
        quote_identifier(database)
    except ValueError:
        raise ValueError("unsafe Redshift attempt database identifier") from None
    try:
        quote_identifier(schema)
    except ValueError:
        raise ValueError("unsafe Redshift attempt schema identifier") from None
    if database != database.lower() or schema != schema.lower():
        raise ValueError("Redshift attempt identifiers must be lowercase")
    # Redshift users are global across every database in a cluster. Include the
    # fresh database and logical schema in this digest so attempts cannot
    # silently select the same principal.
    digest = hashlib.sha256(
        f"{database.casefold()}\0{schema.casefold()}".encode("utf-8")
    ).hexdigest()[:20]
    user = f"elt_{digest}_user"
    # Check derived names as well: suffixes can cross Redshift's 127-byte limit.
    quote_identifier(schema)
    quote_identifier(user)
    return schema, user


def _current_database(row: Any) -> str:
    if isinstance(row, Mapping):
        values = list(row.values())
    elif isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        values = list(row)
    else:
        values = []
    if len(values) != 1 or not isinstance(values[0], str) or not values[0]:
        raise RedshiftRuntimeError("Redshift did not report the current database")
    return values[0]


def _current_user(row: Any) -> str:
    if isinstance(row, Mapping):
        values = list(row.values())
    elif isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        values = list(row)
    else:
        values = []
    if len(values) != 1 or not isinstance(values[0], str) or not values[0]:
        raise RedshiftRuntimeError("Redshift did not report the current user")
    return values[0]


def _initialize_attempt_schema(
    connection: Any,
    *,
    database: str,
    schema: str,
    username: str,
) -> None:
    """Create the destination schema through the scoped solver connection.

    destination-redshift 4.0.7 selects its configured schema while building
    the JDBC pool, before its own setup path can create that schema.  Creating
    it here also proves that the returned solver credential reaches the fresh
    database as the intended cluster-global user.
    """

    target_schema = quote_identifier(schema)
    owner = quote_identifier(username)
    cursor = None
    previous_autocommit: bool | None = None
    try:
        previous_autocommit = connection.autocommit
        if not isinstance(previous_autocommit, bool):
            raise RedshiftRuntimeError(
                "Redshift solver connection has no boolean autocommit mode"
            )
        connection.rollback()
        connection.autocommit = True
        cursor = connection.cursor()
        cursor.execute("SELECT current_database()")
        if _current_database(cursor.fetchone()).casefold() != database.casefold():
            raise RedshiftRuntimeError(
                "solver connection targets a different Redshift database"
            )
        cursor.execute("SELECT current_user")
        if _current_user(cursor.fetchone()).casefold() != username.casefold():
            raise RedshiftRuntimeError(
                "solver connection authenticated as a different Redshift user"
            )
        cursor.execute(f"CREATE SCHEMA {target_schema} AUTHORIZATION {owner}")
    except Exception:
        raise RedshiftRuntimeError(
            "could not initialize isolated Redshift schema"
        ) from None
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if previous_autocommit is not None:
            try:
                connection.autocommit = previous_autocommit
            except Exception:
                pass


def provision_attempt(
    connection: Any,
    schema: str,
    *,
    credentials: Mapping[str, object],
    database: str,
    s3_bucket_path: str | None = None,
    password: str | None = None,
    attempt_dedicated_deployment: bool = False,
    attempt_connector: Callable[[Mapping[str, object]], Any] | None = None,
) -> dict[str, Any]:
    """Create an isolated Redshift user, database, and schema for one attempt.

    Verify the administrator session and reject existing users or databases.
    Return scoped solver credentials plus supplied S3 staging fields. Derive a
    staging path when absent and never copy administrator credentials.
    """

    if attempt_dedicated_deployment is not True:
        raise ValueError(
            "Redshift provisioning requires an explicit attempt-dedicated "
            "cluster/workgroup assertion"
        )

    normalized = _normalize_credentials(credentials)
    try:
        target_database = quote_identifier(database)
    except ValueError:
        raise ValueError("unsafe Redshift attempt database identifier") from None
    if database.casefold() == normalized["database"].casefold():
        raise ValueError(
            "Redshift attempt database must differ from the management database"
        )
    schema_name, user_name = _attempt_principal_names(database, schema)
    user = quote_identifier(user_name)
    configured_prefix = normalized.get("s3_bucket_path")
    if (
        configured_prefix is not None
        and s3_bucket_path is not None
        and configured_prefix != s3_bucket_path
    ):
        raise ValueError("conflicting Redshift S3 staging paths supplied")
    staging_prefix = _staging_prefix(
        s3_bucket_path
        or configured_prefix
        # Derive the pinned connector's staging prefix from `schema` so
        # provisioning and installation use the same contract.
        or f"elt-bench/{schema_name}"
    )

    if password is None:
        attempt_password = "Aa1!" + secrets.token_urlsafe(24)
    else:
        if not isinstance(password, str) or not 8 <= len(password) <= 64:
            raise ValueError("Redshift attempt password must contain 8-64 characters")
        if (
            not any(
                character.isascii() and character.isupper()
                for character in password
            )
            or not any(
                character.isascii() and character.islower()
                for character in password
            )
            or not any(
                character.isascii() and character.isdigit()
                for character in password
            )
            or any(not 33 <= ord(character) <= 126 for character in password)
            or any(character in "'\"\\/@" for character in password)
        ):
            raise ValueError("Redshift attempt password does not meet safety requirements")
        attempt_password = password
    # Keep the cleartext solver password out of Redshift query history. Redshift
    # accepts this documented digest+256-bit-salt representation while the
    # solver continues to authenticate with ``attempt_password``.
    password_sql = _quote_literal(_password_hash(attempt_password))

    scoped = {
        "host": normalized["host"],
        "port": normalized["port"],
        "database": database,
        "username": user_name,
        "password": attempt_password,
        "schema": schema_name,
        "s3_bucket_name": normalized["s3_bucket_name"],
        "s3_bucket_region": normalized["s3_bucket_region"],
        "access_key_id": normalized["access_key_id"],
        "secret_access_key": normalized["secret_access_key"],
        "s3_bucket_path": staging_prefix,
    }

    cursor = None
    attempt_connection = None
    previous_autocommit: bool | None = None
    user_created = False
    database_created = False
    try:
        previous_autocommit = connection.autocommit
        if not isinstance(previous_autocommit, bool):
            raise RedshiftRuntimeError(
                "Redshift administrator connection has no boolean autocommit mode"
            )
        # Redshift restricts GRANT/REVOKE in transaction blocks. The official
        # Python connector defaults autocommit off and requires a rollback before
        # changing this property.
        connection.rollback()
        connection.autocommit = True
        cursor = connection.cursor()
        cursor.execute("SELECT current_database()")
        active_database = _current_database(cursor.fetchone())
        if active_database.casefold() != normalized["database"].casefold():
            raise RedshiftRuntimeError(
                "administrator connection targets a different Redshift database"
            )

        # Both names must be fresh. Reusing a cluster-global user can preserve
        # grants in another database, while dropping it can orphan objects in
        # that database. Fail closed instead.
        cursor.execute(
            "SELECT 1 FROM pg_user WHERE usename = " + _quote_literal(user_name)
        )
        if cursor.fetchone() is not None:
            raise RedshiftRuntimeError("Redshift attempt user already exists")
        cursor.execute(
            "SELECT 1 FROM pg_database WHERE datname = "
            + _quote_literal(database)
        )
        if cursor.fetchone() is not None:
            raise RedshiftRuntimeError("Redshift attempt database already exists")

        cursor.execute(f"CREATE USER {user} PASSWORD {password_sql}")
        user_created = True
        cursor.execute(f"CREATE DATABASE {target_database} OWNER {user}")
        database_created = True
        cursor.execute(
            f"REVOKE TEMPORARY ON DATABASE {target_database} FROM PUBLIC"
        )
        cursor.execute(
            f"GRANT CREATE, TEMPORARY ON DATABASE {target_database} TO {user}"
        )
        attempt_connection = (attempt_connector or connect)(scoped)
        _initialize_attempt_schema(
            attempt_connection,
            database=database,
            schema=schema_name,
            username=user_name,
        )
    except Exception:
        # Autocommit is required by Redshift for the privilege statements, so a
        # failure cannot be rolled back atomically. Remove only objects this call
        # proved it created; collisions were rejected before mutation.
        if cursor is not None and user_created:
            try:
                if database_created:
                    cursor.execute(f"DROP DATABASE IF EXISTS {target_database}")
                cursor.execute(f"DROP USER IF EXISTS {user}")
            except Exception:
                pass
        raise RedshiftRuntimeError(
            f"could not provision isolated Redshift database {database!r}"
        ) from None
    finally:
        if attempt_connection is not None:
            try:
                attempt_connection.close()
            except Exception:
                pass
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if previous_autocommit is not None:
            try:
                connection.autocommit = previous_autocommit
            except Exception:
                # Do not replace the provisioning result. The harness owns and
                # closes this administrator connection immediately afterwards.
                pass

    return scoped


__all__ = [
    "RedshiftRuntimeError",
    "connect",
    "load_connection_credentials",
    "load_credentials",
    "provision_attempt",
    "quote_identifier",
]

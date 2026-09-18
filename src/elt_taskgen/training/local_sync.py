"""Execute candidate-selected streams through trusted local readers.

Candidate Terraform supplies only normalized intent, never paths or reader
code. Retain DuckDB state for dbt while fingerprints remain private.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

import duckdb
from pydantic import BaseModel, ConfigDict, Field

from elt_taskgen.destinations import Destination
from elt_taskgen.models import Backend, PopulationName, TableSpec, canonical_json
from elt_taskgen.training.contract import WorkspaceFailureClass
from elt_taskgen.training.namespace import (
    NamespaceProjection,
    NamespaceProjectionError,
    project_namespace,
    quote_duckdb_identifier,
)
from elt_taskgen.training.package import WorkspacePackage
from elt_taskgen.training.warehouse_profiles import warehouse_profile
from elt_taskgen.verification import strict_diagnostic, upstream_eval
from elt_taskgen.verification.canonical_fingerprint import (
    CanonicalFingerprintError,
    CanonicalRelationFingerprint,
    NaiveTimestampPolicy,
    canonical_relation_fingerprint,
)


MAX_LOCAL_RAW_ROWS = 2_000_000
MAX_LOCAL_RAW_BYTES = 512 * 1024 * 1024


class SyncLifecycleState(str, Enum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"


class LocalSyncErrorCode(str, Enum):
    """Stable local-sync codes; no value embeds private population details."""

    INTENT_INVALID = "local_sync_intent_invalid"
    DESTINATION_MISMATCH = "local_sync_destination_mismatch"
    NAMESPACE_MISMATCH = "local_sync_namespace_mismatch"
    DUPLICATE_STREAM = "local_sync_duplicate_stream"
    MISSING_STREAM = "local_sync_missing_stream"
    EXTRA_STREAM = "local_sync_extra_stream"
    BACKEND_MISMATCH = "local_sync_backend_mismatch"
    SYNC_MODE_UNSUPPORTED = "local_sync_mode_unsupported"
    RAW_TABLE_MISSING = "local_sync_raw_table_missing"
    RAW_TABLE_UNEXPECTED = "local_sync_raw_table_unexpected"
    RAW_SCHEMA_MISMATCH = "local_sync_raw_schema_mismatch"
    RAW_COUNT_MISMATCH = "local_sync_raw_count_mismatch"
    RAW_CONTENT_MISMATCH = "local_sync_raw_content_mismatch"
    RAW_SIZE_LIMIT = "local_sync_raw_size_limit"

    DATABASE_NOT_FRESH = "local_sync_database_not_fresh"
    NAMESPACE_INVALID = "local_sync_namespace_invalid"
    PRIVATE_SOURCE_INVALID = "local_sync_private_source_invalid"
    PRIVATE_GOLD_INVALID = "local_sync_private_gold_invalid"
    DUCKDB_FAILED = "local_sync_duckdb_failed"


_HARNESS_FAILURE_CLASS: Mapping[LocalSyncErrorCode, WorkspaceFailureClass] = {
    LocalSyncErrorCode.DATABASE_NOT_FRESH: WorkspaceFailureClass.HARNESS_DEFECT,
    LocalSyncErrorCode.NAMESPACE_INVALID: WorkspaceFailureClass.TASK_DEFECT,
    LocalSyncErrorCode.PRIVATE_SOURCE_INVALID: WorkspaceFailureClass.TASK_DEFECT,
    LocalSyncErrorCode.PRIVATE_GOLD_INVALID: WorkspaceFailureClass.TASK_DEFECT,
    LocalSyncErrorCode.DUCKDB_FAILED: WorkspaceFailureClass.HARNESS_DEFECT,
}


class LocalSyncHarnessError(RuntimeError):
    """A label-ineligible local-sync failure with sanitized public identity."""

    def __init__(self, code: LocalSyncErrorCode) -> None:
        if code not in _HARNESS_FAILURE_CLASS:
            raise ValueError("candidate error code cannot be raised as a harness error")
        self.code = code
        self.classification = _HARNESS_FAILURE_CLASS[code]
        super().__init__(code.value)


class LocalStreamSelection(BaseModel):
    """The narrow normalized-intent surface consumed by trusted sync."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_key: str = Field(min_length=1)
    connector_kind: str = Field(min_length=1)
    stream_name: str = Field(min_length=1)
    sync_mode: str = Field(min_length=1)


@runtime_checkable
class SelectedStreamIntentLike(Protocol):
    source_key: str
    connector_kind: str
    stream_name: str
    sync_mode: str


@runtime_checkable
class TerraformIntentDestinationLike(Protocol):
    kind: object
    logical_namespace: str


@runtime_checkable
class NormalizedIntentGraphLike(Protocol):
    selected_streams: tuple[SelectedStreamIntentLike, ...]
    destination: TerraformIntentDestinationLike


class RawColumnFingerprint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    logical_type: str = Field(min_length=1)
    physical_type: str = Field(min_length=1)
    nullable: bool


class RawTableFingerprint(BaseModel):
    """Private typed identity for one business-column projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: str = Field(min_length=1)
    columns: tuple[RawColumnFingerprint, ...]
    relation: CanonicalRelationFingerprint
    physical_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RawTableVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    table: str = Field(min_length=1)
    present: bool
    count_match: bool
    schema_match: bool
    content_match: bool
    error_code: LocalSyncErrorCode | None = None
    fingerprint: RawTableFingerprint | None = None

    @property
    def matched(self) -> bool:
        return (
            self.present
            and self.count_match
            and self.schema_match
            and self.content_match
        )


class RawStateVerification(BaseModel):
    """Evaluator-private strict state; callers publish only aggregate heads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    upstream_stage1: bool
    strict_raw_tables: float = Field(ge=0.0, le=1.0)
    strict_pass: bool
    tables: tuple[RawTableVerification, ...]
    unexpected_tables: tuple[str, ...] = ()
    error_codes: tuple[LocalSyncErrorCode, ...] = ()


@dataclass(frozen=True)
class LocalSyncExecution:
    """Private same-state handoff from local EL to dbt."""

    task_id: str
    task_content_hash: str
    population: str
    database_path: Path
    namespace: NamespaceProjection
    lifecycle: SyncLifecycleState
    upstream_stage1: bool
    strict_raw_tables: float
    raw_state: RawStateVerification
    error_codes: tuple[LocalSyncErrorCode, ...]
    expected_fingerprints: Mapping[str, RawTableFingerprint]
    selected_streams: tuple[LocalStreamSelection, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "expected_fingerprints",
            MappingProxyType(dict(self.expected_fingerprints)),
        )

    @property
    def sync_lifecycle(self) -> bool:
        return self.lifecycle is SyncLifecycleState.SUCCEEDED


class _RawLimitError(ValueError):
    pass


_BACKEND_ROUTE: Mapping[Backend, tuple[str, str]] = {
    Backend.POSTGRES: ("postgres", "postgres"),
    Backend.MONGODB: ("mongodb", "mongodb"),
    Backend.REST: ("custom_api", "custom_api"),
    Backend.S3: ("aws_s3", "aws_s3"),
    # The source key for files is per table and is handled below.
    Backend.FILES: ("", "file"),
}


def _expected_route(package: WorkspacePackage, table: str) -> tuple[str, str]:
    backend = package.task.backend_for(table).backend
    source_key, connector_kind = _BACKEND_ROUTE[backend]
    if backend is Backend.FILES:
        source_key = f"file_{table}"
    return source_key, connector_kind


def _value(obj: object, name: str) -> object:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _enum_text(value: object) -> str:
    enum_value = getattr(value, "value", None)
    return str(enum_value if enum_value is not None else value)


def _normalize_intent(
    package: WorkspacePackage,
    namespace: NamespaceProjection,
    intent: object,
) -> tuple[
    tuple[LocalStreamSelection, ...],
    tuple[LocalSyncErrorCode, ...],
    bool,
]:
    """Return selections, stable codes, and whether loading must be refused."""

    codes: list[LocalSyncErrorCode] = []
    destination = _value(intent, "destination")
    raw_streams = _value(intent, "selected_streams")
    if destination is None or not isinstance(raw_streams, (list, tuple)):
        return (), (LocalSyncErrorCode.INTENT_INVALID,), True

    kind = _enum_text(_value(destination, "kind"))
    if kind != package.destination.value:
        codes.append(LocalSyncErrorCode.DESTINATION_MISMATCH)
    logical_namespace = _value(destination, "logical_namespace")
    if logical_namespace != package.logical_namespace:
        codes.append(LocalSyncErrorCode.NAMESPACE_MISMATCH)
    declared_schema = _value(destination, "schema")
    if declared_schema is not None and declared_schema != namespace.logical_schema:
        codes.append(LocalSyncErrorCode.NAMESPACE_MISMATCH)

    selections: list[LocalStreamSelection] = []
    try:
        for item in raw_streams:
            selections.append(
                LocalStreamSelection(
                    source_key=_value(item, "source_key"),
                    connector_kind=_value(item, "connector_kind"),
                    stream_name=_value(item, "stream_name"),
                    sync_mode=_value(item, "sync_mode"),
                )
            )
    except (TypeError, ValueError):
        return (), (LocalSyncErrorCode.INTENT_INVALID,), True

    identities = [(item.source_key, item.stream_name) for item in selections]
    stream_names = [item.stream_name for item in selections]
    if len(identities) != len(set(identities)) or len(stream_names) != len(
        set(stream_names)
    ):
        codes.append(LocalSyncErrorCode.DUPLICATE_STREAM)

    expected_tables = {table.name for table in package.task.tables}
    for item in selections:
        if item.sync_mode != "full_refresh_append":
            codes.append(LocalSyncErrorCode.SYNC_MODE_UNSUPPORTED)
        if item.stream_name not in expected_tables:
            codes.append(LocalSyncErrorCode.EXTRA_STREAM)
            continue
        if (item.source_key, item.connector_kind) != _expected_route(
            package, item.stream_name
        ):
            codes.append(LocalSyncErrorCode.BACKEND_MISMATCH)

    if set(stream_names) != expected_tables:
        missing = expected_tables - set(stream_names)
        if missing:
            codes.append(LocalSyncErrorCode.MISSING_STREAM)

    normalized_codes = tuple(dict.fromkeys(codes))
    hard_rejection = any(
        code
        in {
            LocalSyncErrorCode.INTENT_INVALID,
            LocalSyncErrorCode.DESTINATION_MISMATCH,
            LocalSyncErrorCode.NAMESPACE_MISMATCH,
            LocalSyncErrorCode.DUPLICATE_STREAM,
            LocalSyncErrorCode.EXTRA_STREAM,
            LocalSyncErrorCode.BACKEND_MISMATCH,
            LocalSyncErrorCode.SYNC_MODE_UNSUPPORTED,
        }
        for code in normalized_codes
    )
    return (
        tuple(sorted(selections, key=lambda item: (item.stream_name, item.source_key))),
        normalized_codes,
        hard_rejection,
    )


def _pin_connection(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("SET default_collation = 'binary'")
    con.execute("SET default_null_order = 'NULLS_LAST'")
    con.execute("SET threads = 1")
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")


def _initialize_connection(
    con: duckdb.DuckDBPyConnection, namespace: NamespaceProjection
) -> None:
    # DDL and search-path setup precede lock_configuration; all candidate SQL
    # execution is owned by the later sandboxed dbt runner.
    con.execute(
        f"CREATE SCHEMA {quote_duckdb_identifier(namespace.raw_schema)}"
    )
    if namespace.mart_schema.casefold() != namespace.raw_schema.casefold():
        con.execute(
            f"CREATE SCHEMA {quote_duckdb_identifier(namespace.mart_schema)}"
        )
    escaped = namespace.raw_schema.replace("'", "''")
    con.execute(f"SET schema = '{escaped}'")
    _pin_connection(con)


def _claim_database(database_path: Path) -> Path:
    path = Path(database_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise LocalSyncHarnessError(LocalSyncErrorCode.DUCKDB_FAILED) from None
    if path.parent.is_symlink():
        raise LocalSyncHarnessError(LocalSyncErrorCode.DUCKDB_FAILED)
    claim = path.with_name(path.name + ".local-sync-claim")
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        raise LocalSyncHarnessError(LocalSyncErrorCode.DATABASE_NOT_FRESH) from None
    except OSError:
        raise LocalSyncHarnessError(LocalSyncErrorCode.DUCKDB_FAILED) from None
    wal = Path(str(path) + ".wal")
    if path.exists() or path.is_symlink() or wal.exists() or wal.is_symlink():
        try:
            claim.unlink()
        except OSError:
            pass
        raise LocalSyncHarnessError(LocalSyncErrorCode.DATABASE_NOT_FRESH)
    return claim


def _remove_database(database_path: Path) -> None:
    for path in (
        Path(database_path),
        Path(str(database_path) + ".wal"),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _selected_task(package: WorkspacePackage, names: set[str]):
    """An unvalidated private view used only by the existing table loader."""

    tables = tuple(table for table in package.task.tables if table.name in names)
    backends = tuple(
        assignment
        for assignment in package.task.backends
        if assignment.table in names
    )
    return package.task.model_copy(update={"tables": tables, "backends": backends})


def _fetch_bounded_rows(
    con: duckdb.DuckDBPyConnection,
    relation: str,
) -> tuple[tuple[str, ...], list[tuple]]:
    cursor = con.execute(f"SELECT * FROM {relation}")
    columns = tuple(str(item[0]) for item in cursor.description)
    rows: list[tuple] = []
    size = 0
    while True:
        batch = cursor.fetchmany(2048)
        if not batch:
            break
        rows.extend(tuple(row) for row in batch)
        if len(rows) > MAX_LOCAL_RAW_ROWS:
            raise _RawLimitError("raw relation row limit")
        size += sum(
            len(str(value).encode("utf-8"))
            for row in batch
            for value in row
        )
        if size > MAX_LOCAL_RAW_BYTES:
            raise _RawLimitError("raw relation byte limit")
    return columns, rows


def _fingerprint_table(
    con: duckdb.DuckDBPyConnection,
    table: TableSpec,
    relation: str,
    *,
    allowed_metadata_columns: tuple[str, ...] = (),
) -> RawTableFingerprint:
    description = con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()
    physical_by_folded: dict[str, tuple[str, str, bool]] = {}
    for row in description:
        name, physical_type = str(row[0]), str(row[1])
        nullable = str(row[2]).upper() != "NO"
        folded = name.casefold()
        if folded in physical_by_folded:
            raise CanonicalFingerprintError("raw schema has a case collision")
        physical_by_folded[folded] = (name, physical_type, nullable)

    actual_columns, rows = _fetch_bounded_rows(con, relation)
    expected_columns = tuple((column.name, column.type) for column in table.columns)
    expected_folded = {column.name.casefold() for column in table.columns}
    admitted_metadata = {name.casefold() for name in allowed_metadata_columns}
    allowed_metadata = tuple(
        sorted(
            name.casefold()
            for name in actual_columns
            if name.casefold() not in expected_folded
            and name.casefold() in admitted_metadata
        )
    )
    canonical = canonical_relation_fingerprint(
        actual_columns=actual_columns,
        expected_columns=expected_columns,
        rows=rows,
        allowed_extra_columns=allowed_metadata,
        naive_timestamp_policy=NaiveTimestampPolicy.ASSUME_UTC,
    )

    columns: list[RawColumnFingerprint] = []
    for column in table.columns:
        try:
            _actual_name, physical_type, nullable = physical_by_folded[
                column.name.casefold()
            ]
        except KeyError as exc:
            raise CanonicalFingerprintError("raw schema is missing a column") from exc
        columns.append(
            RawColumnFingerprint(
                name=column.name,
                logical_type=column.type.value,
                physical_type=physical_type,
                nullable=nullable,
            )
        )
    schema_payload = [item.model_dump(mode="json") for item in columns]
    physical_schema_sha256 = hashlib.sha256(
        canonical_json(schema_payload).encode("utf-8")
    ).hexdigest()
    identity_payload = {
        "table": table.name,
        "columns": schema_payload,
        # Airbyte metadata is diagnostic and excluded from business-row identity.
        "business_relation": {
            "version": canonical.version,
            "row_order": canonical.row_order.value,
            "naive_timestamp_policy": canonical.naive_timestamp_policy.value,
            "row_count": canonical.row_count,
            "result_digest": canonical.result_digest,
        },
    }
    return RawTableFingerprint(
        table=table.name,
        columns=tuple(columns),
        relation=canonical,
        physical_schema_sha256=physical_schema_sha256,
        fingerprint_sha256=hashlib.sha256(
            canonical_json(identity_payload).encode("utf-8")
        ).hexdigest(),
    )


def _expected_raw_state(
    package: WorkspacePackage,
    population: str,
) -> Mapping[str, RawTableFingerprint]:
    try:
        source_root = package.source_root(population)
        con = duckdb.connect(":memory:")
        try:
            _pin_connection(con)
            counts = strict_diagnostic.load_sources_duckdb_strict(
                package.task, source_root, con
            )
            expected_counts = package.gold.stage1.get(population)
            if not isinstance(expected_counts, Mapping) or set(expected_counts) != {
                table.name for table in package.task.tables
            }:
                raise LocalSyncHarnessError(LocalSyncErrorCode.PRIVATE_GOLD_INVALID)
            if counts != dict(expected_counts):
                raise LocalSyncHarnessError(LocalSyncErrorCode.PRIVATE_SOURCE_INVALID)
            fingerprints = {
                table.name: _fingerprint_table(
                    con,
                    table,
                    quote_duckdb_identifier(table.name),
                    allowed_metadata_columns=warehouse_profile(
                        package.destination
                    ).metadata_columns,
                )
                for table in package.task.tables
            }
            return MappingProxyType(fingerprints)
        finally:
            con.close()
    except LocalSyncHarnessError:
        raise
    except (FileNotFoundError, OSError, ValueError, CanonicalFingerprintError):
        raise LocalSyncHarnessError(LocalSyncErrorCode.PRIVATE_SOURCE_INVALID) from None
    except Exception:
        raise LocalSyncHarnessError(LocalSyncErrorCode.DUCKDB_FAILED) from None


def _listed_raw_tables(
    con: duckdb.DuckDBPyConnection, namespace: NamespaceProjection
) -> tuple[str, ...]:
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE lower(table_schema) = lower(?) AND table_type = 'BASE TABLE' "
        "ORDER BY lower(table_name), table_name",
        [namespace.raw_schema],
    ).fetchall()
    names = tuple(str(row[0]) for row in rows)
    if len({name.casefold() for name in names}) != len(names):
        raise CanonicalFingerprintError("raw tables have a case collision")
    return names


def shared_schema_mart_names(
    package: WorkspacePackage, namespace: NamespaceProjection
) -> frozenset[str]:
    """Mart names that are materialized INTO the raw schema, casefolded.

    Databricks and Redshift land the raw tables into the task-named schema,
    which is also the dbt target schema, so after dbt the marts are tables in
    the raw schema. They are not raw tables and are checked as marts; a task
    whose mart shadows a raw table is refused by ``TaskIR`` itself, so nothing
    a raw table is named can be excluded here. Snowflake keeps the two schemas
    apart and gets the empty set, leaving its check exactly as strict.
    """

    if namespace.mart_schema.casefold() != namespace.raw_schema.casefold():
        return frozenset()
    return frozenset(mart.name.casefold() for mart in package.task.marts)


def _verify_connection(
    package: WorkspacePackage,
    population: str,
    namespace: NamespaceProjection,
    expected: Mapping[str, RawTableFingerprint],
    con: duckdb.DuckDBPyConnection,
    *,
    allow_marts: bool = False,
) -> RawStateVerification:
    expected_tables = {table.name: table for table in package.task.tables}
    try:
        listed = _listed_raw_tables(con, namespace)
    except (duckdb.Error, CanonicalFingerprintError):
        raise LocalSyncHarnessError(LocalSyncErrorCode.DUCKDB_FAILED) from None
    listed_folded = {name.casefold(): name for name in listed}
    expected_folded = {name.casefold(): name for name in expected_tables}
    # Only the post-dbt re-verification passes ``allow_marts``: during EL no
    # mart exists yet, so a table with a mart's name would be an intruder.
    ignored = (
        shared_schema_mart_names(package, namespace) if allow_marts else frozenset()
    )
    unexpected = tuple(
        name
        for name in listed
        if name.casefold() not in expected_folded and name.casefold() not in ignored
    )

    actual_counts: dict[str, int] = {}
    results: list[RawTableVerification] = []
    codes: list[LocalSyncErrorCode] = []
    for table in package.task.tables:
        actual_name = listed_folded.get(table.name.casefold())
        if actual_name is None:
            code = LocalSyncErrorCode.RAW_TABLE_MISSING
            codes.append(code)
            results.append(
                RawTableVerification(
                    table=table.name,
                    present=False,
                    count_match=False,
                    schema_match=False,
                    content_match=False,
                    error_code=code,
                )
            )
            continue

        relation = namespace.raw_relation(actual_name)
        # Capture Stage 1's count before strict fingerprinting so schema or
        # content errors do not change its count-only behavior.
        try:
            (raw_count,) = con.execute(
                f"SELECT COUNT(*) FROM {relation}"
            ).fetchone()
            actual_counts[table.name] = int(raw_count)
        except (duckdb.Error, TypeError, ValueError):
            raise LocalSyncHarnessError(LocalSyncErrorCode.DUCKDB_FAILED) from None
        expected_row_count = expected[table.name].relation.row_count
        count_match = actual_counts[table.name] == expected_row_count
        try:
            fingerprint = _fingerprint_table(
                con,
                table,
                relation,
                allowed_metadata_columns=namespace.behavior_profile.metadata_columns,
            )
        except _RawLimitError:
            code = LocalSyncErrorCode.RAW_SIZE_LIMIT
            codes.append(code)
            results.append(
                RawTableVerification(
                    table=table.name,
                    present=True,
                    count_match=count_match,
                    schema_match=False,
                    content_match=False,
                    error_code=code,
                )
            )
            continue
        except CanonicalFingerprintError:
            code = LocalSyncErrorCode.RAW_SCHEMA_MISMATCH
            codes.append(code)
            results.append(
                RawTableVerification(
                    table=table.name,
                    present=True,
                    count_match=count_match,
                    schema_match=False,
                    content_match=False,
                    error_code=code,
                )
            )
            continue
        except duckdb.Error:
            raise LocalSyncHarnessError(LocalSyncErrorCode.DUCKDB_FAILED) from None

        expected_fingerprint = expected[table.name]
        # The streamed fingerprint row count must agree with COUNT(*); any
        # disagreement means the evaluator observed a changing relation.
        count_match = count_match and (
            fingerprint.relation.row_count == actual_counts[table.name]
        )
        schema_match = (
            fingerprint.physical_schema_sha256
            == expected_fingerprint.physical_schema_sha256
        )
        content_match = (
            fingerprint.relation.result_digest
            == expected_fingerprint.relation.result_digest
        )
        code: LocalSyncErrorCode | None = None
        if not count_match:
            code = LocalSyncErrorCode.RAW_COUNT_MISMATCH
        elif not schema_match:
            code = LocalSyncErrorCode.RAW_SCHEMA_MISMATCH
        elif not content_match:
            code = LocalSyncErrorCode.RAW_CONTENT_MISMATCH
        if code is not None:
            codes.append(code)
        results.append(
            RawTableVerification(
                table=table.name,
                present=True,
                count_match=count_match,
                schema_match=schema_match,
                content_match=content_match,
                error_code=code,
                fingerprint=fingerprint,
            )
        )

    expected_counts = package.gold.stage1.get(population)
    if not isinstance(expected_counts, Mapping):
        raise LocalSyncHarnessError(LocalSyncErrorCode.PRIVATE_GOLD_INVALID)
    upstream_stage1, _detail = upstream_eval.compare_stage1(
        dict(expected_counts), actual_counts
    )
    if unexpected:
        codes.append(LocalSyncErrorCode.RAW_TABLE_UNEXPECTED)
    matched = sum(result.matched for result in results)
    denominator = len(results) + len(unexpected)
    strict_reward = matched / denominator if denominator else 0.0
    strict_pass = strict_reward == 1.0
    return RawStateVerification(
        upstream_stage1=upstream_stage1,
        strict_raw_tables=strict_reward,
        strict_pass=strict_pass,
        tables=tuple(results),
        unexpected_tables=unexpected,
        error_codes=tuple(dict.fromkeys(codes)),
    )


def verify_raw_state(
    package: WorkspacePackage,
    execution: LocalSyncExecution,
) -> RawStateVerification:
    """Re-verify raw tables in the retained same-state database after dbt."""

    if execution.task_id != package.task_id or execution.task_content_hash != (
        package.task.content_hash()
    ):
        raise LocalSyncHarnessError(LocalSyncErrorCode.DUCKDB_FAILED)
    if not execution.database_path.is_file() or execution.database_path.is_symlink():
        raise LocalSyncHarnessError(LocalSyncErrorCode.DUCKDB_FAILED)
    try:
        con = duckdb.connect(str(execution.database_path), read_only=True)
        try:
            _pin_connection(con)
            return _verify_connection(
                package,
                execution.population,
                execution.namespace,
                execution.expected_fingerprints,
                con,
                allow_marts=True,
            )
        finally:
            con.close()
    except LocalSyncHarnessError:
        raise
    except Exception:
        raise LocalSyncHarnessError(LocalSyncErrorCode.DUCKDB_FAILED) from None


def raw_state_immutable(
    before: RawStateVerification,
    after: RawStateVerification,
) -> bool:
    """Exact typed raw identity check used by the post-dbt hard gate."""

    if before.unexpected_tables != after.unexpected_tables:
        return False
    before_by_table = {item.table: item for item in before.tables}
    after_by_table = {item.table: item for item in after.tables}
    if set(before_by_table) != set(after_by_table):
        return False
    for table, left in before_by_table.items():
        right = after_by_table[table]
        if left.present != right.present:
            return False
        left_digest = left.fingerprint.fingerprint_sha256 if left.fingerprint else None
        right_digest = right.fingerprint.fingerprint_sha256 if right.fingerprint else None
        if left_digest != right_digest:
            return False
    return True


def raw_state_fingerprints(
    state: RawStateVerification,
) -> Mapping[str, RawTableFingerprint]:
    """Return the immutable business fingerprints captured for present tables."""

    return MappingProxyType(
        {
            item.table: item.fingerprint
            for item in state.tables
            if item.fingerprint is not None
        }
    )


def run_local_sync(
    package: WorkspacePackage,
    population: PopulationName | str,
    intent: NormalizedIntentGraphLike | object,
    database_path: Path,
) -> LocalSyncExecution:
    """Load normalized streams into fresh retained DuckDB state.

    Invalid intent is measured; trusted package or evaluator faults raise a
    sanitized, label-ineligible ``LocalSyncHarnessError``.
    """

    population_name = (
        population.value if isinstance(population, PopulationName) else str(population)
    )
    try:
        namespace = project_namespace(package, Path(database_path))
    except (NamespaceProjectionError, ValueError):
        raise LocalSyncHarnessError(LocalSyncErrorCode.NAMESPACE_INVALID) from None

    claim = _claim_database(Path(database_path))
    con: duckdb.DuckDBPyConnection | None = None
    published = False
    try:
        try:
            con = duckdb.connect(str(database_path))
            _initialize_connection(con, namespace)
        except Exception:
            raise LocalSyncHarnessError(LocalSyncErrorCode.DUCKDB_FAILED) from None

        expected = _expected_raw_state(package, population_name)
        selections, intent_codes, hard_rejection = _normalize_intent(
            package, namespace, intent
        )
        if not hard_rejection:
            selected_names = {item.stream_name for item in selections}
            selected_task = _selected_task(package, selected_names)
            try:
                strict_diagnostic.load_sources_duckdb_strict(
                    selected_task,
                    package.source_root(population_name),
                    con,
                )
            except Exception:
                raise LocalSyncHarnessError(
                    LocalSyncErrorCode.PRIVATE_SOURCE_INVALID
                ) from None

        raw_state = _verify_connection(
            package,
            population_name,
            namespace,
            expected,
            con,
        )
        lifecycle = (
            SyncLifecycleState.REJECTED
            if hard_rejection
            else SyncLifecycleState.SUCCEEDED
        )
        combined_codes = tuple(
            dict.fromkeys((*intent_codes, *raw_state.error_codes))
        )
        con.close()
        con = None
        published = True
        return LocalSyncExecution(
            task_id=package.task_id,
            task_content_hash=package.task.content_hash(),
            population=population_name,
            database_path=Path(database_path).resolve(),
            namespace=namespace,
            lifecycle=lifecycle,
            upstream_stage1=raw_state.upstream_stage1,
            strict_raw_tables=raw_state.strict_raw_tables,
            raw_state=raw_state,
            error_codes=combined_codes,
            expected_fingerprints=expected,
            selected_streams=selections,
        )
    except LocalSyncHarnessError:
        raise
    except Exception:
        raise LocalSyncHarnessError(LocalSyncErrorCode.DUCKDB_FAILED) from None
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        try:
            claim.unlink(missing_ok=True)
        except OSError:
            pass
        if not published:
            _remove_database(Path(database_path))


__all__ = [
    "LocalStreamSelection",
    "LocalSyncErrorCode",
    "LocalSyncExecution",
    "LocalSyncHarnessError",
    "MAX_LOCAL_RAW_BYTES",
    "MAX_LOCAL_RAW_ROWS",
    "RawColumnFingerprint",
    "RawStateVerification",
    "RawTableFingerprint",
    "RawTableVerification",
    "NormalizedIntentGraphLike",
    "SelectedStreamIntentLike",
    "SyncLifecycleState",
    "TerraformIntentDestinationLike",
    "raw_state_immutable",
    "shared_schema_mart_names",
    "raw_state_fingerprints",
    "run_local_sync",
    "verify_raw_state",
]

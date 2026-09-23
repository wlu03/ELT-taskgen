"""Evaluate a combined task through an injected warehouse connection.

The harness owns credentials and creates the DB-API connection. The evaluator
uses only ``connection.cursor()``, keeps private answer keys on disk, and
delegates semantic comparison to ``upstream_eval``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, cast

import duckdb
import sqlglot
import yaml
from sqlglot import exp
from sqlglot.dialects.databricks import Databricks
from sqlglot.dialects.redshift import Redshift
from sqlglot.dialects.snowflake import Snowflake
from sqlglot.errors import SqlglotError

from elt_taskgen.destinations import (
    Destination,
    destination_from_config,
    normalize_destination,
)
from elt_taskgen.models import (
    Backend,
    ColumnType,
    MartColumn,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    PopulationName,
    Row,
    task_from_json,
)
from elt_taskgen.sql_identifiers import quote_sql_identifier
from elt_taskgen.verification.canonical_fingerprint import (
    CanonicalFingerprintError,
    CanonicalRelationFingerprint,
    NaiveTimestampPolicy,
    canonical_relation_fingerprint,
)
from elt_taskgen.verification import strict_diagnostic, upstream_eval


AIRBYTE_SCHEMA = "AIRBYTE_SCHEMA"

# Optional metadata columns from pinned Airbyte destination connectors.
AIRBYTE_METADATA_COLUMNS = tuple(
    sorted(
        (
            "_airbyte_extracted_at",
            "_airbyte_generation_id",
            "_airbyte_meta",
            "_airbyte_raw_id",
        )
    )
)

# Source metadata columns allowed for each pinned connector backend.
AIRBYTE_SOURCE_METADATA_COLUMNS: dict[Backend, tuple[str, ...]] = {
    Backend.MONGODB: (
        "_ab_cdc_cursor",
        "_ab_cdc_deleted_at",
        "_ab_cdc_updated_at",
        "_id",
    ),
    Backend.FILES: (
        "_ab_source_file_last_modified",
        "_ab_source_file_url",
    ),
    Backend.S3: (
        "_ab_source_file_last_modified",
        "_ab_source_file_url",
    ),
}

# Validate all ELT-Bench identifiers before quoting warehouse references.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_LOGICAL_NAMESPACE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_$]*$")


class _EvaluationSnowflake(Snowflake):
    """Stored answer keys may use the exporter's digit-leading namespaces."""

    IDENTIFIERS_CAN_START_WITH_DIGIT = True


class _EvaluationRedshift(Redshift):
    IDENTIFIERS_CAN_START_WITH_DIGIT = True


_EVALUATION_DIALECTS: dict[Destination, type[sqlglot.Dialect]] = {
    Destination.SNOWFLAKE: _EvaluationSnowflake,
    Destination.DATABRICKS: Databricks,  # already permits digit-leading identifiers
    Destination.REDSHIFT: _EvaluationRedshift,
}

# Parse stored evaluation SQL with its canonical Snowflake dialect before
# rendering it for a target warehouse, preserving quoted identifiers.
_STORED_EVALUATION_DIALECT: type[sqlglot.Dialect] = _EvaluationSnowflake

# Mirrors semantic/scoring.py:_MUTATING_SQL_NODES (getattr keeps the tuple
# valid across the pinned sqlglot range); Into/Lock/TruncateTable additionally
# refuse SELECT INTO, FOR UPDATE and TRUNCATE, which the legacy regex missed.
_MUTATING_SQL_NODES = tuple(
    node
    for node in (
        getattr(exp, name, None)
        for name in (
            "Alter",
            "Attach",
            "Command",
            "Copy",
            "Create",
            "Delete",
            "Detach",
            "Drop",
            "Grant",
            "Insert",
            "Into",
            "Kill",
            "Lock",
            "Merge",
            "Pragma",
            "Revoke",
            "Set",
            "Transaction",
            "TruncateTable",
            "Update",
            "Use",
        )
    )
    if isinstance(node, type)
)


class EvaluationError(RuntimeError):
    """The runtime result cannot be scored safely from the supplied evidence."""


class DBAPICursor(Protocol):
    description: Any

    def execute(self, operation: str, *args: Any, **kwargs: Any) -> Any: ...

    def fetchall(self) -> Sequence[Any]: ...

    def close(self) -> Any: ...


class DBAPIConnection(Protocol):
    def cursor(self) -> DBAPICursor: ...


@dataclass(frozen=True)
class CountMismatch:
    expected: int
    actual: int


@dataclass(frozen=True)
class Stage1EvaluationResult:
    """Count-only reward plus optional exact typed certification evidence.

    ``passed`` and ``reward`` retain upstream count semantics. Explicit
    certification populates the ``canonical_*`` fields used by
    ``certification_passed``.
    """

    database: str
    population: str
    expected_counts: dict[str, int]
    actual_counts: dict[str, int]
    missing_tables: tuple[str, ...]
    unexpected_tables: tuple[str, ...]
    count_mismatches: dict[str, CountMismatch]
    detail: dict[str, str]
    errors: dict[str, str]
    passed: bool
    reward: float
    expected_repetitions: int = 1
    canonical_table_scores: dict[str, bool] = field(default_factory=dict)
    canonical_mismatch_codes: dict[str, str] = field(default_factory=dict)
    canonical_reference_fingerprints: dict[str, str] = field(default_factory=dict)
    canonical_actual_fingerprints: dict[str, str] = field(default_factory=dict)
    # Databricks and Redshift need a separate physical container; Snowflake uses
    # `database`. The empty default is for compatibility; evaluators populate it.
    physical_container: str = ""

    @property
    def stage1_pass(self) -> bool:
        """Compatibility spelling used by the shared reward result."""

        return self.passed

    @property
    def certification_passed(self) -> bool:
        """Fail-closed exact-content verdict, separate from the reward."""

        expected = set(self.expected_counts)
        return (
            self.passed
            and not self.unexpected_tables
            and set(self.canonical_table_scores) == expected
            and bool(self.canonical_table_scores)
            and all(self.canonical_table_scores.values())
            and not any(self.canonical_mismatch_codes.values())
        )


@dataclass(frozen=True)
class Stage2EvaluationResult:
    """Per-mart Stage 2 result and fraction of matching marts.

    Strict fields are diagnostic. Canonical fields affect only certification.
    Neither changes ``passed`` or ``reward``.
    """

    database: str
    population: str
    mart_scores: dict[str, bool]
    errors: dict[str, str]
    reward: float
    strict_mart_scores: dict[str, bool] = field(default_factory=dict)
    strict_mismatch_codes: dict[str, str] = field(default_factory=dict)
    column_fingerprints: dict[str, tuple[tuple[str, str], ...]] = field(
        default_factory=dict
    )
    canonical_mart_scores: dict[str, bool] = field(default_factory=dict)
    canonical_mismatch_codes: dict[str, str] = field(default_factory=dict)
    canonical_reference_fingerprints: dict[str, str] = field(default_factory=dict)
    canonical_actual_fingerprints: dict[str, str] = field(default_factory=dict)
    # See Stage1EvaluationResult.physical_container.  Certification must reject
    # this compatibility default for destinations with a distinct container.
    physical_container: str = ""

    @property
    def passed(self) -> bool:
        return bool(self.mart_scores) and all(self.mart_scores.values()) and not self.errors

    @property
    def certification_passed(self) -> bool:
        """Exact typed mart verdict; never changes the upstream reward."""

        expected = set(self.mart_scores)
        return (
            self.passed
            and set(self.canonical_mart_scores) == expected
            and bool(self.canonical_mart_scores)
            and all(self.canonical_mart_scores.values())
            and not any(self.canonical_mismatch_codes.values())
        )


@dataclass(frozen=True)
class EndToEndEvaluationResult:
    """Original ELT-Bench reward: Stage 1 gates Stage 2."""

    stage1: Stage1EvaluationResult
    stage2: Stage2EvaluationResult | None
    reward: float

    @property
    def passed(self) -> bool:
        return self.stage1.passed and self.stage2 is not None and self.stage2.passed

    @property
    def certification_passed(self) -> bool:
        return (
            self.stage1.certification_passed
            and self.stage2 is not None
            and self.stage2.certification_passed
        )


def _population_name(population: PopulationName | str) -> str:
    try:
        return PopulationName(population).value
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"unsupported population {population!r}") from exc


def _evaluation_destination(value: Destination | str) -> Destination:
    try:
        return normalize_destination(value)
    except ValueError as exc:
        raise EvaluationError(str(exc)) from exc


def _destination_label(destination: Destination) -> str:
    return {
        Destination.SNOWFLAKE: "Snowflake",
        Destination.DATABRICKS: "Databricks",
        Destination.REDSHIFT: "Redshift",
    }[destination]


def _validate_identifier(
    identifier: str,
    *,
    what: str,
    destination: Destination = Destination.SNOWFLAKE,
) -> str:
    if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
        raise EvaluationError(
            f"invalid {_destination_label(destination)} {what} identifier {identifier!r}"
        )
    return identifier


def _validate_logical_namespace(
    identifier: str,
    *,
    what: str = "database",
    destination: Destination = Destination.SNOWFLAKE,
) -> str:
    if not isinstance(identifier, str) or not _LOGICAL_NAMESPACE_IDENTIFIER.fullmatch(
        identifier
    ):
        raise EvaluationError(
            f"invalid {_destination_label(destination)} {what} identifier {identifier!r}"
        )
    return identifier


def _quote_validated_identifier(value: str, destination: Destination) -> str:
    if destination is Destination.SNOWFLAKE:
        return quote_sql_identifier(
            value.upper(), dialect="snowflake", force=True
        )
    if destination is Destination.DATABRICKS:
        return quote_sql_identifier(value, dialect="databricks", force=True)
    return quote_sql_identifier(value.lower(), dialect="redshift", force=True)


def _quote_logical_namespace(
    identifier: str,
    *,
    what: str = "database",
    destination: Destination = Destination.SNOWFLAKE,
) -> str:
    value = _validate_logical_namespace(
        identifier,
        what=what,
        destination=destination,
    )
    return _quote_validated_identifier(value, destination)


def _physical_container(
    destination: Destination,
    physical_container: str | None,
) -> str | None:
    if destination is Destination.DATABRICKS and physical_container is None:
        raise EvaluationError(
            "Databricks evaluation requires physical_container catalog"
        )
    if physical_container is not None:
        return _validate_logical_namespace(
            physical_container,
            what="physical container",
            destination=destination,
        )
    return None


def _table_relation_parts(
    logical_namespace: str,
    table: str,
    *,
    destination: Destination,
    physical_container: str | None,
) -> tuple[str, ...]:
    """Validated, case-folded physical relation parts for one destination."""

    logical = _validate_logical_namespace(logical_namespace, destination=destination)
    table_name = _validate_identifier(table, what="table", destination=destination)
    if destination is Destination.SNOWFLAKE:
        return (logical.upper(), AIRBYTE_SCHEMA, table_name.upper())
    if destination is Destination.DATABRICKS:
        assert physical_container is not None
        container = _validate_logical_namespace(
            physical_container,
            what="physical container",
            destination=destination,
        )
        return (container, logical, table_name)
    return (logical.lower(), table_name.lower())


def _table_relation(
    logical_namespace: str,
    table: str,
    *,
    destination: Destination,
    physical_container: str | None,
) -> str:
    parts = _table_relation_parts(
        logical_namespace,
        table,
        destination=destination,
        physical_container=physical_container,
    )
    return ".".join(_quote_validated_identifier(part, destination) for part in parts)


def _list_tables_sql(
    logical_namespace: str,
    *,
    destination: Destination,
    physical_container: str | None,
) -> str:
    logical = _validate_logical_namespace(
        logical_namespace,
        destination=destination,
    )
    if destination is Destination.SNOWFLAKE:
        quoted_logical = _quote_logical_namespace(
            logical,
            destination=destination,
        )
        # Preserve the original Snowflake query byte-for-byte.
        return (
            "SELECT TABLE_NAME "
            f"FROM {quoted_logical}.\"INFORMATION_SCHEMA\".\"TABLES\" "
            f"WHERE TABLE_SCHEMA = '{AIRBYTE_SCHEMA}' "
            "AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
        )
    if destination is Destination.DATABRICKS:
        assert physical_container is not None
        catalog = _quote_logical_namespace(
            physical_container,
            what="physical container",
            destination=destination,
        )
        return (
            "SELECT table_name "
            f"FROM {catalog}.information_schema.tables "
            f"WHERE table_schema = '{logical.lower()}' "
            "AND table_type IN ('MANAGED', 'EXTERNAL') ORDER BY table_name"
        )
    return (
        "SELECT table_name FROM information_schema.tables "
        f"WHERE table_schema = '{logical.lower()}' "
        "AND table_type = 'BASE TABLE' ORDER BY table_name"
    )


def _validate_task_id(task_id: str) -> str:
    if (
        not isinstance(task_id, str)
        or not task_id
        or task_id in {".", ".."}
        or Path(task_id).name != task_id
        or "/" in task_id
        or "\\" in task_id
    ):
        raise EvaluationError(f"invalid release task id {task_id!r}")
    return task_id


def _read_json(path: Path, *, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvaluationError(f"missing {what}: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot read {what} at {path}: {exc}") from exc


def _read_yaml(path: Path, *, what: str) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvaluationError(f"missing {what}: {path}") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise EvaluationError(f"cannot read {what} at {path}: {exc}") from exc


def _database_entry(
    payload: Any,
    database: str | None,
    *,
    path: Path,
    what: str,
    destination: Destination = Destination.SNOWFLAKE,
) -> tuple[str, Any]:
    if not isinstance(payload, dict) or not payload:
        raise EvaluationError(f"{what} at {path} must be a non-empty object")
    if any(not isinstance(key, str) for key in payload):
        raise EvaluationError(f"{what} at {path} has a non-string database name")

    keys = list(payload)
    if database is None:
        if len(keys) != 1:
            raise EvaluationError(
                f"{what} at {path} names {len(keys)} databases; select one explicitly"
            )
        selected = keys[0]
    else:
        _validate_logical_namespace(database, destination=destination)
        matches = [key for key in keys if key.casefold() == database.casefold()]
        if len(matches) != 1:
            raise EvaluationError(
                f"database {database!r} does not identify exactly one entry in {path}"
            )
        selected = matches[0]
    _validate_logical_namespace(selected, destination=destination)
    return selected, payload[selected]


def _count_mapping(payload: Any, *, path: Path) -> dict[str, int]:
    if not isinstance(payload, dict) or not payload:
        raise EvaluationError(f"stage-1 counts at {path} must be a non-empty object")
    counts: dict[str, int] = {}
    seen: set[str] = set()
    for table, count in payload.items():
        _validate_identifier(table, what="table")
        folded = table.casefold()
        if folded in seen:
            raise EvaluationError(f"duplicate case-insensitive table {table!r} in {path}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise EvaluationError(
                f"stage-1 count for table {table!r} at {path} is not a non-negative integer"
            )
        seen.add(folded)
        counts[table] = count
    return counts


def _load_stage1_counts(
    answer_key_dir: Path,
    database: str | None,
    population: str,
    destination: Destination = Destination.SNOWFLAKE,
) -> tuple[str, dict[str, int]]:
    """Load the upstream primary key, plus explicit non-primary population gold.

    ``table.json`` remains the required source of the primary count contract and
    the required table set.  A non-primary run must additionally have its
    private ``gold/<population>/stage1_counts.json``; it never silently reuses
    primary counts.
    """

    table_path = answer_key_dir / "table.json"
    database_name, raw_primary = _database_entry(
        _read_json(table_path, what="stage-1 table answer key"),
        database,
        path=table_path,
        what="stage-1 table answer key",
        destination=destination,
    )
    primary = _count_mapping(raw_primary, path=table_path)
    population_path = answer_key_dir / "gold" / population / "stage1_counts.json"
    if population == PopulationName.PRIMARY.value:
        # When the richer population key exists, disagreement is corrupt private
        # evidence rather than a reason to choose whichever file is convenient.
        if population_path.is_file():
            population_counts = _count_mapping(
                _read_json(population_path, what="primary stage-1 population gold"),
                path=population_path,
            )
            if population_counts != primary:
                raise EvaluationError(
                    f"primary stage-1 counts disagree between {table_path} and "
                    f"{population_path}"
                )
        return database_name, primary

    population_counts = _count_mapping(
        _read_json(population_path, what=f"{population} stage-1 population gold"),
        path=population_path,
    )
    if {name.casefold() for name in population_counts} != {
        name.casefold() for name in primary
    }:
        raise EvaluationError(
            f"{population} stage-1 gold does not name the table set declared by table.json"
        )
    return database_name, population_counts


def _query_all(connection: DBAPIConnection, sql: str) -> tuple[Any, list[Any]]:
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
        description = cursor.description
        rows = list(cursor.fetchall())
        return description, rows
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                # Closing must not replace the query outcome.  DB-API drivers
                # commonly make close idempotent, but the protocol does not.
                pass


_REDSHIFT_CURRENT_DATABASE_SQL = "SELECT current_database()"


def _observed_physical_container(
    connection: DBAPIConnection,
    destination: Destination,
    requested: str | None,
) -> str:
    """Resolve and verify the physical target used by the evaluated session."""

    container = _physical_container(destination, requested)
    if destination is Destination.SNOWFLAKE:
        return ""
    if destination is Destination.DATABRICKS:
        assert container is not None
        return container

    try:
        _, rows = _query_all(connection, _REDSHIFT_CURRENT_DATABASE_SQL)
    except Exception:
        # Driver exceptions can contain endpoint or credential details.
        raise EvaluationError(
            "could not verify Redshift connection database"
        ) from None
    if len(rows) != 1:
        raise EvaluationError(
            "Redshift current_database() returned an invalid result"
        )
    row = rows[0]
    if isinstance(row, Mapping):
        values = list(row.values())
    elif isinstance(row, Sequence) and not isinstance(
        row, (str, bytes, bytearray)
    ):
        values = list(row)
    else:
        raise EvaluationError(
            "Redshift current_database() returned an invalid result"
        )
    if len(values) != 1 or not isinstance(values[0], str):
        raise EvaluationError(
            "Redshift current_database() returned an invalid result"
        )
    observed = _validate_logical_namespace(
        values[0],
        what="connection database",
        destination=Destination.REDSHIFT,
    )
    if container is not None and container.casefold() != observed.casefold():
        raise EvaluationError(
            "requested Redshift physical container does not match the "
            "connection database"
        )
    return observed


def _error_text(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _single_count(rows: list[Any], *, table: str) -> int:
    if len(rows) != 1:
        raise EvaluationError(
            f"COUNT(*) for table {table!r} returned {len(rows)} rows instead of one"
        )
    row = rows[0]
    if isinstance(row, Mapping):
        values = list(row.values())
    elif isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        values = list(row)
    else:
        raise EvaluationError(f"COUNT(*) for table {table!r} returned a malformed row")
    if len(values) != 1:
        raise EvaluationError(f"COUNT(*) for table {table!r} returned a malformed row")
    value = values[0]
    if isinstance(value, bool):
        raise EvaluationError(f"COUNT(*) for table {table!r} returned a boolean")
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvaluationError(
            f"COUNT(*) for table {table!r} returned non-integral value {value!r}"
        ) from exc
    if count < 0 or value != count:
        raise EvaluationError(
            f"COUNT(*) for table {table!r} returned non-integral value {value!r}"
        )
    return count


def evaluate_stage1(
    connection: DBAPIConnection,
    answer_key_dir: Path,
    *,
    population: PopulationName | str = PopulationName.PRIMARY,
    database: str | None = None,
    destination: Destination | str = Destination.SNOWFLAKE,
    physical_container: str | None = None,
    expected_repetitions: int = 1,
    allowed_existing_tables: Iterable[str] = (),
) -> Stage1EvaluationResult:
    """Score raw warehouse tables with the upstream strict-binary comparator.

    Unexpected tables are reported but, matching upstream ELT-Bench, do not
    alter the count-only reward.  Missing tables, count mismatches, or warehouse
    query errors produce reward 0.0.
    """

    answer_key_dir = Path(answer_key_dir)
    if (
        isinstance(expected_repetitions, bool)
        or not isinstance(expected_repetitions, int)
        or expected_repetitions < 1
    ):
        raise EvaluationError("expected_repetitions must be a positive integer")
    pop = _population_name(population)
    resolved_destination = _evaluation_destination(destination)
    db, base_expected = _load_stage1_counts(
        answer_key_dir,
        database,
        pop,
        resolved_destination,
    )
    expected = {
        table: count * expected_repetitions
        for table, count in base_expected.items()
    }
    container = _observed_physical_container(
        connection, resolved_destination, physical_container
    )
    list_sql = _list_tables_sql(
        db,
        destination=resolved_destination,
        physical_container=container,
    )

    errors: dict[str, str] = {}
    listed: dict[str, str] = {}
    try:
        _, rows = _query_all(connection, list_sql)
        for row in rows:
            if (
                not isinstance(row, Sequence)
                or isinstance(row, (str, bytes, bytearray))
                or len(row) < 1
                or not isinstance(row[0], str)
            ):
                raise EvaluationError("INFORMATION_SCHEMA returned a malformed table row")
            table = row[0]
            _validate_identifier(
                table,
                what="table",
                destination=resolved_destination,
            )
            folded = table.casefold()
            if folded in listed:
                raise EvaluationError(
                    f"INFORMATION_SCHEMA returned duplicate table name {table!r}"
                )
            listed[folded] = table
    except Exception as exc:
        errors["__tables__"] = _error_text(exc)

    expected_by_folded = {name.casefold(): name for name in expected}
    allowed_by_folded: set[str] = set()
    for table in allowed_existing_tables:
        validated = _validate_identifier(
            table,
            what="allowed existing table",
            destination=resolved_destination,
        )
        folded = validated.casefold()
        if folded in allowed_by_folded:
            raise EvaluationError(
                f"duplicate case-insensitive allowed existing table {table!r}"
            )
        allowed_by_folded.add(folded)
    missing = tuple(
        sorted(
            (name for folded, name in expected_by_folded.items() if folded not in listed),
            key=str.casefold,
        )
    )
    unexpected = tuple(
        sorted(
            (
                name
                for folded, name in listed.items()
                if folded not in expected_by_folded
                and folded not in allowed_by_folded
            ),
            key=str.casefold,
        )
    )

    actual: dict[str, int] = {}
    for folded, expected_name in sorted(expected_by_folded.items()):
        actual_name = listed.get(folded)
        if actual_name is None:
            continue
        count_sql = "SELECT COUNT(*) FROM " + _table_relation(
            db,
            actual_name,
            destination=resolved_destination,
            physical_container=container,
        )
        try:
            _, rows = _query_all(connection, count_sql)
            actual[expected_name] = _single_count(rows, table=expected_name)
        except Exception as exc:
            errors[expected_name] = _error_text(exc)

    count_mismatches = {
        table: CountMismatch(expected=want, actual=actual[table])
        for table, want in expected.items()
        if table in actual and actual[table] != want
    }
    compared, detail = upstream_eval.compare_stage1(expected, actual)
    passed = compared and not errors
    return Stage1EvaluationResult(
        database=db,
        population=pop,
        expected_counts=expected,
        actual_counts=actual,
        missing_tables=missing,
        unexpected_tables=unexpected,
        count_mismatches=count_mismatches,
        detail=detail,
        errors=errors,
        passed=passed,
        reward=1.0 if passed else 0.0,
        expected_repetitions=expected_repetitions,
        physical_container=container,
    )


def _canonical_verdict(
    reference: CanonicalRelationFingerprint,
    actual: CanonicalRelationFingerprint,
) -> tuple[bool, str]:
    """Return a stable, non-secret mismatch classification for two digests."""

    if reference.row_count != actual.row_count:
        return False, "row_count"
    if reference.schema_digest != actual.schema_digest:
        return False, "schema"
    if reference.result_digest != actual.result_digest:
        return False, "values"
    return True, ""


def _canonicalization_error_code(exc: CanonicalFingerprintError) -> str:
    """Collapse detailed value/schema failures into a stable public code."""

    message = str(exc).casefold()
    schema_markers = (
        "actual relation",
        "expected relation",
        "allowed extra-column policy",
        "case-folding collision",
        "row mapping",
        "row width",
    )
    if any(marker in message for marker in schema_markers):
        return "schema"
    return "values:type"


def _stage1_canonical_evidence(
    root: Path,
    task_id: str,
    connection: DBAPIConnection,
    result: Stage1EvaluationResult,
    *,
    destination: Destination,
    physical_container: str | None,
) -> Stage1EvaluationResult:
    """Attach exact source-vs-warehouse fingerprints without changing reward.

    The reference side is rebuilt from the frozen rendered source artifacts
    through the strict typed loader.  Warehouse reads are capped at the
    expected row count plus one, preventing an unexpectedly large relation
    from turning certification into an unbounded fetch.
    """

    expected_names = tuple(result.expected_counts)
    failed_scores = {name: False for name in expected_names}
    failed_codes = {name: "error:reference_failed" for name in expected_names}
    reference_digests: dict[str, str] = {}
    actual_digests: dict[str, str] = {}

    task_path = root / "private" / task_id / "semantic" / "task_ir.json"
    rendered_dir = (
        root
        / "private"
        / task_id
        / "populations"
        / result.population
        / "rendered"
    )
    reference_fingerprints: dict[str, CanonicalRelationFingerprint] = {}
    tables_by_folded: dict[str, Any] = {}
    con: duckdb.DuckDBPyConnection | None = None
    try:
        task_ir = task_from_json(task_path.read_text(encoding="utf-8"))
        if task_ir.task_id != task_id:
            raise EvaluationError("private TaskIR task id does not match release task")
        tables_by_folded = {table.name.casefold(): table for table in task_ir.tables}
        if set(tables_by_folded) != {name.casefold() for name in expected_names}:
            raise EvaluationError("private TaskIR table set disagrees with answer key")

        con = duckdb.connect(":memory:")
        loaded_counts = strict_diagnostic.load_sources_duckdb_strict(
            task_ir, rendered_dir, con
        )
        repeated_loaded_counts = {
            name.casefold(): count * result.expected_repetitions
            for name, count in loaded_counts.items()
        }
        if repeated_loaded_counts != {
            name.casefold(): count for name, count in result.expected_counts.items()
        }:
            raise EvaluationError("rendered source counts disagree with answer key")

        for name in expected_names:
            table = tables_by_folded[name.casefold()]
            allowed_metadata_columns = (
                AIRBYTE_METADATA_COLUMNS
                + AIRBYTE_SOURCE_METADATA_COLUMNS.get(
                    task_ir.backend_for(table.name).backend,
                    (),
                )
            )
            projected = ", ".join(
                quote_sql_identifier(column.name, force=True)
                for column in table.columns
            )
            relation = quote_sql_identifier(table.name, force=True)
            cursor = con.execute(f"SELECT {projected} FROM {relation}")
            columns = tuple(str(item[0]) for item in cursor.description)
            reference_rows = list(cursor.fetchall()) * result.expected_repetitions
            fingerprint = canonical_relation_fingerprint(
                actual_columns=columns,
                expected_columns=tuple(
                    (column.name, column.type) for column in table.columns
                ),
                rows=reference_rows,
                allowed_extra_columns=allowed_metadata_columns,
                naive_timestamp_policy=NaiveTimestampPolicy.ASSUME_UTC,
            )
            reference_fingerprints[name] = fingerprint
            reference_digests[name] = fingerprint.result_digest
    except Exception:
        return replace(
            result,
            canonical_table_scores=failed_scores,
            canonical_mismatch_codes=failed_codes,
            canonical_reference_fingerprints=reference_digests,
            canonical_actual_fingerprints=actual_digests,
        )
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                # A diagnostic teardown failure must not rewrite the upstream
                # compatibility reward; missing evidence already fails strict.
                pass

    scores: dict[str, bool] = {}
    codes: dict[str, str] = {}
    missing = {name.casefold() for name in result.missing_tables}
    for name in expected_names:
        if name.casefold() in missing:
            scores[name] = False
            codes[name] = "table_missing"
            continue
        if name in result.count_mismatches:
            scores[name] = False
            codes[name] = "row_count"
            continue
        if name in result.errors or "__tables__" in result.errors:
            scores[name] = False
            codes[name] = "error:warehouse_query"
            continue

        table = tables_by_folded[name.casefold()]
        allowed_metadata_columns = (
            AIRBYTE_METADATA_COLUMNS
            + AIRBYTE_SOURCE_METADATA_COLUMNS.get(
                task_ir.backend_for(table.name).backend,
                (),
            )
        )
        relation = _table_relation(
            result.database,
            name,
            destination=destination,
            physical_container=physical_container,
        )
        # LIMIT is an integer derived from the validated private count, never
        # caller-controlled SQL.  The +1 detects a concurrent/stale extra row.
        sql = f"SELECT * FROM {relation} LIMIT {result.expected_counts[name] + 1}"
        try:
            description, rows = _query_all(connection, sql)
            columns = _description_names(description, mart=name)
            actual = canonical_relation_fingerprint(
                actual_columns=columns,
                expected_columns=tuple(
                    (column.name, column.type) for column in table.columns
                ),
                rows=rows,
                allowed_extra_columns=allowed_metadata_columns,
                naive_timestamp_policy=NaiveTimestampPolicy.ASSUME_UTC,
            )
            actual_digests[name] = actual.result_digest
            scores[name], codes[name] = _canonical_verdict(
                reference_fingerprints[name], actual
            )
        except CanonicalFingerprintError as exc:
            scores[name] = False
            codes[name] = _canonicalization_error_code(exc)
        except Exception:
            scores[name] = False
            codes[name] = "error:warehouse_query"

    return replace(
        result,
        canonical_table_scores=scores,
        canonical_mismatch_codes=codes,
        canonical_reference_fingerprints=reference_digests,
        canonical_actual_fingerprints=actual_digests,
    )


def prepare_evaluation_sql(
    sql: str,
    database: str,
    *,
    expected_mart: str | None = None,
    destination: Destination | str = Destination.SNOWFLAKE,
    physical_container: str | None = None,
) -> str:
    """Rewrite canonical ``database.table`` references for a destination.

    Validate and quote identifiers, reject multiple or writable statements,
    and render the parsed AST in the destination dialect. The result is executed
    but not persisted.
    """

    resolved_destination = _evaluation_destination(destination)
    _validate_logical_namespace(database, destination=resolved_destination)
    container = _physical_container(resolved_destination, physical_container)
    if not isinstance(sql, str) or not sql.strip():
        raise EvaluationError("evaluation SQL is empty")
    stripped = sql.strip()

    dialect = _EVALUATION_DIALECTS[resolved_destination]
    try:
        statements = sqlglot.parse(stripped, read=_STORED_EVALUATION_DIALECT)
    except SqlglotError as exc:
        raise EvaluationError(f"evaluation SQL does not parse: {exc}") from exc
    if len(statements) != 1 or statements[0] is None:
        raise EvaluationError("evaluation SQL must contain exactly one statement")
    tree = statements[0]
    if not isinstance(tree, exp.Query) or tree.find(*_MUTATING_SQL_NODES) is not None:
        raise EvaluationError("evaluation SQL must be a read-only SELECT")

    # Track CTE aliases statement-wide; exporter-generated answer keys never
    # contain cross-scope alias collisions.
    cte_aliases = {cte.alias_or_name.casefold() for cte in tree.find_all(exp.CTE)}
    references: list[tuple[str, str]] = []
    for table in list(tree.find_all(exp.Table)):
        if not isinstance(table.this, exp.Identifier):
            # A bare table function (GENERATOR, FLATTEN, RANGE, ...) reads no
            # warehouse relation; stages and namespace-qualified callables are
            # refused rather than passed through unqualified.
            if isinstance(table.this, exp.Func) and not table.db and not table.catalog:
                continue
            raise EvaluationError(
                f"evaluation SQL contains unsupported relation "
                f"{table.sql(dialect=dialect)!r}"
            )
        if table.catalog:
            raise EvaluationError(
                f"evaluation relation {table.sql(dialect=dialect)!r} "
                "must be database.table"
            )
        if not table.db:
            if table.name.casefold() in cte_aliases:
                continue
            raise EvaluationError(
                f"evaluation relation {table.name!r} must be database.table"
            )
        stored_db = table.db
        _validate_logical_namespace(
            stored_db,
            destination=resolved_destination,
        )
        table_name = _validate_identifier(
            table.name,
            what="table",
            destination=resolved_destination,
        )
        if stored_db.casefold() != database.casefold():
            raise EvaluationError(
                f"evaluation SQL references database {stored_db!r}, expected {database!r}"
            )
        parts = _table_relation_parts(
            database,
            table_name,
            destination=resolved_destination,
            physical_container=container,
        )
        references.append((stored_db, table_name))
        identifiers = [exp.Identifier(this=part, quoted=True) for part in parts]
        table.set("this", identifiers[-1])
        table.set("db", identifiers[-2] if len(identifiers) >= 2 else None)
        table.set("catalog", identifiers[-3] if len(identifiers) >= 3 else None)

    if not references:
        raise EvaluationError("evaluation SQL contains no database.table relation")
    # A column the exporter had to quote (a reserved word such as `format`)
    # is stored lower-case; a quoted identifier is case-sensitive on the
    # warehouse, whose unquoted names fold upper on Snowflake and lower on
    # Redshift. Fold it the way the relation parts are folded so the query
    # names the column a submission actually created.
    for column in tree.find_all(exp.Column):
        identifier = column.this
        if isinstance(identifier, exp.Identifier) and identifier.quoted:
            if resolved_destination is Destination.SNOWFLAKE:
                identifier.set("this", identifier.this.upper())
            elif resolved_destination is Destination.REDSHIFT:
                identifier.set("this", identifier.this.lower())
    if expected_mart is not None:
        _validate_identifier(
            expected_mart,
            what="mart",
            destination=resolved_destination,
        )
        if not any(table.casefold() == expected_mart.casefold() for _, table in references):
            raise EvaluationError(
                f"evaluation SQL for mart {expected_mart!r} does not reference that mart"
            )
    return tree.sql(dialect=dialect) + (";" if stripped.endswith(";") else "")


def _model_specs(
    public_task_dir: Path,
    answer_key_dir: Path,
    database: str | None,
    destination: Destination = Destination.SNOWFLAKE,
) -> tuple[str, tuple[MartSpec, ...]]:
    data_model_path = public_task_dir / "data_model.yaml"
    data_model = _read_yaml(data_model_path, what="public data model")
    if not isinstance(data_model, dict) or set(data_model) != {"models"}:
        raise EvaluationError(
            f"public data model at {data_model_path} must contain only a models key"
        )
    raw_models = data_model.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise EvaluationError(f"public data model at {data_model_path} declares no marts")

    sort_key_path = answer_key_dir / "sort_key.json"
    db, raw_by_mart = _database_entry(
        _read_json(sort_key_path, what="private sort keys"),
        database,
        path=sort_key_path,
        what="private sort keys",
        destination=destination,
    )
    if not isinstance(raw_by_mart, dict) or not raw_by_mart:
        raise EvaluationError(f"private sort keys at {sort_key_path} declare no marts")

    models_by_name: dict[str, Mapping[str, Any]] = {}
    display_names: dict[str, str] = {}
    for raw_model in raw_models:
        if not isinstance(raw_model, Mapping):
            raise EvaluationError(f"a model in {data_model_path} is not an object")
        name = raw_model.get("name")
        _validate_identifier(name, what="mart")
        folded = name.casefold()
        if folded in models_by_name:
            raise EvaluationError(f"duplicate case-insensitive mart {name!r}")
        models_by_name[folded] = raw_model
        display_names[folded] = name

    sort_by_name: dict[str, tuple[str, Sequence[Any]]] = {}
    for name, keys in raw_by_mart.items():
        _validate_identifier(name, what="mart")
        folded = name.casefold()
        if folded in sort_by_name:
            raise EvaluationError(f"duplicate case-insensitive sort-key mart {name!r}")
        if (
            not isinstance(keys, Sequence)
            or isinstance(keys, (str, bytes, bytearray))
            or not keys
        ):
            raise EvaluationError(f"mart {name!r} has no private sort keys")
        sort_by_name[folded] = (name, keys)

    if set(models_by_name) != set(sort_by_name):
        missing = sorted(display_names[k] for k in set(models_by_name) - set(sort_by_name))
        unexpected = sorted(sort_by_name[k][0] for k in set(sort_by_name) - set(models_by_name))
        raise EvaluationError(
            f"data-model/sort-key mart mismatch: missing={missing}, unexpected={unexpected}"
        )

    marts: list[MartSpec] = []
    for folded, raw_model in models_by_name.items():
        name = display_names[folded]
        grain = raw_model.get("grain")
        if not isinstance(grain, str) or not grain.strip():
            raise EvaluationError(f"mart {name!r} has no declared grain")
        raw_columns = raw_model.get("columns")
        if not isinstance(raw_columns, list) or not raw_columns:
            raise EvaluationError(f"mart {name!r} declares no columns")

        columns: list[MartColumn] = []
        columns_by_name: dict[str, str] = {}
        for raw_column in raw_columns:
            if not isinstance(raw_column, Mapping):
                raise EvaluationError(f"mart {name!r} has a malformed column")
            column_name = raw_column.get("name")
            _validate_identifier(column_name, what="column")
            column_folded = column_name.casefold()
            if column_folded in columns_by_name:
                raise EvaluationError(
                    f"mart {name!r} has duplicate case-insensitive column {column_name!r}"
                )
            description = raw_column.get("description")
            if not isinstance(description, str) or not description.strip():
                raise EvaluationError(
                    f"mart {name!r} column {column_name!r} has no description"
                )
            try:
                column_type = ColumnType(raw_column.get("type"))
            except (TypeError, ValueError) as exc:
                raise EvaluationError(
                    f"mart {name!r} column {column_name!r} has unsupported type "
                    f"{raw_column.get('type')!r}"
                ) from exc
            columns_by_name[column_folded] = column_name
            columns.append(
                MartColumn(
                    name=column_name,
                    type=column_type,
                    description=description,
                )
            )

        private_keys: list[str] = []
        for raw_key in sort_by_name[folded][1]:
            if not isinstance(raw_key, str):
                raise EvaluationError(f"mart {name!r} has a non-string private sort key")
            actual_key = columns_by_name.get(raw_key.casefold())
            if actual_key is None:
                raise EvaluationError(
                    f"mart {name!r} private sort key {raw_key!r} is not a model column"
                )
            if actual_key in private_keys:
                raise EvaluationError(f"mart {name!r} repeats sort key {raw_key!r}")
            private_keys.append(actual_key)

        public_raw_keys = raw_model.get("key_columns")
        if (
            not isinstance(public_raw_keys, Sequence)
            or isinstance(public_raw_keys, (str, bytes, bytearray))
            or not public_raw_keys
        ):
            raise EvaluationError(f"mart {name!r} has no public key_columns")
        public_keys: list[str] = []
        for raw_key in public_raw_keys:
            if not isinstance(raw_key, str):
                raise EvaluationError(f"mart {name!r} has a non-string public key")
            actual_key = columns_by_name.get(raw_key.casefold())
            if actual_key is None:
                raise EvaluationError(
                    f"mart {name!r} public key {raw_key!r} is not a model column"
                )
            public_keys.append(actual_key)
        if public_keys != private_keys:
            raise EvaluationError(
                f"mart {name!r} public key_columns disagree with private sort_key.json"
            )

        # data_model.yaml intentionally omits the private declarative TaskIR
        # plan.  A minimal adapter plan lets us use the canonical MartSpec and
        # therefore the one existing compare_mart implementation.
        plan = MartPlan(
            mart=name,
            ops=(
                MartOp(
                    kind=MartOpKind.SOURCE,
                    description="Runtime comparison view of the evaluated mart",
                    tables=(name,),
                    columns=tuple(column.name for column in columns),
                ),
            ),
        )
        marts.append(
            MartSpec(
                name=name,
                description=str(raw_model.get("description") or ""),
                grain=grain,
                key_columns=tuple(private_keys),
                columns=tuple(columns),
                plan=plan,
            )
        )
    return db, tuple(marts)


def _gold_csv_paths(answer_key_dir: Path, population: str) -> dict[str, Path]:
    """Return the authoritative mart set for one population.

    A primary key may use the historical ``gt/`` directory only when the
    population-specific directory contains no mart CSVs.  Never merge the two
    roots: a partially copied richer key is corrupt evidence, not permission to
    fill gaps from a fallback directory.
    """

    candidates = [answer_key_dir / "gold" / population]
    if population == PopulationName.PRIMARY.value:
        candidates.append(answer_key_dir / "gt")
    for root in candidates:
        paths = sorted(root.glob("*.csv")) if root.is_dir() else []
        if not paths:
            continue
        by_name: dict[str, Path] = {}
        for path in paths:
            _validate_identifier(path.stem, what="mart")
            folded = path.stem.casefold()
            if folded in by_name:
                raise EvaluationError(
                    f"duplicate case-insensitive gold mart {path.stem!r} in {root}"
                )
            by_name[folded] = path
        return by_name
    raise EvaluationError(f"missing {population} gold CSV directory for marts")


def _description_names(description: Any, *, mart: str) -> tuple[str, ...]:
    if not isinstance(description, Sequence) or isinstance(
        description, (str, bytes, bytearray)
    ) or not description:
        raise EvaluationError(f"query for mart {mart!r} returned no column description")
    names: list[str] = []
    seen: set[str] = set()
    for item in description:
        name: Any = None
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            if item:
                name = item[0]
        if name is None:
            name = getattr(item, "name", None)
        if not isinstance(name, str) or not name:
            raise EvaluationError(
                f"query for mart {mart!r} returned a malformed column description"
            )
        folded = name.casefold()
        if folded in seen:
            raise EvaluationError(
                f"query for mart {mart!r} returned duplicate column {name!r}"
            )
        seen.add(folded)
        names.append(name)
    return tuple(names)


def _rows_from_cursor(description: Any, raw_rows: list[Any], *, mart: str) -> list[Row]:
    names = _description_names(description, mart=mart)
    rows: list[Row] = []
    for index, raw_row in enumerate(raw_rows, start=1):
        if isinstance(raw_row, Mapping):
            folded = {str(key).casefold(): value for key, value in raw_row.items()}
            if any(name.casefold() not in folded for name in names):
                raise EvaluationError(f"mart {mart!r} row {index} is missing a column")
            values = [folded[name.casefold()] for name in names]
        elif isinstance(raw_row, Sequence) and not isinstance(
            raw_row, (str, bytes, bytearray)
        ):
            values = list(raw_row)
        else:
            raise EvaluationError(f"mart {mart!r} row {index} is malformed")
        if len(values) != len(names):
            raise EvaluationError(
                f"mart {mart!r} row {index} has {len(values)} values for "
                f"{len(names)} columns"
            )
        rows.append(cast(Row, dict(zip(names, values))))
    return rows


def evaluate_stage2(
    connection: DBAPIConnection,
    public_task_dir: Path,
    answer_key_dir: Path,
    *,
    population: PopulationName | str = PopulationName.PRIMARY,
    database: str | None = None,
    destination: Destination | str = Destination.SNOWFLAKE,
    physical_container: str | None = None,
) -> Stage2EvaluationResult:
    """Execute and score one private evaluation query per declared public mart."""

    public_task_dir = Path(public_task_dir)
    answer_key_dir = Path(answer_key_dir)
    pop = _population_name(population)
    resolved_destination = _evaluation_destination(destination)
    db, marts = _model_specs(
        public_task_dir,
        answer_key_dir,
        database,
        resolved_destination,
    )
    container = _observed_physical_container(
        connection, resolved_destination, physical_container
    )

    gold_paths = _gold_csv_paths(answer_key_dir, pop)
    marts_by_name = {mart.name.casefold(): mart for mart in marts}
    if set(gold_paths) != set(marts_by_name):
        missing = sorted(
            marts_by_name[name].name for name in set(marts_by_name) - set(gold_paths)
        )
        unexpected = sorted(
            gold_paths[name].stem for name in set(gold_paths) - set(marts_by_name)
        )
        raise EvaluationError(
            f"data-model/gold mart mismatch: missing={missing}, unexpected={unexpected}"
        )

    # Resolve and validate every private artifact before running the first query;
    # a half-present key must never produce a partial score that looks valid.
    prepared: list[tuple[MartSpec, str, str]] = []
    for mart in marts:
        sql_path = answer_key_dir / "evaluation" / "sql" / f"{mart.name}.sql"
        try:
            stored_sql = sql_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise EvaluationError(f"missing evaluation SQL: {sql_path}") from exc
        except (OSError, UnicodeError) as exc:
            raise EvaluationError(f"cannot read evaluation SQL at {sql_path}: {exc}") from exc
        sql = prepare_evaluation_sql(
            stored_sql,
            db,
            expected_mart=mart.name,
            destination=resolved_destination,
            physical_container=container or None,
        )
        gold_path = gold_paths[mart.name.casefold()]
        try:
            gold_csv = gold_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise EvaluationError(f"cannot read gold CSV at {gold_path}: {exc}") from exc
        prepared.append((mart, sql, gold_csv))

    scores: dict[str, bool] = {}
    errors: dict[str, str] = {}
    strict_scores: dict[str, bool] = {}
    strict_codes: dict[str, str] = {}
    fingerprints: dict[str, tuple[tuple[str, str], ...]] = {}
    canonical_scores: dict[str, bool] = {}
    canonical_codes: dict[str, str] = {}
    canonical_reference_digests: dict[str, str] = {}
    canonical_actual_digests: dict[str, str] = {}
    for mart, sql, gold_csv in prepared:
        description: Any = None
        actual_columns: tuple[str, ...] | None = None
        actual_rows: list[Row] | None = None
        try:
            description, raw_rows = _query_all(connection, sql)
            actual_columns = _description_names(description, mart=mart.name)
            actual_rows = _rows_from_cursor(description, raw_rows, mart=mart.name)
            scores[mart.name] = upstream_eval.compare_mart(
                gold_csv,
                actual_rows,
                mart,
                actual_columns=actual_columns,
            )
        except Exception as exc:
            scores[mart.name] = False
            errors[mart.name] = _error_text(exc)
        # Strict typed diagnostic on the SAME already-fetched evidence.  It is
        # wrapped so a diagnostic bug can never alter mart_scores, errors, or
        # the reward.
        try:
            if actual_rows is None:
                strict_scores[mart.name] = False
                strict_codes[mart.name] = "error:query_failed"
            else:
                fingerprints[mart.name] = strict_diagnostic.description_fingerprint(
                    description
                )
                matched, code = strict_diagnostic.strict_text_compare(
                    gold_csv, actual_rows, actual_columns, mart
                )
                strict_scores[mart.name] = matched
                strict_codes[mart.name] = code
        except Exception:
            strict_scores[mart.name] = False
            strict_codes[mart.name] = "error:strict_failed"

        # Certification compares exact logical values, not driver spellings.
        # This accepts equivalent UTC timestamp/decimal/JSON representations
        # across warehouses while preserving text case, nulls and every digit.
        if actual_rows is None or actual_columns is None:
            canonical_scores[mart.name] = False
            canonical_codes[mart.name] = "error:query_failed"
            continue
        expected_columns = tuple(
            (column.name, column.type) for column in mart.columns
        )
        try:
            gold_columns, gold_rows = upstream_eval.parse_canonical_csv(gold_csv)
            if not gold_columns:
                raise CanonicalFingerprintError("gold relation has no columns")
            reference_fingerprint = canonical_relation_fingerprint(
                actual_columns=gold_columns,
                expected_columns=expected_columns,
                rows=gold_rows,
                naive_timestamp_policy=NaiveTimestampPolicy.ASSUME_UTC,
            )
            canonical_reference_digests[mart.name] = (
                reference_fingerprint.result_digest
            )
        except Exception:
            canonical_scores[mart.name] = False
            canonical_codes[mart.name] = "error:gold_unreadable"
            continue
        try:
            actual_fingerprint = canonical_relation_fingerprint(
                actual_columns=actual_columns,
                expected_columns=expected_columns,
                rows=actual_rows,
                naive_timestamp_policy=NaiveTimestampPolicy.ASSUME_UTC,
            )
            canonical_actual_digests[mart.name] = actual_fingerprint.result_digest
            canonical_scores[mart.name], canonical_codes[mart.name] = (
                _canonical_verdict(reference_fingerprint, actual_fingerprint)
            )
        except CanonicalFingerprintError as exc:
            canonical_scores[mart.name] = False
            canonical_codes[mart.name] = _canonicalization_error_code(exc)
        except Exception:
            canonical_scores[mart.name] = False
            canonical_codes[mart.name] = "error:canonical_failed"

    reward = sum(scores.values()) / len(marts) if marts else 0.0
    return Stage2EvaluationResult(
        database=db,
        population=pop,
        mart_scores=scores,
        errors=errors,
        reward=reward,
        physical_container=container,
        strict_mart_scores=strict_scores,
        strict_mismatch_codes=strict_codes,
        column_fingerprints=fingerprints,
        canonical_mart_scores=canonical_scores,
        canonical_mismatch_codes=canonical_codes,
        canonical_reference_fingerprints=canonical_reference_digests,
        canonical_actual_fingerprints=canonical_actual_digests,
    )


def _release_destination(
    release_dir: Path,
    task_id: str,
    explicit: Destination | str | None,
) -> Destination:
    """Infer the release-bound warehouse and reject an explicit mismatch."""

    config_path = Path(release_dir) / "public" / task_id / "config.yaml"
    config = _read_yaml(config_path, what="public task config")
    if not isinstance(config, Mapping):
        raise EvaluationError(f"public task config at {config_path} is not an object")
    try:
        inferred = destination_from_config(config)
    except ValueError as exc:
        raise EvaluationError(f"invalid public task destination: {exc}") from exc
    if explicit is None:
        return inferred
    resolved = _evaluation_destination(explicit)
    if resolved is not inferred:
        raise EvaluationError(
            "explicit evaluation destination does not match the public task config"
        )
    return resolved


def evaluate_stage1_release(
    release_dir: Path,
    task_id: str,
    connection: DBAPIConnection,
    *,
    population: PopulationName | str = PopulationName.PRIMARY,
    database: str | None = None,
    destination: Destination | str | None = None,
    physical_container: str | None = None,
    certification_strict: bool = False,
    expected_repetitions: int = 1,
    allowed_existing_tables: Iterable[str] = (),
) -> Stage1EvaluationResult:
    """Resolve a schema-3 release and score its raw-table state.

    ``certification_strict`` adds bounded, exact typed content collection.
    Without it, Stage 1 performs count-only queries.
    """

    task = _validate_task_id(task_id)
    root = Path(release_dir)
    resolved_destination = _release_destination(root, task, destination)
    result = evaluate_stage1(
        connection,
        root / "private" / task / "answer_key",
        population=population,
        database=database,
        destination=resolved_destination,
        physical_container=physical_container,
        expected_repetitions=expected_repetitions,
        allowed_existing_tables=allowed_existing_tables,
    )
    if not certification_strict:
        return result
    return _stage1_canonical_evidence(
        root,
        task,
        connection,
        result,
        destination=resolved_destination,
        physical_container=_physical_container(
            resolved_destination, physical_container
        ),
    )


def evaluate_stage2_release(
    release_dir: Path,
    task_id: str,
    connection: DBAPIConnection,
    *,
    population: PopulationName | str = PopulationName.PRIMARY,
    database: str | None = None,
    destination: Destination | str | None = None,
    physical_container: str | None = None,
) -> Stage2EvaluationResult:
    """Resolve a schema-3 release and score its mart state."""

    task = _validate_task_id(task_id)
    root = Path(release_dir)
    resolved_destination = _release_destination(root, task, destination)
    return evaluate_stage2(
        connection,
        root / "public" / task,
        root / "private" / task / "answer_key",
        population=population,
        database=database,
        destination=resolved_destination,
        physical_container=physical_container,
    )


def evaluate_end_to_end_release(
    release_dir: Path,
    task_id: str,
    connection: DBAPIConnection,
    *,
    population: PopulationName | str = PopulationName.PRIMARY,
    database: str | None = None,
    destination: Destination | str | None = None,
    physical_container: str | None = None,
    certification_strict: bool = False,
) -> EndToEndEvaluationResult:
    """Score one combined task, skipping T when EL did not pass."""

    task = _validate_task_id(task_id)
    root = Path(release_dir)
    resolved_destination = _release_destination(root, task, destination)
    declared_marts: tuple[MartSpec, ...] = ()
    if certification_strict:
        _, declared_marts = _model_specs(
            root / "public" / task,
            root / "private" / task / "answer_key",
            database,
            resolved_destination,
        )
    stage1 = evaluate_stage1_release(
        release_dir,
        task_id,
        connection,
        population=population,
        database=database,
        destination=resolved_destination,
        physical_container=physical_container,
        certification_strict=certification_strict,
        # End-to-end verification runs after dbt, so the public data model's
        # declared marts are expected to coexist with the raw Airbyte tables.
        # Standalone Stage 1 certification keeps rejecting every extra table.
        allowed_existing_tables=(mart.name for mart in declared_marts),
    )
    if not stage1.passed or (
        certification_strict and not stage1.certification_passed
    ):
        return EndToEndEvaluationResult(stage1=stage1, stage2=None, reward=0.0)
    stage2 = evaluate_stage2_release(
        release_dir,
        task_id,
        connection,
        population=population,
        database=database or stage1.database,
        destination=resolved_destination,
        physical_container=physical_container,
    )
    return EndToEndEvaluationResult(
        stage1=stage1,
        stage2=stage2,
        reward=stage2.reward,
    )


__all__ = [
    "AIRBYTE_METADATA_COLUMNS",
    "AIRBYTE_SCHEMA",
    "CountMismatch",
    "DBAPIConnection",
    "EndToEndEvaluationResult",
    "EvaluationError",
    "Stage1EvaluationResult",
    "Stage2EvaluationResult",
    "evaluate_end_to_end_release",
    "evaluate_stage1",
    "evaluate_stage1_release",
    "evaluate_stage2",
    "evaluate_stage2_release",
    "prepare_evaluation_sql",
]

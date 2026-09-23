"""Report strict typed diagnostics beside the unchanged reward comparator.

Diagnostics use exact types and values without tolerance, case folding, NA handling, or
cross-type coercion. Results use a closed mismatch-code vocabulary and do not change
reward, loader DDL, or frozen artifacts.
"""

from __future__ import annotations

import datetime
import decimal
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel, ConfigDict

from elt_taskgen.models import (
    Backend,
    ColumnSpec,
    ColumnType,
    MartSpec,
    Row,
    Scalar,
    TableSpec,
    TaskIR,
    canonical_json,
)
from elt_taskgen.reference import solution as solution_mod
from elt_taskgen.reference.solution import MartOutputLimitError
from elt_taskgen.sql_identifiers import quote_sql_identifier
from elt_taskgen.verification import upstream_eval

#: Reported in every result that carries strict diagnostics.
STRICT_DIAGNOSTIC_VERSION = "1.0"

#: Fixed DECIMAL convention used without changing TaskIR content hashes.
#: Values outside it fail the shadow load with a stable diagnostic code.
STRICT_DECIMAL_TYPE = "DECIMAL(38,9)"

#: Identical to the legacy loader map EXCEPT the two deliberate widenings the
#: reward comparator depends on: DECIMAL keeps its digits, JSON keeps its type.
_STRICT_DUCKDB_TYPES: dict[ColumnType, str] = {
    **solution_mod._DUCKDB_TYPES,
    ColumnType.DECIMAL: STRICT_DECIMAL_TYPE,
    ColumnType.JSON: "JSON",
}


class StrictColumnFingerprint(BaseModel):
    """One output column's name and declared engine/driver type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    declared_type: str


class StrictMartDiagnostic(BaseModel):
    """The strict verdict for one mart — diagnostic only, never a reward."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mart: str
    strict_match: bool
    mismatch_code: str = ""
    submission_columns: tuple[StrictColumnFingerprint, ...] = ()
    reference_columns: tuple[StrictColumnFingerprint, ...] = ()


# Typed cell canonicalization (no folding, no tolerance, no NA tokens)

def strict_text(value: object) -> str | None:
    """Exact text of one cell; None means SQL NULL and matches only NULL."""
    if value is None:
        return None
    return strict_cell(value)[1]


def strict_cell(value: object) -> tuple[str, str]:
    """Return an exact `(type_tag, text)` representation for a driver cell.

    No cross-tag coercion occurs: numeric, textual, null, and empty values remain
    distinct. Boolean text matches the shared cell formatter.
    """
    if value is None:
        return ("null", "")
    if isinstance(value, bool):
        return ("bool", "True" if value else "False")
    if isinstance(value, int):
        return ("int", str(value))  # arbitrary precision, never floated
    if isinstance(value, float):
        return ("float", repr(value))  # 'nan'/'inf'/'-inf' for non-finite
    if isinstance(value, decimal.Decimal):
        return ("decimal", str(value))  # scale preserved: '1.50' != '1.5'
    if isinstance(value, datetime.datetime):
        tag = "timestamptz" if value.tzinfo is not None else "timestamp"
        # str(), not isoformat(): this text is compared against frozen CSV
        # gold, which spells a timestamp with a space. The tz offset survives.
        return (tag, str(value))
    if isinstance(value, datetime.date):
        return ("date", value.isoformat())
    if isinstance(value, datetime.time):
        return ("time", value.isoformat())
    if isinstance(value, (bytes, bytearray)):
        return ("bytes", bytes(value).hex())
    if isinstance(value, (list, dict)):
        return ("json", canonical_json(value))
    if isinstance(value, str):
        return ("text", value)  # verbatim: no trim, no fold
    return ("text", str(value))


def strict_sort_key(row: Sequence[object]) -> tuple:
    """Total deterministic order; a tie means byte-identical typed rows."""
    return tuple(strict_cell(value) for value in row)


# DuckDB-side strict execution (semantic scorer tier)

def describe_columns(
    con: duckdb.DuckDBPyConnection, sql: str
) -> tuple[StrictColumnFingerprint, ...]:
    """(name, full logical type) per output column of one certified query.

    This is exactly the engine type metadata the legacy path discards after
    reading column names.  Raises ``ValueError`` on any binder refusal.
    """
    try:
        rows = con.execute("DESCRIBE " + sql).fetchall()
    except Exception as exc:  # noqa: BLE001 - collapsed to a stable code
        raise ValueError("describe_failed") from exc
    return tuple(
        StrictColumnFingerprint(name=str(row[0]), declared_type=str(row[1]))
        for row in rows
    )


def fetch_strict_rows(
    con: duckdb.DuckDBPyConnection,
    mart: MartSpec,
    sql: str,
    *,
    max_rows: int,
    max_bytes: int,
) -> tuple[tuple[str, ...], list[tuple]]:
    """Fetch raw mart rows while enforcing schema, row, and byte limits.

    Streamed byte accounting matches normal execution, but raw driver values are
    retained so exact types survive strict comparison.
    """
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    cur = con.execute(sql)
    out_cols = tuple(d[0] for d in cur.description)
    expected = [c.name for c in mart.columns]
    if sorted(out_cols) != sorted(expected):
        raise ValueError(
            f"mart {mart.name!r}: strict SQL produced columns {list(out_cols)}, "
            f"expected exactly {expected}"
        )
    count = 0
    total = 0
    rows: list[tuple] = []
    while True:
        raw = cur.fetchone()
        if raw is None:
            break
        count += 1
        if count > max_rows:
            raise ValueError(
                f"mart {mart.name!r}: query produced more than the allowed "
                f"{max_rows} rows"
            )
        total += sum(len(str(value).encode("utf-8")) for value in raw)
        if total > max_bytes:
            raise MartOutputLimitError(
                f"mart {mart.name!r}: query output exceeded the allowed "
                f"{max_bytes} bytes"
            )
        rows.append(tuple(raw))
    return out_cols, rows


def strict_compare(
    mart: MartSpec,
    reference_columns: tuple[StrictColumnFingerprint, ...],
    reference_rows: list[tuple],
    submission_columns: tuple[StrictColumnFingerprint, ...],
    submission_rows: list[tuple],
) -> StrictMartDiagnostic:
    """Exact typed compare of two fetched sides; first mismatch names the code.

    Both sides were already guarded to exactly the mart's column names; the
    fingerprint tuples carry each side's output order, and rows are aligned by
    NAME into mart column order before the typed positional compare.
    """
    mart_cols = [c.name for c in mart.columns]

    def diagnostic(matched: bool, code: str) -> StrictMartDiagnostic:
        return StrictMartDiagnostic(
            mart=mart.name,
            strict_match=matched,
            mismatch_code=code,
            submission_columns=submission_columns,
            reference_columns=reference_columns,
        )

    ref_types = {f.name: f.declared_type for f in reference_columns}
    sub_types = {f.name: f.declared_type for f in submission_columns}
    if set(ref_types) != set(mart_cols) or set(sub_types) != set(mart_cols):
        return diagnostic(False, "column_names")
    if any(ref_types[name] != sub_types[name] for name in mart_cols):
        return diagnostic(False, "column_types")
    if len(reference_rows) != len(submission_rows):
        return diagnostic(False, "row_count")

    ref_index = {f.name: i for i, f in enumerate(reference_columns)}
    sub_index = {f.name: i for i, f in enumerate(submission_columns)}
    ref_aligned = sorted(
        (tuple(row[ref_index[name]] for name in mart_cols) for row in reference_rows),
        key=strict_sort_key,
    )
    sub_aligned = sorted(
        (tuple(row[sub_index[name]] for name in mart_cols) for row in submission_rows),
        key=strict_sort_key,
    )
    for ref_row, sub_row in zip(ref_aligned, sub_aligned):
        for position, name in enumerate(mart_cols):
            if strict_cell(ref_row[position]) != strict_cell(sub_row[position]):
                return diagnostic(False, "values:" + name)
    return diagnostic(True, "")


def strict_mart_diagnostic(
    con: duckdb.DuckDBPyConnection,
    mart: MartSpec,
    reference_sql: str,
    submission_sql: str,
    *,
    max_rows: int,
    max_bytes: int,
) -> StrictMartDiagnostic:
    """The full strict verdict for one mart; NEVER raises past this frame."""

    def failed(code: str, sub=(), ref=()) -> StrictMartDiagnostic:
        return StrictMartDiagnostic(
            mart=mart.name,
            strict_match=False,
            mismatch_code=code,
            submission_columns=tuple(sub),
            reference_columns=tuple(ref),
        )

    try:
        try:
            reference_columns = describe_columns(con, reference_sql)
            submission_columns = describe_columns(con, submission_sql)
        except ValueError:
            return failed("error:describe_failed")
        try:
            ref_cols, ref_rows = fetch_strict_rows(
                con, mart, reference_sql, max_rows=max_rows, max_bytes=max_bytes
            )
            sub_cols, sub_rows = fetch_strict_rows(
                con, mart, submission_sql, max_rows=max_rows, max_bytes=max_bytes
            )
        except MartOutputLimitError:
            return failed(
                "error:output_limit", submission_columns, reference_columns
            )
        except Exception:  # noqa: BLE001 - stable code, no exception text
            return failed(
                "error:query_failed", submission_columns, reference_columns
            )
        if (
            tuple(f.name for f in reference_columns) != ref_cols
            or tuple(f.name for f in submission_columns) != sub_cols
        ):
            return failed(
                "error:strict_failed", submission_columns, reference_columns
            )
        return strict_compare(
            mart, reference_columns, ref_rows, submission_columns, sub_rows
        )
    except Exception:  # noqa: BLE001 - a diagnostic bug must not alter scoring
        return failed("error:strict_failed")


# DB-API-side strict diagnostics (runtime/cloud tier)

def description_fingerprint(description: Any) -> tuple[tuple[str, str], ...]:
    """(name, str(type_code)) per cursor description column — pure capture.

    Tolerates DB-API 7-tuples and objects with ``.name``/``.type_code``, the
    same shapes ``runtime.evaluation`` already accepts for names.  Raises only
    ``ValueError``.
    """
    if (
        not isinstance(description, Sequence)
        or isinstance(description, (str, bytes, bytearray))
        or not description
    ):
        raise ValueError("cursor description is missing or malformed")
    fingerprints: list[tuple[str, str]] = []
    for item in description:
        name: Any = None
        type_code: Any = None
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            if item:
                name = item[0]
            if len(item) > 1:
                type_code = item[1]
        if name is None:
            name = getattr(item, "name", None)
        if type_code is None:
            type_code = getattr(item, "type_code", None)
        if not isinstance(name, str) or not name:
            raise ValueError("cursor description has a malformed column")
        fingerprints.append((name, "" if type_code is None else str(type_code)))
    return tuple(fingerprints)


def strict_text_compare(
    gold_csv: str,
    actual_rows: list[Row],
    actual_columns: Sequence[str] | None,
    mart: MartSpec,
) -> tuple[bool, str]:
    """Compare fetched rows with frozen CSV gold as exact text.

    No NA normalization, trimming, case folding, or numeric tolerance applies; empty
    gold cells match only `None`. Column names align case-insensitively. Frozen CSV
    cannot distinguish null from empty text or recover previously lost precision.
    """
    try:
        gold_cols, gold_rows = upstream_eval.parse_canonical_csv(gold_csv)
    except ValueError:
        return False, "error:gold_unreadable"
    if not gold_cols:
        return False, "error:gold_unreadable"
    if len(actual_rows) != len(gold_rows):
        return False, "row_count"
    if actual_columns is not None:
        actual_cols = tuple(actual_columns)
    elif actual_rows:
        actual_cols = tuple(actual_rows[0].keys())
    else:
        actual_cols = gold_cols
    lower = {c.lower(): c for c in actual_cols}
    aligned: dict[str, str] = {}
    for gold_col in gold_cols:
        actual_col = lower.get(gold_col.lower())
        if actual_col is None:
            return False, "column_names"
        aligned[gold_col] = actual_col
    actual_txt: list[dict[str, str | None]] = []
    for row in actual_rows:
        if any(aligned[gold_col] not in row for gold_col in gold_cols):
            return False, "column_names"
        actual_txt.append(
            {gold_col: strict_text(row[aligned[gold_col]]) for gold_col in gold_cols}
        )

    def row_key(row: Mapping[str, str | None]) -> tuple:
        return tuple(
            (1, "") if (v := row.get(c)) is None else (0, v) for c in gold_cols
        )

    gold_sorted = sorted(gold_rows, key=row_key)
    actual_sorted = sorted(actual_txt, key=row_key)
    for gold_row, actual_row in zip(gold_sorted, actual_sorted):
        for gold_col in gold_cols:
            if gold_row.get(gold_col) != actual_row.get(gold_col):
                return False, "values:" + gold_col
    return True, ""


# Strict shadow loader (opt-in Tier 2; runs in its own scorer connection ONLY,
# never writes a release file, never produces gold — the legacy connection,
# tables and DDL stay byte-identical, so no frozen census or digest moves)

def strict_duckdb_type(column_type: ColumnType) -> str:
    return _STRICT_DUCKDB_TYPES[column_type]


def create_strict_table(con: duckdb.DuckDBPyConnection, table: TableSpec) -> None:
    """CREATE the strictly typed shadow table for a TableSpec."""
    cols = ", ".join(
        f'{quote_sql_identifier(c.name, force=True)} {strict_duckdb_type(c.type)}'
        + ("" if c.nullable else " NOT NULL")
        for c in table.columns
    )
    relation = quote_sql_identifier(table.name, force=True)
    con.execute(f"CREATE TABLE {relation} ({cols})")


def strict_coerce_value(raw: Any, col: ColumnSpec, *, table: str) -> Any:
    """`coerce_value` for every type EXCEPT DECIMAL and JSON, which stay typed.

    DECIMAL takes no float round-trip: the exact decimal text becomes a
    ``decimal.Decimal`` and values outside the ``DECIMAL(38,9)`` convention
    fail closed.  JSON reuses the legacy text form; the typed JSON column
    casts it on insert.
    """
    if col.type is ColumnType.DECIMAL:
        if raw is None:
            return None
        if isinstance(raw, str) and raw == "":
            return None  # same NULL mapping as coerce_value on non-text columns
        if isinstance(raw, bool):
            raise ValueError(
                f"table {table!r} column {col.name!r}: boolean where decimal expected"
            )
        try:
            value = decimal.Decimal(str(raw).strip())
        except decimal.InvalidOperation as exc:
            raise ValueError(
                f"table {table!r} column {col.name!r}: cannot coerce {raw!r} "
                f"to {STRICT_DECIMAL_TYPE}"
            ) from exc
        if not value.is_finite():
            raise ValueError(
                f"table {table!r} column {col.name!r}: non-finite decimal {raw!r}"
            )
        _sign, digits, exponent = value.as_tuple()
        scale = -int(exponent) if int(exponent) < 0 else 0
        integer_digits = len(digits) + int(exponent)
        if scale > 9 or integer_digits > 38 - 9:
            raise ValueError(
                f"table {table!r} column {col.name!r}: {raw!r} does not fit "
                f"{STRICT_DECIMAL_TYPE}"
            )
        return value
    return solution_mod.coerce_value(raw, col, table=table)


def _strict_sql_literal(value: Any) -> str:
    """Literal spelling for strict-coerced cells; Decimal stays fixed-point."""
    if isinstance(value, decimal.Decimal):
        return format(value, "f")  # exact digits, no exponent, no float parse
    return solution_mod._sql_literal(value)


def _strict_insert_rows(
    con: duckdb.DuckDBPyConnection, table: TableSpec, rows: list[dict[str, Any]]
) -> int:
    """`_insert_rows` with strict coercion + literals; same batching."""
    if not rows:
        return 0
    quoted = ", ".join(
        quote_sql_identifier(c.name, force=True) for c in table.columns
    )
    relation = quote_sql_identifier(table.name, force=True)
    prefix = f"INSERT INTO {relation} ({quoted}) VALUES "
    inserted = 0
    batch_rows = solution_mod._INSERT_BATCH_ROWS
    for start in range(0, len(rows), batch_rows):
        batch = rows[start:start + batch_rows]
        values = ", ".join(
            "("
            + ", ".join(
                _strict_sql_literal(
                    strict_coerce_value(row.get(c.name), c, table=table.name)
                )
                for c in table.columns
            )
            + ")"
            for row in batch
        )
        con.execute(prefix + values)
        inserted += len(batch)
    return inserted


def _staged_postgres_rows(
    con: duckdb.DuckDBPyConnection, table: TableSpec, artifact: Path
) -> list[dict[str, Any]]:
    """Stage PostgreSQL insert literals as exact text rows.

    An all-text staging table prevents database casts from rounding decimal literals
    before `strict_coerce_value` validates them.
    """
    cols = ", ".join(
        f'{quote_sql_identifier(c.name, force=True)} VARCHAR'
        for c in table.columns
    )
    relation = quote_sql_identifier(table.name, force=True)
    con.execute(f"CREATE TABLE {relation} ({cols})")
    try:
        solution_mod._load_postgres_sql(con, table, artifact)
        names = [c.name for c in table.columns]
        quoted = ", ".join(
            quote_sql_identifier(name, force=True) for name in names
        )
        fetched = con.execute(f"SELECT {quoted} FROM {relation}").fetchall()
        return [dict(zip(names, row)) for row in fetched]
    finally:
        con.execute(f"DROP TABLE IF EXISTS {relation}")


def load_sources_duckdb_strict(
    task: TaskIR, rendered_dir: Path, con: duckdb.DuckDBPyConnection
) -> dict[str, int]:
    """Shadow E+L: the SAME rendered artifacts into strictly typed tables.

    Walks the legacy loader's readers unchanged; only the DDL map and the
    DECIMAL/JSON coercion differ.  Fails closed like the legacy loader.
    """
    if not rendered_dir.is_dir():
        raise FileNotFoundError(f"rendered dir does not exist: {rendered_dir}")
    counts: dict[str, int] = {}
    for table in task.tables:
        backend = task.backend_for(table.name).backend
        artifact = solution_mod.find_rendered_artifact(task, rendered_dir, table.name)
        if backend is Backend.POSTGRES:
            staged = _staged_postgres_rows(con, table, artifact)
            create_strict_table(con, table)
            _strict_insert_rows(con, table, staged)
            relation = quote_sql_identifier(table.name, force=True)
            (count,) = con.execute(
                f"SELECT COUNT(*) FROM {relation}"
            ).fetchone()
            counts[table.name] = int(count)
            continue
        create_strict_table(con, table)
        if backend is Backend.MONGODB:
            _strict_insert_rows(con, table, solution_mod._read_jsonl(artifact))
        elif backend is Backend.REST:
            _strict_insert_rows(con, table, solution_mod._read_rest(artifact))
        elif backend is Backend.S3:
            _strict_insert_rows(con, table, solution_mod._read_s3(artifact))
        elif backend is Backend.FILES:
            _strict_insert_rows(con, table, solution_mod._read_csv(artifact))
        else:  # pragma: no cover
            raise ValueError(f"unknown backend {backend!r}")
        relation = quote_sql_identifier(table.name, force=True)
        (count,) = con.execute(f"SELECT COUNT(*) FROM {relation}").fetchone()
        counts[table.name] = int(count)
    return counts

"""Build trusted source loaders and compile mart plans to DuckDB SQL.

``validate_plan`` succeeds exactly when ``compile_plan_sql`` does.
"""

from __future__ import annotations

import csv
import datetime
import decimal
import json
import math
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel, ConfigDict
from sqlglot import exp, parse

from elt_taskgen.generation.mart_plan import (
    AGGREGATE_FAMILY_KINDS,
    PROJECTION_KINDS,
    RESERVED_AGGREGATE_DETAIL_KEYS,
    measure_items,
    op_problems,
    quote,
)
from elt_taskgen.models import (
    Backend,
    ColumnSpec,
    ColumnType,
    JoinType,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    ReferenceSolution,
    Row,
    Scalar,
    TableSpec,
    TaskIR,
    canonical_json,
)
from elt_taskgen.sql_identifiers import quote_sql_identifier


class PlanCompilationError(ValueError):
    """A MartPlan is not structured enough to compile without guessing."""


class MartOutputLimitError(ValueError):
    """A mart query's materialized output exceeded the caller's byte budget."""


class LoadedSources(BaseModel):
    """Row counts loaded per table by the trusted Extract+Load."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    counts: dict[str, int]


# DDL + value coercion (logical ColumnType -> DuckDB)

_DUCKDB_TYPES: dict[ColumnType, str] = {
    ColumnType.INTEGER: "INTEGER",
    ColumnType.BIGINT: "BIGINT",
    # DECIMAL is loaded as DOUBLE: the reward comparator is numeric-coercing and
    # tolerance-based, so a declared precision/scale adds risk, not strictness.
    ColumnType.FLOAT: "DOUBLE",
    ColumnType.DECIMAL: "DOUBLE",
    ColumnType.TEXT: "VARCHAR",
    ColumnType.BOOLEAN: "BOOLEAN",
    ColumnType.DATE: "DATE",
    ColumnType.TIMESTAMP: "TIMESTAMP",
    ColumnType.JSON: "VARCHAR",
}

_TRUE_STRINGS = frozenset({"true", "t", "1", "yes"})
_FALSE_STRINGS = frozenset({"false", "f", "0", "no"})


def duckdb_type(column_type: ColumnType) -> str:
    return _DUCKDB_TYPES[column_type]


def create_table(con: duckdb.DuckDBPyConnection, table: TableSpec) -> None:
    """CREATE the canonical DuckDB table for a TableSpec (names/types from IR)."""
    cols = ", ".join(
        f'{quote_sql_identifier(c.name, force=True)} {duckdb_type(c.type)}'
        + ("" if c.nullable else " NOT NULL")
        for c in table.columns
    )
    relation = quote_sql_identifier(table.name, force=True)
    con.execute(f"CREATE TABLE {relation} ({cols})")


def coerce_value(raw: Any, col: ColumnSpec, *, table: str) -> Scalar:
    """Coerce one raw rendered value to the column's logical type; fail closed."""
    if raw is None:
        return None
    if isinstance(raw, str) and raw == "" and col.type is not ColumnType.TEXT:
        # JSON empty strings become null except for text columns; CSV is handled earlier.
        return None
    try:
        if col.type in (ColumnType.INTEGER, ColumnType.BIGINT):
            if isinstance(raw, bool):
                raise ValueError("boolean where integer expected")
            if isinstance(raw, int):
                return raw
            if isinstance(raw, float):
                if raw.is_integer():
                    return int(raw)
                raise ValueError(f"non-integral float {raw!r}")
            text = str(raw).strip()
            try:
                return int(text)
            except ValueError:
                f = float(text)
                if f.is_integer():
                    return int(f)
                raise
        if col.type in (ColumnType.FLOAT, ColumnType.DECIMAL):
            if isinstance(raw, bool):
                raise ValueError("boolean where numeric expected")
            return float(raw)
        if col.type is ColumnType.BOOLEAN:
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, int) and raw in (0, 1):
                return bool(raw)
            text = str(raw).strip().lower()
            if text in _TRUE_STRINGS:
                return True
            if text in _FALSE_STRINGS:
                return False
            raise ValueError(f"unrecognized boolean {raw!r}")
        if col.type in (ColumnType.DATE, ColumnType.TIMESTAMP):
            # Pass through as text; DuckDB casts on insert (typed column).
            return str(raw)
        if col.type is ColumnType.JSON:
            return raw if isinstance(raw, str) else canonical_json(raw)
        return raw if isinstance(raw, str) else str(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"table {table!r} column {col.name!r}: cannot coerce {raw!r} "
            f"to {col.type.value}: {exc}"
        ) from exc


def _sql_literal(value: Scalar) -> str:
    """Render a coerced cell as a DuckDB literal.

    Floats use exponent notation for correct binary rounding. Text doubles
    quotes and represents NUL bytes with ``chr(0)`` concatenation.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isfinite(value):
            text = repr(value)
            return text if "e" in text else text + "e0"
        if math.isnan(value):
            return "'NaN'::DOUBLE"
        return "'Infinity'::DOUBLE" if value > 0 else "'-Infinity'::DOUBLE"
    text = str(value)
    if "\x00" in text:
        return " || chr(0) || ".join(
            "'" + part.replace("'", "''") + "'" for part in text.split("\x00")
        )
    return "'" + text.replace("'", "''") + "'"


#: Rows per multi-row INSERT. Bounded for parser sanity; no semantic effect.
_INSERT_BATCH_ROWS = 500


def _insert_rows(
    con: duckdb.DuckDBPyConnection, table: TableSpec, rows: list[dict[str, Any]]
) -> int:
    """Insert schema columns in coerced literal batches."""
    if not rows:
        return 0
    quoted = ", ".join(
        quote_sql_identifier(c.name, force=True) for c in table.columns
    )
    relation = quote_sql_identifier(table.name, force=True)
    prefix = f"INSERT INTO {relation} ({quoted}) VALUES "
    inserted = 0
    for start in range(0, len(rows), _INSERT_BATCH_ROWS):
        batch = rows[start:start + _INSERT_BATCH_ROWS]
        values = ", ".join(
            "("
            + ", ".join(
                _sql_literal(coerce_value(row.get(c.name), c, table=table.name))
                for c in table.columns
            )
            + ")"
            for row in batch
        )
        con.execute(prefix + values)
        inserted += len(batch)
    return inserted


# Rendered-artifact discovery + per-backend readers

def _artifact_candidates(backend: Backend, rendered_dir: Path, table: str) -> list[Path]:
    b = rendered_dir / backend.value
    if backend is Backend.POSTGRES:
        return [b / f"{table}.sql", b / table / "load.sql", b / table / f"{table}.sql"]
    if backend is Backend.MONGODB:
        return [b / f"{table}.jsonl", b / table / f"{table}.jsonl", b / table / "docs.jsonl"]
    if backend is Backend.REST:
        return [b / table, b / f"{table}.json"]
    if backend is Backend.S3:
        return [b / table, b / f"{table}.jsonl"]
    if backend is Backend.FILES:
        return [b / f"{table}.csv", b / table / f"{table}.csv"]
    raise ValueError(f"unknown backend {backend!r}")  # pragma: no cover


def find_rendered_artifact(task: TaskIR, rendered_dir: Path, table: str) -> Path:
    """Locate the rendered source artifact for one table; fail closed if absent."""
    backend = task.backend_for(table).backend
    candidates = _artifact_candidates(backend, rendered_dir, table)
    for cand in candidates:
        if cand.exists():
            return cand
    raise FileNotFoundError(
        f"no rendered {backend.value} artifact for table {table!r} under "
        f"{rendered_dir} (looked at: {', '.join(str(c) for c in candidates)})"
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{lineno}: jsonl line is not an object")
            rows.append(obj)
    return rows


_PAGE_ROW_KEYS = ("data", "results", "items", "records", "rows")


def _rows_from_page(payload: Any, source: Path) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in _PAGE_ROW_KEYS:
            if key in payload and isinstance(payload[key], list):
                rows = payload[key]
                break
        else:
            raise ValueError(
                f"{source}: REST page object has none of the row keys {_PAGE_ROW_KEYS}"
            )
    else:
        raise ValueError(f"{source}: REST page is neither a list nor an object")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{source}: REST page row is not an object")
    return rows


def _read_rest(path: Path) -> list[dict[str, Any]]:
    """Paginated REST fixture dir (page_0001.json ... + index.json) or one file."""
    if path.is_file():
        return _rows_from_page(json.loads(path.read_text(encoding="utf-8")), path)
    index_path = path / "index.json"
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        pages = index.get("pages") if isinstance(index, dict) else None
        if not isinstance(pages, list):
            raise ValueError(f"{index_path}: expected an object with a 'pages' list")
        page_paths = [path / str(p) for p in pages]
    else:
        page_paths = sorted(path.glob("page_*.json"))
    if not page_paths and not index_path.exists():
        raise FileNotFoundError(f"{path}: REST fixture dir has no index.json or page_*.json")
    rows: list[dict[str, Any]] = []
    root = path.resolve()
    for page_path in page_paths:
        resolved = page_path.resolve()
        if page_path.is_symlink() or (
            resolved != root and root not in resolved.parents
        ):
            raise ValueError(
                f"{page_path}: REST page escapes its fixture directory"
            )
        if not page_path.exists():
            raise FileNotFoundError(f"{page_path}: page listed in index.json is missing")
        rows.extend(
            _rows_from_page(
                json.loads(resolved.read_text(encoding="utf-8")), resolved
            )
        )
    return rows


def _read_s3(path: Path) -> list[dict[str, Any]]:
    """S3 jsonl layout: one .jsonl file, or a prefix dir of .jsonl objects."""
    if path.is_file():
        return _read_jsonl(path)
    parts = sorted(path.rglob("*.jsonl"))
    if not parts:
        raise FileNotFoundError(f"{path}: S3 layout contains no .jsonl objects")
    rows: list[dict[str, Any]] = []
    root = path.resolve()
    for part in parts:
        resolved = part.resolve()
        if part.is_symlink() or (
            resolved != root and root not in resolved.parents
        ):
            raise ValueError(f"{part}: S3 object escapes its fixture prefix")
        rows.extend(_read_jsonl(resolved))
    return rows


def _read_csv(path: Path) -> list[dict[str, Any]]:
    """Read a FILES CSV, interpreting every empty field as null."""
    with path.open("r", encoding="utf-8", newline="") as fh:
        return [
            {k: (None if v == "" else v) for k, v in row.items()}
            for row in csv.DictReader(fh)
        ]


#: Statement types skipped in postgres load SQL; anything else not an INSERT aborts.
_SKIPPABLE_STATEMENTS = tuple(
    t
    for t in (
        getattr(exp, name, None)
        for name in ("Create", "Drop", "Alter", "Transaction", "Commit", "Rollback", "Set", "Comment", "Pragma")
    )
    if isinstance(t, type)
)


def _load_postgres_sql(
    con: duckdb.DuckDBPyConnection, table: TableSpec, path: Path
) -> int:
    """Execute translated INSERTs; reject non-transaction, non-CREATE statements."""
    text = path.read_text(encoding="utf-8")
    inserted = 0
    for statement in parse(text, read="postgres"):
        if statement is None:
            continue
        if isinstance(statement, exp.Insert):
            con.execute(statement.sql(dialect="duckdb"))
            inserted += 1
            continue
        if isinstance(statement, _SKIPPABLE_STATEMENTS):
            continue
        raise ValueError(
            f"{path}: unsupported statement in postgres load SQL for table "
            f"{table.name!r}: {statement.sql(dialect='postgres')[:120]!r}"
        )
    return inserted


# Trusted Extract + Load

def _ingest_table(
    con: duckdb.DuckDBPyConnection, task: TaskIR, rendered_dir: Path, table: TableSpec
) -> None:
    """Ingest one rendered backend artifact into the EXISTING table for it."""
    backend = task.backend_for(table.name).backend
    artifact = find_rendered_artifact(task, rendered_dir, table.name)
    if backend is Backend.POSTGRES:
        _load_postgres_sql(con, table, artifact)
    elif backend is Backend.MONGODB:
        _insert_rows(con, table, _read_jsonl(artifact))
    elif backend is Backend.REST:
        _insert_rows(con, table, _read_rest(artifact))
    elif backend is Backend.S3:
        _insert_rows(con, table, _read_s3(artifact))
    elif backend is Backend.FILES:
        _insert_rows(con, table, _read_csv(artifact))
    else:  # pragma: no cover
        raise ValueError(f"unknown backend {backend!r}")


def load_sources_duckdb(
    task: TaskIR, rendered_dir: Path, con: duckdb.DuckDBPyConnection
) -> LoadedSources:
    """Load all rendered artifacts into exact-name DuckDB tables, failing closed."""
    if not rendered_dir.is_dir():
        raise FileNotFoundError(f"rendered dir does not exist: {rendered_dir}")
    counts: dict[str, int] = {}
    for table in task.tables:
        create_table(con, table)
        _ingest_table(con, task, rendered_dir, table)
        relation = quote_sql_identifier(table.name, force=True)
        (count,) = con.execute(f"SELECT COUNT(*) FROM {relation}").fetchone()
        counts[table.name] = int(count)
    return LoadedSources(counts=counts)


def append_sources_duckdb(
    task: TaskIR, rendered_dir: Path, con: duckdb.DuckDBPyConnection
) -> LoadedSources:
    """Model one ``full_refresh_append`` sync and return post-append counts.

    This specifies data outcomes, not Airbyte connector certification.
    """
    if not rendered_dir.is_dir():
        raise FileNotFoundError(f"rendered dir does not exist: {rendered_dir}")
    counts: dict[str, int] = {}
    for table in task.tables:
        _ingest_table(con, task, rendered_dir, table)
        relation = quote_sql_identifier(table.name, force=True)
        (count,) = con.execute(f"SELECT COUNT(*) FROM {relation}").fetchone()
        counts[table.name] = int(count)
    return LoadedSources(counts=counts)


# MartPlan -> DuckDB SQL compiler (structured plans only; never guesses)

#: Reserved details keys; every OTHER key on an AGGREGATE op is alias -> expression.
_RESERVED_DETAIL_KEYS = RESERVED_AGGREGATE_DETAIL_KEYS

_JOIN_SQL: dict[JoinType, str] = {
    JoinType.INNER: "INNER JOIN",
    JoinType.LEFT: "LEFT JOIN",
    JoinType.RIGHT: "RIGHT JOIN",
    JoinType.FULL: "FULL OUTER JOIN",
    JoinType.CROSS: "CROSS JOIN",
}


def _one_table(op: MartOp, mart: str, problems: list[str]) -> str | None:
    """The op's single table, or None (with a recorded problem) if it has none."""
    if len(op.tables) != 1:
        problems.append(
            f"mart {mart!r}: {op.kind.value} op must name exactly one table, got {op.tables!r}"
        )
        return None
    return op.tables[0]


def _resolve(
    env: dict[str, str], name: str, mart: str, op: MartOp, problems: list[str]
) -> str | None:
    """Resolve a relation against bindings from preceding plan operations."""
    resolved = env.get(name)
    if resolved is None:
        problems.append(
            f"mart {mart!r}: {op.kind.value} op references unknown relation {name!r}"
        )
    return resolved


def _op_body(
    op: MartOp, mart_name: str, env: dict[str, str], problems: list[str]
) -> str | None:
    """Compile one non-source operation, recording problems on refusal."""
    if op.kind is MartOpKind.FILTER:
        t = _one_table(op, mart_name, problems)
        if t is None:
            return None
        if not op.predicate:
            problems.append(f"mart {mart_name!r}: filter op on {t!r} has no predicate")
            return None
        rel = _resolve(env, t, mart_name, op, problems)
        alias = quote_sql_identifier(t)
        return None if rel is None else (
            f"SELECT * FROM {rel} AS {alias} WHERE {op.predicate}"
        )
    if op.kind is MartOpKind.DEDUPE:
        t = _one_table(op, mart_name, problems)
        if t is None:
            return None
        rel = _resolve(env, t, mart_name, op, problems)
        if rel is None:
            return None
        # QUOTED, like every identifier this compiler emits: source column names
        # are VENDORED TEXT and may collide with reserved words (e.g. `cast`).
        cols = ", ".join(quote(c) for c in op.columns) if op.columns else "*"
        return f"SELECT DISTINCT {cols} FROM {rel} AS {quote_sql_identifier(t)}"
    if op.kind in AGGREGATE_FAMILY_KINDS:
        # These aggregate kinds differ in the expression, not compiler structure.
        t = _one_table(op, mart_name, problems)
        if t is None:
            return None
        group_by = op.details.get("group_by", "").strip()
        measures = measure_items(op)
        if not group_by or not measures:
            problems.append(
                f"mart {mart_name!r}: aggregate op on {t!r} needs structured "
                "details: 'group_by' plus at least one 'alias: expression' measure"
            )
            return None
        rel = _resolve(env, t, mart_name, op, problems)
        if rel is None:
            return None
        # Measure aliases QUOTED too: an alias is a mart column name (vendored
        # text), and an unquoted `end`/`cast`/`order` is a ParserException.
        select = group_by + ", " + ", ".join(
            f"{expr} AS {quote(alias)}" for alias, expr in measures
        )
        return (
            f"SELECT {select} FROM {rel} AS {quote_sql_identifier(t)} "
            f"GROUP BY {group_by}"
        )
    if op.kind is MartOpKind.JOIN:
        if len(op.tables) != 2:
            problems.append(
                f"mart {mart_name!r}: join op must name exactly two tables, got {op.tables!r}"
            )
            return None
        left, right = op.tables
        select = op.details.get("select", "").strip()
        if not select:
            problems.append(
                f"mart {mart_name!r}: join op {left!r}x{right!r} needs an explicit "
                "details['select'] list (SELECT * across a join is ambiguous)"
            )
            return None
        if op.join_type is None:  # pragma: no cover - enforced by MartOp validator
            problems.append(f"mart {mart_name!r}: join op {left!r}x{right!r} has no join type")
            return None
        join_sql = _JOIN_SQL[op.join_type]
        left_rel = _resolve(env, left, mart_name, op, problems)
        right_rel = _resolve(env, right, mart_name, op, problems)
        if left_rel is None or right_rel is None:
            return None
        if op.join_type is JoinType.CROSS:
            if op.predicate:
                problems.append(
                    f"mart {mart_name!r}: cross join must not carry a predicate"
                )
                return None
            return (
                f"SELECT {select} FROM {left_rel} AS {quote_sql_identifier(left)} "
                f"{join_sql} {right_rel} AS {quote_sql_identifier(right)}"
            )
        if not op.predicate:
            problems.append(
                f"mart {mart_name!r}: {op.join_type.value} join needs a predicate"
            )
            return None
        return (
            f"SELECT {select} FROM {left_rel} AS {quote_sql_identifier(left)} "
            f"{join_sql} {right_rel} AS {quote_sql_identifier(right)} "
            f"ON {op.predicate}"
        )
    if op.kind is MartOpKind.EXTREMA:
        # QUALIFY isolates argmax selection from the dropped-filter mutant.
        t = _one_table(op, mart_name, problems)
        if t is None:
            return None
        rel = _resolve(env, t, mart_name, op, problems)
        if rel is None:
            return None
        select = op.details.get("select", "").strip()
        partition_by = op.details.get("partition_by", "").strip()
        extrema_order_by = op.details.get("order_by", "").strip()
        return (
            f"SELECT {select} FROM {rel} AS {quote_sql_identifier(t)} "
            f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {partition_by} "
            f"ORDER BY {extrema_order_by}) = 1"
        )
    if op.kind in PROJECTION_KINDS:
        # derive / window / conditional / ratio all compile to a projection with
        # an explicit select list; op_problems already certified the frame, the
        # CASE ELSE and the NULLIF/CAST/ROUND guards.
        t = _one_table(op, mart_name, problems)
        if t is None:
            return None
        select = op.details.get("select", "").strip()
        if not select:
            problems.append(
                f"mart {mart_name!r}: {op.kind.value} op on {t!r} needs an explicit "
                "details['select'] list (COALESCE defaults / window exprs spelled out)"
            )
            return None
        rel = _resolve(env, t, mart_name, op, problems)
        return None if rel is None else (
            f"SELECT {select} FROM {rel} AS {quote_sql_identifier(t)}"
        )
    if op.kind is MartOpKind.UNION:
        if len(op.tables) != 2:
            problems.append(f"mart {mart_name!r}: union op must name exactly two tables")
            return None
        left, right = op.tables
        mode = op.details.get("mode", "all").lower()
        if mode not in ("all", "distinct"):
            problems.append(f"mart {mart_name!r}: union mode must be 'all' or 'distinct'")
            return None
        left_rel = _resolve(env, left, mart_name, op, problems)
        right_rel = _resolve(env, right, mart_name, op, problems)
        if left_rel is None or right_rel is None:
            return None
        keyword = "UNION ALL" if mode == "all" else "UNION"
        return f"SELECT * FROM {left_rel} {keyword} SELECT * FROM {right_rel}"
    problems.append(  # pragma: no cover - all fourteen kinds have a branch
        f"mart {mart_name!r}: cannot compile op kind {op.kind.value!r}"
    )
    return None


def _compile_plan(
    task: TaskIR, mart: MartSpec, plan: MartPlan
) -> tuple[str | None, list[str]]:
    """Compile in one pass, returning SQL only when no problems were recorded."""
    problems: list[str] = []
    env: dict[str, str] = {
        t.name: quote_sql_identifier(t.name, force=True) for t in task.tables
    }
    ctes: list[tuple[str, str]] = []
    order_by: list[str] = []
    current: str | None = None
    for i, op in enumerate(plan.ops):
        step = f"step_{i}"
        if op.kind is MartOpKind.TIE_BREAK:
            if not op.columns:
                problems.append(
                    f"mart {mart.name!r}: tie_break op must list its order columns"
                )
            else:
                order_by = list(op.columns)
            continue
        if op.kind is MartOpKind.SOURCE:
            t = _one_table(op, mart.name, problems)
            if t is not None:
                resolved = _resolve(env, t, mart.name, op, problems)
                if resolved is not None:
                    current = resolved
            continue
        contract = op_problems(op, loc=f"mart {mart.name!r} op[{i}]")
        problems.extend(contract)
        body = None if contract else _op_body(op, mart.name, env, problems)
        if body is not None:
            ctes.append((step, body))
        bind = op.details.get("name") or (op.tables[0] if op.tables else None)
        if not bind:
            problems.append(
                f"mart {mart.name!r}: {op.kind.value} op needs a details['name'] "
                "binding (it names no table)"
            )
        else:
            env[bind] = step
        current = step
    if current is None:
        # A PLAN-level failure, like unknown-relation above: no single op is
        # malformed, the plan as a whole simply produces nothing.
        problems.append(f"mart {mart.name!r}: plan produced no relation")

    mart_cols = [c.name for c in mart.columns]
    order_cols = order_by or list(mart.key_columns)
    unknown_order = [c for c in order_cols if c not in mart_cols]
    if unknown_order:
        problems.append(
            f"mart {mart.name!r}: order columns {unknown_order} are not mart columns"
        )
    if problems:
        return None, problems

    total_order = order_cols + [c for c in mart_cols if c not in order_cols]
    # Quote vendored names and cast at the boundary to enforce the declared schema.
    projection = ", ".join(
        f"CAST({quote(column.name)} AS {duckdb_type(column.type)}) "
        f"AS {quote(column.name)}"
        for column in mart.columns
    )
    final = (
        f"SELECT {projection} FROM {current}\n"
        f"ORDER BY {', '.join(quote(c) for c in total_order)}"
    )
    if not ctes:
        sql = final
    else:
        cte_sql = ",\n".join(f"{name} AS (\n    {body}\n)" for name, body in ctes)
        sql = f"WITH {cte_sql}\n{final}"
    # EXPLAIN binds the statement against empty sandboxed task tables.
    rejected = _duckdb_dry_run_problem(task, mart, sql)
    if rejected:
        return None, [rejected]
    return sql, []


def _duckdb_dry_run_problem(task: TaskIR, mart: MartSpec, sql: str) -> str:
    """Return an empty string if sandboxed DuckDB binds the SQL, else the error."""
    from elt_taskgen.reference.duckdb_sandbox import sandboxed_memory_connection

    con = sandboxed_memory_connection()
    try:
        for table in task.tables:
            create_table(con, table)
        con.execute(f"EXPLAIN {sql}")
    except duckdb.Error as exc:
        detail = " ".join(str(exc).split())
        return f"mart {mart.name!r}: compiled SQL rejected by DuckDB: {detail}"
    finally:
        con.close()
    return ""


def plan_compilation_problems(
    task: TaskIR, mart: MartSpec, *, plan: MartPlan | None = None
) -> list[str]:
    """Return all plan-level and DuckDB binding reasons compilation would fail.

    ``plan`` may override ``mart.plan`` to probe a candidate.
    """
    return _compile_plan(task, mart, plan if plan is not None else mart.plan)[1]


def compile_plan_sql(task: TaskIR, mart: MartSpec) -> str:
    """Compile a plan to totally ordered SQL or raise ``PlanCompilationError``."""
    sql, problems = _compile_plan(task, mart, mart.plan)
    if problems:
        raise PlanCompilationError("; ".join(problems))
    assert sql is not None  # _compile_plan returns SQL exactly when it is clean
    return sql


def build_reference(task: TaskIR) -> ReferenceSolution:
    """Return the complete explicit or compiled reference solution."""
    existing = task.reference
    sql_by_mart: dict[str, str] = dict(existing.sql_by_mart) if existing else {}
    missing = [m for m in task.marts if m.name not in sql_by_mart]
    for mart in missing:
        sql_by_mart[mart.name] = compile_plan_sql(task, mart)
    if existing is not None and not missing:
        return existing
    if existing is not None:
        return existing.model_copy(
            update={
                "sql_by_mart": sql_by_mart,
                "provenance": existing.provenance
                + " + marts "
                + ", ".join(m.name for m in missing)
                + " compiled from MartPlan by reference/solution.py",
            }
        )
    return ReferenceSolution(
        implementation_id=f"{task.task_id}__plan_compiled",
        dialect="duckdb",
        sql_by_mart=sql_by_mart,
        load_notes=(
            "Trusted E+L: reference/solution.py load_sources_duckdb ingests every "
            "rendered backend artifact into DuckDB tables named after the TaskIR tables."
        ),
        provenance="Compiled from MartPlan by reference/solution.py",
        version="1",
    )


def attach_reference(task: TaskIR) -> TaskIR:
    """Attach a complete reference so its SQL participates in the content hash."""
    return task.model_copy(update={"reference": build_reference(task)})


# DuckDB result coercion (shared with runner)

def to_scalar(value: Any) -> Scalar:
    """Coerce a DuckDB result cell to the IR Scalar domain, deterministically."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        # str(), not isoformat(): a submission's cell reaches the comparator as
        # str(value), which separates a timestamp's date and time with a space.
        # Gold has to spell it the same way or every timestamp column
        # mismatches as text.
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, (list, dict)):
        return canonical_json(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _row_size(row: Row) -> int:
    """Measure a row using the scorer's per-cell UTF-8 accounting."""
    return sum(len(str(value).encode("utf-8")) for value in row.values())


def execute_mart(
    con: duckdb.DuckDBPyConnection,
    mart: MartSpec,
    sql: str,
    *,
    max_rows: int | None = None,
    max_bytes: int | None = None,
) -> list[Row]:
    """Execute a mart with exact output-column and optional materialization bounds."""
    cur = con.execute(sql)
    out_cols = [d[0] for d in cur.description]
    expected = [c.name for c in mart.columns]
    if sorted(out_cols) != sorted(expected):
        raise ValueError(
            f"mart {mart.name!r}: reference SQL produced columns {out_cols}, "
            f"expected exactly {expected}"
        )
    if max_rows is not None and max_rows < 1:
        raise ValueError("max_rows must be positive")
    if max_bytes is not None and max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if max_rows is None and max_bytes is None:
        raw_rows = cur.fetchall()
        rows: list[Row] = []
        for raw in raw_rows:
            by_name = dict(zip(out_cols, raw))
            rows.append({name: to_scalar(by_name[name]) for name in expected})
        return rows
    # Limited callers stream one row at a time so an oversized result is
    # rejected while it is being fetched, bounding Python-side
    # materialization to the budget plus a single row.
    count = 0
    total = 0
    streamed: list[Row] = []
    while True:
        raw = cur.fetchone()
        if raw is None:
            break
        count += 1
        if max_rows is not None and count > max_rows:
            raise ValueError(
                f"mart {mart.name!r}: query produced more than the allowed "
                f"{max_rows} rows"
            )
        by_name = dict(zip(out_cols, raw))
        row = {name: to_scalar(by_name[name]) for name in expected}
        if max_bytes is not None:
            total += _row_size(row)
            if total > max_bytes:
                raise MartOutputLimitError(
                    f"mart {mart.name!r}: query output exceeded the allowed "
                    f"{max_bytes} bytes"
                )
        streamed.append(row)
    return streamed

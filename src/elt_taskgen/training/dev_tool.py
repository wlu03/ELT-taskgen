"""Provide bounded, read-only DuckDB tools for DEVELOPMENT data.

Queries are limited to 200 rows and 16 KiB on a locked, external-access-disabled
connection resolved by exact path. Workers receive only public ``TaskIR``, the
development warehouse path, and candidate SQL, and scrub their environment.
No gold, credentials, hidden populations, or oracle paths are exposed.
"""

from __future__ import annotations

import multiprocessing
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import duckdb
from sqlglot import exp, parse

from elt_taskgen.models import PopulationName, TaskIR
from elt_taskgen.reference.runner import rendered_dir
from elt_taskgen.review.session import (
    ForbiddenArgument,
    SandboxFault,
    ToolDeadlineExceeded,
    ToolHarnessFault,
)
from elt_taskgen.review.tools.projection import MAX_DEV_ROWS, DevRows
from elt_taskgen.semantic.models import SemanticLimits

__all__ = [
    "DEV_WAREHOUSE_BASENAME",
    "DEV_QUERY_DEADLINE_S",
    "DRY_RUN_DEADLINE_S",
    "EXTERNAL_FUNCTIONS",
    "MART_DEV_DEADLINE_S",
    "MAX_DEV_QUERY_BYTES",
    "DevQueryError",
    "assert_development_warehouse",
    "dev_warehouse_path",
    "dry_run_in_worker",
    "materialize_dev_warehouse",
    "run_dev_query",
    "run_marts_dev_in_worker",
    "spawned_env_survivors",
]

DEV = PopulationName.DEVELOPMENT

#: Reward populations exclude the solver-visible development warehouse.
_HIDDEN_POPULATION_NAMES: frozenset[str] = frozenset(
    p.value for p in PopulationName if p is not DEV
)
_FORBIDDEN_PATH_PARTS: frozenset[str] = _HIDDEN_POPULATION_NAMES | {
    "answer_key",
    "gold",
    "oracle",
    "private",
}

#: The DEVELOPMENT warehouse's canonical basename.  The file lives in a
#: task-partitioned scratch tree, never beside the immutable population
#: artifacts it is derived from.
DEV_WAREHOUSE_BASENAME = "dev_warehouse.duckdb"

#: Disposable namespace outside the immutable populations tree.
_DEV_SCRATCH_DIRNAME = ".elt-taskgen-scratch"

#: Per-tool deadlines. The query child expires before the harness wall so a
#: runaway query remains a measured execution timeout.
DEV_QUERY_DEADLINE_S = 9.0
DRY_RUN_DEADLINE_S = 10.0
MART_DEV_DEADLINE_S = 60.0

#: `dev_query` output bytes cap (the sibling's 16 KiB), enforced by dropping
#: trailing rows so a wide DEVELOPMENT table still returns a bounded page.
MAX_DEV_QUERY_BYTES = 16 * 1024

#: The sibling's external-access function allowlist (`duckdb_tool.py`): a
#: query that names one, or any `read_*` / `scan_*` function, is refused.
EXTERNAL_FUNCTIONS: frozenset[str] = frozenset(
    {
        "delta_scan",
        "excel_scan",
        "getenv",
        "glob",
        "httpfs_scan",
        "iceberg_scan",
        "parquet_scan",
        "postgres_scan",
        "query",
        "query_table",
        "read_blob",
        "read_csv",
        "read_csv_auto",
        "read_json",
        "read_json_auto",
        "read_ndjson",
        "read_parquet",
        "read_text",
        "sqlite_scan",
    }
)

#: DuckDB functions that expose host state. Development rows are visible;
#: paths, settings, secrets, and platform details are not.
_CATALOG_FUNCTION_PREFIXES: tuple[str, ...] = ("duckdb_", "pragma_")
_CATALOG_FUNCTIONS: frozenset[str] = frozenset(
    {
        "current_setting",
        "current_database",
        "current_schema",
        "current_schemas",
        "current_catalog",
        "current_query",
        "current_user",
        "current_version",
        "session_user",
        "user",
        "version",
        "which_secret",
        "gen_random_uuid",
        "uuid",
    }
)

#: AST classes for unnamed host/session-state functions denied as external access.
_CATALOG_NODE_CLASSES: frozenset[str] = frozenset(
    {
        "CurrentVersion",
        "CurrentDatabase",
        "CurrentSchema",
        "CurrentSchemas",
        "CurrentCatalog",
        "CurrentUser",
        "SessionUser",
    }
)

#: Denied system schemas; only unqualified development source tables are readable.
_DENIED_TABLE_SCHEMAS: frozenset[str] = frozenset(
    {"information_schema", "pg_catalog", "system", "temp", "duckdb"}
)

#: Environment variable names a spawned worker scrubs before it touches
#: DuckDB (A7: the worker runs with a replaced, keyless environment).
_CREDENTIAL_ENV_RE = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE
)


class DevQueryError(RuntimeError):
    """A `dev_query` failure carrying its stable projection code (never DuckDB
    text): one of `projection.DEV_QUERY_CODES`."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


# ---------------------------------------------------------------------------
# The DEVELOPMENT warehouse: canonical path, materialization, path equality
# ---------------------------------------------------------------------------

def dev_warehouse_path(storage_root: Path, task_id: str) -> Path:
    """Return the task-partitioned development path outside immutable populations."""
    return (
        Path(storage_root)
        / _DEV_SCRATCH_DIRNAME
        / str(task_id)
        / "independent_implementer"
        / DEV_WAREHOUSE_BASENAME
    )


def materialize_dev_warehouse(
    task: TaskIR,
    workspace: Path,
    *,
    warehouse_root: Path | None = None,
) -> Path:
    """Replace and return a disposable source-only development warehouse."""
    from elt_taskgen.reference.solution import load_sources_duckdb

    storage_root = Path(workspace) if warehouse_root is None else Path(warehouse_root)
    dest = dev_warehouse_path(storage_root, task.task_id)
    rdir = rendered_dir(Path(workspace), str(task.task_id), DEV)
    if not rdir.is_dir():
        raise ToolHarnessFault(
            "dev_query",
            code="no_development_rendered_tree",
            cause_type="FileNotFoundError",
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    con = duckdb.connect(str(dest))
    try:
        load_sources_duckdb(task, rdir, con)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    return dest


def assert_development_warehouse(
    warehouse: Path,
    workspace: Path,
    task_id: str,
    *,
    warehouse_root: Path | None = None,
) -> Path:
    """Require exact equality with the session development warehouse path.

    Reject hidden populations, private data, gold, and oracle paths before open.
    """
    resolved = Path(warehouse).resolve()
    storage_root = (
        Path(workspace).resolve()
        if warehouse_root is None
        else Path(warehouse_root).resolve()
    )
    expected = dev_warehouse_path(storage_root, task_id).resolve()
    # Only inspect the part of the path INSIDE the trusted storage root: an
    # absolute prefix such as macOS `/private/var` is host state, not the
    # forbidden task-private tree.
    try:
        relative_parts = set(resolved.relative_to(storage_root).parts)
    except ValueError:
        raise ForbiddenArgument(
            tool="dev_query", detail="warehouse_path_mismatch"
        ) from None
    if _FORBIDDEN_PATH_PARTS.intersection(relative_parts):
        raise ForbiddenArgument(tool="dev_query", detail="not_development_warehouse")
    if resolved != expected:
        raise ForbiddenArgument(tool="dev_query", detail="warehouse_path_mismatch")
    return resolved


# ---------------------------------------------------------------------------
# dev_query: the ported, read-only, allowlisted DuckDB inspection
# ---------------------------------------------------------------------------

def _function_names(node: Any) -> tuple[str, ...]:
    """Return every normalized name associated with a function-like AST node."""
    names: list[str] = []
    sql_name = getattr(node, "sql_name", None)
    if callable(sql_name):
        try:
            names.append(str(sql_name()))
        except Exception:  # noqa: BLE001 - a name we could not read is not a pass
            pass
    names.append(str(getattr(node, "name", "")))
    return tuple(n.lower() for n in names if n)


def _is_external_function(names: tuple[str, ...]) -> bool:
    return any(
        n in EXTERNAL_FUNCTIONS or n.startswith(("read_", "scan_")) for n in names
    )


def _is_catalog_function(names: tuple[str, ...]) -> bool:
    return any(
        n in _CATALOG_FUNCTIONS or n.startswith(_CATALOG_FUNCTION_PREFIXES)
        for n in names
    )


def _validate_query(sql: str, allowed_tables: frozenset[str]) -> None:
    """Validate one read-only query from its AST.

    Require exactly one query over development source tables or local CTEs.
    Reject external, catalog, settings, secret, table-function, qualified, and
    system-schema access with stable error codes.
    """
    try:
        statements = parse(sql, read="duckdb")
    except Exception as exc:  # noqa: BLE001 - a stable code, never the parser text
        raise DevQueryError("invalid_query") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise DevQueryError("invalid_query")
    statement = statements[0]
    # CTE names defined in this query are legitimate relation references.
    cte_names = {
        str(cte.alias or "").lower()
        for cte in statement.find_all(exp.CTE)
        if cte.alias
    }
    for node in statement.walk():
        if isinstance(node, (exp.Func, exp.Anonymous)):
            if type(node).__name__ in _CATALOG_NODE_CLASSES:
                raise DevQueryError("external_access")
            names = _function_names(node)
            if _is_external_function(names) or _is_catalog_function(names):
                raise DevQueryError("external_access")
        elif isinstance(node, exp.Table):
            # A table-valued function in FROM parses as an `exp.Table` with an
            # empty name (its callee is a function child): never a base table.
            name = str(node.name or "").lower()
            if not name or any(
                isinstance(child, (exp.Func, exp.Anonymous))
                for child in (node.this, *node.args.values())
                if child is not None and not isinstance(child, (str, exp.Identifier))
            ):
                raise DevQueryError("external_access")
            schema = str(node.db or "").lower()
            catalog = str(node.catalog or "").lower()
            if catalog or schema in _DENIED_TABLE_SCHEMAS or (schema and schema != "main"):
                raise DevQueryError("external_access")
            if name not in allowed_tables and name not in cte_names:
                raise DevQueryError("invalid_query")


def _open_dev_connection(path: Path) -> duckdb.DuckDBPyConnection:
    """Open a locked read-only development connection with external access disabled."""
    con = duckdb.connect(str(path), read_only=True)
    for statement in (
        "SET threads = 1",
        "SET memory_limit = '512MB'",
        "SET autoload_known_extensions = false",
        "SET autoinstall_known_extensions = false",
        "SET allow_community_extensions = false",
        "SET allow_persistent_secrets = false",
        "SET allow_unredacted_secrets = false",
        "SET enable_http_metadata_cache = false",
        "SET secret_directory = ''",
        "SET home_directory = ''",
        "SET extension_directory = ''",
        "SET max_temp_directory_size = '0B'",
        "SET enable_external_access = false",
        "SET lock_configuration = true",
    ):
        con.execute(statement)
    return con


def _cap_rows(columns: Sequence[str], rows: list[tuple]) -> tuple[list[tuple], bool]:
    """Trim `rows` to fit `MAX_DEV_QUERY_BYTES` of rendered output, dropping
    trailing rows (the row count is a solver-visible page, not a leak)."""
    import json

    header = len(("," .join(columns)).encode("utf-8")) + 16
    total = header
    kept: list[tuple] = []
    truncated = False
    for row in rows:
        size = len("|".join(json.dumps(cell, ensure_ascii=True) for cell in row).encode("utf-8")) + 1
        if total + size > MAX_DEV_QUERY_BYTES and kept:
            truncated = True
            break
        total += size
        kept.append(row)
    return kept, truncated


#: A cell string longer than this is truncated to fit the `DevRows` per-cell
#: bound (a wide cell is a bounded page, not a harness fault; finding 3-1).
_MAX_DEV_CELL_CHARS = 4096


def _coerce_cell(cell: Any) -> tuple[Any, bool]:
    """Return a bounded JSON-safe cell and whether it was truncated."""
    import math

    if isinstance(cell, float) and not math.isfinite(cell):
        return None, False
    if isinstance(cell, str) and len(cell) > _MAX_DEV_CELL_CHARS:
        return cell[:_MAX_DEV_CELL_CHARS], True
    return cell, False


def _finite_nested(value: Any) -> Any:
    """Replace nested non-finite floats with null before scalar conversion."""
    import math

    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [_finite_nested(item) for item in value]
    if isinstance(value, dict):
        return {_finite_nested(key): _finite_nested(item) for key, item in value.items()}
    return value


def _dev_query_worker_main(send: Connection, warehouse: str, sql: str) -> None:
    """Run a validated query in a scrubbed, killable child and return bounded rows.

    DuckDB and scalar errors become measured codes; other faults expose only
    their class.
    """
    try:
        _apply_keyless_env()
        from elt_taskgen.reference.solution import to_scalar

        try:
            con = _open_dev_connection(Path(warehouse))
        except duckdb.Error:
            send.send(("result", {"error": "query_failed"}))
            return
        try:
            try:
                cursor = con.execute(sql)
                names = [item[0] for item in (cursor.description or ())]
                raw = cursor.fetchmany(MAX_DEV_ROWS + 1)
            except duckdb.Error:
                send.send(("result", {"error": "query_failed"}))
                return
        finally:
            con.close()
        try:
            rows = [[to_scalar(_finite_nested(cell)) for cell in row] for row in raw]
        except (ValueError, TypeError):
            # Unrepresentable cells are measured candidate errors, not harness faults.
            send.send(("result", {"error": "invalid_query"}))
            return
        send.send(("result", {"columns": [str(n) for n in names], "rows": rows}))
    except BaseException as exc:  # noqa: BLE001 - a harness fault (bad warehouse, etc.)
        try:
            send.send(("fault", type(exc).__name__))
        except BaseException:  # noqa: BLE001
            pass
    finally:
        send.close()


def run_dev_query(
    warehouse: Path,
    sql: str,
    *,
    allowed_tables: frozenset[str] | None = None,
    deadline_s: float = DEV_QUERY_DEADLINE_S,
) -> DevRows:
    """Run a bounded development query in a killable worker.

    Return at most 200 rows and 16 KiB. Validation, timeout, identifier, query,
    and cell errors use stable measured codes and never expose DuckDB text.
    """
    _validate_query(sql, frozenset(allowed_tables or ()))
    body = _run_in_spawned_worker(
        _dev_query_worker_main,
        (str(Path(warehouse)), str(sql)),
        deadline_s=float(deadline_s),
        timeout_payload={"error": "execution_timeout"},
        tool="dev_query",
    )
    if not isinstance(body, Mapping):
        raise DevQueryError("query_failed")
    if "error" in body:
        raise DevQueryError(str(body["error"]))
    names = [str(n) for n in body.get("columns", ())]
    raw = list(body.get("rows", ()))
    row_truncated = len(raw) > MAX_DEV_ROWS
    cell_truncated = False
    trimmed: list[tuple] = []
    for row in raw[:MAX_DEV_ROWS]:
        cells = []
        for cell in row:
            value, cut = _coerce_cell(cell)
            cell_truncated = cell_truncated or cut
            cells.append(value)
        trimmed.append(tuple(cells))
    kept, byte_truncated = _cap_rows(names, trimmed)
    try:
        return DevRows(
            columns=tuple(names),
            rows=tuple(kept),
            truncated=bool(row_truncated or byte_truncated or cell_truncated),
        )
    except ValueError as exc:
        # Invalid columns or cells are measured candidate errors.
        raise DevQueryError("invalid_query") from exc


# ---------------------------------------------------------------------------
# Keyless environment and the spawned-worker supervisor
# ---------------------------------------------------------------------------

def _apply_keyless_env() -> None:
    """Scrub credential-shaped variables from the current process env (A7).
    Called at the top of every spawned DEV worker so a DuckDB-engine escape
    would find no key to steal."""
    for name in list(os.environ):
        if _CREDENTIAL_ENV_RE.search(name):
            os.environ.pop(name, None)


def _env_probe_worker(send: Connection) -> None:
    """A spawned probe: apply the keyless scrub, report the credential-shaped
    variables that survive (for `test_implementer_worker_env_is_replaced_and_keyless`)."""
    try:
        _apply_keyless_env()
        survivors = sorted(n for n in os.environ if _CREDENTIAL_ENV_RE.search(n))
        send.send(("result", survivors))
    except BaseException as exc:  # noqa: BLE001 - reported, never re-raised into mp
        try:
            send.send(("fault", type(exc).__name__))
        except BaseException:  # noqa: BLE001
            pass
    finally:
        send.close()


def spawned_env_survivors() -> list[str]:
    """Spawn the env probe and return the credential-shaped env vars that
    survived the scrub inside the child (a keyless worker returns [])."""
    message = _run_in_spawned_worker(_env_probe_worker, (), deadline_s=30.0)
    return list(message)


def _spawn(target, args) -> tuple[Any, Connection]:
    methods = multiprocessing.get_all_start_methods()
    method = "spawn" if "spawn" in methods else methods[0]
    context = multiprocessing.get_context(method)
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=target, args=(send, *args), daemon=True)
    process.start()
    send.close()
    return process, receive


def _drain(receive: Connection):
    while receive.poll(0.0):
        try:
            return receive.recv()
        except EOFError:
            return None
    return None


def _run_in_spawned_worker(
    target, args, *, deadline_s: float, timeout_payload: Any = None, tool: str = "dev_worker"
) -> Any:
    """Run a killable child under a deadline and classify timeout or worker faults."""
    process, receive = _spawn(target, args)
    deadline = time.monotonic() + float(deadline_s)
    message: Any = None
    try:
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0.0:
                break
            if receive.poll(min(0.05, remaining)):
                try:
                    message = receive.recv()
                except EOFError:
                    message = None
                break
            if not process.is_alive():
                message = _drain(receive)
                break
    finally:
        receive.close()
        if process.is_alive():
            process.kill()
            process.join(2.0)
        else:
            process.join(1.0)
    if message is None:
        if time.monotonic() >= deadline:
            if timeout_payload is not None:
                return timeout_payload
            raise ToolDeadlineExceeded(tool, deadline_s=float(deadline_s))
        raise SandboxFault(
            f"{tool} worker exited without reporting a result; a harness fault",
            code="worker_failed",
        )
    kind, body = message
    if kind == "result":
        return body
    raise ToolHarnessFault(tool, code="worker_fault", cause_type=str(body))


# ---------------------------------------------------------------------------
# dry_run_sql: EXPLAIN over empty typed tables, in a spawned worker
# ---------------------------------------------------------------------------

def _classify_bind_error(problem: str) -> str:
    """Classify `_duckdb_dry_run_problem`'s reason into a stable error class —
    from the DuckDB error SHAPE only; the reason text itself is never
    returned to the model."""
    text = problem
    if "Parser Error" in text:
        return "parse_error"
    if "Catalog Error" in text:
        return "unknown_table" if "Table with name" in text else "other"
    if "Binder Error" in text:
        return "unknown_column" if "not found in FROM clause" in text else "type_error"
    if "Conversion Error" in text:
        return "type_error"
    return "other"


def _dry_run_worker_main(send: Connection, task: TaskIR, mart_name: str, sql: str) -> None:
    try:
        _apply_keyless_env()
        from elt_taskgen.reference import solution
        from elt_taskgen.reference.duckdb_sandbox import sandboxed_memory_connection

        mart = next(m for m in task.marts if m.name == mart_name)
        problem = solution._duckdb_dry_run_problem(task, mart, sql)  # noqa: SLF001 - wrapped
        if problem:
            send.send(
                (
                    "result",
                    {
                        "binds": False,
                        "error_class": _classify_bind_error(problem),
                        "columns_match": False,
                        "missing_columns": [],
                    },
                )
            )
            return
        con = sandboxed_memory_connection()
        try:
            for table in task.tables:
                solution.create_table(con, table)
            described = con.execute(f"DESCRIBE {sql}").fetchall()
        finally:
            con.close()
        out_cols = [str(row[0]) for row in described]
        expected = [c.name for c in mart.columns]
        columns_match = sorted(out_cols) == sorted(expected)
        missing = [c for c in expected if c not in out_cols]
        send.send(
            (
                "result",
                {
                    "binds": True,
                    "error_class": "",
                    "columns_match": columns_match,
                    "missing_columns": missing,
                },
            )
        )
    except BaseException as exc:  # noqa: BLE001 - typed, then sent
        try:
            send.send(("fault", type(exc).__name__))
        except BaseException:  # noqa: BLE001
            pass
    finally:
        send.close()


def dry_run_in_worker(
    task: TaskIR, mart_name: str, sql: str, *, deadline_s: float = DRY_RUN_DEADLINE_S
) -> dict[str, Any]:
    """Bind and describe SQL in a spawned worker without returning DuckDB text."""
    return _run_in_spawned_worker(
        _dry_run_worker_main, (task, str(mart_name), str(sql)),
        deadline_s=float(deadline_s), tool="dry_run_sql",
    )


# ---------------------------------------------------------------------------
# run_mart_sql_dev: DEVELOPMENT-only execution, rows discarded
# ---------------------------------------------------------------------------

def _mart_dev_worker_main(
    send: Connection,
    task: TaskIR,
    rendered_dev: str,
    sql_by_mart: Mapping[str, str],
    limits: SemanticLimits,
) -> None:
    try:
        _apply_keyless_env()
        from elt_taskgen.reference import solution
        from elt_taskgen.reference.duckdb_sandbox import sandboxed_memory_connection

        con = sandboxed_memory_connection(
            memory_limit_mb=limits.memory_limit_mb,
            threads=limits.threads,
            disable_temp_spill=True,
            deterministic_settings=True,
        )
        try:
            # DEVELOPMENT rendered SOURCE tables only — gold-free by construction.
            solution.load_sources_duckdb(task, Path(rendered_dev), con)
            for mart in task.marts:
                sql = sql_by_mart.get(mart.name)
                if sql is None:
                    send.send(("result", (mart.name, "invalid_query")))
                    return
                code = "ok"
                try:
                    solution.execute_mart(
                        con,
                        mart,
                        sql,
                        max_rows=limits.max_result_rows_per_mart,
                        max_bytes=limits.max_result_bytes_per_mart,
                    )
                    # Rows and row counts are DISCARDED (OQ-43).
                except solution.MartOutputLimitError:
                    code = "output_limit"
                except duckdb.Error:
                    code = "invalid_query"
                except ValueError as exc:
                    code = "column_set_mismatch" if "produced columns" in str(exc) else "query_failed"
                except MemoryError:
                    code = "memory_limit"
                except Exception:  # noqa: BLE001 - a candidate failure, a stable code
                    code = "query_failed"
                if code != "ok":
                    send.send(("result", (mart.name, code)))
                    return
            send.send(("result", ("", "ok")))
        finally:
            con.close()
    except MemoryError:
        try:
            send.send(("result", ("", "memory_limit")))
        except BaseException:  # noqa: BLE001
            pass
    except BaseException as exc:  # noqa: BLE001 - a harness fault (bad DEV tree, etc.)
        try:
            send.send(("fault", type(exc).__name__))
        except BaseException:  # noqa: BLE001
            pass
    finally:
        send.close()


def run_marts_dev_in_worker(
    task: TaskIR,
    rendered_dev: Path,
    sql_by_mart: Mapping[str, str],
    *,
    limits: SemanticLimits | None = None,
    deadline_s: float = MART_DEV_DEADLINE_S,
) -> tuple[str, str]:
    """Run all marts on gold-free development rows and return the first failure."""
    active = limits or SemanticLimits(timeout_seconds=float(deadline_s))
    body = _run_in_spawned_worker(
        _mart_dev_worker_main,
        (task, str(Path(rendered_dev)), dict(sql_by_mart), active),
        deadline_s=float(deadline_s),
        timeout_payload=("", "execution_timeout"),
        tool="run_mart_sql_dev",
    )
    return (str(body[0]), str(body[1]))

"""Create locked in-memory DuckDB connections for untrusted SQL.

External access must be disabled before configuration is locked. Persistent
database attachments are unsupported.
"""

from __future__ import annotations

import threading
from typing import Any

import duckdb

__all__ = [
    "assert_sandboxed",
    "begin_interruptible_scope",
    "current_scope_memory_limit_mb",
    "end_interruptible_scope",
    "interrupt_scope",
    "register_interruptible",
    "sandboxed_memory_connection",
]


# Scoped connections let supervisors interrupt timed-out workers.

_SCOPE_LOCK = threading.Lock()
_SCOPES: dict[int, dict[str, Any]] = {}


def begin_interruptible_scope(*, memory_limit_mb: int | None = None) -> int:
    """Open an interruptible scope for the current thread."""
    if memory_limit_mb is not None and not 32 <= int(memory_limit_mb) <= 1_048_576:
        raise ValueError("memory_limit_mb must be between 32 and 1048576")
    ident = threading.get_ident()
    with _SCOPE_LOCK:
        _SCOPES[ident] = {
            "memory_limit_mb": None if memory_limit_mb is None else int(memory_limit_mb),
            "connections": [],
        }
    return ident


def end_interruptible_scope(handle: int) -> None:
    """Forget a scope without closing its owner-managed connections."""
    with _SCOPE_LOCK:
        _SCOPES.pop(int(handle), None)


def current_scope_memory_limit_mb() -> int | None:
    """Return the current scope's memory limit, if any."""
    with _SCOPE_LOCK:
        scope = _SCOPES.get(threading.get_ident())
        return None if scope is None else scope.get("memory_limit_mb")


def register_interruptible(con: duckdb.DuckDBPyConnection) -> bool:
    """Register a connection in the current scope; return false without one."""
    with _SCOPE_LOCK:
        scope = _SCOPES.get(threading.get_ident())
        if scope is None:
            return False
        scope["connections"].append(con)
        return True


def interrupt_scope(handle: int) -> int:
    """Interrupt live connections in a scope and return the count."""
    with _SCOPE_LOCK:
        scope = _SCOPES.get(int(handle))
        connections = list(scope["connections"]) if scope is not None else []
    interrupted = 0
    for con in connections:
        try:
            con.interrupt()
        except Exception:  # noqa: BLE001 - a closed or already-finished connection
            continue
        interrupted += 1
    return interrupted


def sandboxed_memory_connection(
    *,
    memory_limit_mb: int | None = None,
    threads: int | None = None,
    disable_temp_spill: bool = False,
    deterministic_settings: bool = False,
) -> duckdb.DuckDBPyConnection:
    """Create a locked in-memory connection with optional resource bounds."""
    if memory_limit_mb is not None and not 32 <= int(memory_limit_mb) <= 1_048_576:
        raise ValueError("memory_limit_mb must be between 32 and 1048576")
    if threads is not None and not 1 <= int(threads) <= 256:
        raise ValueError("threads must be between 1 and 256")
    con = duckdb.connect(":memory:")
    # Extension controls are separate from DuckDB's external-access setting.
    con.execute("SET autoinstall_known_extensions=false")
    con.execute("SET autoload_known_extensions=false")
    con.execute("SET allow_community_extensions=false")
    con.execute("SET allow_persistent_secrets=false")
    con.execute("SET allow_unredacted_secrets=false")
    con.execute("SET enable_http_metadata_cache=false")
    # Avoid disclosing host paths through current_setting()/catalog helpers.
    con.execute("SET secret_directory=''")
    con.execute("SET home_directory=''")
    if memory_limit_mb is not None:
        con.execute(f"SET memory_limit='{int(memory_limit_mb)}MB'")
    if threads is not None:
        con.execute(f"SET threads={int(threads)}")
    if disable_temp_spill:
        con.execute("SET max_temp_directory_size='0B'")
    if deterministic_settings:
        con.execute("SET default_collation='binary'")
        con.execute("SET default_null_order='NULLS_LAST'")
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")
    return con


def assert_sandboxed(con: duckdb.DuckDBPyConnection) -> None:
    """Fail closed if `con` can still reach the filesystem."""
    (value,) = con.execute("SELECT current_setting('enable_external_access')").fetchone()
    if str(value).lower() not in ("false", "0"):
        raise RuntimeError("DuckDB connection is not sandboxed: enable_external_access is on")

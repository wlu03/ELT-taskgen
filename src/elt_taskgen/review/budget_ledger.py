"""Durable budget reservations shared across pipeline workers.

The SQLite ledger stores call identities and costs, never provider content or
credentials. It represents money as integer nano-dollars and rounds conservatively for
admission.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = [
    "BudgetLedger",
    "BudgetLedgerConfigurationError",
    "BudgetLedgerError",
    "BudgetReservation",
    "BudgetReservationError",
    "BudgetSnapshot",
    "DurableBudgetLedger",
    "ReservationConflictError",
    "ReservationState",
    "UnknownReservationError",
    "initialize",
]


_DATABASE_NAME = "pipeline_budget.sqlite3"
_BUSY_TIMEOUT_MS = 30_000
_NANOS_PER_USD = 1_000_000_000


_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_runs (
    run_id TEXT PRIMARY KEY,
    total_limit_nanos INTEGER NOT NULL CHECK (total_limit_nanos >= 0),
    per_task_limit_nanos INTEGER CHECK (
        per_task_limit_nanos IS NULL OR per_task_limit_nanos >= 0
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_reservations (
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    role TEXT NOT NULL,
    estimated_nanos INTEGER NOT NULL CHECK (estimated_nanos >= 0),
    actual_nanos INTEGER CHECK (actual_nanos IS NULL OR actual_nanos >= 0),
    state TEXT NOT NULL CHECK (
        state IN ('reserved', 'committed', 'released', 'uncertain')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, call_id),
    FOREIGN KEY (run_id) REFERENCES budget_runs(run_id)
);

CREATE INDEX IF NOT EXISTS budget_reservations_run_state
    ON budget_reservations(run_id, state);

CREATE INDEX IF NOT EXISTS budget_reservations_run_task_state
    ON budget_reservations(run_id, task_id, state);
"""


class BudgetLedgerError(RuntimeError):
    """Base class for durable budget-ledger failures."""


class BudgetLedgerConfigurationError(BudgetLedgerError):
    """An existing run was opened with a different budget configuration."""


class BudgetReservationError(BudgetLedgerError):
    """A prospective reservation would exceed a durable run or task limit."""

    def __init__(
        self,
        message: str,
        *,
        limit_usd: float,
        committed_usd: float,
        reserved_usd: float,
        uncertain_usd: float,
        requested_usd: float,
        scope: str = "total",
        task_id: str | None = None,
    ) -> None:
        super().__init__(message)
        if scope not in {"task", "total"}:
            raise ValueError("budget reservation scope must be 'task' or 'total'")
        self.limit_usd = limit_usd
        self.committed_usd = committed_usd
        self.reserved_usd = reserved_usd
        self.uncertain_usd = uncertain_usd
        self.requested_usd = requested_usd
        self.scope = scope
        self.task_id = task_id


class ReservationConflictError(BudgetLedgerError):
    """A call id was reused with different identity or accounting data."""


class UnknownReservationError(BudgetLedgerError):
    """A commit or release named a call that this run never reserved."""


class ReservationState(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    """One reservation's durable state."""

    run_id: str
    call_id: str
    task_id: str
    role: str
    estimated_usd: float
    actual_usd: float | None
    state: ReservationState
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = self.state.value
        return payload


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """A complete, JSON-ready accounting snapshot for one pipeline run."""

    run_id: str
    total_limit_usd: float
    per_task_limit_usd: float | None
    committed_usd: float
    reserved_usd: float
    uncertain_usd: float
    available_usd: float
    over_limit_usd: float
    reservation_count: int
    committed_count: int
    reserved_count: int
    uncertain_count: int
    released_count: int
    reservations: tuple[BudgetReservation, ...]
    created_at: str
    updated_at: str

    @property
    def spent_usd(self) -> float:
        """Compatibility spelling for committed provider cost."""

        return self.committed_usd

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["spent_usd"] = self.spent_usd
        payload["reservations"] = [row.as_dict() for row in self.reservations]
        return payload


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _required_identifier(value: str, *, name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} must be a non-empty string")
    if "\x00" in normalized:
        raise ValueError(f"{name} must not contain NUL")
    return normalized


def _money_nanos(value: float | int | Decimal, *, name: str, limit: bool = False) -> int:
    """Convert a non-negative finite USD value to conservative integer units."""

    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite non-negative USD amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be a finite non-negative USD amount")
    rounding = ROUND_FLOOR if limit else ROUND_CEILING
    nanos = int((amount * _NANOS_PER_USD).to_integral_value(rounding=rounding))
    if nanos > 2**63 - 1:
        raise ValueError(f"{name} exceeds the SQLite accounting range")
    return nanos


def _usd(nanos: int) -> float:
    return float(Decimal(int(nanos)) / _NANOS_PER_USD)


def _reservation_from_row(row: sqlite3.Row) -> BudgetReservation:
    actual = row["actual_nanos"]
    return BudgetReservation(
        run_id=str(row["run_id"]),
        call_id=str(row["call_id"]),
        task_id=str(row["task_id"]),
        role=str(row["role"]),
        estimated_usd=_usd(int(row["estimated_nanos"])),
        actual_usd=None if actual is None else _usd(int(actual)),
        state=ReservationState(str(row["state"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class DurableBudgetLedger:
    """Share one durable budget view across concurrent workers.

    Every mutation uses `BEGIN IMMEDIATE`, making admission and insertion atomic.
    Initialization is worker-safe and does not mark live reservations uncertain.
    """

    def __init__(
        self,
        *,
        database_path: Path,
        run_id: str,
        enforce_task_limit: bool = True,
    ) -> None:
        self.database_path = Path(database_path)
        self.run_id = _required_identifier(run_id, name="run_id")
        self.enforce_task_limit = bool(enforce_task_limit)

    @classmethod
    def initialize(
        cls,
        run_id: str,
        total_limit_usd: float,
        workspace: Path | str,
        *,
        per_task_limit_usd: float | None = None,
        enforce_task_limit: bool = True,
    ) -> "DurableBudgetLedger":
        """Create or reopen a run without changing its immutable limits.

        ``per_task_limit_usd=None`` preserves the original aggregate-only
        ledger for legacy callers.  A configured run supplies its typed task
        limit.  An old aggregate-only row may bind that value once during
        migration; after binding, both limits are immutable.
        """

        run = _required_identifier(run_id, name="run_id")
        limit_nanos = _money_nanos(total_limit_usd, name="total_limit_usd", limit=True)
        task_limit_nanos = (
            None
            if per_task_limit_usd is None
            else _money_nanos(
                per_task_limit_usd,
                name="per_task_limit_usd",
                limit=True,
            )
        )
        state_dir = Path(workspace).resolve() / "state"
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        database_path = state_dir / _DATABASE_NAME
        con = cls._connect(database_path, initialize=True)
        try:
            con.executescript(_SCHEMA)
            con.execute("BEGIN IMMEDIATE")
            columns = {
                str(column["name"])
                for column in con.execute("PRAGMA table_info(budget_runs)")
            }
            if "per_task_limit_nanos" not in columns:
                # Migration from the aggregate-only schema. SQLite serializes
                # this ALTER under the same immediate transaction used by
                # concurrent worker initialization.
                con.execute(
                    "ALTER TABLE budget_runs ADD COLUMN per_task_limit_nanos "
                    "INTEGER CHECK (per_task_limit_nanos IS NULL OR "
                    "per_task_limit_nanos >= 0)"
                )
            row = con.execute(
                "SELECT total_limit_nanos, per_task_limit_nanos "
                "FROM budget_runs WHERE run_id=?",
                (run,),
            ).fetchone()
            now = _utc_now()
            if row is None:
                con.execute(
                    "INSERT INTO budget_runs"
                    " (run_id, total_limit_nanos, per_task_limit_nanos,"
                    " created_at, updated_at) VALUES (?,?,?,?,?)",
                    (run, limit_nanos, task_limit_nanos, now, now),
                )
            elif int(row["total_limit_nanos"]) != limit_nanos:
                raise BudgetLedgerConfigurationError(
                    f"budget run {run!r} already has total limit "
                    f"${_usd(int(row['total_limit_nanos'])):.9f}; refused requested "
                    f"${_usd(limit_nanos):.9f}"
                )
            elif task_limit_nanos is not None:
                existing_task_limit = row["per_task_limit_nanos"]
                if existing_task_limit is None:
                    con.execute(
                        "UPDATE budget_runs SET per_task_limit_nanos=?, "
                        "updated_at=? WHERE run_id=?",
                        (task_limit_nanos, now, run),
                    )
                elif int(existing_task_limit) != task_limit_nanos:
                    raise BudgetLedgerConfigurationError(
                        f"budget run {run!r} already has per-task limit "
                        f"${_usd(int(existing_task_limit)):.9f}; refused requested "
                        f"${_usd(task_limit_nanos):.9f}"
                    )
            con.commit()
        except BaseException:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()
        try:
            os.chmod(database_path, 0o600)
        except OSError:
            # The database remains useful on filesystems without chmod support.
            pass
        return cls(
            database_path=database_path,
            run_id=run,
            enforce_task_limit=enforce_task_limit,
        )

    @staticmethod
    def _connect(path: Path, *, initialize: bool = False) -> sqlite3.Connection:
        con = sqlite3.connect(
            str(path),
            timeout=_BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,
        )
        con.row_factory = sqlite3.Row
        try:
            con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            con.execute("PRAGMA foreign_keys=ON")
            if initialize:
                mode = str(con.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
                if mode != "wal":
                    raise BudgetLedgerError(
                        f"budget ledger {path} refused WAL mode (reported {mode!r})"
                    )
            con.execute("PRAGMA synchronous=FULL")
            return con
        except BaseException:
            con.close()
            raise

    def _connection(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise BudgetLedgerError(f"budget ledger is missing: {self.database_path}")
        return self._connect(self.database_path)

    @staticmethod
    def _totals(
        con: sqlite3.Connection,
        run_id: str,
        *,
        task_id: str | None = None,
    ) -> tuple[int, int, int]:
        where = "run_id=?"
        parameters: tuple[str, ...] = (run_id,)
        if task_id is not None:
            where += " AND task_id=?"
            parameters += (task_id,)
        row = con.execute(
            "SELECT"
            " COALESCE(SUM(CASE WHEN state='committed' THEN actual_nanos ELSE 0 END),0)"
            " AS committed_nanos,"
            " COALESCE(SUM(CASE WHEN state='reserved' THEN estimated_nanos ELSE 0 END),0)"
            " AS reserved_nanos,"
            " COALESCE(SUM(CASE WHEN state='uncertain' THEN estimated_nanos ELSE 0 END),0)"
            " AS uncertain_nanos"
            f" FROM budget_reservations WHERE {where}",
            parameters,
        ).fetchone()
        assert row is not None
        return (
            int(row["committed_nanos"]),
            int(row["reserved_nanos"]),
            int(row["uncertain_nanos"]),
        )

    def reserve(
        self,
        task_id: str,
        role: str,
        estimated_usd: float,
        call_id: str,
    ) -> BudgetReservation:
        """Atomically reserve headroom before one provider transport call.

        Repeating an identical call id is idempotent and returns its current
        state.  Reusing it with different identity or cost is refused.
        """

        task = _required_identifier(task_id, name="task_id")
        role_name = _required_identifier(role, name="role")
        call = _required_identifier(call_id, name="call_id")
        requested = _money_nanos(estimated_usd, name="estimated_usd")
        con = self._connection()
        try:
            con.execute("BEGIN IMMEDIATE")
            limit_row = con.execute(
                "SELECT total_limit_nanos, per_task_limit_nanos "
                "FROM budget_runs WHERE run_id=?",
                (self.run_id,),
            ).fetchone()
            if limit_row is None:
                raise BudgetLedgerError(f"budget run is missing: {self.run_id!r}")
            existing = con.execute(
                "SELECT * FROM budget_reservations WHERE run_id=? AND call_id=?",
                (self.run_id, call),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["task_id"]) != task
                    or str(existing["role"]) != role_name
                    or int(existing["estimated_nanos"]) != requested
                ):
                    raise ReservationConflictError(
                        f"call_id {call!r} is already bound to different reservation data"
                    )
                con.commit()
                return _reservation_from_row(existing)

            task_limit_value = limit_row["per_task_limit_nanos"]
            if self.enforce_task_limit and task_limit_value is not None:
                task_committed, task_reserved, task_uncertain = self._totals(
                    con,
                    self.run_id,
                    task_id=task,
                )
                task_limit = int(task_limit_value)
                if (
                    task_committed
                    + task_reserved
                    + task_uncertain
                    + requested
                    > task_limit
                ):
                    raise BudgetReservationError(
                        f"task budget cannot absorb call {call!r} for {task!r}: "
                        f"${_usd(task_committed):.9f} committed + "
                        f"${_usd(task_reserved):.9f} reserved + "
                        f"${_usd(task_uncertain):.9f} uncertain + "
                        f"${_usd(requested):.9f} requested > "
                        f"${_usd(task_limit):.9f}",
                        limit_usd=_usd(task_limit),
                        committed_usd=_usd(task_committed),
                        reserved_usd=_usd(task_reserved),
                        uncertain_usd=_usd(task_uncertain),
                        requested_usd=_usd(requested),
                        scope="task",
                        task_id=task,
                    )

            committed, reserved, uncertain = self._totals(con, self.run_id)
            limit = int(limit_row["total_limit_nanos"])
            if committed + reserved + uncertain + requested > limit:
                raise BudgetReservationError(
                    f"run budget cannot absorb call {call!r}: "
                    f"${_usd(committed):.9f} committed + "
                    f"${_usd(reserved):.9f} reserved + "
                    f"${_usd(uncertain):.9f} uncertain + "
                    f"${_usd(requested):.9f} requested > ${_usd(limit):.9f}",
                    limit_usd=_usd(limit),
                    committed_usd=_usd(committed),
                    reserved_usd=_usd(reserved),
                    uncertain_usd=_usd(uncertain),
                    requested_usd=_usd(requested),
                    scope="total",
                )
            now = _utc_now()
            con.execute(
                "INSERT INTO budget_reservations"
                " (run_id, call_id, task_id, role, estimated_nanos, actual_nanos,"
                " state, created_at, updated_at) VALUES (?,?,?,?,?,NULL,?,?,?)",
                (
                    self.run_id,
                    call,
                    task,
                    role_name,
                    requested,
                    ReservationState.RESERVED.value,
                    now,
                    now,
                ),
            )
            con.execute(
                "UPDATE budget_runs SET updated_at=? WHERE run_id=?",
                (now, self.run_id),
            )
            row = con.execute(
                "SELECT * FROM budget_reservations WHERE run_id=? AND call_id=?",
                (self.run_id, call),
            ).fetchone()
            assert row is not None
            con.commit()
            return _reservation_from_row(row)
        except BaseException:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()

    def commit(
        self,
        call_id: str,
        actual_usd: float,
        *,
        task_id: str | None = None,
        role: str | None = None,
    ) -> BudgetReservation:
        """Commit the actual cost of a paid attempt.

        Actual cost is never refused because transport already occurred; an overrun
        reduces later headroom. Optional task and role bindings are verified in the same
        transaction.
        """

        call = _required_identifier(call_id, name="call_id")
        actual = _money_nanos(actual_usd, name="actual_usd")
        expected_task = (
            None if task_id is None else _required_identifier(task_id, name="task_id")
        )
        expected_role = (
            None if role is None else _required_identifier(role, name="role")
        )
        con = self._connection()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM budget_reservations WHERE run_id=? AND call_id=?",
                (self.run_id, call),
            ).fetchone()
            if row is None:
                raise UnknownReservationError(f"unknown reservation {call!r}")
            if expected_task is not None and str(row["task_id"]) != expected_task:
                raise ReservationConflictError(
                    f"reservation {call!r} belongs to task {row['task_id']!r}, "
                    f"not {expected_task!r}"
                )
            if expected_role is not None and str(row["role"]) != expected_role:
                raise ReservationConflictError(
                    f"reservation {call!r} belongs to role {row['role']!r}, "
                    f"not {expected_role!r}"
                )
            state = ReservationState(str(row["state"]))
            if state is ReservationState.RELEASED:
                raise ReservationConflictError(
                    f"released reservation {call!r} cannot be committed"
                )
            if state is ReservationState.COMMITTED:
                if int(row["actual_nanos"]) != actual:
                    raise ReservationConflictError(
                        f"reservation {call!r} is already committed at a different cost"
                    )
                con.commit()
                return _reservation_from_row(row)
            now = _utc_now()
            con.execute(
                "UPDATE budget_reservations SET state=?, actual_nanos=?, updated_at=?"
                " WHERE run_id=? AND call_id=?",
                (
                    ReservationState.COMMITTED.value,
                    actual,
                    now,
                    self.run_id,
                    call,
                ),
            )
            con.execute(
                "UPDATE budget_runs SET updated_at=? WHERE run_id=?",
                (now, self.run_id),
            )
            updated = con.execute(
                "SELECT * FROM budget_reservations WHERE run_id=? AND call_id=?",
                (self.run_id, call),
            ).fetchone()
            assert updated is not None
            con.commit()
            return _reservation_from_row(updated)
        except BaseException:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()

    def release(
        self,
        call_id: str,
        *,
        definite_no_charge: bool = False,
    ) -> BudgetReservation:
        """Release headroom only after the caller proves no charge occurred."""

        if definite_no_charge is not True:
            raise ValueError(
                "release requires definite_no_charge=True; an ambiguous/crashed "
                "attempt must remain uncertain"
            )
        call = _required_identifier(call_id, name="call_id")
        con = self._connection()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM budget_reservations WHERE run_id=? AND call_id=?",
                (self.run_id, call),
            ).fetchone()
            if row is None:
                raise UnknownReservationError(f"unknown reservation {call!r}")
            state = ReservationState(str(row["state"]))
            if state is ReservationState.COMMITTED:
                raise ReservationConflictError(
                    f"committed reservation {call!r} cannot be released"
                )
            if state is ReservationState.RELEASED:
                con.commit()
                return _reservation_from_row(row)
            now = _utc_now()
            con.execute(
                "UPDATE budget_reservations SET state=?, actual_nanos=NULL, updated_at=?"
                " WHERE run_id=? AND call_id=?",
                (ReservationState.RELEASED.value, now, self.run_id, call),
            )
            con.execute(
                "UPDATE budget_runs SET updated_at=? WHERE run_id=?",
                (now, self.run_id),
            )
            updated = con.execute(
                "SELECT * FROM budget_reservations WHERE run_id=? AND call_id=?",
                (self.run_id, call),
            ).fetchone()
            assert updated is not None
            con.commit()
            return _reservation_from_row(updated)
        except BaseException:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()

    def mark_outstanding_uncertain(self) -> int:
        """Conservatively quarantine every in-flight reservation on resume.

        Returns the number transitioned.  Repeating this operation is safe.
        Uncertain estimates continue to consume headroom until committed with
        actual cost or explicitly released as a definite no-charge.
        """

        con = self._connection()
        try:
            con.execute("BEGIN IMMEDIATE")
            now = _utc_now()
            cursor = con.execute(
                "UPDATE budget_reservations SET state=?, updated_at=?"
                " WHERE run_id=? AND state=?",
                (
                    ReservationState.UNCERTAIN.value,
                    now,
                    self.run_id,
                    ReservationState.RESERVED.value,
                ),
            )
            changed = int(cursor.rowcount)
            if changed:
                con.execute(
                    "UPDATE budget_runs SET updated_at=? WHERE run_id=?",
                    (now, self.run_id),
                )
            con.commit()
            return changed
        except BaseException:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()

    # Long spelling for callers that prefer the requirement's terminology.
    mark_outstanding_reservations_uncertain = mark_outstanding_uncertain

    def snapshot(self) -> BudgetSnapshot:
        """Return a consistent run-wide accounting snapshot and all call rows."""

        con = self._connection()
        try:
            con.execute("BEGIN")
            run = con.execute(
                "SELECT * FROM budget_runs WHERE run_id=?", (self.run_id,)
            ).fetchone()
            if run is None:
                raise BudgetLedgerError(f"budget run is missing: {self.run_id!r}")
            rows = con.execute(
                "SELECT * FROM budget_reservations WHERE run_id=?"
                " ORDER BY created_at, call_id",
                (self.run_id,),
            ).fetchall()
            reservations = tuple(_reservation_from_row(row) for row in rows)
            committed, reserved, uncertain = self._totals(con, self.run_id)
            counts = {state: 0 for state in ReservationState}
            for reservation in reservations:
                counts[reservation.state] += 1
            limit = int(run["total_limit_nanos"])
            consumed = committed + reserved + uncertain
            con.commit()
            return BudgetSnapshot(
                run_id=self.run_id,
                total_limit_usd=_usd(limit),
                per_task_limit_usd=(
                    None
                    if run["per_task_limit_nanos"] is None
                    else _usd(int(run["per_task_limit_nanos"]))
                ),
                committed_usd=_usd(committed),
                reserved_usd=_usd(reserved),
                uncertain_usd=_usd(uncertain),
                available_usd=_usd(max(0, limit - consumed)),
                over_limit_usd=_usd(max(0, consumed - limit)),
                reservation_count=len(reservations),
                committed_count=counts[ReservationState.COMMITTED],
                reserved_count=counts[ReservationState.RESERVED],
                uncertain_count=counts[ReservationState.UNCERTAIN],
                released_count=counts[ReservationState.RELEASED],
                reservations=reservations,
                created_at=str(run["created_at"]),
                updated_at=str(run["updated_at"]),
            )
        except BaseException:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()


# The shorter public name reads naturally at integration sites.
BudgetLedger = DurableBudgetLedger


def initialize(
    run_id: str,
    total_limit_usd: float,
    workspace: Path | str,
    *,
    per_task_limit_usd: float | None = None,
    enforce_task_limit: bool = True,
) -> DurableBudgetLedger:
    """Module-level constructor matching ``initialize(run, limit, workspace)``."""

    return DurableBudgetLedger.initialize(
        run_id,
        total_limit_usd,
        workspace,
        per_task_limit_usd=per_task_limit_usd,
        enforce_task_limit=enforce_task_limit,
    )

"""Focused tests for the concurrent prospective budget ledger."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from elt_taskgen.review.budget_ledger import (
    BudgetLedgerConfigurationError,
    BudgetReservationError,
    DurableBudgetLedger,
    ReservationConflictError,
    ReservationState,
    UnknownReservationError,
    initialize,
)


class DurableBudgetLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "workspace"
        self.ledger = initialize("run-1", 1.0, self.workspace)

    def test_initialization_is_idempotent_but_limit_is_immutable(self) -> None:
        reopened = DurableBudgetLedger.initialize("run-1", 1.0, self.workspace)
        self.assertEqual(reopened.snapshot().total_limit_usd, 1.0)
        self.assertIsNone(reopened.snapshot().per_task_limit_usd)
        with self.assertRaisesRegex(BudgetLedgerConfigurationError, "already has"):
            DurableBudgetLedger.initialize("run-1", 2.0, self.workspace)

        bound = DurableBudgetLedger.initialize(
            "run-1", 1.0, self.workspace, per_task_limit_usd=0.6
        )
        self.assertEqual(bound.snapshot().per_task_limit_usd, 0.6)
        # Omitting the optional value on a later worker cannot erase a bound
        # configured-run limit.
        self.assertEqual(
            DurableBudgetLedger.initialize(
                "run-1", 1.0, self.workspace
            ).snapshot().per_task_limit_usd,
            0.6,
        )
        with self.assertRaisesRegex(
            BudgetLedgerConfigurationError, "per-task limit"
        ):
            DurableBudgetLedger.initialize(
                "run-1", 1.0, self.workspace, per_task_limit_usd=0.7
            )

        con = sqlite3.connect(self.ledger.database_path)
        try:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertGreaterEqual(con.execute("PRAGMA busy_timeout").fetchone()[0], 0)
        finally:
            con.close()

    def test_initialization_migrates_an_aggregate_only_database(self) -> None:
        workspace = self.workspace.parent / "old-schema-workspace"
        state = workspace / "state"
        state.mkdir(parents=True)
        database = state / "pipeline_budget.sqlite3"
        con = sqlite3.connect(database)
        try:
            con.execute(
                "CREATE TABLE budget_runs ("
                "run_id TEXT PRIMARY KEY, total_limit_nanos INTEGER NOT NULL, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            con.execute(
                "INSERT INTO budget_runs VALUES (?,?,?,?)",
                ("old-run", 2_000_000_000, "before", "before"),
            )
            con.commit()
        finally:
            con.close()

        migrated = DurableBudgetLedger.initialize(
            "old-run", 2.0, workspace, per_task_limit_usd=0.75
        )
        self.assertEqual(migrated.snapshot().per_task_limit_usd, 0.75)
        con = sqlite3.connect(database)
        try:
            columns = {row[1] for row in con.execute("PRAGMA table_info(budget_runs)")}
        finally:
            con.close()
        self.assertIn("per_task_limit_nanos", columns)

    def test_reserve_commit_and_release_account_for_each_state(self) -> None:
        first = self.ledger.reserve("task-a", "author", 0.4, "call-a")
        second = self.ledger.reserve("task-b", "reviewer", 0.3, "call-b")
        self.assertEqual(first.state, ReservationState.RESERVED)
        self.assertEqual(second.estimated_usd, 0.3)

        committed = self.ledger.commit("call-a", 0.25)
        self.assertEqual(committed.state, ReservationState.COMMITTED)
        with self.assertRaisesRegex(ValueError, "definite_no_charge=True"):
            self.ledger.release("call-b")
        released = self.ledger.release("call-b", definite_no_charge=True)
        self.assertEqual(released.state, ReservationState.RELEASED)

        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.committed_usd, 0.25)
        self.assertEqual(snapshot.spent_usd, 0.25)
        self.assertEqual(snapshot.reserved_usd, 0.0)
        self.assertEqual(snapshot.available_usd, 0.75)
        self.assertEqual(snapshot.committed_count, 1)
        self.assertEqual(snapshot.released_count, 1)
        json.dumps(snapshot.as_dict())

    def test_budget_admission_counts_committed_reserved_and_uncertain(self) -> None:
        self.ledger.reserve("task-a", "author", 0.3, "paid")
        self.ledger.commit("paid", 0.2)
        self.ledger.reserve("task-b", "reviewer", 0.35, "live")
        self.ledger.reserve("task-c", "attacker", 0.35, "crashed")
        self.assertEqual(self.ledger.mark_outstanding_uncertain(), 2)
        self.assertEqual(self.ledger.mark_outstanding_reservations_uncertain(), 0)

        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.committed_usd, 0.2)
        self.assertEqual(snapshot.uncertain_usd, 0.7)
        self.assertEqual(snapshot.uncertain_count, 2)
        with self.assertRaises(BudgetReservationError) as caught:
            self.ledger.reserve("task-d", "reviewer", 0.11, "refused")
        self.assertEqual(caught.exception.requested_usd, 0.11)
        self.assertEqual(self.ledger.snapshot().reservation_count, 3)

        self.ledger.release("live", definite_no_charge=True)
        admitted = self.ledger.reserve("task-d", "reviewer", 0.11, "accepted")
        self.assertEqual(admitted.state, ReservationState.RESERVED)

    def test_call_ids_are_idempotent_and_cannot_be_rebound(self) -> None:
        original = self.ledger.reserve("task-a", "author", 0.2, "same")
        replay = self.ledger.reserve("task-a", "author", 0.2, "same")
        self.assertEqual(replay, original)
        with self.assertRaises(ReservationConflictError):
            self.ledger.reserve("task-b", "author", 0.2, "same")

        committed = self.ledger.commit("same", 0.18)
        self.assertEqual(self.ledger.commit("same", 0.18), committed)
        self.assertEqual(
            self.ledger.reserve("task-a", "author", 0.2, "same").state,
            ReservationState.COMMITTED,
        )
        with self.assertRaises(ReservationConflictError):
            self.ledger.commit("same", 0.19)
        with self.assertRaises(ReservationConflictError):
            self.ledger.release("same", definite_no_charge=True)
        with self.assertRaises(UnknownReservationError):
            self.ledger.commit("missing", 0.01)

    def test_commit_can_require_the_original_task_and_role_binding(self) -> None:
        self.ledger.reserve("task-a", "author", 0.2, "bound-call")
        with self.assertRaisesRegex(ReservationConflictError, "belongs to task"):
            self.ledger.commit(
                "bound-call", 0.1, task_id="task-b", role="author"
            )
        with self.assertRaisesRegex(ReservationConflictError, "belongs to role"):
            self.ledger.commit(
                "bound-call", 0.1, task_id="task-a", role="reviewer"
            )
        self.assertEqual(self.ledger.snapshot().reserved_count, 1)
        committed = self.ledger.commit(
            "bound-call", 0.1, task_id="task-a", role="author"
        )
        self.assertEqual(committed.state, ReservationState.COMMITTED)

    def test_actual_cost_can_truthfully_put_run_over_limit(self) -> None:
        self.ledger.reserve("task-a", "author", 0.5, "underestimated")
        self.ledger.commit("underestimated", 1.2)
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.committed_usd, 1.2)
        self.assertEqual(snapshot.available_usd, 0.0)
        self.assertEqual(snapshot.over_limit_usd, 0.2)
        with self.assertRaises(BudgetReservationError):
            self.ledger.reserve("task-b", "reviewer", 0.0, "later")

    def test_per_task_limit_survives_resume_and_counts_uncertainty(self) -> None:
        workspace = self.workspace.parent / "task-cap-resume"
        first = DurableBudgetLedger.initialize(
            "task-cap-run", 5.0, workspace, per_task_limit_usd=1.0
        )
        first.reserve("same-task", "author", 0.4, "paid")
        first.commit("paid", 0.4)
        first.reserve("same-task", "reviewer", 0.5, "crashed")

        resumed = DurableBudgetLedger.initialize(
            "task-cap-run", 5.0, workspace, per_task_limit_usd=1.0
        )
        self.assertEqual(resumed.mark_outstanding_uncertain(), 1)
        with self.assertRaises(BudgetReservationError) as caught:
            resumed.reserve("same-task", "attacker", 0.11, "refused")
        self.assertEqual(caught.exception.scope, "task")
        self.assertEqual(caught.exception.task_id, "same-task")
        self.assertEqual(caught.exception.committed_usd, 0.4)
        self.assertEqual(caught.exception.uncertain_usd, 0.5)
        # The task scope is isolated; another task can still use aggregate
        # headroom from this same run.
        self.assertEqual(
            resumed.reserve("other-task", "author", 0.8, "other").state,
            ReservationState.RESERVED,
        )

    def test_legacy_aggregate_only_run_has_no_implicit_task_cap(self) -> None:
        legacy = DurableBudgetLedger.initialize(
            "legacy-run", 2.0, self.workspace.parent / "legacy"
        )
        legacy.reserve("same-task", "author", 0.75, "one")
        legacy.reserve("same-task", "reviewer", 0.75, "two")
        snapshot = legacy.snapshot()
        self.assertIsNone(snapshot.per_task_limit_usd)
        self.assertEqual(snapshot.reserved_usd, 1.5)

    def test_explicit_aggregate_only_view_shares_parent_total_not_task_cap(self) -> None:
        workspace = self.workspace.parent / "parent-budget"
        candidate = DurableBudgetLedger.initialize(
            "parent-run", 3.0, workspace, per_task_limit_usd=0.5
        )
        candidate.reserve("candidate-a", "author", 0.4, "candidate-call")
        candidate.commit("candidate-call", 0.4)
        with self.assertRaises(BudgetReservationError):
            candidate.reserve("metrology", "critic", 0.6, "task-capped")

        # Fresh metrology is not a candidate. An explicit aggregate-only view
        # bypasses the candidate cap while reserving atomically against the
        # same immutable $3 parent allowance and the same historical spend.
        parent = DurableBudgetLedger.initialize(
            "parent-run",
            3.0,
            workspace,
            enforce_task_limit=False,
        )
        parent.reserve("metrology", "critic", 1.0, "metrology-call")
        self.assertEqual(parent.snapshot().reserved_usd, 1.0)
        self.assertEqual(parent.snapshot().committed_usd, 0.4)
        with self.assertRaises(BudgetReservationError) as caught:
            parent.reserve("metrology", "critic", 1.61, "over-parent")
        self.assertEqual(caught.exception.scope, "total")

    def test_invalid_money_and_identifiers_fail_closed(self) -> None:
        for invalid in (-0.01, float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.ledger.reserve("task", "role", invalid, f"call-{invalid}")
        with self.assertRaises(ValueError):
            self.ledger.reserve("", "role", 0.1, "call")

    def test_concurrent_workers_cannot_oversubscribe_one_total(self) -> None:
        worker_count = 20
        barrier = threading.Barrier(worker_count)

        def attempt(index: int) -> bool:
            worker = DurableBudgetLedger.initialize("run-1", 1.0, self.workspace)
            barrier.wait(timeout=10)
            try:
                worker.reserve(
                    f"task-{index}",
                    "semantic_author",
                    0.1,
                    f"call-{index}",
                )
            except BudgetReservationError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            admitted = list(executor.map(attempt, range(worker_count)))

        self.assertEqual(sum(admitted), 10)
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.reserved_count, 10)
        self.assertEqual(snapshot.reserved_usd, 1.0)
        self.assertEqual(snapshot.available_usd, 0.0)

    def test_concurrent_workers_cannot_oversubscribe_one_task(self) -> None:
        workspace = self.workspace.parent / "task-concurrency"
        ledger = DurableBudgetLedger.initialize(
            "task-concurrency-run",
            10.0,
            workspace,
            per_task_limit_usd=1.0,
        )
        worker_count = 20
        barrier = threading.Barrier(worker_count)

        def attempt(index: int) -> bool:
            worker = DurableBudgetLedger.initialize(
                "task-concurrency-run",
                10.0,
                workspace,
                per_task_limit_usd=1.0,
            )
            barrier.wait(timeout=10)
            try:
                worker.reserve(
                    "shared-task",
                    "semantic_author",
                    0.1,
                    f"task-call-{index}",
                )
            except BudgetReservationError as exc:
                self.assertEqual(exc.scope, "task")
                return False
            return True

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            admitted = list(executor.map(attempt, range(worker_count)))

        self.assertEqual(sum(admitted), 10)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot.reserved_count, 10)
        self.assertEqual(snapshot.reserved_usd, 1.0)
        self.assertEqual(snapshot.available_usd, 9.0)


if __name__ == "__main__":
    unittest.main()

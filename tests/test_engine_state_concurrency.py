"""Identity and cross-process state guarantees for :mod:`elt_taskgen.engine`."""

from __future__ import annotations

import errno
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from elt_taskgen import engine as engine_mod
from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.engine import (
    FINAL_IN_PROGRESS,
    FINAL_REJECTED,
    SQLITE_BUSY_TIMEOUT_MS,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
    Engine,
    EngineError,
    StageName,
    StageOutcome,
    StagePayload,
    TaskLockBusy,
)
from elt_taskgen.models import RepairRoute, TaskIR, TaskStatus


def _one_stage_worker(
    workspace: str,
    task_id: str,
    start: Any,
    events: Any,
    rendezvous: Any | None,
) -> None:
    """Spawn-safe worker used by the cross-process tests below."""
    engine: Engine | None = None
    try:

        def runner(_engine: Engine, _task: Any) -> StageOutcome:
            events.put(("ran", task_id, os.getpid()))
            if rendezvous is None:
                # Keep the winner in the stage long enough for the other
                # same-task worker to contend on the task lock.
                time.sleep(0.35)
            else:
                rendezvous.wait(timeout=10)
                events.put(("crossed", task_id, os.getpid()))
            return StageOutcome(
                verdict=VERDICT_PASS,
                payload=StagePayload(detail="worker passed"),
            )

        engine = Engine(
            Path(workspace),
            stage_runners={StageName.CONTAMINATION_PRE.value: runner},
            max_repair_rounds=0,
        )
        events.put(("ready", task_id, os.getpid()))
        if not start.wait(timeout=10):
            raise TimeoutError("parent never released worker start gate")
        engine.run(task_id, until=StageName.CONTAMINATION_PRE.value)
        events.put(("done", task_id, os.getpid()))
    except BaseException as exc:
        events.put(("error", task_id, f"{type(exc).__name__}: {exc}"))
        raise
    finally:
        if engine is not None:
            engine.close()


def _hold_task_lock_worker(workspace: str, task_id: str, ready: Any) -> None:
    """Hold a task lock until the test kills this process."""
    engine = Engine(Path(workspace))
    try:
        with engine._task_lock(task_id):
            ready.put(("locked", os.getpid()))
            time.sleep(30)
    finally:
        engine.close()


def _hold_coordinator_lock_worker(workspace: str, ready: Any) -> None:
    """Hold the workspace publication lock until the test kills this process."""
    engine = Engine(Path(workspace))
    try:
        with engine.coordinator_lock():
            ready.put(("locked", os.getpid()))
            time.sleep(30)
    finally:
        engine.close()


def _blocking_stage_worker(
    workspace: str,
    task_id: str,
    entered: Any,
    release: Any,
) -> None:
    """Hold Engine.run inside a stage until the parent releases its barrier."""

    def runner(_engine: Engine, _task: TaskIR) -> StageOutcome:
        entered.set()
        if not release.wait(timeout=15):
            raise TimeoutError("parent did not release the stage barrier")
        return StageOutcome(
            verdict=VERDICT_PASS,
            payload=StagePayload(detail="holder passed"),
        )

    engine = Engine(
        Path(workspace),
        stage_runners={StageName.CONTAMINATION_PRE.value: runner},
        max_repair_rounds=0,
    )
    try:
        engine.run(task_id, until=StageName.CONTAMINATION_PRE.value)
    finally:
        engine.close()


def _locked_invalidation_worker(
    workspace: str,
    task_id: str,
    ready: Any,
    pre_run_entered: Any,
) -> None:
    """Append one invalidation through Engine.run's lock-scoped callback."""
    engine = Engine(
        Path(workspace),
        stage_runners={
            StageName.CONTAMINATION_PRE.value: lambda _engine, _task: StageOutcome(
                verdict=VERDICT_PASS,
                payload=StagePayload(detail="invalidated stage reran"),
            )
        },
        max_repair_rounds=0,
    )

    def invalidate(locked_engine: Engine, task: TaskIR) -> None:
        pre_run_entered.set()
        locked_engine.record_report(
            task,
            StageName.CONTAMINATION_PRE.value,
            VERDICT_FAIL,
            StagePayload(detail="forced under task lock"),
        )

    try:
        ready.set()
        engine.run(
            task_id,
            until=StageName.CONTAMINATION_PRE.value,
            pre_run=invalidate,
        )
    finally:
        engine.close()


def _fork_connection_probe(workspace: str, result: Any) -> None:
    """Exercise inherited-connection rebinding in an isolated spawn worker."""
    engine = Engine(Path(workspace))
    parent_pid = os.getpid()
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - assertions happen in the test process
        os.close(read_fd)
        try:
            inherited_owner = engine._connection_pid
            mode = engine._con.execute("PRAGMA journal_mode").fetchone()[0]
            rebound_owner = engine._connection_pid
            message = f"{inherited_owner}|{rebound_owner}|{mode}".encode()
            os.write(write_fd, message)
            engine.close()
            os.close(write_fd)
            os._exit(0)
        except BaseException:  # noqa: BLE001 - child must report even SystemExit
            os._exit(1)

    os.close(write_fd)
    try:
        message = os.read(read_fd, 1024).decode()
        waited_pid, status = os.waitpid(child_pid, 0)
        # The child closing its duplicate must not disturb this process's handle.
        engine._con.execute("SELECT 1").fetchone()
        result.send(
            (
                parent_pid,
                child_pid,
                waited_pid,
                status,
                engine._connection_pid,
                message,
            )
        )
    finally:
        os.close(read_fd)
        engine.close()
        result.close()


class EngineStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "workspace"
        self.task = demo_task()
        self.engine = Engine(self.workspace)
        self.addCleanup(self.engine.close)

    def test_task_ids_are_safe_segments_at_model_and_raw_engine_boundaries(self) -> None:
        invalid_ids = (
            "../../outside",
            "nested/task",
            r"nested\task",
            ".",
            "..",
            "/absolute/path",
            r"C:\absolute\path",
            "CON",
            "con.json",
        )
        payload = self.task.model_dump(mode="json")
        for task_id in invalid_ids:
            with self.subTest(task_id=task_id):
                candidate = {**payload, "task_id": task_id}
                with self.assertRaisesRegex(ValueError, "task_id"):
                    TaskIR.model_validate(candidate)
                # Raw CLI/API ids reach load_task before any TaskIR is parsed.
                with self.assertRaisesRegex(EngineError, "unsafe task_id"):
                    self.engine.load_task(task_id)

        # model_copy intentionally does not revalidate; the engine write
        # boundary must still prevent it from constructing an escaping path.
        copied = self.task.model_copy(update={"task_id": "../../outside"})
        with self.assertRaisesRegex(EngineError, "unsafe task_id"):
            self.engine.save_task(copied)

        valid = TaskIR.model_validate({**payload, "task_id": "safe-id.v1"})
        self.assertEqual(valid.task_id, "safe-id.v1")

    def test_load_task_refuses_task_ir_filed_under_another_id(self) -> None:
        self.engine.register(self.task)
        foreign = self.task.model_copy(update={"task_id": "another_safe_task"})
        self.engine._task_ir_path(self.task.task_id).write_text(
            foreign.model_dump_json(), encoding="utf-8"
        )

        with self.assertRaisesRegex(EngineError, "names another task"):
            self.engine.load_task(self.task.task_id)

    def test_task_paths_refuse_symlinked_workspace_components(self) -> None:
        outside = self.workspace.parent / "outside-tasks"
        outside.mkdir()
        tasks_root = self.workspace / "tasks"
        tasks_root.rmdir()
        tasks_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(EngineError, "tasks root.*symbolic link"):
            self.engine.task_dir("safe_task")

        tasks_root.unlink()
        tasks_root.mkdir()
        task_link = tasks_root / "safe_task"
        task_link.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(EngineError, "task directory.*symbolic link"):
            self.engine.task_dir("safe_task")

    def test_stage_cannot_write_another_task_under_the_current_task_lock(self) -> None:
        self.engine.register(self.task)
        other_id = "another_safe_task"

        def changes_identity(_engine: Engine, task: TaskIR) -> StageOutcome:
            return StageOutcome(
                verdict=VERDICT_PASS,
                payload=StagePayload(detail="invalid identity change"),
                task=task.model_copy(update={"task_id": other_id}),
            )

        self.engine.set_stage_runner(StageName.CONTAMINATION_PRE, changes_identity)
        with self.assertRaisesRegex(EngineError, "holding the wrong lock"):
            self.engine.run(
                self.task.task_id, until=StageName.CONTAMINATION_PRE.value
            )
        self.assertFalse(self.engine._task_ir_path(other_id).exists())

    def test_identical_registration_preserves_status_and_revision_lineage(self) -> None:
        self.engine.register(self.task)
        stored = self.engine.load_task(self.task.task_id)
        progressed = stored.with_revision(
            route=RepairRoute.SPECIFICATION,
            reason="test repair",
        ).with_status(TaskStatus.RELEASED)
        self.engine.save_task(progressed)
        rows_before = self.engine._con.execute(
            "SELECT COUNT(*) FROM reports WHERE task_id=?", (self.task.task_id,)
        ).fetchone()[0]

        # Ingestion commonly reconstructs the original DRAFT object. Its content
        # hash matches, but its volatile fields must not replace workspace state.
        self.engine.register(self.task)

        reloaded = self.engine.load_task(self.task.task_id)
        self.assertEqual(reloaded.status, TaskStatus.RELEASED)
        self.assertEqual(reloaded.revisions, progressed.revisions)
        self.assertEqual(reloaded.current_revision, 2)
        self.assertEqual(
            self.engine._con.execute(
                "SELECT COUNT(*) FROM reports WHERE task_id=?", (self.task.task_id,)
            ).fetchone()[0],
            rows_before,
        )

    def test_reingest_retains_but_ignores_fatal_from_previous_hash(self) -> None:
        self.engine.register(self.task)
        old = self.engine.load_task(self.task.task_id)
        self.engine.record_report(
            old,
            StageName.REVIEW.value,
            VERDICT_FATAL,
            StagePayload(error="old identity is irreparable"),
        )
        self.assertEqual(self.engine.final_verdict(old.task_id), FINAL_REJECTED)

        replacement = self.task.model_copy(update={"title": "replacement identity"})
        self.engine.register(replacement, allow_overwrite=True)
        current = self.engine.load_task(old.task_id)
        self.assertNotEqual(current.content_hash(), old.content_hash())
        self.assertEqual(self.engine.final_verdict(old.task_id), FINAL_IN_PROGRESS)
        self.assertEqual(
            self.engine._con.execute(
                "SELECT COUNT(*) FROM reports WHERE task_id=? AND verdict=?",
                (old.task_id, VERDICT_FATAL),
            ).fetchone()[0],
            1,
        )

        # A fatal row for the replacement identity still rejects normally.
        self.engine.record_report(
            current,
            StageName.REVIEW.value,
            VERDICT_FATAL,
            StagePayload(error="new identity is irreparable"),
        )
        self.assertEqual(self.engine.final_verdict(old.task_id), FINAL_REJECTED)

    def test_ledger_uses_wal_and_a_real_busy_timeout(self) -> None:
        mode = self.engine._con.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = self.engine._con.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(str(mode).lower(), "wal")
        self.assertGreaterEqual(int(timeout), SQLITE_BUSY_TIMEOUT_MS)

    def test_windows_lock_fallback_retries_contention_and_unlocks_one_byte(
        self,
    ) -> None:
        class FakeMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2

            def __init__(self) -> None:
                self.calls: list[tuple[int, int]] = []
                self.contended = True

            def locking(self, _descriptor: int, operation: int, count: int) -> None:
                self.calls.append((operation, count))
                if operation == self.LK_NBLCK and self.contended:
                    self.contended = False
                    raise OSError(errno.EACCES, "lock is held")

        fallback = FakeMsvcrt()
        lock_path = Path(self._tmp.name) / "windows.lock"
        with (
            mock.patch.object(engine_mod, "_fcntl", None),
            mock.patch.object(engine_mod, "_msvcrt", fallback),
            mock.patch.object(engine_mod.time, "sleep") as sleep,
            lock_path.open("a+b") as handle,
        ):
            Engine._acquire_task_file_lock(handle)
            Engine._release_task_file_lock(handle)

        self.assertEqual(lock_path.read_bytes(), b"\0")
        self.assertEqual(
            fallback.calls,
            [
                (fallback.LK_NBLCK, 1),
                (fallback.LK_NBLCK, 1),
                (fallback.LK_UNLCK, 1),
            ],
        )
        sleep.assert_called_once()

    def test_task_locks_sorts_deduplicates_and_releases_on_exception(self) -> None:
        events: list[tuple[str, str]] = []

        class RecordingLock:
            def __init__(self, task_id: str) -> None:
                self.task_id = task_id

            def __enter__(self):
                events.append(("enter", self.task_id))

            def __exit__(self, *_exc) -> None:
                events.append(("exit", self.task_id))

        with mock.patch.object(
            self.engine,
            "_task_lock",
            side_effect=lambda task_id, **_kwargs: RecordingLock(task_id),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop inside lock set"):
                with self.engine.task_locks(("z-task", "a-task", "z-task")) as held:
                    self.assertEqual(held, ("a-task", "z-task"))
                    raise RuntimeError("stop inside lock set")

        self.assertEqual(
            events,
            [
                ("enter", "a-task"),
                ("enter", "z-task"),
                ("exit", "z-task"),
                ("exit", "a-task"),
            ],
        )

    def test_task_locks_validates_every_id_before_acquiring_any(self) -> None:
        with mock.patch.object(self.engine, "_task_lock") as acquire:
            with self.assertRaisesRegex(EngineError, "unsafe task_id"):
                with self.engine.task_locks(("safe-task", "../escape")):
                    self.fail("unsafe lock set was entered")
        acquire.assert_not_called()

    def test_task_locks_reenters_a_lock_owned_by_this_engine_thread(self) -> None:
        task_id = self.task.task_id
        other_id = "other-task"
        with self.engine._task_lock(task_id):
            self.assertTrue(self.engine.task_lock_held(task_id))
            with self.engine.task_locks((other_id, task_id, task_id)) as held:
                self.assertEqual(held, tuple(sorted((other_id, task_id))))
                self.assertTrue(self.engine.task_lock_held(task_id))
                self.assertTrue(self.engine.task_lock_held(other_id))
            self.assertTrue(self.engine.task_lock_held(task_id))
            self.assertFalse(self.engine.task_lock_held(other_id))
        self.assertFalse(self.engine.task_lock_held(task_id))

    def test_coordinator_lock_is_reentrant_and_releases_on_exception(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "publication failed"):
            with self.engine.coordinator_lock():
                with self.engine.coordinator_lock():
                    raise RuntimeError("publication failed")

        # A fresh Engine uses a separate OS handle, proving the outermost
        # exception released the workspace lock rather than only local depth.
        other = Engine(self.workspace)
        try:
            with other.coordinator_lock():
                pass
        finally:
            other.close()

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork semantics")
    def test_forked_engine_rebinds_sqlite_connection_to_child_pid(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        receiver, sender = ctx.Pipe(duplex=False)
        worker = ctx.Process(
            target=_fork_connection_probe,
            args=(str(self.workspace), sender),
        )
        worker.start()
        sender.close()
        try:
            self.assertTrue(receiver.poll(15), "fork probe produced no result")
            values = receiver.recv()
            worker.join(timeout=15)
            self.assertFalse(worker.is_alive())
            self.assertEqual(worker.exitcode, 0)
        finally:
            receiver.close()
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=5)

        parent_pid, child_pid, waited_pid, status, owner_after, message = values
        self.assertEqual(waited_pid, child_pid)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 0)
        inherited_owner, rebound_owner, mode = message.split("|")
        self.assertEqual(int(inherited_owner), parent_pid)
        self.assertEqual(int(rebound_owner), child_pid)
        self.assertEqual(mode.lower(), "wal")
        self.assertEqual(owner_after, parent_pid)


class EngineConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "workspace"
        self.task = demo_task()
        engine = Engine(self.workspace)
        try:
            engine.register(self.task)
        finally:
            engine.close()

    def _run_workers(
        self, task_ids: tuple[str, str], *, rendezvous: bool
    ) -> list[tuple]:
        ctx = multiprocessing.get_context("spawn")
        events = ctx.Queue()
        start = ctx.Event()
        barrier = ctx.Barrier(2) if rendezvous else None
        workers = [
            ctx.Process(
                target=_one_stage_worker,
                args=(str(self.workspace), task_id, start, events, barrier),
            )
            for task_id in task_ids
        ]
        for worker in workers:
            worker.start()
        try:
            ready: list[tuple] = []
            while len(ready) < 2:
                event = events.get(timeout=15)
                self.assertNotEqual(event[0], "error", event)
                self.assertEqual(event[0], "ready", event)
                ready.append(event)
            start.set()

            observed: list[tuple] = []
            while sum(event[0] == "done" for event in observed) < 2:
                observed.append(events.get(timeout=20))
            for worker in workers:
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive(), f"worker {worker.pid} did not exit")
                self.assertEqual(worker.exitcode, 0)
            return observed
        finally:
            start.set()
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=5)
            events.close()
            events.join_thread()

    def test_two_processes_cannot_execute_the_same_task_stage_twice(self) -> None:
        observed = self._run_workers(
            (self.task.task_id, self.task.task_id), rendezvous=False
        )
        ran = [event for event in observed if event[0] == "ran"]
        errors = [event for event in observed if event[0] == "error"]
        self.assertEqual(errors, [])
        self.assertEqual(len(ran), 1, observed)

        engine = Engine(self.workspace)
        try:
            rows = engine._con.execute(
                "SELECT COUNT(*) FROM reports WHERE task_id=? AND stage=?"
                " AND verdict=?",
                (
                    self.task.task_id,
                    StageName.CONTAMINATION_PRE.value,
                    VERDICT_PASS,
                ),
            ).fetchone()[0]
        finally:
            engine.close()
        self.assertEqual(rows, 1)

    def test_pre_run_invalidation_waits_for_and_shares_the_execution_lock(self) -> None:
        """A force-rerun row cannot interleave with an active task execution."""
        ctx = multiprocessing.get_context("spawn")
        holder_entered = ctx.Event()
        release_holder = ctx.Event()
        invalidator_ready = ctx.Event()
        pre_run_entered = ctx.Event()
        holder = ctx.Process(
            target=_blocking_stage_worker,
            args=(
                str(self.workspace),
                self.task.task_id,
                holder_entered,
                release_holder,
            ),
        )
        invalidator = ctx.Process(
            target=_locked_invalidation_worker,
            args=(
                str(self.workspace),
                self.task.task_id,
                invalidator_ready,
                pre_run_entered,
            ),
        )
        holder.start()
        try:
            self.assertTrue(holder_entered.wait(timeout=15))
            invalidator.start()
            self.assertTrue(invalidator_ready.wait(timeout=15))
            # The second process reached Engine.run, but its callback must remain
            # outside the critical section until the holder commits and unlocks.
            self.assertFalse(pre_run_entered.wait(timeout=0.4))
            release_holder.set()
            holder.join(timeout=15)
            invalidator.join(timeout=15)
            self.assertFalse(holder.is_alive())
            self.assertFalse(invalidator.is_alive())
            self.assertEqual(holder.exitcode, 0)
            self.assertEqual(invalidator.exitcode, 0)
            self.assertTrue(pre_run_entered.is_set())

            engine = Engine(self.workspace)
            try:
                rows = engine._con.execute(
                    "SELECT verdict FROM reports WHERE task_id=? AND stage=?"
                    " ORDER BY id",
                    (self.task.task_id, StageName.CONTAMINATION_PRE.value),
                ).fetchall()
            finally:
                engine.close()
            self.assertEqual(
                [str(row[0]) for row in rows],
                [VERDICT_PASS, VERDICT_FAIL, VERDICT_PASS],
            )
        finally:
            release_holder.set()
            for process in (holder, invalidator):
                if process.pid is None:
                    continue
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)

    def test_nested_task_lock_expansion_fails_fast_on_process_contention(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        other_id = "other-task"
        ready = ctx.Queue()
        holder = ctx.Process(
            target=_hold_task_lock_worker,
            args=(str(self.workspace), other_id, ready),
        )
        holder.start()
        try:
            self.assertEqual(ready.get(timeout=15)[0], "locked")
            engine = Engine(self.workspace)
            try:
                with engine._task_lock(self.task.task_id):
                    started = time.monotonic()
                    with self.assertRaisesRegex(TaskLockBusy, "other-task"):
                        with engine.task_locks((self.task.task_id, other_id)):
                            self.fail("contended nested lock set was entered")
                    self.assertLess(time.monotonic() - started, 2.0)
                    self.assertTrue(engine.task_lock_held(self.task.task_id))
            finally:
                engine.close()
        finally:
            if holder.is_alive():
                holder.terminate()
            holder.join(timeout=5)
            ready.close()
            ready.join_thread()

    def test_task_to_coordinator_lock_inversion_fails_fast(self) -> None:
        """A release runner cannot deadlock a coordinator waiting on its task."""
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Queue()
        holder = ctx.Process(
            target=_hold_coordinator_lock_worker,
            args=(str(self.workspace), ready),
        )
        holder.start()
        try:
            self.assertEqual(ready.get(timeout=15)[0], "locked")
            engine = Engine(self.workspace)
            try:
                with engine._task_lock(self.task.task_id):
                    started = time.monotonic()
                    with self.assertRaisesRegex(
                        TaskLockBusy, "coordinator publication"
                    ):
                        with engine.coordinator_lock():
                            self.fail("contended publication lock was entered")
                    self.assertLess(time.monotonic() - started, 2.0)
                    self.assertTrue(engine.task_lock_held(self.task.task_id))
            finally:
                engine.close()
        finally:
            if holder.is_alive():
                holder.terminate()
            holder.join(timeout=5)
            ready.close()
            ready.join_thread()

    def test_different_task_locks_do_not_serialize_stage_execution(self) -> None:
        other = self.task.model_copy(
            update={
                "task_id": f"{self.task.task_id}-other",
                "cluster_id": f"{self.task.cluster_id}-other",
            }
        )
        engine = Engine(self.workspace)
        try:
            engine.register(other)
        finally:
            engine.close()

        observed = self._run_workers(
            (self.task.task_id, other.task_id), rendezvous=True
        )
        self.assertEqual([event for event in observed if event[0] == "error"], [])
        self.assertCountEqual(
            [event[1] for event in observed if event[0] == "ran"],
            [self.task.task_id, other.task_id],
        )
        self.assertCountEqual(
            [event[1] for event in observed if event[0] == "crossed"],
            [self.task.task_id, other.task_id],
        )

    def test_killed_worker_does_not_leave_a_stale_task_lock(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Queue()
        worker = ctx.Process(
            target=_hold_task_lock_worker,
            args=(str(self.workspace), self.task.task_id, ready),
        )
        worker.start()
        try:
            self.assertEqual(ready.get(timeout=15)[0], "locked")
            worker.terminate()
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())

            engine = Engine(
                self.workspace,
                stage_runners={
                    StageName.CONTAMINATION_PRE.value: lambda _engine, _task: (
                        StageOutcome(
                            verdict=VERDICT_PASS,
                            payload=StagePayload(
                                detail="recovered after killed worker"
                            ),
                        )
                    )
                },
            )
            try:
                task = engine.run(
                    self.task.task_id, until=StageName.CONTAMINATION_PRE.value
                )
                row = engine.latest_report(
                    task.task_id, StageName.CONTAMINATION_PRE.value
                )
                self.assertIsNotNone(row)
                self.assertEqual(row.verdict, VERDICT_PASS)
            finally:
                engine.close()
        finally:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=5)
            ready.close()
            ready.join_thread()

    def test_killed_worker_does_not_leave_a_stale_coordinator_lock(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Queue()
        worker = ctx.Process(
            target=_hold_coordinator_lock_worker,
            args=(str(self.workspace), ready),
        )
        worker.start()
        try:
            self.assertEqual(ready.get(timeout=15)[0], "locked")
            worker.terminate()
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())

            engine = Engine(self.workspace)
            try:
                with engine.coordinator_lock():
                    pass
            finally:
                engine.close()
        finally:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=5)
            ready.close()
            ready.join_thread()


if __name__ == "__main__":
    unittest.main()

"""Crash-atomic repair journal and recovery tests.

These tests pin the two unsafe windows the journal closes: SQLite may fail
halfway through the repair/invalidation append, or the process may die after
that transaction while only a prefix of target files is installed.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elt_taskgen import repair
from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.engine import (
    FINAL_ACCEPTED,
    FINAL_IN_PROGRESS,
    Engine,
    RepairCommitError,
    StageOutcome,
    StagePayload,
    VERDICT_FAIL,
    VERDICT_PASS,
    variant_gate_stage,
)
from elt_taskgen.models import (
    RLVR_TASK_VARIANTS,
    AcceptanceReport,
    GateResult,
    RepairEdit,
    RepairEditOp,
    RepairPatch,
    RepairRoute,
    TaskStatus,
    canonical_json,
    variant_task_id,
)
from elt_taskgen.review import repair_proposer
from elt_taskgen.verification import gates as gates_mod
from elt_taskgen.verification import variant_battery


def _accepted_report(task, variant) -> AcceptanceReport:
    roster = variant_battery.gate_roster(variant)
    return AcceptanceReport.from_gates(
        task_id=variant_task_id(task.task_id, variant),
        revision=task.current_revision,
        task_content_hash=task.content_hash(),
        gates=tuple(
            GateResult(gate=name, passed=True, details="fixture pass")
            for name in roster
        ),
        scorer_version=gates_mod.SCORER_VERSION,
        roster_digest=gates_mod.ROSTER_DIGEST,
        roster=roster,
    )


class RepairCommitAtomicityTest(unittest.TestCase):
    def _workspace(self, name: str) -> Path:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        return Path(root.name) / name

    def _seed(self, name: str, route: RepairRoute = RepairRoute.REFERENCE):
        workspace = self._workspace(name)
        engine = Engine(workspace)
        self.addCleanup(engine.close)
        engine.register(demo_task())
        task = engine.load_task(demo_task().task_id)

        battery_stages = {
            variant_gate_stage(variant).value: variant for variant in RLVR_TASK_VARIANTS
        }
        for stage in repair.stages_to_rerun(route):
            variant = battery_stages.get(stage)
            payload = (
                _accepted_report(task, variant)
                if variant is not None
                else StagePayload(detail=f"{stage} accepted before repair")
            )
            engine.record_report(task, stage, VERDICT_PASS, payload)

        # final_verdict is deliberately status-independent and should recognize
        # the two current batteries before the repair starts.
        self.assertEqual(engine.final_verdict(task.task_id), FINAL_ACCEPTED)
        return engine, task

    def test_repair_row_and_all_invalidations_are_one_sqlite_transaction(self) -> None:
        """Abort after any invalidation prefix: SQLite retains none of it."""
        route = RepairRoute.REFERENCE
        stages = repair.stages_to_rerun(route)
        for index, failing_stage in enumerate(stages):
            with self.subTest(after_invalidation_count=index):
                engine, task = self._seed(f"ledger-{index}", route)
                before_reports = engine._con.execute(
                    "SELECT COUNT(*) FROM reports WHERE task_id=?", (task.task_id,)
                ).fetchone()[0]
                engine._con.execute(
                    "CREATE TRIGGER abort_repair_invalidation BEFORE INSERT ON reports "
                    f"WHEN NEW.stage={failing_stage!r} AND NEW.verdict='fail' "
                    "BEGIN SELECT RAISE(ABORT, 'injected repair crash'); END"
                )
                engine._con.commit()

                with self.assertRaises(sqlite3.IntegrityError):
                    repair.apply_repair(engine, task, route, "atomicity test")

                self.assertEqual(engine.repair_rounds_used(task.task_id), 0)
                self.assertIsNone(engine.pending_repair_intent(task.task_id))
                self.assertEqual(
                    engine._con.execute(
                        "SELECT COUNT(*) FROM repair_intents WHERE task_id=?",
                        (task.task_id,),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    engine._con.execute(
                        "SELECT COUNT(*) FROM reports WHERE task_id=?", (task.task_id,)
                    ).fetchone()[0],
                    before_reports,
                )
                self.assertEqual(engine.load_task(task.task_id), task)
                self.assertEqual(engine.final_verdict(task.task_id), FINAL_ACCEPTED)
                engine.close()

    def test_every_live_file_cut_recovers_once_and_never_exposes_old_acceptance(
        self,
    ) -> None:
        """Crash after each replacement, including before/after task_ir."""
        route = RepairRoute.REFERENCE
        for cut in (1, 2, 3):
            with self.subTest(crash_after_file=cut):
                engine, task = self._seed(f"files-{cut}", route)
                root = engine.task_dir(task.task_id) / "answer_key" / "reference"
                root.mkdir(parents=True, exist_ok=True)
                first = root / "a.sql"
                second = root / "b.sql"
                first.write_bytes(b"SELECT 'old-a';\n")
                second.write_bytes(b"SELECT 'old-b';\n")
                patched = task.model_copy(
                    update={"solver_prompt": task.solver_prompt + " Repaired semantics."}
                )
                staged = {
                    first.relative_to(engine.workspace).as_posix(): b"SELECT 'new-a';\n",
                    second.relative_to(engine.workspace).as_posix(): None,
                }

                original = engine._install_repair_intent_file
                installed = 0

                def crash_after(intent, row):
                    nonlocal installed
                    original(intent, row)
                    installed += 1
                    if installed == cut:
                        raise OSError("injected process death after replacement")

                with mock.patch.object(
                    engine, "_install_repair_intent_file", side_effect=crash_after
                ):
                    with self.assertRaises(RepairCommitError):
                        repair.apply_repair(
                            engine,
                            patched,
                            route,
                            "journal recovery test",
                            staged_files=staged,
                        )

                pending = engine.pending_repair_intent(task.task_id)
                self.assertIsNotNone(pending)
                self.assertEqual(engine.repair_rounds_used(task.task_id), 1)
                repair_row = engine.last_repair(task.task_id)
                self.assertIsNotNone(repair_row)
                self.assertEqual(
                    repair_row.lineage_root_hash, task.revisions[0].content_hash
                )
                self.assertEqual(engine.final_verdict(task.task_id), FINAL_IN_PROGRESS)

                # Even a stage outside REFERENCE's invalidation set cannot reuse
                # its old-hash PASS while the target TaskIR may still be old.
                intake = engine.latest_report(task.task_id, "intake")
                live_during_crash = engine.load_task(task.task_id)
                self.assertFalse(
                    engine.report_is_current(live_during_crash, "intake", intake)[0]
                )
                for stage in repair.stages_to_rerun(route):
                    self.assertEqual(
                        engine.latest_report(task.task_id, stage).verdict,
                        VERDICT_FAIL,
                    )

                repair_count = engine.repair_rounds_used(task.task_id)
                invalidation_count = engine._con.execute(
                    "SELECT COUNT(*) FROM reports WHERE task_id=? AND verdict='fail'",
                    (task.task_id,),
                ).fetchone()[0]
                engine.close()

                recovered_engine = Engine(engine.workspace)
                self.addCleanup(recovered_engine.close)
                recovered = recovered_engine.recover_pending_repair(task.task_id)
                self.assertIsNotNone(recovered)
                self.assertEqual(recovered.status, TaskStatus.IN_REPAIR)
                self.assertEqual(recovered.current_revision, task.current_revision + 1)
                self.assertEqual(recovered.content_hash(), patched.content_hash())
                self.assertEqual(first.read_bytes(), b"SELECT 'new-a';\n")
                self.assertFalse(second.exists())
                self.assertIsNone(recovered_engine.pending_repair_intent(task.task_id))
                self.assertEqual(
                    recovered_engine.repair_rounds_used(task.task_id), repair_count
                )
                recovered_row = recovered_engine.last_repair(task.task_id)
                self.assertIsNotNone(recovered_row)
                self.assertEqual(
                    recovered_row.lineage_root_hash, task.revisions[0].content_hash
                )
                self.assertEqual(
                    recovered_engine._con.execute(
                        "SELECT COUNT(*) FROM reports WHERE task_id=? AND verdict='fail'",
                        (task.task_id,),
                    ).fetchone()[0],
                    invalidation_count,
                )
                # Recovery is idempotent and committed target BLOBs are discarded.
                self.assertIsNone(recovered_engine.recover_pending_repair(task.task_id))
                self.assertEqual(
                    recovered_engine._con.execute(
                        "SELECT COUNT(*) FROM repair_intent_files WHERE content IS NOT NULL"
                    ).fetchone()[0],
                    0,
                )
                recovered_engine.close()

    def test_recovery_refuses_unjournaled_concurrent_bytes_and_stays_fail_closed(
        self,
    ) -> None:
        engine, task = self._seed("conflict", RepairRoute.REFERENCE)
        target = engine.task_dir(task.task_id) / "answer_key" / "reference" / "x.sql"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"old\n")
        staged = {target.relative_to(engine.workspace).as_posix(): b"new\n"}

        with mock.patch.object(
            engine,
            "_install_repair_intent_file",
            side_effect=OSError("die before first replacement"),
        ):
            with self.assertRaises(RepairCommitError):
                repair.apply_repair(
                    engine,
                    task,
                    RepairRoute.REFERENCE,
                    "conflict test",
                    staged_files=staged,
                )
        target.write_bytes(b"unrelated concurrent edit\n")

        with self.assertRaisesRegex(RepairCommitError, "could not be recovered"):
            engine.recover_pending_repair(task.task_id)
        self.assertIsNotNone(engine.pending_repair_intent(task.task_id))
        self.assertEqual(engine.final_verdict(task.task_id), FINAL_IN_PROGRESS)

    def test_engine_certified_patch_uses_one_committed_intent_and_one_round(self) -> None:
        """The production proposer path uses the journal, not raw ``_commit``."""
        sentinel = " Ties are broken by customer_id ascending."

        class Provider:
            def complete(self, _role, _prompt):
                patch = RepairPatch(
                    route=RepairRoute.SPECIFICATION,
                    artifact="task_ir.json",
                    edits=(
                        RepairEdit(
                            op=RepairEditOp.INSERT,
                            locator="solver_prompt",
                            old="",
                            new=sentinel,
                        ),
                    ),
                    rationale="review found an undefined tie-break",
                    proposer_role=repair_proposer.ROLE_NAME,
                )
                return canonical_json(patch.model_dump(mode="json"))

        workspace = self._workspace("production-proposer")

        # Keep the local runners explicit so the trial certifies the same author
        # and review stages that the live ladder executes.
        def pass_outcome(_engine, _task):
            return StageOutcome(VERDICT_PASS, StagePayload(detail="fixture pass"))

        def review_outcome(_engine, task):
            if sentinel.strip() in task.solver_prompt:
                return StageOutcome(VERDICT_PASS, StagePayload(detail="repair is green"))
            return StageOutcome(
                VERDICT_FAIL, StagePayload(error="undefined tie-break in prose")
            )

        engine = Engine(
            workspace,
            stage_runners={
                "contamination_pre": pass_outcome,
                "generate": pass_outcome,
                "reference": pass_outcome,
                "author": pass_outcome,
                "review": review_outcome,
            },
            repair_proposer=repair_proposer.RepairProposer(Provider(), max_attempts=1),
            max_repair_rounds=1,
        )
        self.addCleanup(engine.close)
        engine.register(demo_task())
        before = engine.load_task(demo_task().task_id)

        committed = engine.run(before.task_id, until="review")

        self.assertIn(sentinel.strip(), committed.solver_prompt)
        self.assertEqual(committed.current_revision, before.current_revision + 1)
        self.assertEqual(engine.repair_rounds_used(before.task_id), 1)
        self.assertIsNone(engine.pending_repair_intent(before.task_id))
        intents = engine._con.execute(
            "SELECT state, repair_id FROM repair_intents WHERE task_id=?",
            (before.task_id,),
        ).fetchall()
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0][0], "committed")
        self.assertEqual(
            engine._con.execute(
                "SELECT COUNT(*) FROM repairs WHERE task_id=?",
                (before.task_id,),
            ).fetchone()[0],
            1,
        )

    def test_run_recovers_before_its_locked_pre_run_hook(self) -> None:
        engine, task = self._seed("pre-run", RepairRoute.SPECIFICATION)
        patched = task.model_copy(
            update={"solver_prompt": task.solver_prompt + " Recovered first."}
        )
        with mock.patch.object(
            engine,
            "_install_repair_intent_file",
            side_effect=OSError("die after the journal transaction"),
        ):
            with self.assertRaises(RepairCommitError):
                repair.apply_repair(
                    engine, patched, RepairRoute.SPECIFICATION, "pre-run ordering"
                )

        class StopAfterObservation(RuntimeError):
            pass

        seen = []

        def inspect_then_stop(_engine, recovered):
            seen.append(recovered)
            raise StopAfterObservation

        with self.assertRaises(StopAfterObservation):
            engine.run(task.task_id, pre_run=inspect_then_stop)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].content_hash(), patched.content_hash())
        self.assertEqual(seen[0].current_revision, task.current_revision + 1)
        self.assertIsNone(engine.pending_repair_intent(task.task_id))


if __name__ == "__main__":
    unittest.main()

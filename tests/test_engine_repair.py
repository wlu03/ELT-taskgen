"""Tests for engine.py (append-only ledger, resumable orchestration, repair
budget, stale-verdict shadowing) and repair.py (artifact-diff routing,
failure-report routing, invalidation cascade).

WHY THIS EXISTS
The engine is the only component allowed to say "accepted", and the rejected
predecessor implementation died on exactly three failure modes these tests pin:
duplicate work after a crash-resume, unbounded repair loops, and a stale accept
verdict surviving a later rejection. Repair routing must come from changed
artifacts / typed failure payloads, never opinion, and a population repair must
invalidate gold, attacks, gate evidence, and calibration in one committed step.
"""

import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from elt_taskgen import engine as engine_mod
from elt_taskgen import repair
from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.engine import (
    DB_FILENAME,
    FINAL_ACCEPTED,
    FINAL_IN_PROGRESS,
    FINAL_REJECTED,
    STAGE_ORDER,
    Engine,
    EngineError,
    StageName,
    StageNotWiredError,
    StageOutcome,
    StagePayload,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
    variant_gate_stage,
)
from elt_taskgen.models import (
    RLVR_TASK_VARIANTS,
    AcceptanceReport,
    GateResult,
    RepairRoute,
    TaskStatus,
    TaskVariant,
    variant_task_id,
)
from elt_taskgen.verification import gates as gates_mod
from elt_taskgen.verification import variant_battery


def ok_runner(counter: Counter, stage: str):
    def run(engine: Engine, task):
        counter[stage] += 1
        if stage == StageName.RELEASE.value:
# A release stub must create the artifacts it attests to; a pass row without
# an output directory is stale, not successful.
            out = engine.workspace / engine_mod.RELEASE_DIRNAME
            out.mkdir(parents=True, exist_ok=True)
            (out / engine_mod.RELEASE_MANIFEST_FILENAME).write_text(
                json.dumps({"tasks": {task.task_id: task.content_hash()}}),
                encoding="utf-8",
            )
        return StageOutcome(verdict=VERDICT_PASS, payload=StagePayload(detail=f"{stage} ok"))

    return run


def fail_runner(counter: Counter, stage: str, *, detail: str = "boom"):
    def run(engine: Engine, task):
        counter[stage] += 1
        return StageOutcome(verdict=VERDICT_FAIL, payload=StagePayload(detail=detail))

    return run


def accepted_unit_report(
    task,
    variant: TaskVariant,
    *,
    scorer_version: str | None = None,
    roster_digest: str | None = None,
    drop: tuple[str, ...] = (),
) -> AcceptanceReport:
    """A roster-complete acceptance bound to one RLVR unit and parent hash.

    Stamped with the LIVE scorer version and roster digest, because a battery
    is only current evidence when it was measured under the current scorer AND
    the current roster (engine.report_is_current). The keyword arguments exist
    so a test can build the stale shapes deliberately."""
    variant = TaskVariant(variant)
    return AcceptanceReport.from_gates(
        task_id=variant_task_id(task.task_id, variant),
        revision=task.current_revision,
        task_content_hash=task.content_hash(),
        gates=tuple(
            GateResult(gate=name, passed=True, details="test fixture pass")
            for name in variant_battery.gate_roster(variant)
            if name not in drop
        ),
        scorer_version=(
            gates_mod.SCORER_VERSION if scorer_version is None else scorer_version
        ),
        roster_digest=(
            gates_mod.ROSTER_DIGEST if roster_digest is None else roster_digest
        ),
        roster=tuple(
            name for name in variant_battery.gate_roster(variant) if name not in drop
        ),
    )


def accepted_unit_runner(counter: Counter, variant: TaskVariant):
    stage = variant_gate_stage(variant).value

    def run(engine: Engine, task):
        counter[stage] += 1
        return StageOutcome(
            verdict=VERDICT_PASS,
            payload=accepted_unit_report(task, variant),
        )

    return run


def record_accepted_units(
    engine: Engine,
    task,
    variants: tuple[TaskVariant, ...] = RLVR_TASK_VARIANTS,
) -> None:
    """Record independently roster-complete EL/T battery reports."""
    for variant in variants:
        engine.record_report(
            task,
            variant_gate_stage(variant).value,
            VERDICT_PASS,
            accepted_unit_report(task, variant),
        )


def all_pass_runners(counter: Counter, overrides: dict | None = None):
    runners = {s.value: ok_runner(counter, s.value) for s in StageName}
    for variant in RLVR_TASK_VARIANTS:
        runners[variant_gate_stage(variant).value] = accepted_unit_runner(counter, variant)
    if overrides:
        runners.update(overrides)
    return runners


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "taskgen-workspace"
        self.task = demo_task()
        self.task_id = self.task.task_id

    def make_engine(self, counter=None, overrides=None, **kwargs) -> Engine:
        counter = counter if counter is not None else Counter()
        engine = Engine(
            self.workspace,
            stage_runners=all_pass_runners(counter, overrides),
            **kwargs,
        )
        self.addCleanup(engine.close)
        return engine


class TestLedger(EngineTestCase):
    def test_stage_order_matches_pipeline(self):
        self.assertEqual(
            [s.value for s in STAGE_ORDER],
            [
                "intake",
                "contamination_pre",
                "generate",
                "reference",
                "author",
                "review",
                "attack",
                "gates",
                # Shared task-integrity evidence retains the historical
                # ``gates`` value/alias, but has no admission authority. The
                # two following stages independently accept the two RLVR units.
                "gates_extract_load",
                "gates_transform",
                "calibrate",
                "contamination_post",
                "select",
                "release",
            ],
        )
        self.assertIs(StageName.GATES, StageName.TASK_INTEGRITY)

    def test_register_writes_task_ir_and_intake_report(self):
        engine = self.make_engine()
        engine.register(self.task)
        loaded = engine.load_task(self.task_id)
        self.assertEqual(loaded.content_hash(), self.task.content_hash())
        row = engine.latest_report(self.task_id, "intake")
        self.assertIsNotNone(row)
        self.assertEqual(row.verdict, VERDICT_PASS)
        self.assertEqual(row.content_hash, self.task.content_hash())

    def test_register_is_idempotent(self):
        engine = self.make_engine()
        engine.register(self.task)
        first = engine.latest_report(self.task_id, "intake")
        engine.register(self.task)
        second = engine.latest_report(self.task_id, "intake")
        self.assertEqual(first.id, second.id)  # no duplicate ledger row

    def test_reports_are_append_only_latest_wins(self):
        engine = self.make_engine()
        engine.register(self.task)
        task = engine.load_task(self.task_id)
        id_fail = engine.record_report(task, "gates", VERDICT_FAIL, StagePayload(detail="f"))
        id_pass = engine.record_report(task, "gates", VERDICT_PASS, StagePayload(detail="p"))
        self.assertGreater(id_pass, id_fail)
        latest = engine.latest_report(self.task_id, "gates")
        self.assertEqual(latest.id, id_pass)
        self.assertEqual(latest.verdict, VERDICT_PASS)
        # The shared/legacy gates alias is evidence, not an admission verdict.
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_IN_PROGRESS)
        # both rows still exist in the ledger
        con = sqlite3.connect(str(self.workspace / "state" / DB_FILENAME))
        try:
            n = con.execute(
                "SELECT COUNT(*) FROM reports WHERE task_id=? AND stage='gates'",
                (self.task_id,),
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n, 2)

    def test_final_verdict_requires_both_independent_unit_reports(self):
        engine = self.make_engine()
        engine.register(self.task)
        task = engine.load_task(self.task_id)
        engine.record_report(
            task,
            StageName.GATES.value,
            VERDICT_PASS,
            StagePayload(detail="shared integrity passed"),
        )
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_IN_PROGRESS)

        record_accepted_units(engine, task, (TaskVariant.EXTRACT_LOAD,))
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_IN_PROGRESS)

        record_accepted_units(engine, task, (TaskVariant.TRANSFORM,))
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_ACCEPTED)

    def test_record_report_rejects_unknown_stage_and_verdict(self):
        engine = self.make_engine()
        engine.register(self.task)
        task = engine.load_task(self.task_id)
        with self.assertRaises(ValueError):
            engine.record_report(task, "not_a_stage", VERDICT_PASS, StagePayload())
        with self.assertRaises(ValueError):
            engine.record_report(task, "gates", "maybe", StagePayload())

    def test_record_artifact(self):
        engine = self.make_engine()
        engine.register(self.task)
        task = engine.load_task(self.task_id)
        engine.record_artifact(task, f"tasks/{self.task_id}/task_ir.json", "ab" * 32)
        con = sqlite3.connect(str(self.workspace / "state" / DB_FILENAME))
        try:
            row = con.execute(
                "SELECT rel_path, sha256 FROM artifacts WHERE task_id=?",
                (self.task_id,),
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(row, (f"tasks/{self.task_id}/task_ir.json", "ab" * 32))

    def test_load_task_missing_fails_closed(self):
        engine = self.make_engine()
        with self.assertRaises(EngineError):
            engine.load_task("no_such_task")


class TestOrchestration(EngineTestCase):
    def test_full_run_accepts(self):
        counter = Counter()
        engine = self.make_engine(counter)
        engine.register(self.task)
        task = engine.run(self.task_id, until=StageName.GATES_TRANSFORM.value)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_ACCEPTED)
        self.assertEqual(task.status, TaskStatus.ACCEPTED)

        task = engine.run(self.task_id)
        self.assertEqual(task.status, TaskStatus.RELEASED)
        for stage in STAGE_ORDER:
            if stage is StageName.INTAKE:
                continue  # register() recorded intake; runner never invoked
            self.assertEqual(counter[stage.value], 1, stage.value)

    def test_crash_resume_no_duplicate_work(self):
        counter = Counter()
        engine1 = self.make_engine(counter)
        engine1.register(self.task)
        engine1.run(self.task_id, until="reference")
        self.assertEqual(counter["contamination_pre"], 1)
        self.assertEqual(counter["generate"], 1)
        self.assertEqual(counter["reference"], 1)
        self.assertEqual(counter["author"], 0)  # until is inclusive-stop
        engine1.close()

        # "crash": a brand-new engine instance over the same workspace resumes
        engine2 = self.make_engine(counter)
        engine2.run(self.task_id)
        self.assertEqual(engine2.final_verdict(self.task_id), FINAL_ACCEPTED)
        for stage in ("contamination_pre", "generate", "reference", "author", "gates", "release"):
            self.assertEqual(counter[stage], 1, stage)

        # rerunning a completed pipeline is a no-op
        engine2.run(self.task_id)
        for stage in STAGE_ORDER:
            self.assertLessEqual(counter[stage.value], 1, stage.value)

    def test_completed_stage_reruns_when_hash_changes(self):
        counter = Counter()
        engine = self.make_engine(counter)
        engine.register(self.task)
        engine.run(self.task_id)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_ACCEPTED)

        # Semantic edit (authoring prose) moves the content hash: the old
        # verdicts are stale, and every stage must re-attest at the new hash.
        task = engine.load_task(self.task_id)
        edited = task.model_copy(update={"solver_prompt": "Build customer_summary."})
        engine.save_task(edited)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_IN_PROGRESS)

        engine.run(self.task_id)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_ACCEPTED)
        self.assertEqual(counter["generate"], 2)
        self.assertEqual(counter["gates"], 2)
        for stage in ("gates", "gates_extract_load", "gates_transform"):
            self.assertEqual(counter[stage], 2)
            latest = engine.latest_report(self.task_id, stage)
            self.assertEqual(latest.content_hash, edited.content_hash())

    def test_stage_runner_updating_task_forces_reattestation(self):
        counter = Counter()

        def authoring(engine, task):
            counter["author"] += 1
            if not task.solver_prompt:
                task = task.model_copy(update={"solver_prompt": "prose v1"})
            return StageOutcome(
                verdict=VERDICT_PASS, payload=StagePayload(detail="authored"), task=task
            )

        engine = self.make_engine(counter, overrides={"author": authoring})
        engine.register(self.task)
        engine.run(self.task_id)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_ACCEPTED)
        # the prose edit forced one full re-attestation sweep of earlier stages
        self.assertEqual(counter["generate"], 2)
        # the author pass was recorded AT the post-edit hash, so it is not rerun
        self.assertEqual(counter["author"], 1)
        current_hash = engine.load_task(self.task_id).content_hash()
        for stage in ("gates", "gates_extract_load", "gates_transform"):
            self.assertEqual(engine.latest_report(self.task_id, stage).content_hash, current_hash)

    def test_repair_limit_enforced(self):
        """The budget bounds a repair path that CAN change something.

        RE-PINNED (lane 3, inert-repair rule). This used to drive the budget
        with a stub runner that never wrote a byte, so the three measured
        numbers were rounds=2, generate=3, "repair budget exhausted". Under the
        inert-repair rule that is no longer the budget being tested: round 1
        provably changed nothing, so the engine now rejects on entry to round 2
        and the numbers would be rounds=1, generate=2, "INERT REPAIR PATH"
        (pinned in TestInertRepair below). The failing runner here therefore
        WRITES a distinct artifact each round, which is what a real repair
        round does — and the old measured values hold exactly.
        """
        counter = Counter()

        def failing_but_productive(engine: Engine, task):
            counter["generate"] += 1
            out = engine.task_dir(task.task_id) / "populations" / "primary.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(f"round {counter['generate']}\n", encoding="utf-8")
            return StageOutcome(
                verdict=VERDICT_FAIL, payload=StagePayload(detail="boom")
            )

        engine = self.make_engine(
            counter,
            overrides={"generate": failing_but_productive},
            max_repair_rounds=2,
        )
        engine.register(self.task)
        task = engine.run(self.task_id)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_REJECTED)
        self.assertEqual(task.status, TaskStatus.REJECTED)
        self.assertEqual(engine.repair_rounds_used(self.task_id), 2)
        # initial attempt + one retry per repair round
        self.assertEqual(counter["generate"], 3)
        latest = engine.latest_report(self.task_id, "generate")
        self.assertEqual(latest.verdict, VERDICT_FATAL)
        self.assertIn("repair budget exhausted", latest.payload_json)
        # rejection is terminal: rerun does no work
        engine.run(self.task_id)
        self.assertEqual(counter["generate"], 3)

    def test_zero_repair_budget_rejects_on_first_failure(self):
        counter = Counter()
        engine = self.make_engine(
            counter,
            overrides={"reference": fail_runner(counter, "reference")},
            max_repair_rounds=0,
        )
        engine.register(self.task)
        engine.run(self.task_id)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_REJECTED)
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        self.assertEqual(counter["reference"], 1)

    def test_runner_exception_is_a_stage_failure(self):
        counter = Counter()

        def broken(engine, task):
            counter["generate"] += 1
            raise RuntimeError("renderer exploded")

        engine = self.make_engine(counter, overrides={"generate": broken}, max_repair_rounds=1)
        engine.register(self.task)
        engine.run(self.task_id)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_REJECTED)
        self.assertEqual(counter["generate"], 2)  # initial + one repaired retry

    def test_fatal_route_rejects_without_consuming_repair_budget(self):
        counter = Counter()
        engine = self.make_engine(
            counter,
            overrides={
                "contamination_pre": fail_runner(
                    counter, "contamination_pre", detail="schema collision vs eltbench"
                )
            },
        )
        engine.register(self.task)
        task = engine.run(self.task_id)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_REJECTED)
        self.assertEqual(task.status, TaskStatus.REJECTED)
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        self.assertEqual(counter["generate"], 0)  # never ran past the gate
        latest = engine.latest_report(self.task_id, "contamination_pre")
        self.assertEqual(latest.verdict, VERDICT_FATAL)

    def test_fatal_outcome_verdict_rejects(self):
        counter = Counter()

        def fatal_gates(engine, task):
            counter["gates"] += 1
            return StageOutcome(
                verdict=VERDICT_FATAL,
                payload=StagePayload(detail="reference SQL leaked into prose"),
            )

        engine = self.make_engine(counter, overrides={"gates": fatal_gates})
        engine.register(self.task)
        engine.run(self.task_id)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_REJECTED)
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)

    def test_stale_verdict_regression_later_rejection_shadows_accept(self):
        counter = Counter()
        engine = self.make_engine(counter)
        engine.register(self.task)
        engine.run(self.task_id)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_ACCEPTED)

        # A later fatal report (e.g. post-release contamination discovery) must
        # shadow the earlier accept: append-only, MAX(id) wins.
        task = engine.load_task(self.task_id)
        engine.record_report(
            task,
            "contamination_post",
            VERDICT_FATAL,
            StagePayload(detail="near-duplicate of admitted task discovered"),
        )
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_REJECTED)
        # and run() honors the rejection as terminal
        engine.run(self.task_id)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_REJECTED)

    def test_accept_requires_current_hash(self):
        counter = Counter()
        engine = self.make_engine(counter)
        engine.register(self.task)
        engine.run(self.task_id)
        task = engine.load_task(self.task_id)
        edited = task.model_copy(update={"solver_prompt": "different prose"})
        engine.save_task(edited)
        # gates row is a pass, but bound to a stale hash: re-run, not accept
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_IN_PROGRESS)

    def test_unwired_stage_fails_closed(self):
        engine = Engine(self.workspace)  # only intake is built in
        self.addCleanup(engine.close)
        engine.register(self.task)
        with self.assertRaises(StageNotWiredError):
            engine.run(self.task_id)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_IN_PROGRESS)

    def test_until_unknown_stage_rejected(self):
        engine = self.make_engine()
        engine.register(self.task)
        with self.assertRaises(ValueError):
            engine.run(self.task_id, until="ship_it")


class TestTripwireDisposition(EngineTestCase):
    """A sanitizer trip is a HARNESS fault: exit 2 / reward None / never a
    rejection (C7). Pinned at the engine, where the disposition is decided."""

    def test_tripwire_halts_as_infrastructure_reward_none(self):
        from elt_taskgen.review.tools.projection import DiagnosticTripwire

        counter = Counter()

        def leaky_review(engine, task):
            counter["review"] += 1
            raise DiagnosticTripwire(
                "canary", "private_scalar", source="gate", quarantined=b"4711"
            )

        engine = self.make_engine(counter, overrides={StageName.REVIEW.value: leaky_review})
        engine.register(self.task)
        with self.assertRaises(engine_mod.InfrastructureFailure) as ctx:
            engine.run(self.task_id)
        # Markers are normalized to lower case by `_marker_text`.
        self.assertEqual(ctx.exception.marker, "DiagnosticTripwire".lower())
        self.assertEqual(ctx.exception.stage, StageName.REVIEW.value)
        # No repair round was spent, nothing was rejected, the quarantined
        # bytes never reached the ledger's marker, and the run resumes here.
        self.assertEqual(counter["review"], 1)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_IN_PROGRESS)
        self.assertIsNot(engine.load_task(self.task_id).status, TaskStatus.REJECTED)
        row = engine.latest_report(self.task_id, StageName.REVIEW.value)
        self.assertEqual(row.verdict, VERDICT_FAIL)
        payload = json.loads(row.payload_json)
        self.assertEqual(payload["infrastructure"].lower(), "diagnostictripwire")
        self.assertNotIn("4711", row.payload_json)
        self.assertEqual(
            engine._con.execute(
                "SELECT COUNT(*) FROM repairs WHERE task_id=?", (self.task_id,)
            ).fetchone()[0],
            0,
        )
        # Reward disposition: the class it maps to is not label-eligible, so the
        # training signal is None, never 0.0.
        exc = DiagnosticTripwire("schema", "invalid_shape")
        self.assertFalse(exc.failure_class.label_eligible)
        self.assertEqual(exc.boundary, "sanitizer")


class TestSnapshotRouting(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "ws"
        self.task_id = "demo__t"
        self.root = self.workspace / "tasks" / self.task_id

    def _write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_snapshot_and_diff(self):
        self._write("task_ir.json", "{}")
        self._write("populations/primary/rows/orders.jsonl", "r1")
        before = repair.snapshot(self.workspace, self.task_id)
        self.assertEqual(
            set(before),
            {
                f"tasks/{self.task_id}/task_ir.json",
                f"tasks/{self.task_id}/populations/primary/rows/orders.jsonl",
            },
        )
        self._write("populations/primary/rows/orders.jsonl", "r2")  # changed
        self._write("answer_key/gold/primary/stage1_counts.json", "{}")  # added
        after = repair.snapshot(self.workspace, self.task_id)
        diff = repair.diff_snapshots(before, after)
        self.assertEqual(
            diff.changed,
            frozenset(
                {
                    f"tasks/{self.task_id}/populations/primary/rows/orders.jsonl",
                    f"tasks/{self.task_id}/answer_key/gold/primary/stage1_counts.json",
                }
            ),
        )
        # unchanged file is not in the diff
        self.assertNotIn(f"tasks/{self.task_id}/task_ir.json", diff.changed)
        # removed file counts as changed
        (self.root / "task_ir.json").unlink()
        diff2 = repair.diff_snapshots(after, repair.snapshot(self.workspace, self.task_id))
        self.assertEqual(diff2.changed, frozenset({f"tasks/{self.task_id}/task_ir.json"}))

    def test_snapshot_excludes_ledger_report_copies(self):
        self._write("task_ir.json", "{}")
        self._write("reports/000001_intake.json", "{}")
        snap = repair.snapshot(self.workspace, self.task_id)
        self.assertEqual(set(snap), {f"tasks/{self.task_id}/task_ir.json"})

    def test_snapshot_missing_task_is_empty(self):
        self.assertEqual(repair.snapshot(self.workspace, "ghost"), {})

    def _diff(self, *paths: str) -> repair.ArtifactDiff:
        return repair.ArtifactDiff(changed=frozenset(paths))

    def test_route_reference_beats_everything(self):
        diff = self._diff(
            "tasks/t/answer_key/reference/customer_summary.sql",
            "tasks/t/populations/primary/rows/orders.jsonl",
            "tasks/t/task_ir.json",
        )
        self.assertIs(repair.route_from_diff(diff), RepairRoute.REFERENCE)
        gold = self._diff("tasks/t/answer_key/gold/primary/customer_summary.csv")
        self.assertIs(repair.route_from_diff(gold), RepairRoute.REFERENCE)

    def test_route_population_beats_runtime_and_spec(self):
        diff = self._diff(
            "tasks/t/populations/stress/rows/orders.jsonl",
            "tasks/t/populations/stress/rendered/postgres/orders.sql",
            "tasks/t/task_ir.json",
        )
        self.assertIs(repair.route_from_diff(diff), RepairRoute.POPULATION)

    def test_route_runtime_when_only_renders_or_sources_moved(self):
        diff = self._diff(
            "tasks/t/populations/primary/rendered/rest/orders/page_0001.json",
            "tasks/t/task/sources/postgres/load.sql",
        )
        self.assertIs(repair.route_from_diff(diff), RepairRoute.RUNTIME)

    def test_route_specification_for_prose_only(self):
        diff = self._diff("tasks/t/task_ir.json", "tasks/t/task/config.yaml")
        self.assertIs(repair.route_from_diff(diff), RepairRoute.SPECIFICATION)

    def test_route_empty_diff_fails_closed(self):
        with self.assertRaises(ValueError):
            repair.route_from_diff(self._diff())

    def test_agent_claim_is_ignored_only_hashes_matter(self):
        # "I only touched prose" — but the reference SQL hash moved.
        diff = self._diff(
            "tasks/t/task_ir.json",
            "tasks/t/answer_key/reference/customer_summary.sql",
        )
        self.assertIs(repair.route_from_diff(diff), RepairRoute.REFERENCE)


class TestFailureRouting(unittest.TestCase):
    def _p(self, detail: str) -> StagePayload:
        return StagePayload(detail=detail)

    def test_contamination_stages_are_fatal(self):
        self.assertIs(
            repair.route_for_failure("contamination_pre", self._p("anything")),
            RepairRoute.FATAL,
        )
        self.assertIs(
            repair.route_for_failure("contamination_post", self._p("anything")),
            RepairRoute.FATAL,
        )

    def test_licensing_evidence_is_fatal_anywhere(self):
        self.assertIs(
            repair.route_for_failure("select", self._p("license mismatch on source repo")),
            RepairRoute.FATAL,
        )

    def test_smoke_and_determinism_route_runtime(self):
        self.assertIs(
            repair.route_for_failure("reference", self._p("smoke check: table missing")),
            RepairRoute.RUNTIME,
        )
        # determinism wins even when gold is mentioned in the same detail
        self.assertIs(
            repair.route_for_failure(
                "reference", self._p("determinism: gold CSV differed between runs")
            ),
            RepairRoute.RUNTIME,
        )

    def test_gold_mismatch_routes_reference(self):
        self.assertIs(
            repair.route_for_failure("gates", self._p("gold mismatch on customer_summary")),
            RepairRoute.REFERENCE,
        )

    def test_gate_data_failures_route_population(self):
        self.assertIs(
            repair.route_for_failure("gates", self._p("required-mutants: inner_join kept 1.0")),
            RepairRoute.POPULATION,
        )
        self.assertIs(
            repair.route_for_failure("gates", self._p("data-sensitivity gate failed")),
            RepairRoute.POPULATION,
        )

    def test_required_mutants_leak_detail_routes_population_not_fatal(self):
        # Regression: the EXACT detail string gates._gate_required_mutants
        # emits for a leaking required mutant ("LEAK — must lose reward on
        # <pop>") is population evidence — the bare word "leak" must never be
        # read as fatal contamination.
        from elt_taskgen.models import PopulationName
        from elt_taskgen.verification import gates

        task = demo_task()
        leaked = {
            case.name: {pop: 1.0 for pop in PopulationName}
            for case in task.attack_cases
        }
        gate = gates._gate_required_mutants(task, leaked)
        self.assertFalse(gate.passed)
        self.assertIn("LEAK — must lose reward on", gate.details)
        self.assertIs(
            repair.route_for_failure("gates", self._p(gate.details)),
            RepairRoute.POPULATION,
        )

    def test_acceptance_report_routes_off_failing_gates_only(self):
        # A gate-battery payload carries EVERY gate result; the names of
        # healthy gates ("contamination-clean", "determinism") must not pick
        # the route. Only the failing required-mutants gate may.
        from elt_taskgen.models import AcceptanceReport, GateResult

        report = AcceptanceReport.from_gates(
            task_id="t",
            revision=1,
            task_content_hash="0" * 64,
            gates=(
                GateResult(gate="trusted-solution", passed=True, details="gold ok"),
                GateResult(gate="determinism", passed=True, details="3/3 identical"),
                GateResult(gate="contamination-clean", passed=True, details="clean"),
                GateResult(
                    gate="required-mutants",
                    passed=False,
                    details=(
                        "required mutant matrix not reproduced: inner_join: "
                        "LEAK — must lose reward on primary, got 1.0"
                    ),
                ),
            ),
            scorer_version="1.0.0",
        )
        self.assertIs(
            repair.route_for_failure("gates", report), RepairRoute.POPULATION
        )

    def test_acceptance_report_with_fatal_contamination_gate_stays_fatal(self):
        from elt_taskgen.models import AcceptanceReport, GateResult

        report = AcceptanceReport.from_gates(
            task_id="t",
            revision=1,
            task_content_hash="0" * 64,
            gates=(
                GateResult(gate="required-mutants", passed=True, details="0 leaks"),
                GateResult(
                    gate="contamination-clean",
                    passed=False,
                    details="fatal contamination collisions recorded: sql vs eltbench",
                ),
            ),
            scorer_version="1.0.0",
        )
        self.assertIs(repair.route_for_failure("gates", report), RepairRoute.FATAL)

    def test_a_currency_refusal_never_picks_the_route(self):
        """A gate that refused to read STALE EVIDENCE measured nothing, so it
        must not route — and two of the refusals name their remedy stage with a
        routing keyword in it ("re-run measure-target and contamination-post").

        Regression: a battery mixing ONE such refusal with a genuine population
        failure keyword-matched "contamination" and returned FATAL, so a
        healthy, fully certified task was REJECTED at round zero and the ledger
        recorded a contamination rejection that never happened."""
        from elt_taskgen.models import AcceptanceReport, GateResult

        currency = GateResult(
            gate="contamination-clean",
            passed=False,
            details=(
                "contamination scan predates type-blind shape fingerprints — "
                "re-run measure-target and contamination-post"
            ),
        )
        stale_determinism = GateResult(
            gate="determinism",
            passed=False,
            details=(
                "determinism evidence records no task_content_hash binding "
                "(re-run reference-run)"
            ),
        )
        genuine = GateResult(
            gate="info-content",
            passed=False,
            details="mart m: only 1 gold row(s) on primary",
        )

        def report(*gates_):
            return AcceptanceReport.from_gates(
                task_id="t",
                revision=1,
                task_content_hash="0" * 64,
                gates=gates_,
                scorer_version=gates_mod.SCORER_VERSION,
            )

        # MIXED: the genuine failure alone picks the route.
        self.assertIs(
            repair.route_for_failure("gates", report(currency, genuine)),
            RepairRoute.POPULATION,
        )
        # ONLY currency refusals: nothing judged, so the router falls back to
        # the stage default instead of reading a rejection into the remedy
        # text. (cli._evidence_currency_block answers BLOCKED before this.)
        self.assertIsNot(
            repair.route_for_failure("gates", report(currency, stale_determinism)),
            RepairRoute.FATAL,
        )
        # A REAL contamination finding is still fatal.
        self.assertIs(
            repair.route_for_failure(
                "gates",
                report(
                    GateResult(
                        gate="contamination-clean",
                        passed=False,
                        details="fatal contamination collisions recorded: schema:abc",
                    )
                ),
            ),
            RepairRoute.FATAL,
        )

    def test_cli_and_router_share_one_currency_reader(self):
        """`cli._is_currency_refusal` and the router must never disagree about
        what "not measured yet" means, or a battery reads BLOCKED in one place
        and rejectable in the other."""
        from elt_taskgen import cli as cli_mod
        from elt_taskgen.models import GateResult

        gate = GateResult(
            gate="determinism",
            passed=False,
            details="STALE: re-run reference-run",
        )
        self.assertTrue(cli_mod._is_currency_refusal(gate))
        self.assertTrue(repair.is_currency_refusal(gate))
        judged = GateResult(gate="info-content", passed=False, details="only 1 gold row")
        self.assertFalse(cli_mod._is_currency_refusal(judged))
        self.assertFalse(repair.is_currency_refusal(judged))
        # The mapping form (a dumped payload) reads identically.
        self.assertTrue(repair.is_currency_refusal(gate.model_dump(mode="json")))

    def test_population_gate_names_outrank_the_word_gold(self):
        """Every population gate states its complaint in terms of the gold it
        compared against, so with _REFERENCE_KEYWORDS checked first ALL of them
        routed REFERENCE — whose rerun set omits `generate` and whose proposer
        allowlist admits only the reference SQL. The router was asking an agent
        to edit the TRUSTED SOLUTION to cure a data-quality complaint."""
        cases = {
            "data-sensitivity": (
                "primary and resampled: the frozen gold does not track the "
                "recorded perturbations"
            ),
            "info-content": "mart m: only 2 gold row(s) on primary",
            "populations-load": "frozen stage-1 gold expects 12 rows for table t",
            "degenerate-zero": "empty gold mart(s): m on primary",
        }
        for gate, details in cases.items():
            with self.subTest(gate=gate):
                report = AcceptanceReport.from_gates(
                    task_id="t",
                    revision=1,
                    task_content_hash="0" * 64,
                    gates=(
                        GateResult(gate="trusted-solution", passed=True, details="ok"),
                        GateResult(gate=gate, passed=False, details=details),
                    ),
                    scorer_version=gates_mod.SCORER_VERSION,
                )
                self.assertIs(
                    repair.route_for_failure("gates_transform", report),
                    RepairRoute.POPULATION,
                )

    def test_every_population_gate_key_still_names_a_live_gate(self):
        """The keys are substrings of gate NAMES; a renamed gate must not make
        them silently dead (which is exactly how the population keywords
        became unreachable in the first place)."""
        roster = set(gates_mod.GATE_NAMES) | set(
            variant_battery.gate_roster(TaskVariant.EXTRACT_LOAD)
        ) | set(variant_battery.gate_roster(TaskVariant.TRANSFORM))
        for key in repair._POPULATION_GATE_NAMES:
            with self.subTest(key=key):
                self.assertTrue(
                    any(key in name for name in roster),
                    f"no live gate name contains {key!r}",
                )

    def test_a_failing_trusted_solution_still_routes_reference(self):
        """The discriminating half: a REFERENCE defect must not be swallowed
        by the new gate-name precedence."""
        report = AcceptanceReport.from_gates(
            task_id="t",
            revision=1,
            task_content_hash="0" * 64,
            gates=(
                GateResult(
                    gate="trusted-solution",
                    passed=False,
                    details="the trusted solution scores 0.5 on primary",
                ),
            ),
            scorer_version=gates_mod.SCORER_VERSION,
        )
        self.assertIs(
            repair.route_for_failure("gates", report), RepairRoute.REFERENCE
        )

    def test_stage_fallbacks(self):
        self.assertIs(
            repair.route_for_failure("generate", self._p("boom")), RepairRoute.POPULATION
        )
        self.assertIs(
            repair.route_for_failure("author", self._p("boom")), RepairRoute.SPECIFICATION
        )
        self.assertIs(
            repair.route_for_failure("reference", self._p("boom")), RepairRoute.RUNTIME
        )

    def test_unknown_stage_rejected(self):
        with self.assertRaises(ValueError):
            repair.route_for_failure("mystery", self._p("boom"))


class TestApplyRepair(EngineTestCase):
    def _accepted_engine(self):
        counter = Counter()
        engine = self.make_engine(counter)
        engine.register(self.task)
        engine.run(self.task_id)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_ACCEPTED)
        return engine, counter

    def test_rerun_sets(self):
        self.assertEqual(
            repair.stages_to_rerun(RepairRoute.SPECIFICATION)[:2], ("author", "review")
        )
        self.assertIn("generate", repair.stages_to_rerun(RepairRoute.POPULATION))
        self.assertNotIn("author", repair.stages_to_rerun(RepairRoute.POPULATION))
        # RUNTIME now LEADS with `generate`: a runtime repair is a mechanical
        # rebuild, and re-deriving the populations from the IR is the only
        # mechanical rebuild there is (run_generate is idempotent when nothing
        # drifted, so this costs nothing when nothing is wrong).
        self.assertEqual(
            repair.stages_to_rerun(RepairRoute.RUNTIME)[:2],
            ("generate", "reference"),
        )
        self.assertEqual(
            repair.stages_to_rerun(RepairRoute.REFERENCE)[0], "reference"
        )
        self.assertEqual(repair.stages_to_rerun(RepairRoute.FATAL), ())
        for route in (RepairRoute.SPECIFICATION, RepairRoute.POPULATION):
            self.assertIn("gates", repair.stages_to_rerun(route))
            self.assertIn("gates_extract_load", repair.stages_to_rerun(route))
            self.assertIn("gates_transform", repair.stages_to_rerun(route))
            self.assertIn("calibrate", repair.stages_to_rerun(route))

    def test_population_repair_invalidates_downstream(self):
        engine, _ = self._accepted_engine()
        task = engine.load_task(self.task_id)
        repaired = repair.apply_repair(
            engine, task, RepairRoute.POPULATION, "primary population too easy"
        )
        # revision lineage advanced; repair round consumed
        self.assertEqual(repaired.current_revision, task.current_revision + 1)
        self.assertEqual(repaired.status, TaskStatus.IN_REPAIR)
        self.assertEqual(engine.repair_rounds_used(self.task_id), 1)
        # gold, attacks, gate evidence, calibration (and later) invalidated
        for stage in (
            "generate",
            "reference",
            "attack",
            "gates",
            "gates_extract_load",
            "gates_transform",
            "calibrate",
            "contamination_post",
            "select",
            "release",
        ):
            self.assertEqual(
                engine.latest_report(self.task_id, stage).verdict, VERDICT_FAIL, stage
            )
        # prose stages untouched
        for stage in ("author", "review"):
            self.assertEqual(
                engine.latest_report(self.task_id, stage).verdict, VERDICT_PASS, stage
            )
        # the earlier accept no longer stands
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_IN_PROGRESS)

    def test_repair_then_run_reaccepts_at_same_identity(self):
        engine, counter = self._accepted_engine()
        task = engine.load_task(self.task_id)
        repair.apply_repair(engine, task, RepairRoute.RUNTIME, "flaky renderer")
        engine.run(self.task_id)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_ACCEPTED)
        # only the invalidated stages re-ran
        self.assertEqual(counter["reference"], 2)
        self.assertEqual(counter["gates"], 2)
        self.assertEqual(counter["gates_extract_load"], 2)
        self.assertEqual(counter["gates_transform"], 2)
        # `generate` is part of the RUNTIME rerun set (the mechanical rebuild).
        self.assertEqual(counter["generate"], 2)
        self.assertEqual(counter["author"], 1)

    def test_specification_repair_leaves_data_pipeline_alone(self):
        engine, counter = self._accepted_engine()
        task = engine.load_task(self.task_id)
        repair.apply_repair(engine, task, RepairRoute.SPECIFICATION, "ambiguous grain prose")
        self.assertEqual(engine.latest_report(self.task_id, "generate").verdict, VERDICT_PASS)
        self.assertEqual(engine.latest_report(self.task_id, "author").verdict, VERDICT_FAIL)
        engine.run(self.task_id)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_ACCEPTED)
        self.assertEqual(counter["generate"], 1)
        self.assertEqual(counter["author"], 2)

    def test_apply_repair_fatal_raises(self):
        engine, _ = self._accepted_engine()
        task = engine.load_task(self.task_id)
        with self.assertRaises(ValueError):
            repair.apply_repair(engine, task, RepairRoute.FATAL, "contaminated")
        # nothing committed: still accepted, no repair rounds consumed
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_ACCEPTED)

    def test_apply_repair_requires_reason(self):
        engine, _ = self._accepted_engine()
        task = engine.load_task(self.task_id)
        with self.assertRaises(ValueError):
            repair.apply_repair(engine, task, RepairRoute.RUNTIME, "")

    def test_invalidation_skips_never_run_stages(self):
        counter = Counter()
        engine = self.make_engine(counter)
        engine.register(self.task)
        engine.run(self.task_id, until="reference")
        task = engine.load_task(self.task_id)
        repair.apply_repair(engine, task, RepairRoute.POPULATION, "weak data")
        self.assertEqual(engine.latest_report(self.task_id, "generate").verdict, VERDICT_FAIL)
        # gates never ran: no phantom invalidation row for it
        self.assertIsNone(engine.latest_report(self.task_id, "gates"))


class InfraMarkerChainTest(unittest.TestCase):
    """`_infra_marker_for` walks `__cause__` / `__context__` (bounded, cycle
    safe; Phase 0 review finding 4), so a harness fault a wrapper swallowed
    still halts the ladder instead of reading as a task defect (C7)."""

    def test_infra_marker_walks_cause_and_context(self):
        from elt_taskgen.review import providers as providers_mod
        from elt_taskgen.review.tools.projection import DiagnosticTripwire

        marker = engine_mod._infra_marker_for
        budget = providers_mod.BudgetExceededError("per-task budget", scope="task")
        explicit = RuntimeError("wrapped")
        explicit.__cause__ = budget
        self.assertEqual(marker(explicit), "BudgetExceededError")
        self.assertEqual(engine_mod._budget_scope_for(explicit), "task")
        total = providers_mod.BudgetExceededError(
            "message says task but attribute is authoritative", scope="total"
        )
        outer = RuntimeError("message says role")
        outer.__cause__ = total
        self.assertEqual(engine_mod._budget_scope_for(outer), "total")
        self.assertEqual(
            engine_mod._budget_scope_for(providers_mod.RoleCapExceeded("cap")),
            "",
        )
        foreign = RuntimeError("not a budget exception")
        foreign.scope = "task"
        self.assertEqual(engine_mod._budget_scope_for(foreign), "")
        try:
            try:
                raise providers_mod.TranscriptMissingError("replay-only: no transcript")
            except providers_mod.TranscriptMissingError:
                raise ValueError("raised while handling the miss")
        except ValueError as exc:
            implicit = exc
        self.assertEqual(marker(implicit), "TranscriptMissingError")
        try:
            try:
                raise DiagnosticTripwire("schema", "unknown_key")
            except DiagnosticTripwire:
                raise RuntimeError("hidden") from None
        except RuntimeError as exc:
            suppressed = exc
        self.assertTrue(suppressed.__suppress_context__)
        self.assertEqual(marker(suppressed), "DiagnosticTripwire")
        deep = RuntimeError("outer")
        mid = KeyError("mid")
        deep.__cause__ = mid
        mid.__context__ = RuntimeError("provider http 503 after 4 retries")
        self.assertEqual(marker(deep), "provider http")
        infra = engine_mod.InfrastructureFailure("t", "review", "sandboxfault")
        self.assertEqual(infra.budget_scope, "")
        self.assertIsNone(
            StagePayload.model_validate(
                {"infrastructure": "BudgetExceededError"}
            ).budget_scope
        )
        wrapper = RuntimeError("x")
        wrapper.__cause__ = infra
        self.assertEqual(marker(wrapper), "sandboxfault")
        self.assertEqual(marker(infra), "sandboxfault")
        # The cause outranks the context.
        both = RuntimeError("both")
        both.__cause__ = providers_mod.MissingCredentialsError("no key")
        both.__context__ = budget
        self.assertEqual(marker(both), "MissingCredentialsError")
        # Cycles terminate; a chain with no fault is ''.
        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__cause__ = b
        b.__cause__ = a
        self.assertEqual(engine_mod._exception_chain(a), [a, b])
        self.assertEqual(marker(a), "")
        self.assertEqual(marker(RuntimeError("just a bug")), "")
        # The walk is bounded.
        head = RuntimeError("0")
        link = head
        for i in range(1, engine_mod._MAX_EXCEPTION_CHAIN + 5):
            nxt = RuntimeError(str(i))
            link.__cause__ = nxt
            link = nxt
        link.__cause__ = budget
        self.assertEqual(len(engine_mod._exception_chain(head)), engine_mod._MAX_EXCEPTION_CHAIN)
        self.assertEqual(marker(head), "")


class StageBlockedTest(EngineTestCase):
    """`StageBlocked`: a stage that WAITED (`VERDICT_BLOCKED`) where only a
    measurement would do — inside a repair proposer's certification — is an
    `InfrastructureFailure` under `blocked_on:<reason>` (a no-measure
    outcome, C7), classified by the same marker walk as every other halt,
    and `_halt_on_proposer_fault` puts the reason on the FAIL row."""

    def test_stage_blocked_is_an_infrastructure_failure_carrying_the_reason(self):
        cases = (
            (engine_mod.BLOCKED_ON_ENVIRONMENT, "environment"),
            (engine_mod.BLOCKED_ON_HUMAN, "human"),
            ("Session_Limit:Turns", "session_limit:turns"),
            ("", engine_mod.BLOCKED_ON_UNKNOWN),
            ("  ", engine_mod.BLOCKED_ON_UNKNOWN),
        )
        for reason, expected in cases:
            with self.subTest(blocked_on=reason or "(none)"):
                exc = engine_mod.StageBlocked("task-1", "review", reason)
                marker = f"{engine_mod.BLOCKED_STAGE_MARKER_PREFIX}{expected}"
                self.assertIsInstance(exc, engine_mod.InfrastructureFailure)
                self.assertIsInstance(exc, engine_mod.EngineError)
                self.assertEqual((exc.task_id, exc.stage, exc.blocked_on, exc.marker), ("task-1", "review", expected, marker))
                self.assertEqual(engine_mod._infra_marker_for(exc), marker)
                wrapper = RuntimeError("wrapped by a certifier")
                wrapper.__cause__ = exc
                self.assertEqual(engine_mod._infra_marker_for(wrapper), marker)
                self.assertEqual(engine_mod.blocked_stage_marker(reason), marker)
                self.assertEqual(engine_mod.blocked_on_from_marker(marker), expected)
                self.assertEqual(engine_mod.blocked_on_from_marker(marker.upper()), expected)
                self.assertIn("no repair round is spent", str(exc))
                self.assertIn(f"blocked_on={expected}", str(exc))
        # A marker of any other kind names no reason.
        for other in ("", "sandboxfault", "TranscriptMissingError", "blocked", "blocked_on"):
            self.assertEqual(engine_mod.blocked_on_from_marker(other), "")
        self.assertEqual(engine_mod.blocked_on_from_marker("blocked_on:"), engine_mod.BLOCKED_ON_UNKNOWN)
        # `blocked_on_of` reads the payload's `data.blocked_on` and nothing else.
        payload = StagePayload(detail="waiting", data={engine_mod.BLOCKED_ON_KEY: "Human"})
        self.assertEqual(engine_mod.blocked_on_of(payload), "human")
        self.assertEqual(engine_mod.blocked_on_of(StagePayload(detail="waiting")), "")
        self.assertEqual(engine_mod.blocked_on_of(StagePayload(error="blocked_on: human")), "")
        self.assertEqual(engine_mod.blocked_on_of({"data": {"blocked_on": "environment"}}), "environment")
        self.assertEqual(engine_mod.blocked_on_of({"blocked_on": "environment"}), "")
        self.assertEqual(engine_mod.blocked_on_of(None), "")
        self.assertEqual(engine_mod.blocked_on_of("blocked_on: human"), "")

    def test_halt_on_proposer_fault_puts_the_blocked_reason_on_the_row(self):
        engine = self.make_engine()
        engine.register(self.task)
        task = engine.load_task(self.task_id)
        marker = engine_mod.blocked_stage_marker(engine_mod.BLOCKED_ON_HUMAN)
        with self.assertRaises(engine_mod.InfrastructureFailure) as ctx:
            engine._halt_on_proposer_fault(
                task, StageName.REVIEW, RepairRoute.SPECIFICATION, marker, "StageBlocked: waiting"
            )
        self.assertEqual((ctx.exception.stage, ctx.exception.marker), ("review", marker))
        row = engine.latest_report(task.task_id, "review")
        self.assertEqual(row.verdict, VERDICT_FAIL)
        payload = json.loads(row.payload_json)
        self.assertEqual(payload["infrastructure"], marker)
        self.assertEqual(payload["data"][engine_mod.BLOCKED_ON_KEY], engine_mod.BLOCKED_ON_HUMAN)
        self.assertEqual(payload["data"]["status"], "halted")
        self.assertEqual(engine.repair_rounds_used(task.task_id), 0)
        self.assertIsNot(engine.load_task(task.task_id).status, TaskStatus.REJECTED)
        # A halt under any other marker names no reason.
        with self.assertRaises(engine_mod.InfrastructureFailure):
            engine._halt_on_proposer_fault(
                task, StageName.REVIEW, RepairRoute.SPECIFICATION, "sandboxfault", "worker died"
            )
        payload = json.loads(engine.latest_report(task.task_id, "review").payload_json)
        self.assertNotIn(engine_mod.BLOCKED_ON_KEY, payload["data"])


if __name__ == "__main__":
    unittest.main()

"""Group-H fixes whose natural home is a test file this group does not own.

Everything here is offline: stub runners, hand-written evidence, and mocks of
the two release helpers whose real implementations belong to another module.
Each test names the measured defect it pins.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elt_taskgen import catalog, cli, demo_fixture
from elt_taskgen import engine as engine_mod
from elt_taskgen.corpus.selection import SelectionResult
from elt_taskgen.engine import (
    Engine,
    EngineError,
    StageName,
    StageOutcome,
    StagePayload,
    VERDICT_BLOCKED,
    VERDICT_FAIL,
    VERDICT_PASS,
    variant_gate_stage,
)
from elt_taskgen.export import release as release_mod
from elt_taskgen.models import (
    AcceptanceReport,
    GateResult,
    RLVR_TASK_VARIANTS,
    TaskVariant,
    canonical_json,
    variant_task_id,
)
from elt_taskgen.verification import gates as gates_mod
from elt_taskgen.verification import variant_battery


def battery_report(task, variant, *, scorer_version=None, roster_digest=None):
    variant = TaskVariant(variant)
    roster = variant_battery.gate_roster(variant)
    return AcceptanceReport.from_gates(
        task_id=variant_task_id(task.task_id, variant),
        revision=task.current_revision,
        task_content_hash=task.content_hash(),
        gates=tuple(
            GateResult(gate=name, passed=True, details="fixture pass")
            for name in roster
        ),
        scorer_version=(
            gates_mod.SCORER_VERSION if scorer_version is None else scorer_version
        ),
        roster_digest=(
            gates_mod.ROSTER_DIGEST if roster_digest is None else roster_digest
        ),
        roster=roster,
    )


class ReleaseStageTestCase(unittest.TestCase):
    """`release` on a workspace whose ledger is already at its door."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "ws"
        self.engine = Engine(self.workspace)
        self.addCleanup(self.engine.close)
        self.task = demo_fixture.demo_task()
        self.engine.register(self.task)
        (cli._evidence_dir(self.engine, self.task) / "selection.json").write_text(
            SelectionResult(
                train=(self.task.task_id,),
                val=(),
                rejected={},
                variants={self.task.task_id: [v.value for v in RLVR_TASK_VARIANTS]},
                rejected_variants={},
            ).model_dump_json(),
            encoding="utf-8",
        )
        for variant in RLVR_TASK_VARIANTS:
            self.engine.record_report(
                self.task,
                variant_gate_stage(variant).value,
                VERDICT_PASS,
                battery_report(self.task, variant),
            )

    def write_release(self, **overrides):
        """A release dir that NAMES this task at its current content hash."""
        out = self.workspace / "release"
        out.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": release_mod.RELEASE_SCHEMA_VERSION,
            "release_id": "release-deadbeefdeadbeef",
            "corpus_profile": release_mod.COMBINED_CORPUS_PROFILE,
            "public_layout": release_mod.COMBINED_PUBLIC_LAYOUT,
            "tasks": {self.task.task_id: self.task.content_hash()},
            "variants": {
                self.task.task_id: [v.value for v in RLVR_TASK_VARIANTS]
            },
            # A 2.1 release names the graded EL source roots it ships; a
            # manifest without them is a STALE release and 'already released'
            # would be a false statement (the EL reward is over every graded
            # population, and a 2.0 tree shipped only `development`).
            "el_sources": {
                self.task.task_id: {
                    pop.name.value: (
                        f"private/{self.task.task_id}/populations/"
                        f"{pop.name.value}/rendered"
                    )
                    for pop in self.task.populations
                }
            },
        }
        manifest.update(overrides)
        (out / "release_manifest.json").write_text(
            canonical_json(manifest), encoding="utf-8"
        )
        return out

    def arm_coverage(self):
        """Make the firewall grade ARMED without a benchmark checkout."""
        index = cli._contamination_index(self.engine)
        return mock.patch.object(
            type(index),
            "coverage",
            lambda self: _ArmedCoverage(),
        )

    def assert_not_rejected_through_the_engine(self, stage, outcome):
        """Drive an outcome through the ENGINE'S OWN dispatch and assert the
        task survives it.

        Pinning only `outcome.verdict` is how the C2 refusal shipped broken: it
        was a FAIL with no explicit route, and `repair.route_for_failure` reads
        the word "contamination" as FATAL, so `_handle_failure` wrote a FATAL
        row and rejected a fully certified task (verdict=fail -> route=FATAL ->
        status REJECTED, final_verdict 'rejected', release row 'fatal').
        """
        from elt_taskgen.engine import FINAL_REJECTED
        from elt_taskgen.models import TaskStatus

        if outcome.verdict == VERDICT_BLOCKED:
            # Exactly what Engine.run does with a BLOCKED outcome.
            self.engine.record_report(
                self.task, stage.value, VERDICT_BLOCKED, outcome.payload
            )
        else:
            self.engine._handle_failure(self.task, stage, outcome)
        row = self.engine.latest_report(self.task.task_id, stage.value)
        self.assertIsNotNone(row)
        self.assertNotEqual(row.verdict, "fatal", row.payload_json)
        self.assertIsNot(
            self.engine.load_task(self.task.task_id).status, TaskStatus.REJECTED
        )
        self.assertNotEqual(
            self.engine.final_verdict(self.task.task_id), FINAL_REJECTED
        )
        self.assertEqual(self.engine.repair_rounds_used(self.task.task_id), 0)


class _ArmedCoverage:
    """Duck-typed IndexCoverage stand-in that grades ARMED."""

    @property
    def level(self):
        from elt_taskgen.verification import contamination as cont

        return cont.CoverageLevel.ARMED

    def summary(self) -> str:
        return "firewall armed (test stub)"


class TestReleaseAlreadyReleasedShortcut(ReleaseStageTestCase):
    """The shortcut used to trust the manifest's own words.

    Measured on a scratch copy of runs/synsql_elt: tampering
    private/<tid>__t/reward.json left `verify_release(...).ok` False while
    `run_release` still reported PASS "already released as release-...".
    """

    def test_named_but_unverifiable_release_blocks_on_the_environment(self):
        """A corrupt tree in the way is the same condition as a FOREIGN tree in
        the way, and gets the same answer. As an unrouted FAIL,
        `route_for_failure('release', ...)` gave RUNTIME, which spends the
        repair budget re-running deterministic stages and ends in the
        inert/budget FATAL that rejects a certified task."""
        self.write_release()
        verification = mock.Mock(
            ok=False, failures=("private/x/reward.json: sha256 mismatch",),
            files_checked=3,
        )
        with mock.patch(
            "elt_taskgen.export.release.verify_release", return_value=verification
        ):
            outcome = cli.run_release(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_BLOCKED)
        self.assertEqual(outcome.payload.data["blocked_on"], "environment")
        self.assertIn("FAILS byte verification", outcome.payload.error)
        self.assertIn("sha256 mismatch", outcome.payload.error)
        self.assertIn("move it aside", outcome.payload.error)
        self.assert_not_rejected_through_the_engine(StageName.RELEASE, outcome)

    def test_verified_release_passes_and_indexes_the_admitted_task(self):
        self.write_release()
        verification = mock.Mock(ok=True, failures=(), files_checked=7)
        with mock.patch(
            "elt_taskgen.export.release.verify_release", return_value=verification
        ):
            outcome = cli.run_release(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_PASS)
        self.assertIn("already released", outcome.payload.detail)
        self.assertIn("7 file(s) re-verified", outcome.payload.detail)
        # The admitted-corpus index is what stops a later near-duplicate from
        # being ingested; the shortcut used to skip it, so whether it happened
        # depended on which branch the release took.
        self.assertTrue(
            cli._contamination_index(self.engine).is_armed()
            or (self.workspace / "state" / "contamination").is_dir()
        )

    def test_a_release_without_el_sources_is_not_already_released(self):
        """A 2.0 tree named this task at this hash but shipped only the
        development population's rendered root, so four of five graded
        populations were unscorable from the release alone."""
        out = self.write_release()
        manifest = json.loads(
            (out / "release_manifest.json").read_text(encoding="utf-8")
        )
        manifest.pop("el_sources")
        (out / "release_manifest.json").write_text(
            canonical_json(manifest), encoding="utf-8"
        )
        outcome = cli.run_release(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_BLOCKED)
        self.assertIn("move it aside", outcome.payload.error)

    def test_a_foreign_release_dir_blocks_on_the_environment(self):
        """A directory in the way is not a task defect. Routed as an ordinary
        FAIL this spent the whole repair budget re-running batteries and then
        rejected a certified task."""
        out = self.workspace / "release"
        out.mkdir(parents=True)
        (out / "release_manifest.json").write_text(
            canonical_json({"release_id": "other", "tasks": {"someone_else": "x"}}),
            encoding="utf-8",
        )
        outcome = cli.run_release(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_BLOCKED)
        self.assertEqual(outcome.payload.data["blocked_on"], "environment")
        self.assertIn("move it aside", outcome.payload.error)
        self.assert_not_rejected_through_the_engine(StageName.RELEASE, outcome)


class TestReleaseRequiresAnArmedFirewall(ReleaseStageTestCase):
    """C2 option (b): `release` is the only stage that emits trainable data, so
    it requires coverage ARMED unconditionally rather than via an env var an
    operator can forget. Every other call point stays warn-only."""

    def test_release_refuses_a_name_only_firewall(self):
        outcome = cli.run_release(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_BLOCKED)
        self.assertEqual(outcome.payload.data["blocked_on"], "environment")
        self.assertIn("not ARMED", outcome.payload.error)
        self.assertIn("measure-target", outcome.payload.error)
        self.assertFalse((self.workspace / "release").exists())
        # THE POINT OF THE FIX. The refusal necessarily says "contamination",
        # which repair.route_for_failure reads as a FATAL keyword — so as an
        # unrouted FAIL this destroyed a fully certified task and recorded a
        # contamination rejection that never happened.
        self.assert_not_rejected_through_the_engine(StageName.RELEASE, outcome)

    def test_the_refusal_text_would_route_fatal_if_it_were_a_failure(self):
        """Why the outcome above MUST NOT be a plain VERDICT_FAIL."""
        from elt_taskgen import repair as repair_mod

        outcome = cli.run_release(self.engine, self.task)
        route = repair_mod.route_for_failure(
            StageName.RELEASE.value, outcome.payload
        )
        self.assertIs(route, repair_mod.RepairRoute.FATAL)

    def test_an_armed_firewall_reaches_the_freeze(self):
        frozen = mock.Mock(
            release_id="release-1", tasks={self.task.task_id: "h"},
            checksums={}, scorer_version=gates_mod.SCORER_VERSION,
            generator_version="0",
        )
        verification = mock.Mock(ok=True, failures=(), files_checked=11)
        with self.arm_coverage(), mock.patch(
            "elt_taskgen.export.release.freeze_release", return_value=frozen
        ) as freeze, mock.patch(
            "elt_taskgen.export.release.verify_release", return_value=verification
        ):
            outcome = cli.run_release(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_PASS)
        self.assertEqual(freeze.call_count, 1)
        # The freeze SELF-VERIFIES: verify_release existed but was called from
        # nowhere in src/, and the documented `shasum -c` remedy skips every
        # census-pinned .duckdb warehouse.
        self.assertEqual(outcome.payload.data["verified_files"], "11")

    def test_a_freeze_that_does_not_verify_blocks_on_the_environment(self):
        frozen = mock.Mock(
            release_id="release-1", tasks={self.task.task_id: "h"},
            checksums={}, scorer_version=gates_mod.SCORER_VERSION,
            generator_version="0",
        )
        verification = mock.Mock(
            ok=False, failures=("public/x/config.yaml: missing",), files_checked=11
        )
        with self.arm_coverage(), mock.patch(
            "elt_taskgen.export.release.freeze_release", return_value=frozen
        ), mock.patch(
            "elt_taskgen.export.release.verify_release", return_value=verification
        ):
            outcome = cli.run_release(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_BLOCKED)
        self.assertEqual(outcome.payload.data["blocked_on"], "environment")
        self.assertIn("does not verify against its own manifest", outcome.payload.error)
        # No repair round can make a partial write verify: routed as a FAIL
        # this spends the budget on deterministic re-runs and rejects the task.
        self.assert_not_rejected_through_the_engine(StageName.RELEASE, outcome)

    def test_environment_drift_blocks_instead_of_raising_out_of_the_freeze(self):
        """P1's drift check lives in export/release.py (`environment_drift`),
        and `freeze_release` raises a ValueError on drift. Left to the engine's
        catch-all that ValueError becomes an ordinary FAIL, routes RUNTIME,
        spends a repair round on a deterministic re-run and rejects the task
        for an ENVIRONMENT condition. The stage answers BLOCKED first.

        (The pre-fix code imported a module named `elt_taskgen.environment`,
        which does not exist, so `except ImportError: return None, {}` made the
        whole guard dead code.)"""
        frozen = mock.Mock(
            release_id="release-1", tasks={self.task.task_id: "h"},
            checksums={}, scorer_version=gates_mod.SCORER_VERSION,
            generator_version="0",
        )
        drift = {"duckdb": ("9.9.9", "1.5.5")}
        with self.arm_coverage(), mock.patch(
            "elt_taskgen.export.release.environment_drift", return_value=drift
        ), mock.patch(
            "elt_taskgen.export.release.freeze_release", return_value=frozen
        ) as freeze:
            outcome = cli.run_release(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_BLOCKED)
        self.assertEqual(outcome.payload.data["blocked_on"], "environment")
        self.assertIn("duckdb", outcome.payload.error)
        self.assertIn("uv sync --frozen", outcome.payload.error)
        self.assertEqual(freeze.call_count, 0)
        self.assert_not_rejected_through_the_engine(StageName.RELEASE, outcome)

    def test_allow_unlocked_env_records_the_drift_instead_of_hiding_it(self):
        frozen = mock.Mock(
            release_id="release-1", tasks={self.task.task_id: "h"},
            checksums={}, scorer_version=gates_mod.SCORER_VERSION,
            generator_version="0",
        )
        verification = mock.Mock(ok=True, failures=(), files_checked=11)
        drift = {"duckdb": ("9.9.9", "1.5.5")}
        self.engine.allow_unlocked_env = True
        with self.arm_coverage(), mock.patch(
            "elt_taskgen.export.release.environment_drift", return_value=drift
        ), mock.patch(
            "elt_taskgen.export.release.freeze_release", return_value=frozen
        ), mock.patch(
            "elt_taskgen.export.release.verify_release", return_value=verification
        ):
            outcome = cli.run_release(self.engine, self.task)
        self.assertEqual(outcome.verdict, VERDICT_PASS)
        self.assertIn("duckdb", outcome.payload.data["environment_drift"])


class TestRosterStalenessIsNotAFailure(unittest.TestCase):
    """A gate added after a battery was recorded must make the stage RE-RUN,
    not make the task unreleasable.

    Measured on all five kept drives: `mart-key-unique` landed after they were
    certified, so every battery row was PASS at the current hash while
    `final_verdict` answered in_progress forever — `release --replay-only`
    re-ran nothing and exited 1, and `export` died in a ValueError traceback.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "ws"
        self.engine = Engine(self.workspace)
        self.addCleanup(self.engine.close)
        self.task = demo_fixture.demo_task()
        self.engine.register(self.task)

    def test_a_battery_recorded_under_an_older_scorer_is_not_current(self):
        stage = variant_gate_stage(TaskVariant.TRANSFORM)
        self.engine.record_report(
            self.task,
            stage.value,
            VERDICT_PASS,
            battery_report(
                self.task, TaskVariant.TRANSFORM, scorer_version="1.0.0"
            ),
        )
        row = self.engine.latest_report(self.task.task_id, stage.value)
        ok, why = self.engine.report_is_current(self.task, stage, row)
        self.assertFalse(ok)
        self.assertIn("scorer", why)
        # ... and the same predicate answers the acceptance question, so the
        # two can never disagree about the same row.
        self.assertNotEqual(self.engine.final_verdict(self.task.task_id), "accepted")

    def test_a_battery_recorded_under_an_older_roster_is_not_current(self):
        stage = variant_gate_stage(TaskVariant.TRANSFORM)
        self.engine.record_report(
            self.task,
            stage.value,
            VERDICT_PASS,
            battery_report(
                self.task, TaskVariant.TRANSFORM, roster_digest="deadbeefdeadbeef"
            ),
        )
        row = self.engine.latest_report(self.task.task_id, stage.value)
        ok, why = self.engine.report_is_current(self.task, stage, row)
        self.assertFalse(ok)
        self.assertIn("roster", why)

    def test_the_stale_battery_re_runs_without_a_repair_round(self):
        calls: list[str] = []

        def battery(variant):
            def run(engine, task):
                calls.append(variant.value)
                return StageOutcome(
                    VERDICT_PASS, battery_report(task, variant)
                )

            return run

        runners = {
            stage.value: (
                lambda e, t, s=stage: StageOutcome(
                    VERDICT_PASS, StagePayload(detail=f"{s.value} ok")
                )
            )
            for stage in StageName
        }
        for variant in RLVR_TASK_VARIANTS:
            runners[variant_gate_stage(variant).value] = battery(variant)
        engine = Engine(self.workspace, stage_runners=runners)
        self.addCleanup(engine.close)
        # Seed the two batteries as they exist on the kept drives: PASS at the
        # current hash, recorded under the PREVIOUS scorer.
        for variant in RLVR_TASK_VARIANTS:
            engine.record_report(
                self.task,
                variant_gate_stage(variant).value,
                VERDICT_PASS,
                battery_report(self.task, variant, scorer_version="1.0.0"),
            )
        engine.run(self.task.task_id)

        self.assertEqual(sorted(calls), ["extract_load", "transform"])
        self.assertEqual(engine.repair_rounds_used(self.task.task_id), 0)
        self.assertEqual(engine.final_verdict(self.task.task_id), "accepted")

    def test_the_printed_ledger_says_STALE_rather_than_ACCEPTED(self):
        """`14/14 gates passed -> variant ACCEPTED` printed while the same row
        made the final verdict in_progress; a reader had no way to tell."""
        stage = variant_gate_stage(TaskVariant.EXTRACT_LOAD)
        self.engine.record_report(
            self.task,
            stage.value,
            VERDICT_PASS,
            battery_report(
                self.task, TaskVariant.EXTRACT_LOAD, scorer_version="1.0.0"
            ),
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._print_stage_report(self.engine, self.task.task_id)
        printed = buf.getvalue()
        self.assertIn("STALE", printed)
        self.assertNotIn("variant ACCEPTED", printed)


def refused_battery(task, variant, *, gate, details):
    """A battery that refuses for exactly one reason."""
    variant = TaskVariant(variant)
    roster = variant_battery.gate_roster(variant)
    return AcceptanceReport.from_gates(
        task_id=variant_task_id(task.task_id, variant),
        revision=task.current_revision,
        task_content_hash=task.content_hash(),
        gates=tuple(
            GateResult(
                gate=name,
                passed=name != gate,
                details=details if name == gate else "fixture pass",
            )
            for name in roster
        ),
        scorer_version=gates_mod.SCORER_VERSION,
        roster_digest=gates_mod.ROSTER_DIGEST,
        roster=roster,
    )


class TestAReleaseRowIsNotAReleaseTree(unittest.TestCase):
    """`release` must re-freeze when the tree its ledger row attests to is gone.

    Measured on a scratch copy of runs/synsql_elt: with `release/` moved aside,
    the PASS-at-current-hash row made the stage SKIP, the CLI printed "frozen
    release release-3616cd60 (1 task(s), 37 files)" straight out of the old
    payload and answered `final verdict: accepted` — for a release directory
    that did not exist. A ledger row is a statement ABOUT an artifact, never a
    substitute for one.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "ws"
        self.engine = Engine(self.workspace)
        self.addCleanup(self.engine.close)
        self.task = demo_fixture.demo_task()
        self.engine.register(self.task)
        self.engine.record_report(
            self.task,
            StageName.RELEASE.value,
            VERDICT_PASS,
            StagePayload(detail="frozen release release-deadbeef (1 task(s), 37 files)"),
        )
        self.row = self.engine.latest_report(self.task.task_id, StageName.RELEASE.value)

    def _release_dir(self) -> Path:
        return self.workspace / engine_mod.RELEASE_DIRNAME

    def test_the_tree_can_be_moved_aside_and_the_stage_reruns(self):
        out = self._release_dir()
        out.mkdir(parents=True)
        manifest = out / engine_mod.RELEASE_MANIFEST_FILENAME
        manifest.write_text("{}", encoding="utf-8")
        self.assertTrue(
            self.engine.report_is_current(self.task, StageName.RELEASE, self.row)[0]
        )
        manifest.unlink()
        ok, why = self.engine.report_is_current(self.task, StageName.RELEASE, self.row)
        self.assertFalse(ok, "a release dir with no manifest still read as current")
        self.assertIn(engine_mod.RELEASE_MANIFEST_FILENAME, why)

    def test_other_stages_are_unaffected_by_the_release_tree(self):
        """Only RELEASE attests to that tree; nothing else may key on it."""
        self.engine.record_report(
            self.task,
            StageName.GENERATE.value,
            VERDICT_PASS,
            StagePayload(detail="built 5 population(s)"),
        )
        row = self.engine.latest_report(self.task.task_id, StageName.GENERATE.value)
        self.assertFalse(self._release_dir().exists())
        ok, why = self.engine.report_is_current(self.task, StageName.GENERATE, row)
        self.assertTrue(ok, why)


class TestStaleEvidenceBlockIsActuallyClearable(unittest.TestCase):
    """A BLOCKED remedy that names a stage the ladder will SKIP is a no-op.

    Measured on a copy of runs/synsql_elt: `release --replay-only` blocked at
    the batteries naming "re-run reference-run"; plain `reference-run` exited 0
    having done nothing (engine.run skips a PASS at the current hash), the
    determinism record still lacked `task_content_hash`, and `release` blocked
    again. Only `reference-run --re-emit` cleared it. All five kept drives are
    in exactly that state.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "ws"
        self.engine = Engine(self.workspace)
        self.addCleanup(self.engine.close)
        self.task = demo_fixture.demo_task()
        self.engine.register(self.task)

    def _pass(self, stage):
        self.engine.record_report(
            self.task, stage.value, VERDICT_PASS, StagePayload(detail="ok")
        )

    def test_a_currency_refusal_schedules_the_stage_that_re_derives_it(self):
        self._pass(StageName.REFERENCE)
        stale = (
            "determinism: determinism evidence records no task_content_hash "
            "binding — re-run reference-run; fail closed"
        )
        with self.engine.task_locks((self.task.task_id,)):
            outcome = cli._blocked_on_stale_evidence(
                self.engine, self.task, StageName.GATES_EXTRACT_LOAD, stale
            )
        self.assertEqual(outcome.verdict, VERDICT_BLOCKED)
        self.assertEqual(outcome.payload.data["blocked_on"], "environment")
        self.assertEqual(outcome.payload.data["rerun_scheduled"], "reference")
        # APPEND-ONLY: the pass is shadowed, never mutated or deleted.
        rows = self.engine._con.execute(
            "SELECT verdict FROM reports WHERE task_id=? AND stage=? ORDER BY id",
            (self.task.task_id, StageName.REFERENCE.value),
        ).fetchall()
        self.assertEqual([r[0] for r in rows], ["pass", "fail"])
        row = self.engine.latest_report(
            self.task.task_id, StageName.REFERENCE.value
        )
        self.assertFalse(
            self.engine.report_is_current(self.task, StageName.REFERENCE, row)[0]
        )
        # No repair round and no rejection: this is a WAIT.
        self.assertEqual(self.engine.repair_rounds_used(self.task.task_id), 0)

    def test_a_refusal_no_stage_can_clear_names_the_operator_command(self):
        """`measure-target` needs an ELT-Bench checkout, so nothing in the
        ladder can re-derive it — the remedy must say so instead of naming a
        stage that would do nothing."""
        stale = (
            "contamination-clean: contamination scan predates type-blind shape "
            "fingerprints — re-run measure-target and contamination-post"
        )
        outcome = cli._blocked_on_stale_evidence(
            self.engine, self.task, StageName.TASK_INTEGRITY, stale
        )
        self.assertEqual(outcome.verdict, VERDICT_BLOCKED)
        self.assertNotIn("rerun_scheduled", outcome.payload.data)
        self.assertIn("measure-target", outcome.payload.error)
        self.assertIn("elt-taskgen validate", outcome.payload.error)
        self.assertIn(self.task.task_id, outcome.payload.error)

    def test_a_downstream_stage_is_never_shadowed_as_a_producer(self):
        """A battery cannot be its own producer, and `contamination_post` runs
        AFTER the batteries — shadowing it would say a stage re-derived
        something it has not run yet."""
        self._pass(StageName.CONTAMINATION_POST)
        outcome = cli._blocked_on_stale_evidence(
            self.engine,
            self.task,
            StageName.GATES_TRANSFORM,
            "x: re-run contamination-post; y: re-run validate-t",
        )
        self.assertNotIn("rerun_scheduled", outcome.payload.data)
        row = self.engine.latest_report(
            self.task.task_id, StageName.CONTAMINATION_POST.value
        )
        self.assertEqual(row.verdict, VERDICT_PASS)

    def test_the_scheduled_producer_actually_re_runs_on_the_next_run(self):
        """End to end: block, then resume — with no operator flag."""
        calls: list[str] = []
        blocked_once: list[str] = []

        def counting(stage):
            def run(engine, task):
                calls.append(stage.value)
                return StageOutcome(
                    VERDICT_PASS, StagePayload(detail=f"{stage.value} ok")
                )

            return run

        def el_battery(engine, task):
            calls.append("gates_extract_load")
            if not blocked_once:
                blocked_once.append("x")
                return cli._blocked_on_stale_evidence(
                    engine,
                    task,
                    StageName.GATES_EXTRACT_LOAD,
                    "determinism: ... — re-run reference-run",
                )
            return StageOutcome(
                VERDICT_PASS, battery_report(task, TaskVariant.EXTRACT_LOAD)
            )

        runners = {stage.value: counting(stage) for stage in StageName}
        runners[StageName.GATES_EXTRACT_LOAD.value] = el_battery
        runners[StageName.GATES_TRANSFORM.value] = (
            lambda e, t: StageOutcome(
                VERDICT_PASS, battery_report(t, TaskVariant.TRANSFORM)
            )
        )
        engine = Engine(self.workspace, stage_runners=runners)
        self.addCleanup(engine.close)

        engine.run(self.task.task_id)
        self.assertEqual(calls.count("reference"), 1)
        self.assertEqual(calls.count("gates_extract_load"), 1)
        blocked = engine.blocked_stage(self.task.task_id)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.stage, StageName.GATES_EXTRACT_LOAD.value)

        # THE RESUME. No --re-emit, no ledger surgery: the shadowed producer
        # re-derives and the battery is re-run because a blocked row is not a
        # pass.
        calls.clear()
        engine.run(self.task.task_id)
        self.assertEqual(calls.count("reference"), 1)
        self.assertEqual(calls.count("gates_extract_load"), 1)
        self.assertIsNone(engine.blocked_stage(self.task.task_id))
        self.assertEqual(engine.final_verdict(self.task.task_id), "accepted")
        self.assertEqual(engine.repair_rounds_used(self.task.task_id), 0)

    def test_the_printed_remedy_names_the_command_that_will_work(self):
        self._pass(StageName.REFERENCE)
        with self.engine.task_locks((self.task.task_id,)):
            outcome = cli._blocked_on_stale_evidence(
                self.engine,
                self.task,
                StageName.GATES_EXTRACT_LOAD,
                "determinism: ... — re-run reference-run",
            )
        self.engine.record_report(
            self.task,
            StageName.GATES_EXTRACT_LOAD.value,
            VERDICT_BLOCKED,
            outcome.payload,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertTrue(
                cli._report_blocked(
                    self.engine, self.task.task_id, StageName.GATES_EXTRACT_LOAD
                )
            )
        printed = buf.getvalue()
        self.assertIn("BLOCKED at gates_extract_load", printed)
        self.assertIn("reference was shadowed", printed)
        self.assertIn(f"--task-id {self.task.task_id}", printed)
        self.assertIn("elt-taskgen validate-el", printed)

    def test_an_unschedulable_block_tells_the_operator_about_re_emit(self):
        buf = io.StringIO()
        self.engine.record_report(
            self.task,
            StageName.AUDIT.value,
            VERDICT_BLOCKED,
            StagePayload(
                error="1 borderline collision(s) require human sign-off",
                data={"blocked_on": "human"},
            ),
        )
        with contextlib.redirect_stdout(buf):
            self.assertTrue(cli._report_blocked(self.engine, self.task.task_id))
        printed = buf.getvalue()
        self.assertIn("waiting on: human", printed)
        self.assertIn("--re-emit", printed)
        self.assertIn(str(self.workspace), printed)

    def test_only_currency_refusals_block_a_battery(self):
        """A genuine defect is still a FAILURE — BLOCKED must not become the
        soft landing for every refusal."""
        defect = refused_battery(
            self.task,
            TaskVariant.EXTRACT_LOAD,
            gate="contamination-clean",
            details="fatal contamination collisions recorded: schema vs eltbench",
        )
        self.assertIsNone(cli._evidence_currency_block(defect))
        currency = refused_battery(
            self.task,
            TaskVariant.EXTRACT_LOAD,
            gate="determinism",
            details="determinism evidence is STALE — re-run reference-run",
        )
        self.assertIsNotNone(cli._evidence_currency_block(currency))

    def test_a_structured_currency_flag_wins_over_the_prose(self):
        """Prose matching is on another module's strings; a structured flag on
        GateResult (if/when it lands) is authoritative."""
        flagged = mock.Mock(passed=False, gate="determinism", details="stale")
        flagged.currency = True
        self.assertTrue(cli._is_currency_refusal(flagged))
        plain = mock.Mock(passed=False, gate="info-content", details="stale")
        plain.currency = False
        self.assertFalse(cli._is_currency_refusal(plain))


class TestAdmittedCorpusDoesNotShrinkOnAScorerBump(unittest.TestCase):
    """The near-duplicate intake firewall asks "has this corpus already
    admitted something like this?".

    Answering it with `final_verdict` (which is deliberately roster- and
    scorer-aware, because RELEASE must re-attest under the current scorer) made
    the admitted corpus shrink on every SCORER_VERSION bump, so a clone of an
    already-released task stopped counting as a duplicate at intake — the
    fail-OPEN direction for a firewall.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "ws"
        self.engine = Engine(self.workspace)
        self.addCleanup(self.engine.close)
        self.admitted = demo_fixture.demo_task()
        self.engine.register(self.admitted)
        self.clone = self.admitted.model_copy(
            update={
                "task_id": "clone__customer_summary",
                "family_id": "clone__customer_summary",
                "cluster_id": "clone__customer_summary",
            }
        )
        self.engine.register(self.clone)

    def _record_batteries(self, **kwargs):
        for variant in RLVR_TASK_VARIANTS:
            self.engine.record_report(
                self.admitted,
                variant_gate_stage(variant).value,
                VERDICT_PASS,
                battery_report(self.admitted, variant, **kwargs),
            )

    def test_a_scorer_bump_does_not_un_admit_the_corpus(self):
        self._record_batteries(scorer_version="1.0.0")
        # The ladder is right to say "re-attest before you release" ...
        self.assertNotEqual(
            self.engine.final_verdict(self.admitted.task_id), "accepted"
        )
        # ... but the corpus still contains the task, so a clone is a duplicate.
        self.assertEqual(
            [t.task_id for t in cli._admitted_tasks(self.engine, self.clone)],
            [self.admitted.task_id],
        )
        outcome = cli.run_intake_filters(self.engine, self.clone)
        self.assertIsNotNone(outcome)
        self.assertIn("near-duplicate", outcome.payload.error)

    def test_a_frozen_release_row_alone_is_admission(self):
        self.engine.record_report(
            self.admitted,
            StageName.RELEASE.value,
            VERDICT_PASS,
            StagePayload(detail="frozen release release-1"),
        )
        self.assertTrue(
            cli._task_was_admitted(self.engine, self.admitted.task_id)
        )

    def test_a_task_that_was_never_admitted_is_not_a_duplicate_source(self):
        self.assertEqual(cli._admitted_tasks(self.engine, self.clone), [])
        self.assertFalse(
            cli._task_was_admitted(self.engine, self.admitted.task_id)
        )

    def test_a_refused_battery_is_not_admission(self):
        for variant in RLVR_TASK_VARIANTS:
            report = refused_battery(
                self.admitted, variant, gate="determinism", details="red"
            )
            self.engine.record_report(
                self.admitted,
                variant_gate_stage(variant).value,
                VERDICT_PASS,
                report,
            )
        self.assertFalse(
            cli._task_was_admitted(self.engine, self.admitted.task_id)
        )

    def test_a_rejected_task_is_not_in_the_corpus(self):
        from elt_taskgen.models import TaskStatus

        self._record_batteries()
        self.assertTrue(
            cli._task_was_admitted(self.engine, self.admitted.task_id)
        )
        self.engine.save_task(
            self.engine.load_task(self.admitted.task_id).with_status(
                TaskStatus.REJECTED
            )
        )
        self.assertFalse(
            cli._task_was_admitted(self.engine, self.admitted.task_id)
        )

    def test_admission_is_bound_to_the_identity_that_was_admitted(self):
        self._record_batteries()
        self.assertTrue(
            cli._task_was_admitted(self.engine, self.admitted.task_id)
        )
        edited = self.admitted.model_copy(update={"title": "a different task"})
        self.engine.save_task(edited)
        self.assertNotEqual(edited.content_hash(), self.admitted.content_hash())
        self.assertFalse(
            cli._task_was_admitted(self.engine, self.admitted.task_id)
        )


class TestCalibrateGoesThroughTheEngine(unittest.TestCase):
    """`calibrate --empirical` used to record the runner's outcome directly,
    bypassing _handle_failure: a measured IMPOSSIBLE was appended as an inert
    FAIL with no route and no repair round, and the next command re-ran the
    stage structurally and painted a PASS over it (FAIL/PASS/FAIL measured on
    three consecutive commands)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "ws"
        self.engine = Engine(self.workspace)
        self.addCleanup(self.engine.close)
        self.task = demo_fixture.demo_task()
        self.engine.register(self.task)

    def _args(self, **overrides):
        args = cli.build_parser().parse_args(
            [
                "calibrate",
                "--task-id",
                self.task.task_id,
                "--workspace",
                str(self.workspace),
                "--empirical",
                "--replay-only",
            ]
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_a_released_identity_is_refused_not_retracted(self):
        # Every stage up to calibrate already passed at this identity, so
        # engine.run has nothing to execute (no provider is ever consulted).
        # The two battery stages must record BATTERIES: a stage whose payload
        # makes no roster claim has not attested, and the engine re-runs it.
        battery_stages = {
            variant_gate_stage(v).value: v for v in RLVR_TASK_VARIANTS
        }
        for stage in StageName:
            if stage is StageName.CALIBRATE:
                break
            if stage.value in battery_stages:
                payload = battery_report(self.task, battery_stages[stage.value])
            else:
                payload = StagePayload(detail="stub")
            self.engine.record_report(
                self.task, stage.value, VERDICT_PASS, payload
            )
        calibration_report_id = self.engine.record_report(
            self.task,
            StageName.CALIBRATE.value,
            VERDICT_PASS,
            cli.CalibratePayload(
                measurement=_structural(self.task),
                detail="structural difficulty only (empirical calibration not requested)",
            ),
        )
        self.engine.record_report(
            self.task,
            StageName.RELEASE.value,
            VERDICT_PASS,
            StagePayload(detail="frozen release release-1"),
        )
        # A RELEASE row without its immutable tree is deliberately stale.  The
        # preflight must be tested against a genuinely current released
        # identity, so materialize the tree marker the row attests to.
        release_dir = self.workspace / engine_mod.RELEASE_DIRNAME
        release_dir.mkdir(parents=True, exist_ok=True)
        (release_dir / engine_mod.RELEASE_MANIFEST_FILENAME).write_text(
            "{}\n", encoding="utf-8"
        )
        self.assertTrue(cli._released_at_current_hash(self.engine, self.task))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.cmd_calibrate(self._args())
        self.assertEqual(code, 2)
        self.assertIn("already RELEASED", buf.getvalue())
        self.assertNotIn("could not measure", buf.getvalue())
        self.assertEqual(
            self.engine.latest_report(
                self.task.task_id, StageName.CALIBRATE.value
            ).id,
            calibration_report_id,
        )

    def test_the_shadow_row_is_appended_never_a_mutation(self):
        before = self.engine.record_report(
            self.task,
            StageName.CALIBRATE.value,
            VERDICT_PASS,
            StagePayload(detail="structural"),
        )
        with self.engine.task_locks((self.task.task_id,)):
            cli._force_stage_rerun(
                self.engine, self.task, StageName.CALIBRATE, "empirical requested"
            )
        rows = self.engine._con.execute(
            "SELECT id, verdict FROM reports WHERE task_id=? AND stage=? ORDER BY id",
            (self.task.task_id, StageName.CALIBRATE.value),
        ).fetchall()
        self.assertEqual([r[1] for r in rows][-2:], [VERDICT_PASS, VERDICT_FAIL])
        self.assertIn(before, [r[0] for r in rows])
        latest = self.engine.latest_report(self.task.task_id, StageName.CALIBRATE.value)
        self.assertIn("superseded", json.loads(latest.payload_json)["detail"])

    def test_force_stage_rerun_refuses_to_bypass_the_task_lock(self):
        with self.assertRaisesRegex(EngineError, "execution lock"):
            cli._force_stage_rerun(
                self.engine,
                self.task,
                StageName.CALIBRATE,
                "unsafe unlocked invalidation",
            )


def _structural(task):
    from elt_taskgen.corpus import difficulty as difficulty_mod

    return difficulty_mod.structural_difficulty(task)


class TestCatalogRootsAreSpelledExactly(unittest.TestCase):
    def test_local_fallbacks_are_discovered_without_a_personal_path(self):
        source = Path(catalog.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/Users/wesleylu", source)
        for key, expected in (
            ("ELT_TASKGEN_DATA_ROOT", "ELT-training-data"),
            ("ELT_TASKGEN_BENCH_ROOT", "ELT-Bench"),
        ):
            value = catalog.PINNED_ROOTS[key]
            if value:
                self.assertEqual(Path(value).name, expected)

    def test_an_excluded_record_raises_the_typed_refusal(self):
        pool = catalog.PoolSource(
            pool="demo",
            origin="synsql",
            root="/tmp/demo",
            license="CC0-1.0",
            excluded=("placeholder",),
        )
        with self.assertRaises(catalog.ExcludedRecordError):
            pool.selection("placeholder")
        self.assertTrue(issubclass(catalog.ExcludedRecordError, ValueError))


class TestAdmissionProvenanceTravels(unittest.TestCase):
    """Revocation is prospective, so a stage that ran under an admission must
    RECORD which admission — nothing in the ledger, the release manifest or the
    transcripts carried that fact."""

    class _ReplayStub:
        replay_only = True

        def complete(self, role, prompt):  # pragma: no cover - never called
            raise AssertionError("replay stub must not be called")

    def test_replay_only_records_the_replay_mode(self):
        detail, provenance = cli._admission_gate(self._ReplayStub(), None)
        self.assertIsNone(detail)
        self.assertEqual(provenance["admission_mode"], "replay_only")

    def test_a_stub_without_routing_records_no_routing(self):
        class Bare:
            def complete(self, role, prompt):  # pragma: no cover
                raise AssertionError

        detail, provenance = cli._admission_gate(Bare(), None)
        self.assertIsNone(detail)
        self.assertEqual(provenance["admission_mode"], "no_routing")

    def test_the_stamp_is_never_fatal_on_a_frozen_stub(self):
        class Frozen:
            __slots__ = ()

        cli._stamp_admission(Frozen(), {"admission_mode": "admitted"})


class TestDltSkipChannelIsForDecisions(unittest.TestCase):
    """The skip channel is for decisions, not for bugs: a pydantic
    ValidationError is a ValueError subclass, so it used to print as
    `SKIPPED <name>` beside benign skips and exit 0."""

    def _args(self, workspace, **overrides):
        args = cli.build_parser().parse_args(
            [
                "ingest-dlt",
                "--connector",
                "workable",
                "--workspace",
                str(workspace),
                "--dry-run",
            ]
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_a_validation_error_is_an_error_not_a_skip(self):
        from pydantic import BaseModel, ValidationError

        class Tiny(BaseModel):
            n: int

        try:
            Tiny(n="not-an-int")
        except ValidationError as exc:
            boom = exc
        self.assertIsInstance(boom, ValueError)

        with tempfile.TemporaryDirectory() as tmp:
            from elt_taskgen.adapters import dlt as dlt_adapter

            def raise_validation(*a, **k):
                raise boom

            buf = io.StringIO()
            with mock.patch.object(dlt_adapter, "to_task_ir", raise_validation):
                with contextlib.redirect_stdout(buf):
                    code = cli.cmd_ingest_dlt(self._args(Path(tmp) / "ws"))
            self.assertEqual(code, 2)
            self.assertIn("ERROR", buf.getvalue())
            self.assertNotIn("SKIPPED", buf.getvalue())


class TestReIngestIsNeverSilent(unittest.TestCase):
    """I6(H). `task_id` hashes the surviving MEMBERSHIP of a cut, so a second
    extraction of the same source can share an id with different content —
    and `register` overwrote tasks/<id>/task_ir.json in place (measured: hash
    877237af -> 51dc9ca9, no refusal, on a task whose reports were all bound to
    the old identity)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "ws"
        self.engine = Engine(self.workspace)
        self.addCleanup(self.engine.close)
        self.task = demo_fixture.demo_task()

    def _advance(self):
        """Give the workspace evidence bound to the current identity."""
        self.engine.record_report(
            self.task,
            StageName.GENERATE.value,
            VERDICT_PASS,
            StagePayload(detail="populations built"),
        )
        stored = self.engine.load_task(self.task.task_id)
        from elt_taskgen.models import TaskStatus

        self.engine.save_task(stored.with_status(TaskStatus.GENERATED))

    def test_the_same_identity_re_registers_idempotently(self):
        self.engine.register(self.task)
        self._advance()
        rows_before = self.engine._con.execute(
            "SELECT COUNT(*) FROM reports WHERE task_id=?", (self.task.task_id,)
        ).fetchone()[0]
        self.engine.register(self.task)
        self.assertEqual(
            self.engine._con.execute(
                "SELECT COUNT(*) FROM reports WHERE task_id=?",
                (self.task.task_id,),
            ).fetchone()[0],
            rows_before,
        )

    def test_a_draft_task_is_still_overwritten_freely(self):
        """Nothing has attested to the old identity yet."""
        self.engine.register(self.task)
        edited = self.task.model_copy(update={"title": "a different title"})
        self.engine.register(edited)
        self.assertEqual(
            self.engine.load_task(self.task.task_id).content_hash(),
            edited.content_hash(),
        )

    def test_a_task_with_evidence_refuses_a_silent_overwrite(self):
        from elt_taskgen.engine import EngineError

        self.engine.register(self.task)
        self._advance()
        edited = self.task.model_copy(update={"title": "a different title"})
        with self.assertRaises(EngineError) as ctx:
            self.engine.register(edited)
        self.assertIn("--reingest", str(ctx.exception))
        self.assertIn(self.task.task_id, str(ctx.exception))
        # ... and nothing was written.
        self.assertEqual(
            self.engine.load_task(self.task.task_id).content_hash(),
            self.task.content_hash(),
        )

    def test_reingest_is_the_explicit_escape(self):
        self.engine.register(self.task)
        self._advance()
        edited = self.task.model_copy(update={"title": "a different title"})
        self.engine.register(edited, allow_overwrite=True)
        self.assertEqual(
            self.engine.load_task(self.task.task_id).content_hash(),
            edited.content_hash(),
        )

    def test_every_registering_command_offers_the_flag(self):
        parser = cli.build_parser()
        sub = next(
            a for a in parser._actions
            if isinstance(a, cli.argparse._SubParsersAction)  # type: ignore[attr-defined]
        )
        for name in cli.REINGESTING_COMMANDS:
            self.assertIn(name, sub.choices, name)
            flags = {
                option
                for action in sub.choices[name]._actions
                for option in action.option_strings
            }
            self.assertIn("--reingest", flags, name)
        # measure-target writes anchors, not candidate tasks.
        anchor_flags = {
            option
            for action in sub.choices[cli.ANCHOR_COMMAND]._actions
            for option in action.option_strings
        }
        self.assertNotIn("--reingest", anchor_flags)


if __name__ == "__main__":
    unittest.main()

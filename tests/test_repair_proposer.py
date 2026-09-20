"""Tests for review/repair_proposer.py — route-scoped repair proposals whose
only authority is mechanical.

WHY THIS EXISTS
A repair proposer is the most dangerous agent in the factory: it writes to the
artifacts the gates measure. These tests pin the three properties that make it
safe, all with FIXTURE patches (no live provider anywhere):

  * an in-scope specification patch commits ONLY after a green re-validation
    on the trial copy;
  * an out-of-scope patch — a 'specification repair' that moves reference SQL,
    whether as a separate answer_key file or as a field inside task_ir.json —
    is rejected by the revived snapshot/diff/route_from_diff validator;
  * a rejected or failing patch leaves the real workspace BYTE-IDENTICAL
    (whole-tree hash before/after), and two failed attempts abstain into the
    Round-1 workspace audit queue instead of forcing something through.

The runtime route is also pinned as no-LLM: a mechanical rebuild never asks a
model to write anything.
"""

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from elt_taskgen import repair as repair_mod
from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.engine import (
    Engine,
    FINAL_IN_PROGRESS,
    InfrastructureFailure,
    StageOutcome,
    StagePayload,
    VERDICT_BLOCKED,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
)
from elt_taskgen.models import (
    CouncilRole,
    RepairEdit,
    RepairEditOp,
    RepairPatch,
    RepairRoute,
    TaskStatus,
    canonical_json,
)
from elt_taskgen.review import repair_proposer as rp
from elt_taskgen.review.council import ProviderProtocolError

PROSE = (
    "Build the customer_summary mart: one row per customer, with the number of "
    "completed orders and the total completed order value."
)
SENTINEL = "Ties are broken by customer_id ascending."
REFERENCE_FILE_SQL = "SELECT 1 AS placeholder_reference;\n"


class ScriptedProvider:
    """Fixture provider: hands back pre-built patches (or raw text). Records
    every call so a test can prove the runtime route never calls a model."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def complete(self, role, prompt: str) -> str:
        self.calls.append((role, prompt))
        if not self._responses:
            raise AssertionError("ScriptedProvider ran out of responses")
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, RepairPatch):
            return canonical_json(item.model_dump(mode="json"))
        return str(item)


def spec_patch(new_text: str = SENTINEL) -> RepairPatch:
    """In-scope: appends a clarifying sentence to the authored prose."""
    return RepairPatch(
        route=RepairRoute.SPECIFICATION,
        artifact="task_ir.json",
        edits=(
            RepairEdit(
                op=RepairEditOp.INSERT, locator="solver_prompt", old="", new=" " + new_text
            ),
        ),
        rationale="the review stage flagged an undefined tie-break",
        proposer_role=rp.ROLE_NAME,
    )


def out_of_scope_file_patch() -> RepairPatch:
    """Claims SPECIFICATION, edits the trusted reference SQL file."""
    return RepairPatch(
        route=RepairRoute.SPECIFICATION,
        artifact="answer_key/reference/solution.sql",
        edits=(
            RepairEdit(
                op=RepairEditOp.REPLACE,
                locator="whole",
                old="SELECT 1 AS placeholder_reference;",
                new="SELECT 2 AS placeholder_reference;",
            ),
        ),
        rationale="'just a prose clarification'",
        proposer_role=rp.ROLE_NAME,
    )


def out_of_scope_ir_patch() -> RepairPatch:
    """Claims SPECIFICATION, edits reference SQL INSIDE task_ir.json — the
    diff alone cannot see this, the field-scope check must."""
    return RepairPatch(
        route=RepairRoute.SPECIFICATION,
        artifact="task_ir.json",
        edits=(
            RepairEdit(
                op=RepairEditOp.REPLACE,
                locator="reference.sql_by_mart.customer_summary",
                old="WITH completed_orders AS (",
                new="WITH completed_orders AS ( -- relaxed\n",
            ),
        ),
        rationale="'just a prose clarification'",
        proposer_role=rp.ROLE_NAME,
    )


def tree_hash(root: Path) -> dict[str, str]:
    """sha256 of every file under `root` (nothing excluded: the claim is
    byte-identity of the durable workspace). SQLite's shared-memory lock page
    is excluded: taking a consistent read snapshot changes reader-lock bytes,
    but no ledger data or WAL frame."""
    out = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if relative == "state/taskgen.sqlite-shm":
                continue
            out[relative] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


class RepairProposerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "taskgen-workspace"
        self.task = demo_task().model_copy(update={"solver_prompt": PROSE})
        self.task_id = self.task.task_id
        self.sentinel_seen = []

    # -- wiring helpers ----------------------------------------------------

    def author_ok(self, engine, task):
        return StageOutcome(VERDICT_PASS, StagePayload(detail="author ok"))

    def review_needs_sentinel(self, engine, task):
        """Green only once the prose states the tie-break — this is what a
        patch must actually FIX to be allowed to commit."""
        self.sentinel_seen.append(SENTINEL in task.solver_prompt)
        if SENTINEL in task.solver_prompt:
            return StageOutcome(VERDICT_PASS, StagePayload(detail="review ok"))
        return StageOutcome(
            VERDICT_FAIL, StagePayload(error="undefined tie-break in prose")
        )

    def make_engine(self, *, review=None, proposer=None, **kwargs) -> Engine:
        runners = {
            "author": self.author_ok,
            "review": review or self.review_needs_sentinel,
        }
        engine = Engine(
            self.workspace,
            stage_runners=runners,
            repair_proposer=proposer,
            **kwargs,
        )
        self.addCleanup(engine.close)
        engine.register(self.task)
        ref = engine.task_dir(self.task_id) / "answer_key" / "reference"
        ref.mkdir(parents=True, exist_ok=True)
        (ref / "solution.sql").write_text(REFERENCE_FILE_SQL, encoding="utf-8")
        return engine

    # -- trial isolation and infrastructure faults -------------------------

    def test_task_scoped_trial_skips_recursive_dbt_build_tree_and_proposer_runs(self):
        """A dbt dependency link may point back to its own package.

        Repair certification never needs ingestion/build caches, so the trial
        must not even visit that link (and must not copy another task tree).
        """
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        unrelated = self.workspace / "tasks" / "unrelated_task"
        unrelated.mkdir(parents=True)
        (unrelated / "marker.txt").write_text("must not be copied", encoding="utf-8")
        package = self.workspace / "dbt_builds" / "dbt_servicenow" / "package"
        packages = package / "integration_tests" / "dbt_packages"
        packages.mkdir(parents=True)
        recursive = packages / "servicenow"
        recursive.symlink_to(package, target_is_directory=True)
        (self.workspace / "tool_raw").mkdir()
        (self.workspace / "tool_raw" / "irrelevant.log").write_text(
            "not a repair input", encoding="utf-8"
        )

        with rp.trial_workspace(self.workspace, task_id=self.task_id) as trial:
            self.assertTrue((trial / "state" / "taskgen.sqlite").is_file())
            self.assertTrue((trial / "tasks" / self.task_id / "task_ir.json").is_file())
            self.assertFalse((trial / "tasks" / "unrelated_task").exists())
            self.assertFalse((trial / "dbt_builds").exists())
            self.assertFalse((trial / "tool_raw").exists())

        provider = ScriptedProvider([spec_patch()])
        outcome = rp.RepairProposer(provider, max_attempts=2).repair(
            engine, task, "review", RepairRoute.SPECIFICATION, "undefined tie-break"
        )
        self.assertTrue(outcome.committed)
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(recursive.is_symlink())
        self.assertIn(SENTINEL, engine.load_task(self.task_id).solver_prompt)

    def test_trial_creation_fault_halts_without_round_or_sticky_rejection(self):
        """A copy/storage failure is harness infrastructure, not a bad patch."""
        provider = ScriptedProvider([spec_patch()])
        engine = self.make_engine(
            proposer=rp.RepairProposer(provider, max_attempts=2),
            max_repair_rounds=2,
        )
        for upstream in ("contamination_pre", "generate", "reference"):
            engine.set_stage_runner(upstream, self.author_ok)

        with mock.patch.object(
            rp,
            "_materialize_trial_workspace",
            side_effect=OSError("injected trial filesystem failure"),
        ):
            with self.assertRaises(InfrastructureFailure) as halted:
                engine.run(self.task_id, until="review")

        self.assertEqual(halted.exception.marker, "trialworkspaceerror")
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        self.assertIsNone(engine.pending_repair_intent(self.task_id))
        self.assertIsNot(engine.load_task(self.task_id).status, TaskStatus.REJECTED)
        self.assertIsNone(rp.load_repair_adjudication(self.workspace, self.task_id))
        self.assertEqual(len(provider.calls), 1)  # no retry as a model failure

        # The same workspace resumes cleanly after the transient fault clears.
        resumed = engine.run(self.task_id, until="review")
        self.assertIs(resumed.status, TaskStatus.REVIEWED)
        self.assertEqual(engine.repair_rounds_used(self.task_id), 1)

    def test_trial_ledger_backup_is_consistent_during_a_wal_write(self):
        """The SQLite backup sees committed WAL state, never an open write."""
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        engine.record_artifact(task, "fixture/committed", "a" * 64)
        before = engine._con.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        started = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def writer() -> None:
            connection = sqlite3.connect(str(self.workspace / "state" / "taskgen.sqlite"))
            try:
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO artifacts (task_id, revision, rel_path, sha256, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (self.task_id, task.current_revision, "fixture/uncommitted", "b" * 64, "now"),
                )
                started.set()
                if not release.wait(10):
                    raise TimeoutError("test did not release concurrent WAL writer")
                connection.commit()
            except BaseException as exc:  # surfaced in the test thread below
                errors.append(exc)
                connection.rollback()
            finally:
                connection.close()

        thread = threading.Thread(target=writer, name="trial-ledger-writer")
        thread.start()
        self.assertTrue(started.wait(10), "concurrent WAL writer did not start")
        try:
            with rp.trial_workspace(self.workspace, task_id=self.task_id) as trial:
                copied = sqlite3.connect(str(trial / "state" / "taskgen.sqlite"))
                try:
                    self.assertEqual(
                        copied.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                    )
                    self.assertEqual(
                        copied.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
                        before,
                    )
                finally:
                    copied.close()
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            engine._con.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
            before + 1,
        )

    # -- 1. in-scope patch commits after a green re-validation --------------

    def test_in_scope_spec_patch_commits_after_green_revalidation(self):
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        proposer = rp.RepairProposer(ScriptedProvider([spec_patch()]), max_attempts=2)

        outcome = proposer.repair(
            engine, task, "review", RepairRoute.SPECIFICATION, "undefined tie-break"
        )

        self.assertTrue(outcome.committed)
        self.assertEqual(outcome.record.status, "committed")
        self.assertEqual(len(outcome.record.attempts), 1)
        self.assertIsNone(outcome.adjudication)
        # committed to the REAL workspace
        committed = engine.load_task(self.task_id)
        self.assertIn(SENTINEL, committed.solver_prompt)
        self.assertNotEqual(committed.content_hash(), task.content_hash())
        # and only the prose moved: reference SQL + the answer-key file are intact
        self.assertEqual(committed.reference, task.reference)
        self.assertEqual(
            (engine.task_dir(self.task_id) / "answer_key" / "reference" / "solution.sql")
            .read_text(encoding="utf-8"),
            REFERENCE_FILE_SQL,
        )
        # re-validation actually ran the review stage on the patched copy
        self.assertIn(True, self.sentinel_seen)

    def test_commit_requires_green_revalidation(self):
        """A patch that does not fix the failure never commits."""
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        before = tree_hash(self.workspace)
        proposer = rp.RepairProposer(
            ScriptedProvider([spec_patch("cosmetic wording change")]), max_attempts=1
        )

        outcome = proposer.repair(
            engine, task, "review", RepairRoute.SPECIFICATION, "undefined tie-break"
        )

        self.assertFalse(outcome.committed)
        self.assertEqual(outcome.record.attempts[0].error_type, "RevalidationFailed")
        # the task tree is untouched; only the audit queue gained an entry
        after = tree_hash(self.workspace)
        self.assertEqual(
            {k: v for k, v in after.items() if k.startswith("tasks/")},
            {k: v for k, v in before.items() if k.startswith("tasks/")},
        )
        self.assertEqual(
            set(after) - set(before),
            {f"audit/{self.task_id}.repair_adjudication.json"},
        )

    def test_rejection_codes_survive_the_proposer_loop(self):
        """The engine-facing record keeps the class name AND the sentence for
        humans; the code is what a session would be shown (Phase 1)."""
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        proposer = rp.RepairProposer(
            ScriptedProvider([spec_patch("cosmetic wording change")]), max_attempts=1
        )
        outcome = proposer.repair(
            engine, task, "review", RepairRoute.SPECIFICATION, "undefined tie-break"
        )
        attempt = outcome.record.attempts[0]
        self.assertEqual(attempt.error_type, "RevalidationFailed")
        self.assertIn("review", attempt.reason)
        # Re-raise through the same path to read the code off the exception.
        with self.assertRaises(rp.RevalidationFailed) as ctx:
            rp.attempt_patch(engine, task, "review", spec_patch("cosmetic wording change"))
        self.assertEqual(ctx.exception.code, "revalidation_red_review")
        self.assertEqual(rp.project_rejection(ctx.exception).code, "revalidation_red_review")

    # -- 1b. harness faults inside re-validation halt (C7) ------------------

    def _review_raising_on_the_trial(self, exc):
        def review(engine, task):
            if SENTINEL in task.solver_prompt:  # the patched TRIAL copy
                raise exc
            return StageOutcome(VERDICT_FAIL, StagePayload(error="undefined tie-break in prose"))

        return review

    def test_nested_budget_breach_inside_trial_phase_is_infrastructure_not_rejection(self):
        """State machine §6 / certify addendum §3.3: a harness fault a stage
        runner raises DURING the proposer's re-validation (a nested budget
        breach at submit, a replay miss, a sanitizer tripwire) is re-raised
        with its marker instead of being wrapped into RevalidationFailed. The
        proposer HALTS on the first attempt (no second model call, nothing
        queued) and the engine raises InfrastructureFailure with zero repair
        rounds and no rejection."""
        from elt_taskgen.engine import InfrastructureFailure
        from elt_taskgen.models import TaskStatus
        from elt_taskgen.review import providers as providers_mod
        from elt_taskgen.review.tools.projection import DiagnosticTripwire

        cases = (
            providers_mod.BudgetExceededError(
                "per-task budget cannot absorb the next call", scope="task"
            ),
            providers_mod.TranscriptMissingError("replay-only mode: no recorded transcript"),
            DiagnosticTripwire("canary", "private_scalar", source="gate", quarantined=b"4711"),
        )
        for exc in cases:
            name = type(exc).__name__
            expected_budget_scope = (
                "task" if name == "BudgetExceededError" else ""
            )
            with self.subTest(exc=name):
                self.workspace = Path(self._tmp.name) / f"ws-{name}"
                review = self._review_raising_on_the_trial(exc)
                engine = self.make_engine(review=review)
                task = engine.load_task(self.task_id)
                with self.assertRaises(type(exc)):
                    rp.attempt_patch(engine, task, "review", spec_patch())
                provider = ScriptedProvider([spec_patch(), spec_patch()])
                outcome = rp.RepairProposer(provider, max_attempts=2).repair(
                    engine, task, "review", RepairRoute.SPECIFICATION, "undefined tie-break"
                )
                self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)
                self.assertEqual(outcome.infrastructure, name)
                self.assertEqual(outcome.budget_scope, expected_budget_scope)
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(len(outcome.record.attempts), 1)
                self.assertEqual(outcome.record.attempts[0].error_type, name)
                self.assertIsNone(rp.load_repair_adjudication(self.workspace, self.task_id))
                self.assertNotIn("4711", outcome.record.detail)

                self.workspace = Path(self._tmp.name) / f"ws2-{name}"
                engine = self.make_engine(
                    review=review,
                    proposer=rp.RepairProposer(ScriptedProvider([spec_patch()]), max_attempts=2),
                    max_repair_rounds=2,
                )
                for upstream in ("contamination_pre", "generate", "reference"):
                    engine.set_stage_runner(upstream, self.author_ok)
                with self.assertRaises(InfrastructureFailure) as ctx:
                    engine.run(self.task_id, until="review")
                self.assertEqual(ctx.exception.marker, name.lower())
                self.assertEqual(
                    ctx.exception.budget_scope, expected_budget_scope
                )
                payload = json.loads(
                    engine.latest_report(
                        self.task_id, "review"
                    ).payload_json
                )
                self.assertEqual(
                    payload.get("budget_scope"),
                    expected_budget_scope or None,
                )
                self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
                self.assertIsNot(engine.load_task(self.task_id).status, TaskStatus.REJECTED)
                self.assertEqual(
                    engine._con.execute(
                        "SELECT COUNT(*) FROM repairs WHERE task_id=?", (self.task_id,)
                    ).fetchone()[0],
                    0,
                )

    def test_nested_transport_fault_inside_trial_phase_halts_without_spending_a_round(self):
        """A nested PLAIN ProviderProtocolError (a critic's malformed output
        during re-validation) is the engine's transport marker, not the
        proposer's own malformed patch: it is re-raised as
        InfrastructureFailure('ProviderProtocolError') so the proposer halts
        rather than retrying. A non-PASS outcome carrying an infrastructure
        marker (a stage that could not MEASURE) halts the same way, while an
        ordinary red re-validation stays a failed proposal."""
        from elt_taskgen.engine import InfrastructureFailure

        engine = self.make_engine(
            review=self._review_raising_on_the_trial(
                ProviderProtocolError("critic returned no findings JSON")
            )
        )
        task = engine.load_task(self.task_id)
        with self.assertRaises(InfrastructureFailure) as ctx:
            rp.attempt_patch(engine, task, "review", spec_patch())
        self.assertEqual(ctx.exception.marker, "ProviderProtocolError")
        self.assertIsInstance(ctx.exception.__cause__, ProviderProtocolError)
        provider = ScriptedProvider([spec_patch(), spec_patch()])
        outcome = rp.RepairProposer(provider, max_attempts=2).repair(
            engine, task, "review", RepairRoute.SPECIFICATION, "undefined tie-break"
        )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)
        self.assertEqual(outcome.infrastructure, "ProviderProtocolError")
        self.assertEqual(len(provider.calls), 1)

        def could_not_measure(engine, task):
            if SENTINEL in task.solver_prompt:
                return StageOutcome(
                    VERDICT_FAIL,
                    StagePayload(error="TranscriptMissingError: replay-only mode"),
                    infrastructure="TranscriptMissingError",
                )
            return StageOutcome(VERDICT_FAIL, StagePayload(error="undefined tie-break in prose"))

        self.workspace = Path(self._tmp.name) / "ws-measure"
        engine = self.make_engine(review=could_not_measure)
        task = engine.load_task(self.task_id)
        with self.assertRaises(InfrastructureFailure) as ctx:
            rp.attempt_patch(engine, task, "review", spec_patch())
        self.assertEqual(ctx.exception.marker, "transcriptmissingerror")
        outcome = rp.RepairProposer(ScriptedProvider([spec_patch()]), max_attempts=2).repair(
            engine, task, "review", RepairRoute.SPECIFICATION, "undefined tie-break"
        )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)

        # An ordinary red re-validation is still a failed proposal, as before.
        self.workspace = Path(self._tmp.name) / "ws-red"
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        outcome = rp.RepairProposer(
            ScriptedProvider([spec_patch("cosmetic wording change")]), max_attempts=1
        ).repair(engine, task, "review", RepairRoute.SPECIFICATION, "undefined tie-break")
        self.assertEqual(outcome.disposition, rp.DISPOSITION_NEEDS_ADJUDICATION)
        self.assertEqual(outcome.record.attempts[0].error_type, "RevalidationFailed")

    def test_wrapped_harness_fault_inside_trial_phase_still_halts(self):
        """`_infra_marker_for` and `halting_marker` walk `__cause__` /
        `__context__` (Phase 0 review finding 4): a stage runner that WRAPS a
        budget breach in its own exception during the proposer's
        re-validation still halts under the breach's marker — no second
        model call, nothing queued, the engine raising InfrastructureFailure
        with zero repair rows. A plain malformed patch wrapping a harmless
        error stays a failed attempt; one wrapping a transport fault halts."""
        from elt_taskgen.engine import InfrastructureFailure, _infra_marker_for
        from elt_taskgen.models import TaskStatus
        from elt_taskgen.review import providers as providers_mod

        inner = providers_mod.BudgetExceededError(
            "per-task budget cannot absorb the next call", scope="task"
        )
        outer = RuntimeError("stage wrapper: could not meter the critic")
        outer.__cause__ = inner
        self.assertEqual(_infra_marker_for(outer), "BudgetExceededError")
        self.assertEqual(rp.halting_marker(outer), "BudgetExceededError")

        review = self._review_raising_on_the_trial(outer)
        engine = self.make_engine(review=review)
        task = engine.load_task(self.task_id)
        with self.assertRaises(RuntimeError):
            rp.attempt_patch(engine, task, "review", spec_patch())
        provider = ScriptedProvider([spec_patch(), spec_patch()])
        outcome = rp.RepairProposer(provider, max_attempts=2).repair(
            engine, task, "review", RepairRoute.SPECIFICATION, "undefined tie-break"
        )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)
        self.assertEqual(outcome.infrastructure, "BudgetExceededError")
        self.assertEqual(len(provider.calls), 1)
        self.assertIsNone(rp.load_repair_adjudication(self.workspace, self.task_id))

        self.workspace = Path(self._tmp.name) / "ws-wrapped"
        engine = self.make_engine(
            review=review,
            proposer=rp.RepairProposer(ScriptedProvider([spec_patch()]), max_attempts=2),
            max_repair_rounds=2,
        )
        for upstream in ("contamination_pre", "generate", "reference"):
            engine.set_stage_runner(upstream, self.author_ok)
        with self.assertRaises(InfrastructureFailure) as ctx:
            engine.run(self.task_id, until="review")
        self.assertEqual(ctx.exception.marker, "budgetexceedederror")
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        self.assertIsNot(engine.load_task(self.task_id).status, TaskStatus.REJECTED)
        self.assertEqual(
            engine._con.execute(
                "SELECT COUNT(*) FROM repairs WHERE task_id=?", (self.task_id,)
            ).fetchone()[0],
            0,
        )

        contextual = RuntimeError("raised while handling the miss")
        contextual.__context__ = providers_mod.TranscriptMissingError("replay-only: no transcript")
        self.assertEqual(rp.halting_marker(contextual), "TranscriptMissingError")
        harmless = ProviderProtocolError("repair proposer returned no JSON object")
        harmless.__cause__ = ValueError("Expecting value")
        self.assertEqual(rp.halting_marker(harmless), "")
        wrapped_miss = ProviderProtocolError("payload is not a valid RepairPatch")
        wrapped_miss.__cause__ = providers_mod.TranscriptMissingError("no transcript")
        self.assertEqual(rp.halting_marker(wrapped_miss), "TranscriptMissingError")

    def test_one_shot_proposer_harness_fault_is_a_pinned_c7_deviation(self):
        """PINNED DEVIATION (Phase 0 review finding 13, accepted): the default
        one-shot `RepairProposer` no longer records an infra-typed exception
        (a replay miss under --replay-only, a budget breach, a tripwire) as a
        failed proposal. The REMOVED baseline path — a FAIL row reading
        'repair proposer failed: ...', a NEEDS_ADJUDICATION queue entry and
        the ordinary repair round (exit 0 or 1) — is gone deliberately: C7 is
        a constraint, not an option, so a harness fault HALTS (exit 2 at the
        CLI's closed error boundary), queues nothing and spends no round,
        and no config key brings the old path back."""
        import argparse
        import contextlib
        import io
        from unittest import mock

        from elt_taskgen import cli
        from elt_taskgen.engine import InfrastructureFailure
        from elt_taskgen.models import TaskStatus
        from elt_taskgen.review import providers as providers_mod

        class ReplayMiss:
            def __init__(self):
                self.calls = 0

            def complete(self, role, prompt):
                self.calls += 1
                raise providers_mod.TranscriptMissingError(
                    "replay-only mode: no recorded transcript for repair_proposer"
                )

        provider = ReplayMiss()
        engine = self.make_engine(
            proposer=rp.RepairProposer(provider, max_attempts=2), max_repair_rounds=2
        )
        for upstream in ("contamination_pre", "generate", "reference"):
            engine.set_stage_runner(upstream, self.author_ok)
        with self.assertRaises(InfrastructureFailure) as ctx:
            engine.run(self.task_id, until="review")
        self.assertEqual(ctx.exception.marker, "transcriptmissingerror")
        self.assertEqual(provider.calls, 1)  # no second attempt on a dead transport
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        self.assertIsNot(engine.load_task(self.task_id).status, TaskStatus.REJECTED)
        self.assertIsNone(rp.load_repair_adjudication(self.workspace, self.task_id))
        rows = engine._con.execute(
            "SELECT verdict, payload_json FROM reports WHERE task_id=? AND stage='review'"
            " ORDER BY id",
            (self.task_id,),
        ).fetchall()
        self.assertNotIn(VERDICT_FATAL, [verdict for verdict, _ in rows])
        for _, payload_json in rows:
            self.assertNotIn("repair proposer failed", payload_json)  # the removed path
        last = json.loads(rows[-1][1])
        self.assertEqual(last["infrastructure"], "transcriptmissingerror")
        self.assertEqual(last["data"]["status"], "halted")

        # ... and the CLI's closed error boundary turns that halt into exit 2.
        def halted_command(args):
            raise ctx.exception

        parser = argparse.ArgumentParser()
        parser.set_defaults(func=halted_command)
        err = io.StringIO()
        with mock.patch.object(cli, "build_parser", return_value=parser):
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.main([]), 2)
        self.assertIn("could not measure", err.getvalue())

    # -- 2. the diff validator rejects out-of-scope patches -----------------

    def test_out_of_scope_file_patch_rejected_by_diff_validator(self):
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        with self.assertRaises(rp.ScopeViolation) as ctx:
            rp.attempt_patch(engine, task, "review", out_of_scope_file_patch())
        message = str(ctx.exception)
        self.assertIn("specification", message)
        self.assertIn("reference", message)

    def test_out_of_scope_ir_field_patch_rejected(self):
        """task_ir.json holds prose AND reference SQL: the path diff alone says
        'specification', so the MOVED FIELDS have to sharpen the route."""
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        with self.assertRaises(rp.ScopeViolation) as ctx:
            rp.attempt_patch(engine, task, "review", out_of_scope_ir_patch())
        message = str(ctx.exception)
        self.assertIn("task_ir.json", message)
        self.assertIn("reference.sql_by_mart.customer_summary", message)
        self.assertIn("route as 'reference'", message)

    def test_in_scope_population_patch_commits(self):
        """A population repair edits the population CONDITIONS in task_ir.json
        (data is regenerated from them); the field-sharpened route makes that
        expressible while a prose route still cannot reach those fields."""
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        engine.set_stage_runner(
            "generate", lambda e, t: StageOutcome(VERDICT_PASS, StagePayload())
        )
        patch = RepairPatch(
            route=RepairRoute.POPULATION,
            artifact="task_ir.json",
            edits=(
                RepairEdit(
                    op=RepairEditOp.INSERT,
                    locator="populations.1.conditions.0",
                    old="",
                    new=" At least one customer has duplicate order rows.",
                ),
            ),
            rationale="the required mutant kept full reward on primary",
            proposer_role=rp.ROLE_NAME,
        )
        patched = rp.attempt_patch(engine, task, "generate", patch)
        self.assertIn("duplicate order rows", patched.populations[1].conditions[0])
        self.assertEqual(patched.reference, task.reference)

        # the SAME edit claimed as a specification repair is rejected
        spec_claim = patch.model_copy(update={"route": RepairRoute.SPECIFICATION})
        with self.assertRaises(rp.ScopeViolation):
            rp.attempt_patch(engine, engine.load_task(self.task_id), "review", spec_claim)

    def test_population_route_may_not_touch_reference(self):
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        patch = RepairPatch(
            route=RepairRoute.POPULATION,
            artifact="answer_key/reference/solution.sql",
            edits=(
                RepairEdit(
                    op=RepairEditOp.REPLACE,
                    locator="whole",
                    old="SELECT 1",
                    new="SELECT 3",
                ),
            ),
            rationale="'fixing the population'",
            proposer_role=rp.ROLE_NAME,
        )
        with self.assertRaises(rp.ScopeViolation):
            rp.attempt_patch(engine, task, "gates", patch)

    def test_no_op_patch_rejected(self):
        # sanity: a non-empty insertion constructs fine (and would crash HERE,
        # outside the assertRaises, if a kwarg or enum member were renamed).
        RepairPatch(
            route=RepairRoute.SPECIFICATION,
            artifact="task_ir.json",
            edits=(
                RepairEdit(
                    op=RepairEditOp.INSERT, locator="solver_prompt", old="", new="x"
                ),
            ),
            rationale="probe",
            proposer_role=rp.ROLE_NAME,
        )
        # the no-op case is an empty insertion, which the frozen model refuses
        # outright ("no edit can be a silent no-op").
        with self.assertRaises(ValidationError) as ctx:
            RepairEdit(op=RepairEditOp.INSERT, locator="solver_prompt", old="", new="")
        self.assertIn("insert edit requires non-empty 'new'", str(ctx.exception))

        # Two individually non-empty edits can still cancel each other out.
        # The trial's final diff — not merely each edit's shape — is the commit
        # boundary, so this valid RepairPatch must also be rejected as a no-op.
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        before = tree_hash(self.workspace)
        intermediate = PROSE + " Temporary clarification."
        cancelling_patch = RepairPatch(
            route=RepairRoute.SPECIFICATION,
            artifact="task_ir.json",
            edits=(
                RepairEdit(
                    op=RepairEditOp.REPLACE,
                    locator="solver_prompt",
                    old=PROSE,
                    new=intermediate,
                ),
                RepairEdit(
                    op=RepairEditOp.REPLACE,
                    locator="solver_prompt",
                    old=intermediate,
                    new=PROSE,
                ),
            ),
            rationale="edits cancel",
            proposer_role=rp.ROLE_NAME,
        )
        with self.assertRaises(rp.ScopeViolation) as rejected:
            rp.attempt_patch(engine, task, "review", cancelling_patch)
        self.assertEqual(rejected.exception.code, rp.RejectionCode.PATCH_NOOP.value)
        self.assertEqual(tree_hash(self.workspace), before)
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)

    # -- 3. failed patches leave the workspace byte-identical ---------------

    def test_failed_patch_leaves_workspace_byte_identical(self):
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        before = tree_hash(self.workspace)

        for patch in (out_of_scope_file_patch(), out_of_scope_ir_patch()):
            with self.assertRaises(rp.PatchRejected):
                rp.attempt_patch(engine, task, "review", patch)
            self.assertEqual(tree_hash(self.workspace), before)

    def test_missing_artifact_is_rejected_not_created(self):
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        before = tree_hash(self.workspace)
        patch = RepairPatch(
            route=RepairRoute.SPECIFICATION,
            artifact="task/documentation.md",
            edits=(
                RepairEdit(
                    op=RepairEditOp.INSERT, locator="whole", old="", new="hello"
                ),
            ),
            rationale="invent a file",
            proposer_role=rp.ROLE_NAME,
        )
        with self.assertRaises(rp.PatchApplicationError):
            rp.attempt_patch(engine, task, "review", patch)
        self.assertEqual(tree_hash(self.workspace), before)

    # -- 4. abstention lands in the audit queue -----------------------------

    def test_two_failed_attempts_queue_an_audit_entry(self):
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        before = tree_hash(self.workspace)
        provider = ScriptedProvider([out_of_scope_file_patch()])
        proposer = rp.RepairProposer(provider, max_attempts=2)

        outcome = proposer.repair(
            engine, task, "review", RepairRoute.SPECIFICATION, "undefined tie-break"
        )

        self.assertFalse(outcome.committed)
        self.assertEqual(outcome.record.status, rp.STATUS_NEEDS_ADJUDICATION)
        self.assertEqual(len(outcome.record.attempts), 2)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(
            [c[0] for c in provider.calls], [rp.ROLE_NAME, rp.ROLE_NAME]
        )

        queued = rp.load_repair_adjudication(self.workspace, self.task_id)
        self.assertIsNotNone(queued)
        self.assertEqual(queued["kind"], rp.ADJUDICATION_KIND)
        self.assertEqual(queued["status"], rp.STATUS_NEEDS_ADJUDICATION)
        self.assertEqual(queued["task_content_hash"], task.content_hash())
        self.assertEqual(len(queued["attempts"]), 2)
        self.assertEqual(
            rp.repair_adjudication_path(self.workspace, self.task_id).parent.name,
            "audit",
        )
        # the queue entry is the ONLY thing that changed
        after = tree_hash(self.workspace)
        self.assertEqual(
            set(after) - set(before), {f"audit/{self.task_id}.repair_adjudication.json"}
        )
        self.assertEqual(
            {k: v for k, v in after.items() if k in before}, before
        )

    def test_engine_handle_failure_bounds_attempts_and_queues_adjudication(self):
        """A genuine proposer abstention is a human wait, not a repair.

        The engine records BLOCKED at the failed stage and preserves the audit
        handoff, but commits no revision, invalidates nothing and consumes no
        repair row.  This is the end-to-end guard against fabricating a no-op
        repair after the proposer explicitly left the workspace unchanged.
        """
        provider = ScriptedProvider([out_of_scope_file_patch()])
        proposer = rp.RepairProposer(provider, max_attempts=2)
        engine = self.make_engine(proposer=proposer, max_repair_rounds=2)
        before = engine.load_task(self.task_id)

        for stage in ("contamination_pre", "generate", "reference"):
            engine.set_stage_runner(
                stage, lambda e, t: StageOutcome(VERDICT_PASS, StagePayload())
            )

        task = engine.run(self.task_id, until="review")

        self.assertEqual(engine.final_verdict(self.task_id), FINAL_IN_PROGRESS)
        self.assertIsNot(task.status, TaskStatus.REJECTED)
        self.assertEqual(task.current_revision, before.current_revision)
        self.assertEqual(task.content_hash(), before.content_hash())
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        # The configured attempt cap still applies to this one failure.
        self.assertEqual(len(provider.calls), 2)
        latest = engine.latest_report(self.task_id, "review")
        self.assertEqual(latest.verdict, VERDICT_BLOCKED)
        payload = json.loads(latest.payload_json)
        self.assertEqual(payload["data"]["blocked_on"], "human")
        self.assertEqual(payload["data"]["status"], rp.STATUS_NEEDS_ADJUDICATION)
        queued = rp.load_repair_adjudication(self.workspace, self.task_id)
        self.assertIsNotNone(queued)
        self.assertEqual(queued["route"], RepairRoute.SPECIFICATION.value)
        self.assertEqual(queued["stage"], "review")
        self.assertEqual(task.task_id, self.task_id)

    def test_engine_commits_a_good_patch_and_continues(self):
        proposer = rp.RepairProposer(ScriptedProvider([spec_patch()]), max_attempts=2)
        engine = self.make_engine(proposer=proposer, max_repair_rounds=2)
        for stage in ("contamination_pre", "generate", "reference"):
            engine.set_stage_runner(
                stage, lambda e, t: StageOutcome(VERDICT_PASS, StagePayload())
            )

        task = engine.run(self.task_id, until="review")

        self.assertIn(SENTINEL, task.solver_prompt)
        review_row = engine.latest_report(self.task_id, "review")
        self.assertEqual(review_row.verdict, VERDICT_PASS)
        self.assertEqual(review_row.content_hash, task.content_hash())
        self.assertIsNone(rp.load_repair_adjudication(self.workspace, self.task_id))

    def test_engine_without_proposer_is_unchanged(self):
        """No proposer wired: no model call, no audit entry, plain repair."""
        engine = self.make_engine(max_repair_rounds=1)
        for stage in ("contamination_pre", "generate", "reference"):
            engine.set_stage_runner(
                stage, lambda e, t: StageOutcome(VERDICT_PASS, StagePayload())
            )
        engine.run(self.task_id, until="review")
        self.assertEqual(engine.final_verdict(self.task_id), "rejected")
        self.assertIsNone(rp.load_repair_adjudication(self.workspace, self.task_id))

    # -- 5. runtime takes no LLM -------------------------------------------

    def test_runtime_route_never_calls_a_model(self):
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        provider = ScriptedProvider([spec_patch()])
        proposer = rp.RepairProposer(provider, max_attempts=2)

        outcome = proposer.repair(
            engine, task, "reference", RepairRoute.RUNTIME, "smoke check failed"
        )

        self.assertFalse(outcome.committed)
        self.assertEqual(outcome.record.status, "not_applicable")
        self.assertEqual(provider.calls, [])
        self.assertIsNone(rp.load_repair_adjudication(self.workspace, self.task_id))
        with self.assertRaises(ValueError):
            rp.view_for_route(task, RepairRoute.RUNTIME, "smoke check failed")

    def test_runtime_patch_is_refused_even_if_proposed(self):
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        patch = RepairPatch(
            route=RepairRoute.RUNTIME,
            artifact="task_ir.json",
            edits=(
                RepairEdit(
                    op=RepairEditOp.INSERT, locator="solver_prompt", old="", new="x"
                ),
            ),
            rationale="rebuild by hand",
            proposer_role=rp.ROLE_NAME,
        )
        with self.assertRaises(rp.ScopeViolation):
            rp.attempt_patch(engine, task, "reference", patch)


class RouteScopedViewTestCase(unittest.TestCase):
    """The information barrier: each route sees only its own material."""

    def setUp(self):
        self.task = demo_task().model_copy(update={"solver_prompt": PROSE})

    def test_specification_view_has_canonical_public_author_context(self):
        view = rp.view_for_route(
            self.task, RepairRoute.SPECIFICATION, "undefined tie-break"
        )
        from elt_taskgen.review import council

        author_context = council.render_view(CouncilRole.SEMANTIC_AUTHOR, self.task)
        self.assertIn(author_context, view)
        self.assertIn("SAFE TASK-PUBLIC AUTHOR CONTEXT", view)
        self.assertIn("SOURCE SCHEMA:", view)
        self.assertIn("MART PLAN SUMMARIES:", view)
        self.assertIn(self.task.tables[0].description, view)
        self.assertIn(self.task.marts[0].plan.ops[0].description, view)
        self.assertIn(self.task.marts[0].description, view)
        self.assertIn(PROSE, view)

    def test_specification_author_context_does_not_leak_private_material(self):
        view = rp.view_for_route(
            self.task, RepairRoute.SPECIFICATION, "undefined tie-break"
        )

        self.assertNotIn("WITH completed_orders", view)
        self.assertNotIn("SELECT", view.upper().replace("SELECTED", ""))
        for sql in self.task.reference.sql_by_mart.values():
            self.assertNotIn(sql, view)
        for case in self.task.attack_cases:
            self.assertNotIn(case.mutation, view)
        for pop in self.task.populations:
            for condition in pop.conditions:
                self.assertNotIn(condition, view)
            for rows in pop.literal_rows.values():
                for row in rows:
                    self.assertNotIn(canonical_json(row), view)

    def test_reference_view_is_sql_only(self):
        view = rp.view_for_route(self.task, RepairRoute.REFERENCE, "gold mismatch")
        self.assertIn("WITH completed_orders", view)
        self.assertNotIn(PROSE, view)

    def test_population_view_is_conditions_plus_failure_detail(self):
        view = rp.view_for_route(
            self.task, RepairRoute.POPULATION, "required-mutants: must lose reward"
        )
        self.assertIn("must lose reward", view)
        self.assertIn("population counterfactual", view)
        self.assertNotIn("WITH completed_orders", view)
        # The literal ROW ARRAYS are never shown. The label used to claim
        # "literal values are never shown", which is false whenever a
        # condition string itself spells a value out (the demo's
        # counterfactual conditions do exactly that), so the label now states
        # only what is actually withheld.
        self.assertIn("the generated row arrays are not shown", view)
        self.assertNotIn("literal values are never shown", view)
        for rows in self.task.populations[3].literal_rows.values():
            for row in rows:
                self.assertNotIn(canonical_json(row), view)
                for key, value in row.items():
                    self.assertNotIn(f"{key}={value}", view)

    def test_population_view_declared_scales_agree_with_private_scalars(self):
        """The POPULATION view prints the declared scale hints a patch may
        edit (`table~scale`); the projector's private scalar set on that
        route leaves exactly those out, so the two definitions of private
        agree, while the literal-row counts stay private on every route
        (Phase 0 review finding 3)."""
        from elt_taskgen.models import PopulationName
        from elt_taskgen.review.tools import projection

        view = rp.view_for_route(
            self.task, RepairRoute.POPULATION, "required-mutants: must lose reward"
        )
        population = projection.private_scalars(self.task, route=RepairRoute.POPULATION)
        everywhere = projection.private_scalars(self.task)
        shown: set[str] = set()
        for pop in self.task.populations:
            if pop.name is PopulationName.DEVELOPMENT:
                continue
            for table, scale in pop.scale.items():
                self.assertIn(f"{table}~{int(scale)}", view)
                shown.add(str(int(scale)))
        self.assertTrue(shown)
        self.assertFalse(shown & population)
        self.assertTrue(shown <= everywhere)
        for route in (RepairRoute.SPECIFICATION, RepairRoute.REFERENCE):
            self.assertTrue(shown <= projection.private_scalars(self.task, route=route))
        literal = {
            str(len(rows))
            for pop in self.task.populations
            for rows in pop.literal_rows.values()
        }
        self.assertTrue(literal)
        self.assertTrue(literal <= population)

    def test_failure_evidence_is_scrubbed_of_private_sql(self):
        leak = "WITH completed_orders AS (\n    SELECT DISTINCT order_id, customer_id"
        view = rp.view_for_route(self.task, RepairRoute.SPECIFICATION, leak)
        self.assertIn("[withheld: private material]", view)
        self.assertNotIn("SELECT DISTINCT order_id, customer_id", view)

    def test_specification_view_allows_sql_shaped_context_already_public(self):
        """A reference can independently reproduce a MartSpec sentence.

        The sentence is already in the sanctioned semantic-author projection,
        so treating it as a private-reference leak would make the repair view
        impossible to construct even though it reveals no new bytes.
        """
        from elt_taskgen.review import council

        public_context = council.render_view(CouncilRole.SEMANTIC_AUTHOR, self.task)
        public_line = next(
            line
            for line in public_context.splitlines()
            if "LEFT JOIN the orders in scope onto customers" in line
        )
        task = self.task.model_copy(
            update={
                "reference": self.task.reference.model_copy(
                    update={"sql_by_mart": {"customer_summary": public_line}}
                )
            }
        )
        raw = council._private_sql_fragments(task)
        normalized_public = rp._normalize(public_context)
        self.assertTrue(
            any(fragment in normalized_public for fragments in raw.values() for fragment in fragments)
        )

        view = rp.view_for_route(
            task,
            RepairRoute.SPECIFICATION,
            "the authored prose omitted one declared projection rule",
        )
        self.assertIn(public_line, view)
        self.assertFalse(
            any(fragment in normalized_public for fragment in rp._private_fragments(task, RepairRoute.SPECIFICATION))
        )

    def test_failure_detail_narrows_a_gate_battery(self):
        from elt_taskgen.models import AcceptanceReport, GateResult

        report = AcceptanceReport(
            task_id=self.task.task_id,
            revision=1,
            task_content_hash=self.task.content_hash(),
            gates=(
                GateResult(gate="determinism", passed=True, details="stable"),
                GateResult(gate="info-content", passed=False, details="constant column"),
            ),
            accepted=False,
            scorer_version="test",
        )
        detail = rp.failure_detail(report)
        self.assertIn("info-content", detail)
        self.assertNotIn("determinism", detail)

    # -- roadmap Phase 0.B: code-only evidence on the solver-visible routes ----

    def _leaky_report(self):
        from elt_taskgen.models import AcceptanceReport, GateResult

        return AcceptanceReport(
            task_id=self.task.task_id,
            revision=1,
            task_content_hash=self.task.content_hash(),
            gates=(
                GateResult(gate="determinism", passed=True, details="3/3 identical"),
                GateResult(
                    gate="required-mutants", passed=False,
                    details="required mutant matrix not reproduced: inner_join: "
                            "LEAK — must lose reward on primary, got 1.0",
                    evidence={"inner_join": "development=1.000000,primary=1.000000"},
                ),
                GateResult(
                    gate="data-sensitivity", passed=False,
                    details="stage-1 vector primary: customers=4711,orders=9130",
                ),
                GateResult(
                    gate="populations-load", passed=False,
                    details="gate crashed (fail closed): OSError('/Users/x/answer_key/gold')",
                ),
            ),
            accepted=False,
            scorer_version="test",
        )

    def test_failure_detail_is_code_only_on_specification_and_reference(self):
        report = self._leaky_report()
        for route in (RepairRoute.SPECIFICATION, RepairRoute.REFERENCE):
            detail = rp.failure_detail(report, route=route)
            self.assertEqual(
                json.loads(detail),
                {
                    "failing_gates": ["required-mutants", "data-sensitivity", "populations-load"],
                    "codes": {
                        "required-mutants": "failed",
                        "data-sensitivity": "failed",
                        "populations-load": "gate_crashed",
                    },
                },
            )
            for withheld in ("got 1.0", "primary", "4711", "9130", "/Users", "OSError", "determinism"):
                self.assertNotIn(withheld, detail, withheld)
            # ... and the route's view carries none of it either.
            view = rp.view_for_route(self.task, route, detail)
            for withheld in ("got 1.0", "4711", "/Users", "OSError"):
                self.assertNotIn(withheld, view, withheld)
        # POPULATION is code-only too (trust-boundary rows 1, 4, 6, 8): with
        # no task in hand it is the same document (the per-case booleans need
        # the projector), and a route-less caller projects like SPECIFICATION.
        population = rp.failure_detail(report, route=RepairRoute.POPULATION)
        self.assertEqual(
            json.loads(population),
            json.loads(rp.failure_detail(report, route=RepairRoute.SPECIFICATION)),
        )
        for withheld in ("got 1.0", "4711", "9130", "/Users", "OSError", "primary", "1.000000"):
            self.assertNotIn(withheld, population, withheld)
        self.assertEqual(rp.failure_detail(report), rp.failure_detail(report, route=None))
        self.assertEqual(
            rp.failure_detail(report), rp.failure_detail(report, route=RepairRoute.SPECIFICATION)
        )

    def test_attack_payload_never_reaches_specification_route_raw(self):
        from elt_taskgen.cli import AttackPayload

        payload = AttackPayload(
            rewards={"inner_join": {"development": 1.0, "primary": 1.0, "counterfactual": 0.0}},
            rewards_by_variant={"transform": {"inner_join": {"primary": 1.0, "stress": 0.0}}},
            cases=("inner_join",),
            rejected_proposals=({"finding_id": "pop-1", "reason": "measured reward 1.0 on primary"},),
            detail="required mutant inner_join kept reward on primary (measured reward 1.0)",
        )
        raw = canonical_json(payload.model_dump(mode="json"))
        self.assertIn("1.0", raw)
        for route in (RepairRoute.SPECIFICATION, RepairRoute.REFERENCE):
            detail = rp.failure_detail(payload, route=route)
            self.assertEqual(
                json.loads(detail), {"failing_gates": [], "codes": {"attack": "proposal_blocked"}}
            )
            for withheld in ("1.0", "0.0", "primary", "counterfactual", "stress", "rewards", "measured"):
                self.assertNotIn(withheld, detail, withheld)
            view = rp.view_for_route(self.task, route, detail)
            self.assertNotIn("1.0", view)
        plain = payload.model_copy(update={"rejected_proposals": (), "detail": "inner_join: LEAK on primary"})
        self.assertEqual(
            json.loads(rp.failure_detail(plain, route=RepairRoute.SPECIFICATION))["codes"],
            {"attack": "mutant_leak"},
        )
        # POPULATION sees the same code-only diagnostic as other routes, never
        # kill maps or post-session proposal verdicts.
        population = rp.failure_detail(payload, route=RepairRoute.POPULATION)
        self.assertEqual(
            json.loads(population),
            {"failing_gates": [], "codes": {"attack": "proposal_blocked"}},
        )
        for withheld in ("1.0", "0.0", "primary", "counterfactual", "stress", "rewards", "measured"):
            self.assertNotIn(withheld, population, withheld)
        view = rp.view_for_route(self.task, RepairRoute.POPULATION, population)
        for withheld in ("rewards_by_variant", "measured reward", "1.0"):
            self.assertNotIn(withheld, view, withheld)
        # ... and neither the promoter's codes nor its reason sentences travel.
        with_outcome = payload.model_copy(update={"proposal_outcomes": (
            {"finding_id": "pop-1", "case_name": "inner_join", "kind": "inner_join",
             "promoted": False, "reason": "proposal could not be executed: Binder Error: x",
             "predicted": {"primary": False}, "measured_pass": {"primary": True}},
        )})
        doc = json.loads(rp.failure_detail(with_outcome, route=RepairRoute.POPULATION))
        self.assertNotIn("proposals", doc)
        self.assertNotIn("Binder", canonical_json(doc))

    def test_attack_payload_projects_one_public_blocking_finding(self):
        from elt_taskgen.cli import AttackFindingDescriptor, AttackPayload
        from elt_taskgen.models import CouncilRole, Severity
        from elt_taskgen.review.tools.projection import DiagnosticTripwire

        payload = AttackPayload(
            rejected_proposals=(
                {
                    "finding_id": "private-finding-id",
                    "reason": "Binder Error on primary: expected 4711 rows",
                },
            ),
            blocking_finding=AttackFindingDescriptor(
                role=CouncilRole.AMBIGUITY_CRITIC,
                severity=Severity.MAJOR,
                identifiers=("customer_id", "customers", "inner_join"),
            ),
            detail="critic-to-mutation handoff BLOCKED on primary: got 1.0",
        )
        want = {
            "failing_gates": [],
            "codes": {"attack": "proposal_blocked"},
            "blocking_finding": {
                "role": "ambiguity_critic",
                "severity": "major",
                "identifiers": ["customer_id", "customers", "inner_join"],
            },
        }
        for route in (
            RepairRoute.SPECIFICATION,
            RepairRoute.REFERENCE,
            RepairRoute.POPULATION,
        ):
            with self.subTest(route=route.value):
                detail = rp.failure_detail(
                    payload, route=route, task=self.task, stage="attack"
                )
                self.assertEqual(json.loads(detail), want)
                view = rp.view_for_route(self.task, route, detail)
                for private in (
                    "private-finding-id",
                    "Binder",
                    "4711",
                    "1.0",
                    "rejected_proposals",
                ):
                    self.assertNotIn(private, view)
                self.assertNotIn("primary", detail)

        # Membership is checked by failure_detail's projector, before it can
        # return a document that a caller might log or pass onward.
        hidden = payload.model_copy(
            update={
                "blocking_finding": AttackFindingDescriptor(
                    role=CouncilRole.POPULATION_ADVERSARY,
                    severity=Severity.MINOR,
                    identifiers=("primary",),
                )
            }
        )
        with self.assertRaises(DiagnosticTripwire) as ctx:
            rp.failure_detail(
                hidden,
                route=RepairRoute.POPULATION,
                task=self.task,
                stage="attack",
            )
        self.assertEqual(ctx.exception.code, "finding_identifier_not_in_vocabulary")

    def test_population_failure_detail_never_carries_promoter_verdict_codes(self):
        """Trust boundary rows 2 and 7 / SoT T3 `mutant_applicable`: the
        promoter's three-value verdicts (`inert`, `inapplicable`,
        `unknown_kind`) and the matrix outcomes (`no_kill_predicted`,
        `mismatch`, `fidelity_failed`) live ONLY in the post-session
        `project_promotion` record (`rejected_proposal.json`, `audit list`).
        A real attack FAIL that routes POPULATION (a confirmed exploit
        keeping full reward beside an inert, an inapplicable and a
        mismatched proposal) projects to `codes.attack` alone on every
        route; the `proposals` key is refused by the evidence gatekeeper
        (Phase 3 review finding 0-0)."""
        from elt_taskgen import cli as cli_mod
        from elt_taskgen.models import CouncilRole, Finding, Severity
        from elt_taskgen.review.tools import projection
        from elt_taskgen.verification.attacks import PromotionOutcome

        outcomes = (
            {"finding_id": "pop-1", "case_name": "inner_join", "kind": "inner_join",
             "promoted": False,
             "reason": "proposal could not be executed: InertAstMutationError: output unchanged",
             "predicted": {"primary": False}, "measured_pass": {"primary": True}},
            {"finding_id": "pop-2", "case_name": "no_dedup", "kind": "no_dedup",
             "promoted": False,
             "reason": "proposal could not be executed: InapplicableLoadMutationError: nothing to dedupe",
             "predicted": {"primary": False}, "measured_pass": {"primary": True}},
            {"finding_id": "pop-3", "case_name": "constants", "kind": "constants",
             "promoted": False,
             "reason": "proposal was confirmed to keep FULL combined reward on all five populations",
             "predicted": {"primary": False, "stress": False},
             "measured_pass": {"primary": True, "stress": True}},
            {"finding_id": "pop-4", "case_name": "wrong_grain", "kind": "wrong_grain",
             "promoted": False,
             "reason": "measured matrix does not match the proposed expectation on primary",
             "predicted": {"primary": False}, "measured_pass": {"primary": True},
             "mismatches": ("primary",)},
        )
        payload = cli_mod.AttackPayload(
            rewards={"inner_join": {"primary": 1.0, "counterfactual": 0.0}},
            rewards_by_variant={},
            cases=("inner_join",),
            rejected_proposals=tuple({"finding_id": o["finding_id"], "reason": o["reason"]} for o in outcomes),
            proposal_outcomes=outcomes,
            detail="critic-to-mutation handoff BLOCKED: pop-3 keeps FULL reward",
        )
        findings = [
            Finding(finding_id=f"pop-{i}", role=CouncilRole.POPULATION_ADVERSARY,
                    severity=Severity.MAJOR, summary="s", detail="d")
            for i in range(1, 5)
        ]
        typed = [PromotionOutcome.model_validate(o) for o in outcomes]
        self.assertIs(cli_mod._proposal_failure_route(findings, typed), RepairRoute.POPULATION)
        # The codes the post-session projection carries — none may travel.
        verdicts = {projection.project_promotion(o)["code"] for o in outcomes}
        self.assertEqual(verdicts, {"inert", "inapplicable", "no_kill_predicted", "mismatch"})
        forbidden = ("inert", "inapplicable", "no_kill_predicted", "mismatch",
                     "fidelity_failed", "unknown_kind", "proposals")
        for route in (RepairRoute.SPECIFICATION, RepairRoute.REFERENCE, RepairRoute.POPULATION):
            with self.subTest(route=route.value):
                detail = rp.failure_detail(payload, route=route, task=self.task, stage="attack")
                self.assertEqual(
                    json.loads(detail), {"failing_gates": [], "codes": {"attack": "proposal_blocked"}}
                )
                view = rp.view_for_route(self.task, route, detail)
                for token in forbidden:
                    self.assertNotIn(token, detail, token)
                    self.assertNotIn(token, view, token)
                for withheld in ("InertAst", "Binder", "FULL combined reward", "1.0", "0.0"):
                    self.assertNotIn(withheld, view, withheld)
        session = rp.session_view(self.task, RepairRoute.POPULATION,
                                  rp.failure_detail(payload, route=RepairRoute.POPULATION,
                                                    task=self.task, stage="attack"))
        for token in forbidden:
            self.assertNotIn(token, session, token)
        # The key itself is outside the evidence vocabulary: a legacy caller
        # that hand-builds it trips the D1 gatekeeper (a harness fault).
        self.assertNotIn("proposals", rp._EVIDENCE_KEYS)
        smuggled = canonical_json(
            {"failing_gates": [], "codes": {"attack": "proposal_blocked"}, "proposals": ["inert"]}
        )
        with self.assertRaises(projection.DiagnosticTripwire) as ctx:
            rp.view_for_route(self.task, RepairRoute.POPULATION, smuggled)
        self.assertEqual(ctx.exception.code, "unknown_key")

    def test_project_rejection_carries_the_raise_site_code(self):
        from elt_taskgen.review.tools import projection

        cases = {
            rp.RevalidationFailed("x", code="revalidation_red_review"): "revalidation_red_review",
            rp.RevalidationFailed("re-validation stage 'reference' verdict 'fail': {...}"): "revalidation_red_reference",
            rp.RevalidationFailed("nothing recorded"): "revalidation_red_unknown",
            rp.ScopeViolation("x", code="scope_field_outside_allowlist"): "scope_field_outside_allowlist",
            rp.ScopeViolation("legacy, no code"): "scope_path_outside_allowlist",
            rp.PatchApplicationError("x", code="patch_anchor_ambiguous"): "patch_anchor_ambiguous",
            rp.PatchApplicationError("legacy"): "patch_anchor_not_found",
            rp.DiscriminationWeakened("m"): "discrimination_weakened",
            rp.ScopeViolation("x", code="bogus_code"): "scope_path_outside_allowlist",
        }
        for exc, code in cases.items():
            diag = rp.project_rejection(exc)
            self.assertEqual(diag.code, code, str(exc))
            self.assertIs(diag.source, projection.DiagnosticSource.REJECTION)
            self.assertFalse(diag.ok)
            text = projection.serialize_for_transport(diag, task=self.task)
            projection.assert_value_free(text.encode("utf-8"), task=self.task, route=None)
            self.assertNotIn(str(exc)[:12], text) if len(str(exc)) > 12 else None
        with self.assertRaises(TypeError):
            rp.project_rejection(RuntimeError("not a rejection"))
        # Every raise site attaches a member of the closed vocabulary.
        missing_anchor = spec_patch().model_copy(update={"edits": (
            RepairEdit(op=RepairEditOp.REPLACE, locator="solver_prompt", old="zzz", new="b"),
        )})
        with self.assertRaises(rp.PatchApplicationError) as ctx:
            rp.apply_patch_text('{"solver_prompt": "a"}', missing_anchor)
        self.assertEqual(ctx.exception.code, "patch_anchor_not_found")
        ambiguous = spec_patch().model_copy(update={"edits": (
            RepairEdit(op=RepairEditOp.REPLACE, locator="solver_prompt", old="a", new="b"),
        )})
        with self.assertRaises(rp.PatchApplicationError) as ctx:
            rp.apply_patch_text('{"solver_prompt": "a a"}', ambiguous)
        self.assertEqual(ctx.exception.code, "patch_anchor_ambiguous")
        with self.assertRaises(rp.PatchApplicationError) as ctx:
            rp.apply_patch_text("not json", spec_patch())
        self.assertEqual(ctx.exception.code, "patch_artifact_unreadable")

    def test_population_route_receives_projected_booleans_not_values(self):
        """Trust-boundary rows 1, 4, 6, 8 on the POPULATION route: a gate's
        count vectors, key tuples, reward values, paths and DuckDB text never
        enter the view. The proposer sees `{gate, passed, code}` rows plus
        the sanctioned per-case / per-relationship booleans, every row having
        passed the projector (D3) and the gatekeeper (D1)."""
        from elt_taskgen.models import AcceptanceReport, GateResult
        from elt_taskgen.review.tools import projection

        report = AcceptanceReport(
            task_id=self.task.task_id,
            revision=1,
            task_content_hash=self.task.content_hash(),
            gates=(
                GateResult(
                    gate="required-mutants", passed=False,
                    details="required mutant matrix not reproduced: inner_join: "
                            "LEAK — must lose reward on primary, got 1.0; "
                            "no_coalesce: expected FULL reward on development, got 0.0",
                    evidence={"inner_join": "development=1.000000,primary=1.000000"},
                ),
                GateResult(
                    gate="referential-integrity", passed=False,
                    details="primary:orders->customers: 3 child row(s) violate the "
                            "declared relationship (row 4: key ('c_9001', 3) has no "
                            "parent row); stress:order_items->orders: 1 child row(s) "
                            "violate the declared relationship (row 0: partially-NULL "
                            "composite key (None, 7) resolves to no parent row)",
                    evidence={"primary:orders->customers": "children=9021,violations=3"},
                ),
                GateResult(
                    gate="data-sensitivity", passed=False,
                    details="the frozen stage-1 gold is not a function of the source data",
                    evidence={"primary": "customers=4711,orders=9021", "stress": "customers=200"},
                ),
                GateResult(
                    gate="populations-load", passed=False,
                    details="gate crashed (fail closed): OSError('/Users/wesleylu/runs/x')",
                ),
            ),
            accepted=False,
            scorer_version="test",
        )
        detail = rp.failure_detail(report, route=RepairRoute.POPULATION, task=self.task)
        doc = json.loads(detail)
        self.assertEqual(
            doc["failing_gates"],
            ["required-mutants", "referential-integrity", "data-sensitivity", "populations-load"],
        )
        self.assertEqual(doc["codes"]["populations-load"], "gate_crashed")
        rows = doc["projections"]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual((row["kind"], row["source"], row["ok"]), ("diagnostic", "gate", False))
            projection.assert_value_free(
                canonical_json(row).encode("utf-8"), task=self.task, route=RepairRoute.POPULATION
            )
        by_case = {tuple(r["names"]): r["flags"] for r in rows if r["subject"] == "required-mutants" and r["names"]}
        self.assertEqual(by_case[("inner_join",)]["leaked_somewhere"], True)
        self.assertEqual(by_case[("inner_join",)]["killed_where_expected"], False)
        self.assertEqual(by_case[("no_coalesce",)]["full_reward_missing"], True)
        self.assertEqual(by_case[("no_coalesce",)]["killed_where_expected"], True)
        by_rel = {tuple(r["names"]): r["flags"] for r in rows if r["subject"] == "referential-integrity" and r["names"]}
        self.assertEqual(by_rel[("orders", "customers")]["dangling"], True)
        self.assertEqual(by_rel[("order_items", "orders")]["partial_null"], True)
        # No hidden population, no count, no key, no reward, no path, no text.
        view = rp.view_for_route(self.task, RepairRoute.POPULATION, detail)
        for withheld in ("4711", "9021", "c_9001", "got 1.0", "1.000000", "/Users",
                         "OSError", "children=", "violations", "None, 7"):
            self.assertNotIn(withheld, detail, withheld)
            self.assertNotIn(withheld, view, withheld)
        # A variant battery's rows carry the scope flag (row 4).
        variant = json.loads(rp.failure_detail(
            report, route=RepairRoute.POPULATION, task=self.task, stage="gates_transform"
        ))
        self.assertIn("variant_local", variant["projections"][0]["flags"])
        # Without the task the projector cannot run, so no rows are emitted.
        self.assertNotIn("projections", json.loads(rp.failure_detail(report, route=RepairRoute.POPULATION)))
        # A producer that leaks a VALUE as a case name trips the projector:
        # nothing is sent (a harness fault, C7), never a redacted row.
        leaky = report.model_copy(update={"gates": (
            GateResult(gate="required-mutants", passed=False,
                       details="required mutant matrix not reproduced: c_9001: "
                               "LEAK — must lose reward on primary, got 1.0"),
        )})
        with self.assertRaises(projection.DiagnosticTripwire):
            rp.failure_detail(leaky, route=RepairRoute.POPULATION, task=self.task)

    def test_failure_detail_fails_closed_on_unknown_payloads(self):
        """Every non-battery payload projects to codes on every route: a
        StagePayload's exception text and paths, a CalibratePayload's
        pass-rate matrix, a ReviewPayload's finding prose and an unknown
        shape never reach a view (trust-boundary row 8)."""
        from elt_taskgen.cli import CalibratePayload, ReviewPayload
        from elt_taskgen.corpus.difficulty import structural_difficulty
        from elt_taskgen.models import CouncilRole, Finding, Severity

        stage = StagePayload(
            error="OSError: /Users/wesleylu/runs/x: expected 4711 rows, got 4710",
            data={"failed_stage": "reference"},
        )
        blocked = StagePayload(detail="waiting on a human", data={"blocked_on": "human"})
        infra = StagePayload(error="x", infrastructure="TranscriptMissingError")
        calibrate = CalibratePayload(
            measurement=structural_difficulty(self.task),
            pass_rates={"transform": {"weak": 0.25, "strong": 0.5}},
            impossible_variants=("transform",),
            detail="transform: 0/4 on every tier",
        )
        review = ReviewPayload(
            findings=(
                Finding(finding_id="f1", role=CouncilRole.AMBIGUITY_CRITIC, severity=Severity.MAJOR,
                        summary="tie-break undefined: expected 4711 rows, got 4710"),
                Finding(finding_id="f2", role=CouncilRole.POPULATION_ADVERSARY, severity=Severity.MINOR,
                        summary="counterfactual C10 names a customer with no orders"),
            ),
            detail="1 major",
        )
        unknown = {"anything": {"primary": 0.25}, "path": "/Users/x/answer_key"}
        expected = {
            "stage-error": (stage, {"failing_gates": [], "codes": {"stage": "error"}}),
            "stage-blocked": (blocked, {"failing_gates": [], "codes": {"stage": "blocked"}}),
            "stage-infra": (infra, {"failing_gates": [], "codes": {"stage": "infrastructure"}}),
            "calibrate": (calibrate, {
                "failing_gates": [], "codes": {"calibrate": "impossible"},
                "variants": {"impossible": ["transform"], "trivial": []}, "skipped": False,
            }),
            "review": (review, {
                "failing_gates": [], "codes": {"review": "findings_blocking"},
                "findings": [{"role": "ambiguity_critic", "severity": "major"},
                             {"role": "population_adversary", "severity": "minor"}],
            }),
            "unknown": (unknown, {"failing_gates": [], "codes": {"stage": "unprojected"}}),
        }
        for label, (payload, want) in expected.items():
            for route in (RepairRoute.SPECIFICATION, RepairRoute.REFERENCE, RepairRoute.POPULATION):
                with self.subTest(label=label, route=route.value):
                    detail = rp.failure_detail(payload, route=route, task=self.task)
                    self.assertEqual(json.loads(detail), want)
                    for withheld in ("4711", "4710", "0.25", "/Users", "OSError", "tie-break",
                                     "customer with no orders", "answer_key"):
                        self.assertNotIn(withheld, detail, withheld)
                    # ... and the view builder accepts every projected document.
                    rp.view_for_route(self.task, route, detail)
        for name, codes in (
            ("STAGE_FAILURE_CODES", rp.STAGE_FAILURE_CODES),
            ("ATTACK_FAILURE_CODES", rp.ATTACK_FAILURE_CODES),
            ("CALIBRATE_FAILURE_CODES", rp.CALIBRATE_FAILURE_CODES),
            ("REVIEW_FAILURE_CODES", rp.REVIEW_FAILURE_CODES),
        ):
            for code in codes:
                self.assertRegex(code, r"^[a-z][a-z0-9_]*$", name)

    def test_view_gatekeeper_trips_on_values_in_evidence(self):
        """The D1 gatekeeper inside `view_for_route`: a forged projected
        document carrying an unknown key, a value as a gate name, a code
        outside its vocabulary, a projection row naming a non-public
        identifier or a non-boolean trips `DiagnosticTripwire` (a harness
        fault the proposer halts on, never a delivery); free text carrying a
        measured value, a count vector, a key tuple, a path or executor text
        trips too, on every route."""
        from elt_taskgen.review.tools.projection import DIAGNOSTICS_VERSION, DiagnosticTripwire

        clean = rp.failure_detail(self._leaky_report(), route=RepairRoute.SPECIFICATION)
        rp.view_for_route(self.task, RepairRoute.SPECIFICATION, clean)
        row = {"kind": "diagnostic", "diagnostics_version": DIAGNOSTICS_VERSION, "source": "gate",
               "ok": False, "code": "failed", "subject": "required-mutants", "flags": {}, "names": []}
        forged = {
            "unknown_key": {"failing_gates": ["required-mutants"], "codes": {"required-mutants": "failed"},
                            "rewards": {"primary": 1.0}},
            "value_as_gate": {"failing_gates": ["primary=4711"], "codes": {}},
            "code_outside_vocabulary": {"failing_gates": [], "codes": {"stage": "expected 4711 rows"}},
            "gate_code_outside_vocabulary": {"failing_gates": ["required-mutants"],
                                             "codes": {"required-mutants": "got_1"}},
            "non_public_name": {"failing_gates": [], "codes": {},
                                "projections": [{**row, "names": ["c_9001"]}]},
            "hidden_population": {"failing_gates": [], "codes": {},
                                  "projections": [{**row, "names": ["primary"]}]},
            "number_in_row": {"failing_gates": [], "codes": {},
                              "projections": [{**row, "flags": {"count": 4711}}]},
            "not_a_boolean": {"failing_gates": [], "codes": {}, "skipped": 1},
            "finding_prose": {"failing_gates": [], "codes": {},
                              "findings": [{"role": "ambiguity_critic", "severity": "major",
                                            "summary": "expected 4711 rows"}]},
            "attack_finding_extra_field": {
                "failing_gates": [], "codes": {"attack": "proposal_blocked"},
                "blocking_finding": {
                    "role": "ambiguity_critic", "severity": "major",
                    "identifiers": ["customers"], "summary": "free prose",
                },
            },
            "attack_finding_hidden_population": {
                "failing_gates": [], "codes": {"attack": "proposal_blocked"},
                "blocking_finding": {
                    "role": "population_adversary", "severity": "major",
                    "identifiers": ["primary"],
                },
            },
            "attack_finding_noncanonical_identifiers": {
                "failing_gates": [], "codes": {"attack": "proposal_blocked"},
                "blocking_finding": {
                    "role": "population_adversary", "severity": "major",
                    "identifiers": ["customers", "customer_id"],
                },
            },
            "attack_finding_bad_role": {
                "failing_gates": [], "codes": {"attack": "proposal_blocked"},
                "blocking_finding": {
                    "role": "critic_chosen_role", "severity": "major",
                    "identifiers": [],
                },
            },
            "attack_finding_without_blocked_attack": {
                "failing_gates": [], "codes": {"attack": "mutant_leak"},
                "blocking_finding": {
                    "role": "population_adversary", "severity": "major",
                    "identifiers": [],
                },
            },
        }
        for label, doc in forged.items():
            for route in (RepairRoute.SPECIFICATION, RepairRoute.POPULATION):
                with self.subTest(label=label, route=route.value), self.assertRaises(DiagnosticTripwire):
                    rp.view_for_route(self.task, route, canonical_json(doc))
        free = (
            "stage-1 vector: customers=4711,orders=9130",
            "key ('c_9001', 3) has no parent row",
            "gate crashed: OSError('/Users/x/answer_key/gold')",
            "Binder Error: no such column",
            "expected 4711 rows, got 4710",
        )
        for text in free:
            for route in (RepairRoute.SPECIFICATION, RepairRoute.REFERENCE, RepairRoute.POPULATION):
                with self.subTest(text=text, route=route.value), self.assertRaises(DiagnosticTripwire):
                    rp.view_for_route(self.task, route, text)
        # A tripwire is what the proposer HALTS on (C7), never a failed attempt.
        self.assertEqual(rp.halting_marker(DiagnosticTripwire("schema", "unknown_key")), "DiagnosticTripwire")
        # Free text with no value keeps today's path (scrubbed, never tripped).
        rp.view_for_route(self.task, RepairRoute.POPULATION, "required-mutants: must lose reward")



class PatchParsingTestCase(unittest.TestCase):
    def test_parses_a_fenced_json_patch(self):
        payload = canonical_json(spec_patch().model_dump(mode="json"))
        patch = rp.parse_patch(f"```json\n{payload}\n```")
        self.assertEqual(patch.route, RepairRoute.SPECIFICATION)
        self.assertEqual(patch.artifact, "task_ir.json")

    def test_rejects_non_json(self):
        with self.assertRaises(ProviderProtocolError):
            rp.parse_patch("I would suggest clarifying the tie-break rule.")

    def test_rejects_a_payload_the_frozen_model_refuses(self):
        bad = json.dumps(
            {
                "route": "fatal",
                "artifact": "task_ir.json",
                "edits": [
                    {"op": "insert", "locator": "solver_prompt", "old": "", "new": "x"}
                ],
                "rationale": "reject the task by patch",
                "proposer_role": rp.ROLE_NAME,
            }
        )
        with self.assertRaises(ProviderProtocolError):
            rp.parse_patch(bad)

    def test_rejects_empty_edit_list(self):
        bad = json.dumps(
            {
                "route": "specification",
                "artifact": "task_ir.json",
                "edits": [],
                "rationale": "nothing",
                "proposer_role": rp.ROLE_NAME,
            }
        )
        with self.assertRaises(ProviderProtocolError):
            rp.parse_patch(bad)

    def test_proposal_may_not_re_route_a_failure(self):
        provider = ScriptedProvider(
            [
                RepairPatch(
                    route=RepairRoute.REFERENCE,
                    artifact="task_ir.json",
                    edits=(
                        RepairEdit(
                            op=RepairEditOp.INSERT,
                            locator="reference.sql_by_mart.customer_summary",
                            old="",
                            new="\n-- tweak",
                        ),
                    ),
                    rationale="actually the reference is wrong",
                    proposer_role=rp.ROLE_NAME,
                )
            ]
        )
        task = demo_task().model_copy(update={"solver_prompt": PROSE})
        with self.assertRaises(rp.ScopeViolation):
            rp.propose_patch(task, RepairRoute.SPECIFICATION, "prose is vague", provider)


class RevalidationScopeTestCase(unittest.TestCase):
    def test_stages_are_truncated_at_the_failing_stage(self):
        stages = rp.revalidation_stages(RepairRoute.SPECIFICATION, "review")
        self.assertEqual(stages, ("author", "review"))
        stages = rp.revalidation_stages(RepairRoute.POPULATION, "gates")
        self.assertEqual(stages, ("generate", "reference", "attack", "gates"))
        # a stage outside the route's rerun set is still proven
        self.assertIn("intake", rp.revalidation_stages(RepairRoute.REFERENCE, "intake"))

    def test_attempt_budget_defaults_when_config_says_nothing(self):
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            # missing file -> DEFAULT_MAX_ATTEMPTS
            self.assertEqual(
                rp.proposer_attempt_budget(Path(tmp) / "no_such_agents.yaml"),
                rp.DEFAULT_MAX_ATTEMPTS,
            )
            # file present but no `repair` section -> DEFAULT_MAX_ATTEMPTS
            path = Path(tmp) / "agents.yaml"
            path.write_text(yaml.safe_dump({"providers": {}}), encoding="utf-8")
            self.assertEqual(rp.proposer_attempt_budget(path), rp.DEFAULT_MAX_ATTEMPTS)

    #: The shipped config/agents.yaml budget, which docs/CONFIGURABLE_PIPELINE.md
    #: documents as 3. It deliberately exceeds DEFAULT_MAX_ATTEMPTS, the value
    #: used when no config file is present.
    COMMITTED_MAX_ATTEMPTS = 3

    def test_committed_config_budget_is_the_documented_one(self):
        """Parity pin: the shipped `repair.max_attempts` is a deliberate spend
        budget, so changing it is an explicit edit here as well, and an
        out-of-range or unparseable committed value still fails."""
        self.assertEqual(rp.proposer_attempt_budget(), self.COMMITTED_MAX_ATTEMPTS)
        self.assertGreaterEqual(self.COMMITTED_MAX_ATTEMPTS, rp.DEFAULT_MAX_ATTEMPTS)
        self.assertLessEqual(self.COMMITTED_MAX_ATTEMPTS, rp.DEFAULT_MAX_ATTEMPTS * 5)

    def test_attempt_budget_reads_config(self):
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agents.yaml"
            path.write_text(yaml.safe_dump({"repair": {"max_attempts": 1}}), encoding="utf-8")
            self.assertEqual(rp.proposer_attempt_budget(path), 1)
            path.write_text(
                yaml.safe_dump({"repair": {"max_attempts": 99}}), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                rp.proposer_attempt_budget(path)


class VerifierInertNoopTestCase(RepairProposerTestCase):
    """docs/plans/bounded_agents_phase1.md §2, finding 3-0: the REFERENCE
    allowlist is `reference.sql_by_mart.*` alone, and a task_ir.json diff
    that moves ONLY fields no certifier reads (`VERIFIER_INERT_IR_PATHS`)
    is refused as `patch_noop` before any allowlist is consulted — a hash
    rotation always carries a verifiable change."""

    @staticmethod
    def inert_patch(*paths: str, route: RepairRoute = RepairRoute.REFERENCE) -> RepairPatch:
        return RepairPatch(
            route=route,
            artifact="task_ir.json",
            edits=tuple(
                RepairEdit(op=RepairEditOp.INSERT, locator=path, old="", new=" (edited)")
                for path in paths
            ),
            rationale="refresh the reference metadata",
            proposer_role=rp.ROLE_NAME,
        )

    def validate(self, engine, task, patch) -> None:
        with rp.trial_workspace(engine.workspace) as trial:
            before = repair_mod.snapshot(trial, self.task_id)
            target = trial / "tasks" / self.task_id / "task_ir.json"
            before_ir = target.read_text(encoding="utf-8")
            target.write_text(rp.apply_patch_text(before_ir, patch), encoding="utf-8")
            rp.validate_scope(
                trial, self.task_id, patch, before, repair_mod.snapshot(trial, self.task_id),
                before_ir=before_ir,
            )

    def test_validate_scope_refuses_verifier_inert_only_diff_as_noop(self):
        MART = "customer_summary"
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        self.assertEqual(rp.ROUTE_IR_PATHS[RepairRoute.REFERENCE], ("reference.sql_by_mart.*",))
        self.assertEqual(
            rp.VERIFIER_INERT_IR_PATHS,
            ("reference.dialect", "reference.implementation_id", "reference.load_notes",
             "reference.provenance", "reference.version"),
        )
        for path in rp.VERIFIER_INERT_IR_PATHS:
            for route in (RepairRoute.SPECIFICATION, RepairRoute.REFERENCE, RepairRoute.POPULATION):
                with self.subTest(field=path, route=route.value):
                    with self.assertRaises(rp.ScopeViolation) as ctx:
                        self.validate(engine, task, self.inert_patch(path, route=route))
                    self.assertEqual(ctx.exception.code, "patch_noop")
                    self.assertEqual(rp.project_rejection(ctx.exception).code, "patch_noop")
                    self.assertIn("verifier-inert", str(ctx.exception))
        # All five at once: still nothing a certifier reads moved.
        with self.assertRaises(rp.ScopeViolation) as ctx:
            self.validate(engine, task, self.inert_patch(*rp.VERIFIER_INERT_IR_PATHS))
        self.assertEqual(ctx.exception.code, "patch_noop")
        # Inert beside the SQL: not a no-op, and the inert field is then
        # outside the (narrowed) allowlist.
        mixed = RepairPatch(
            route=RepairRoute.REFERENCE,
            artifact="task_ir.json",
            edits=(
                RepairEdit(op=RepairEditOp.INSERT, locator="reference.provenance", old="", new=" (edited)"),
                RepairEdit(
                    op=RepairEditOp.INSERT, locator=f"reference.sql_by_mart.{MART}", old="",
                    new="\n-- clarified reference",
                ),
            ),
            rationale="both",
            proposer_role=rp.ROLE_NAME,
        )
        with self.assertRaises(rp.ScopeViolation) as ctx:
            self.validate(engine, task, mixed)
        self.assertEqual(ctx.exception.code, "scope_field_outside_allowlist")
        # The SQL alone validates.
        sql_only = RepairPatch(
            route=RepairRoute.REFERENCE, artifact="task_ir.json", edits=(mixed.edits[1],),
            rationale="sql", proposer_role=rp.ROLE_NAME,
        )
        self.validate(engine, task, sql_only)
        # Through the ONE-SHOT proposer: a provenance-only patch is an
        # ordinary failed attempt (ScopeViolation / patch_noop), never a
        # commit and never a halt; the workspace is byte-identical.
        before = tree_hash(engine.workspace)
        provider = ScriptedProvider([self.inert_patch("reference.provenance")])
        outcome = rp.RepairProposer(provider, max_attempts=1).repair(
            engine, task, "review", RepairRoute.REFERENCE, "reference failure"
        )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_NEEDS_ADJUDICATION)
        attempt = outcome.record.attempts[0]
        self.assertEqual((attempt.accepted, attempt.error_type), (False, "ScopeViolation"))
        self.assertIn("verifier-inert", attempt.reason)
        self.assertEqual(rp.halting_marker(rp.ScopeViolation("x", code="patch_noop")), "")
        self.assertEqual(engine.load_task(self.task_id).content_hash(), task.content_hash())
        outcome.adjudication.unlink()
        self.assertEqual(tree_hash(engine.workspace), before)

    def test_specification_route_admits_public_descriptions_not_structure(self):
        """Public grain/op prose is repairable; executable identity is not."""
        engine = self.make_engine()
        task = engine.load_task(self.task_id)
        grain_patch = RepairPatch(
            route=RepairRoute.SPECIFICATION,
            artifact="task_ir.json",
            edits=(
                RepairEdit(
                    op=RepairEditOp.INSERT,
                    locator="marts.0.grain",
                    old="",
                    new=" Every source parent is retained.",
                ),
            ),
            rationale="settle a solver-visible graded-row ambiguity",
            proposer_role=rp.ROLE_NAME,
        )
        self.validate(engine, task, grain_patch)

        op_description_patch = RepairPatch(
            route=RepairRoute.SPECIFICATION,
            artifact="task_ir.json",
            edits=(
                RepairEdit(
                    op=RepairEditOp.INSERT,
                    locator="marts.0.plan.ops.0.description",
                    old="",
                    new=" Every declared parent is retained.",
                ),
            ),
            rationale="settle the declarative plan wording at its source",
            proposer_role=rp.ROLE_NAME,
        )
        self.validate(engine, task, op_description_patch)

        plan_structure_patch = RepairPatch(
            route=RepairRoute.SPECIFICATION,
            artifact="task_ir.json",
            edits=(
                RepairEdit(
                    op=RepairEditOp.REPLACE_JSON,
                    locator="marts.0.plan.ops.0.kind",
                    old=canonical_json(task.marts[0].plan.ops[0].kind.value),
                    new=canonical_json("source"),
                ),
            ),
            rationale="must not rewrite executable plan behavior",
            proposer_role=rp.ROLE_NAME,
        )
        with self.assertRaises(rp.ScopeViolation) as ctx:
            self.validate(engine, task, plan_structure_patch)
        self.assertEqual(ctx.exception.code, "scope_field_outside_allowlist")

    def test_write_record_never_overwrites_executed_certify_entries_with_served_ones(self):
        """Finding 0-4: a session record that already holds EXECUTED certify
        entries (`served: false`) keeps them when a replay lands on the same
        `session_sha256` path with merely SERVED copies; executed or
        verified fresh entries, an absent or unreadable record, and a record
        of served entries all write the fresh list."""
        from elt_taskgen.models import canonical_json

        path = self.workspace / "repair_proposer.reference.abc.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = "a" * 64
        executed = [{"key": "k", "served": False, "verified": False, "observation_sha256": digest}]
        served = [{"key": "k", "served": True, "verified": False, "observation_sha256": digest}]
        verified = [{"key": "k", "served": False, "verified": True, "observation_sha256": digest}]
        self.assertEqual(rp._certify_entries_to_keep(path, served), served)  # no record yet
        path.write_text(canonical_json({"certify": executed}), encoding="utf-8")
        self.assertEqual(rp._certify_entries_to_keep(path, served), executed)  # the executed evidence stands
        self.assertEqual(rp._certify_entries_to_keep(path, verified), verified)
        self.assertEqual(rp._certify_entries_to_keep(path, executed), executed)
        self.assertEqual(rp._certify_entries_to_keep(path, []), [])
        path.write_text(canonical_json({"certify": served}), encoding="utf-8")
        self.assertEqual(rp._certify_entries_to_keep(path, served), served)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(rp._certify_entries_to_keep(path, served), served)


class CliWiringTest(unittest.TestCase):
    """The proposer is reachable and agentic on the shipped path by default.

    It was briefly implemented but unwired — Engine(repair_proposer=...) was
    never populated by any command, so the whole deliverable was dead code.
    Explicit `--no-repair-proposer` and `--repair-proposer-mode one_shot`
    retain the rollback paths.
    """

    def _args(self, *extra):
        from elt_taskgen import cli

        return cli.build_parser().parse_args(["generate", "--task-id", "t", *extra])

    def test_default_constructs_agentic_proposer(self):
        from elt_taskgen import cli

        self.assertIsInstance(
            cli._make_repair_proposer(self._args(), object()), rp.AgenticRepairProposer
        )

    def test_explicit_one_shot_mode_constructs_legacy_proposer(self):
        from elt_taskgen import cli

        proposer = cli._make_repair_proposer(
            self._args("--repair-proposer-mode", "one_shot"), object()
        )
        self.assertIsInstance(proposer, rp.RepairProposer)
        self.assertEqual(proposer.max_attempts, rp.proposer_attempt_budget())

    def test_attempt_override_is_honoured(self):
        from elt_taskgen import cli

        proposer = cli._make_repair_proposer(
            self._args("--repair-proposer", "--repair-attempts", "1"), object()
        )
        self.assertEqual(proposer.max_attempts, 1)

    def test_engine_receives_the_proposer(self):
        """End of the wire: the flag reaches Engine, not just a helper."""
        from elt_taskgen import cli

        with tempfile.TemporaryDirectory() as tmp:
            args = cli.build_parser().parse_args(
                ["generate", "--task-id", "t", "--workspace", tmp, "--replay-only", "--repair-proposer"]
            )
            engine = cli._make_engine(args, provider=object())
            try:
                self.assertIsInstance(engine._repair_proposer, rp.AgenticRepairProposer)
            finally:
                engine.close()

            args_off = cli.build_parser().parse_args(
                [
                    "generate", "--task-id", "t", "--workspace", tmp,
                    "--replay-only", "--no-repair-proposer",
                ]
            )
            engine_off = cli._make_engine(args_off, provider=object())
            try:
                self.assertIsNone(engine_off._repair_proposer)
            finally:
                engine_off.close()


if __name__ == "__main__":
    unittest.main()

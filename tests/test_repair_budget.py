"""The repair budget must buy something, or say that it cannot (lane 3).

WHY THIS EXISTS
Measured across four end-to-end pools, every SPECIFICATION repair round was a
no-op that still consumed budget: `repair.apply_repair` bumps lineage and
appends invalidation rows but never edits prose, the author stage then rendered
a byte-identical view, the provider cache returned the identical prose, and the
stage recorded a PASS reading "solver prose unchanged". Three rounds of that,
then a rejection blaming the TASK for exhausting a budget that was structurally
incapable of buying anything. The same shape burned three rounds on the
`reference` route (deterministic regeneration of identical bytes) and three
rounds on the offline demo's single MISSING TRANSCRIPT — a transport fault no
edit to any artifact could ever repair.

These tests pin the three rules that close it, by construction and offline:

  1. INFRASTRUCTURE FAILURES SPEND ZERO ROUNDS (engine._handle_failure).
  2. AN INERT ROUND IS PROVED AND REFUSED AFTER ONE ROUND, NOT THREE
     (repair.repair_fingerprint recorded on the repairs row; the next failure
     arriving at the same fingerprint raises repair.InertRepairError).
  3. THE PROSE PATH SPECIFICALLY: the real cli author runner FAILS LOUDLY when
     a specification repair produced byte-identical prose (option B), and
     PRESERVES prose that a certified repair patch committed instead of
     silently re-authoring the patch away (option A — without which a
     committed specification patch is reverted by the very re-validation that
     was supposed to certify it).

Everything here is offline: a stub provider that returns the COMMITTED demo
author transcript verbatim, which is exactly the cache behaviour that made the
defect invisible in the first place.
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from elt_taskgen import repair
from elt_taskgen import engine as engine_mod
from elt_taskgen.cli import (
    AUTHOR_BEHAVIOR_SHA_KEY,
    AUTHOR_PROSE_SHA_KEY,
    make_author_runner,
)
from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.engine import (
    Engine,
    FINAL_IN_PROGRESS,
    InfrastructureFailure,
    StageName,
    StageOutcome,
    StagePayload,
    VERDICT_BLOCKED,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
)
from elt_taskgen.models import RepairRoute, TaskStatus

#: Current fidelity-green, private-answer-safe prose for the demo TaskIR.  The
#: historical author transcript is deliberately NOT reused: its behavior hash
#: is stale and the stronger relationship/rule coverage gate now rejects it.
def fixture_demo_prose() -> str:
    return (
        Path(__file__).parent / "fixtures" / "declarative_prose.txt"
    ).read_text(encoding="utf-8")


class CachedProvider:
    """Answers every author call with the SAME prose, whatever the view says.

    This is not a caricature: it is `providers.transcript_key` behaviour when
    the view did not move, which is precisely what a repair round that edits
    nothing guarantees. Carries no `routing`, so the metrology admission gate
    treats it as a non-live stub (see cli._admission_failure_detail)."""

    def __init__(self, prose: str) -> None:
        self.prose = prose
        self.calls: list[str] = []

    def complete(self, role, prompt: str) -> str:
        self.calls.append(getattr(role, "value", str(role)))
        return self.prose


def pass_runner(stage: str):
    def run(engine, task):
        return StageOutcome(VERDICT_PASS, StagePayload(detail=f"{stage} ok"))

    return run


class RepairBudgetTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "ws"
        self.task = demo_task()
        self.task_id = self.task.task_id

    def make_engine(self, overrides=None, **kwargs) -> Engine:
        runners = {s.value: pass_runner(s.value) for s in StageName}
        runners.update(overrides or {})
        engine = Engine(self.workspace, stage_runners=runners, **kwargs)
        self.addCleanup(engine.close)
        return engine


# ---------------------------------------------------------------------------
# 1. Infrastructure failures never consume a repair round
# ---------------------------------------------------------------------------

class TestInfrastructureSpendsNoRounds(RepairBudgetTestCase):
    """An infrastructure failure HALTS. It spends no repair round, writes no
    fatal row, rejects nothing, and the same workspace resumes at that stage.

    The old branch rejected the task with a FATAL row whose own text promised
    "the ledger resumes at this stage" — a promise `final_verdict` made false
    (any fatal row rejects, and run() returns before looking at a stage), so a
    transient transport fault cost the whole workspace and every transcript in
    it had to be re-bought in a fresh one."""

    def test_missing_transcript_halts_at_round_zero_without_rejecting(self):
        """The offline demo's actual failure, in miniature.

        MEASURED BEFORE THIS RULE (workspace /tmp, `elt-taskgen demo`): one
        missing `ambiguity_critic` transcript produced repairs rows 1..3, three
        identical re-renders of the same view, and a final
        'repair budget exhausted (3 rounds)' FATAL blaming the task."""
        seen = []

        def review(engine, task):
            seen.append(task.content_hash())
            return StageOutcome(
                VERDICT_FAIL,
                StagePayload(
                    error=(
                        "TranscriptMissingError: replay-only mode: no recorded "
                        "transcript for role 'ambiguity_critic' / prompt "
                        "58179ac48ba7"
                    )
                ),
            )

        engine = self.make_engine({"review": review}, max_repair_rounds=3)
        engine.register(self.task)
        with self.assertRaises(InfrastructureFailure) as ctx:
            engine.run(self.task_id)

        self.assertEqual(ctx.exception.stage, "review")
        self.assertEqual(ctx.exception.marker, "transcriptmissingerror")
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        self.assertEqual(len(seen), 1, "the stage must not be retried at all")
        # NOT rejected, and nothing fatal anywhere in the ledger.
        self.assertIsNot(engine.load_task(self.task_id).status, TaskStatus.REJECTED)
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_IN_PROGRESS)
        rows = engine._con.execute(
            "SELECT COUNT(*) FROM reports WHERE task_id=? AND verdict=?",
            (self.task_id, VERDICT_FATAL),
        ).fetchone()[0]
        self.assertEqual(rows, 0)
        row = engine.latest_report(self.task_id, "review")
        self.assertEqual(row.verdict, VERDICT_FAIL)
        payload = json.loads(row.payload_json)
        self.assertEqual(payload["data"]["infrastructure"], "transcriptmissingerror")
        self.assertIn("the transport failed, not the task", payload["error"])
        self.assertIn("is NOT rejected", payload["error"])
        self.assertNotIn("repair budget exhausted", row.payload_json)

    def test_infrastructure_failure_resumes_at_the_failed_stage(self):
        """The promise the payload makes, kept: fix the transport, re-run in
        THIS workspace, and the stage that stopped is the stage that runs."""
        def broken(engine, task):
            return StageOutcome(
                VERDICT_FAIL,
                StagePayload(error="TranscriptMissingError: nothing recorded"),
            )

        engine = self.make_engine({"review": broken}, max_repair_rounds=3)
        engine.register(self.task)
        with self.assertRaises(InfrastructureFailure):
            engine.run(self.task_id)
        engine.close()

        calls = []

        def fixed(engine, task):
            calls.append(task.content_hash())
            return StageOutcome(
                VERDICT_PASS, StagePayload(detail="review ok (transport restored)")
            )

        resumed = self.make_engine({"review": fixed}, max_repair_rounds=3)
        resumed.run(self.task_id)
        self.assertEqual(len(calls), 1, "the halted stage must run again")
        row = resumed.latest_report(self.task_id, "review")
        self.assertEqual(row.verdict, VERDICT_PASS)
        self.assertEqual(
            row.content_hash, resumed.load_task(self.task_id).content_hash()
        )
        self.assertIsNot(
            resumed.load_task(self.task_id).status, TaskStatus.REJECTED
        )
        self.assertEqual(resumed.repair_rounds_used(self.task_id), 0)

    def test_not_admitted_halts_at_round_zero(self):
        def author(engine, task):
            return StageOutcome(
                VERDICT_FAIL,
                StagePayload(
                    error="council not admitted — run metrology: marker absent"
                ),
            )

        engine = self.make_engine({"author": author}, max_repair_rounds=3)
        engine.register(self.task)
        with self.assertRaises(InfrastructureFailure) as ctx:
            engine.run(self.task_id)
        self.assertEqual(ctx.exception.marker, "council not admitted")
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        self.assertIsNot(engine.load_task(self.task_id).status, TaskStatus.REJECTED)

    def test_council_finding_text_is_not_infrastructure(self):
        """A critic that MENTIONS metrology is not a transport fault.

        The old classifier substring-scanned the canonical JSON of the WHOLE
        payload for the bare word "metrology", so a SPECIFICATION finding
        reading "the soil metrology column units are ambiguous" was recorded as
        an infrastructure FATAL and the repairable task was rejected."""
        payload = StagePayload(
            detail="1 finding",
            error="prose fidelity: the soil metrology column units are ambiguous",
        )
        self.assertIsNone(engine_mod._infrastructure_failure(payload))
        self.assertIsNone(
            engine_mod._infrastructure_failure(
                {
                    "findings": [
                        {"message": "metrology of the grain is unstated"},
                        {"message": "the council was not admitted to my liking"},
                    ],
                    "detail": "2 finding(s), 0 fatal",
                }
            )
        )

    def test_table_named_metrology_in_error_detail_is_a_task_defect(self):
        self.assertIsNone(
            engine_mod._infrastructure_failure(
                StagePayload(
                    error="population coverage: table metrology_readings has no rows"
                )
            )
        )

    def test_provider_runtime_transport_texts_are_infrastructure(self):
        """providers.py raises these as PLAIN RuntimeError, so the class name
        carries nothing. Measured in runs/fullrun1: "no pricing entry for
        openai_compat model ''" routed POPULATION, regenerated the populations,
        then hit the inert-repair rule and rejected the task."""
        for text in (
            "RuntimeError: provider HTTP 529 from https://api.anthropic.com/v1/messages",
            "RuntimeError: provider transport error from https://api.example/v1",
            "RuntimeError: no pricing entry for openai_compat model ''",
        ):
            with self.subTest(text=text[:40]):
                self.assertIsNotNone(
                    engine_mod._infrastructure_failure(StagePayload(error=text))
                )

    def test_raised_transport_exception_is_classified_by_its_class(self):
        class TranscriptMissingError(RuntimeError):
            pass

        def review(engine, task):
            raise TranscriptMissingError("no recorded transcript for 'ambiguity_critic'")

        engine = self.make_engine({"review": review}, max_repair_rounds=3)
        engine.register(self.task)
        with self.assertRaises(InfrastructureFailure) as ctx:
            engine.run(self.task_id)
        self.assertEqual(ctx.exception.marker, "transcriptmissingerror")
        row = engine.latest_report(self.task_id, "review")
        self.assertEqual(row.verdict, VERDICT_FAIL)
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)

    def test_structured_outcome_marker_halts_without_scanning_text(self):
        """A battery whose witness the TRANSPORT could not produce records the
        whole AcceptanceReport as evidence, so the cause cannot travel in the
        payload — it travels on the outcome instead."""
        def gates(engine, task):
            return StageOutcome(
                VERDICT_FAIL,
                StagePayload(error="el-independent-load: no evidence recorded"),
                infrastructure="TranscriptMissingError",
            )

        engine = self.make_engine({"gates": gates}, max_repair_rounds=3)
        engine.register(self.task)
        with self.assertRaises(InfrastructureFailure) as ctx:
            engine.run(self.task_id)
        self.assertEqual(ctx.exception.marker, "transcriptmissingerror")
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)

    def test_a_transport_fault_outranks_a_keyword_derived_fatal_route(self):
        """The route is GUESSED from prose when the runner names none, and the
        prose now carries the transport class name (producer notes travel into
        gate details). A battery that failed a contamination-currency gate AND
        a transport-missing witness in the same run keyword-routed FATAL on the
        word "contamination" and rejected the task with the infrastructure
        marker set and ignored."""
        payload = StagePayload(
            error=(
                "contamination-clean: contamination scan predates type-blind "
                "shape fingerprints; el-independent-load: the independent LOAD "
                "build was never performed"
            ),
        )

        def gates(engine, task):
            # The marker travels on the OUTCOME because the payload a battery
            # must record is the whole AcceptanceReport (cli._transport_marker).
            return StageOutcome(
                VERDICT_FAIL, payload, infrastructure="TranscriptMissingError"
            )

        engine = self.make_engine({"gates": gates}, max_repair_rounds=3)
        engine.register(self.task)
        # The route the router WOULD have chosen, i.e. the reason this matters.
        self.assertIs(
            repair.route_for_failure("gates", payload), repair.RepairRoute.FATAL
        )
        with self.assertRaises(InfrastructureFailure) as ctx:
            engine.run(self.task_id)
        self.assertEqual(ctx.exception.marker, "transcriptmissingerror")
        self.assertIsNot(
            engine.load_task(self.task_id).status, TaskStatus.REJECTED
        )
        verdicts = [
            row[0]
            for row in engine._con.execute(
                "SELECT verdict FROM reports WHERE task_id=? AND stage='gates'",
                (self.task_id,),
            ).fetchall()
        ]
        self.assertNotIn(VERDICT_FATAL, verdicts)
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)

    def test_an_explicit_fatal_verdict_still_rejects(self):
        """An explicit FATAL from a runner is a DECISION (a fatal contamination
        collision in the emitted artifacts), not a route guessed from prose —
        it must not be softened just because the payload also names a
        transport."""
        def gates(engine, task):
            return StageOutcome(
                VERDICT_FATAL,
                StagePayload(
                    error=(
                        "fatal contamination collision in emitted artifacts "
                        "(TranscriptMissingError also in flight)"
                    )
                ),
            )

        engine = self.make_engine({"gates": gates}, max_repair_rounds=3)
        engine.register(self.task)
        engine.run(self.task_id)
        self.assertIs(engine.load_task(self.task_id).status, TaskStatus.REJECTED)
        self.assertEqual(
            engine.latest_report(self.task_id, "gates").verdict, VERDICT_FATAL
        )

    def test_an_explicit_route_from_the_runner_is_not_second_guessed(self):
        """A runner that NAMED RepairRoute.FATAL means it."""
        def gates(engine, task):
            return StageOutcome(
                VERDICT_FAIL,
                StagePayload(error="TranscriptMissingError: gone"),
                route=repair.RepairRoute.FATAL,
            )

        engine = self.make_engine({"gates": gates}, max_repair_rounds=3)
        engine.register(self.task)
        engine.run(self.task_id)
        self.assertIs(engine.load_task(self.task_id).status, TaskStatus.REJECTED)


class TestRoleCapIsNotNamedInfrastructure(unittest.TestCase):
    """A role's OWN cap (`session.max_usd`) is the trajectory's `LIMIT_USD`
    stop (SoT T4): attributable to the seat, disposition blocked_limit with
    limit usd, so it must not enter the zero-round infrastructure path that
    would halt every resume on the same cap. providers raises it as
    `RoleCapExceeded`, whose class NAME is deliberately absent from the
    engine's infrastructure set; the task and total budgets keep the base
    `BudgetExceededError`, which IS infrastructure (rule 1 above)."""

    def test_role_cap_exceeded_is_not_a_named_infrastructure_class(self):
        from elt_taskgen.review import providers

        self.assertIn("BudgetExceededError", engine_mod._INFRA_EXCEPTION_NAMES)
        self.assertNotIn("RoleCapExceeded", engine_mod._INFRA_EXCEPTION_NAMES)
        self.assertTrue(issubclass(providers.RoleCapExceeded, providers.BudgetExceededError))
        self.assertEqual(providers.RoleCapExceeded("cap").scope, "role")
        # The task budget's breach is the infrastructure class, by name.
        self.assertEqual(
            engine_mod._infra_marker_for(
                providers.BudgetExceededError("per-task budget breached", scope="task")
            ),
            "BudgetExceededError",
        )


class TestBlockedSpendsNoRounds(RepairBudgetTestCase):
    """BLOCKED is a wait, not a failure: no repair row, no fatal row, no
    rejection, and the stage re-runs when the condition clears."""

    def _blocked_select(self, engine, task):
        return StageOutcome(
            VERDICT_BLOCKED,
            StagePayload(
                error="a pending repair adjudication requires human sign-off",
                data={"blocked_on": "human", "pending": "1"},
            ),
        )

    def test_blocked_outcome_writes_no_repair_row_and_no_fatal(self):
        release_calls = []

        def release(engine, task):
            release_calls.append(1)
            return StageOutcome(VERDICT_PASS, StagePayload(detail="released"))

        engine = self.make_engine(
            {"select": self._blocked_select, "release": release}, max_repair_rounds=3
        )
        engine.register(self.task)
        engine.run(self.task_id)

        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        row = engine.latest_report(self.task_id, "select")
        self.assertEqual(row.verdict, "blocked")
        self.assertEqual(
            engine._con.execute(
                "SELECT COUNT(*) FROM reports WHERE task_id=? AND verdict=?",
                (self.task_id, VERDICT_FATAL),
            ).fetchone()[0],
            0,
        )
        self.assertIsNot(engine.load_task(self.task_id).status, TaskStatus.REJECTED)
        self.assertEqual(release_calls, [], "downstream must not run past a block")
        blocked = engine.blocked_stage(self.task_id)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.stage, "select")

    def test_blocked_stage_resumes_when_the_condition_clears(self):
        engine = self.make_engine({"select": self._blocked_select})
        engine.register(self.task)
        engine.run(self.task_id)
        self.assertIsNotNone(engine.blocked_stage(self.task_id))

        engine.set_stage_runner(
            "select",
            lambda e, t: StageOutcome(VERDICT_PASS, StagePayload(detail="adjudicated")),
        )
        engine.run(self.task_id)
        self.assertEqual(
            engine.latest_report(self.task_id, "select").verdict, VERDICT_PASS
        )
        self.assertIsNone(engine.blocked_stage(self.task_id))
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)

    def test_explicit_retry_guard_does_not_poll_unchanged_protocol_output(self):
        calls = []

        def guarded_review(engine, task):
            calls.append(1)
            return StageOutcome(
                VERDICT_BLOCKED,
                StagePayload(
                    error="critic handoff is incomplete",
                    data={
                        "blocked_on": "human",
                        "failure_class": "protocol_failure",
                        "failure_code": "critic_attack_handoff_invalid",
                        engine_mod.RETRY_GUARD_KEY: engine_mod.RETRY_GUARD_EXPLICIT,
                    },
                ),
            )

        engine = self.make_engine({"review": guarded_review})
        engine.register(self.task)
        engine.run(self.task_id)
        engine.run(self.task_id)
        self.assertEqual(calls, [1], "ordinary resume must not repeat the provider call")

        # Explicit append-only invalidation represents an operator confirming
        # that the named recovery prerequisite changed. It shadows, rather
        # than deletes, the original protocol evidence.
        task = engine.load_task(self.task_id)
        engine.record_report(
            task,
            StageName.REVIEW.value,
            VERDICT_FAIL,
            StagePayload(detail="explicit recovery after transcript replacement"),
        )
        engine.set_stage_runner(
            StageName.REVIEW.value,
            lambda _engine, _task: StageOutcome(
                VERDICT_PASS, StagePayload(detail="valid replacement handoff")
            ),
        )
        engine.run(self.task_id)
        self.assertEqual(
            engine.latest_report(self.task_id, StageName.REVIEW.value).verdict,
            VERDICT_PASS,
        )
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)

    def test_blocked_never_shadows_the_accepting_batteries(self):
        engine = self.make_engine({"select": self._blocked_select})
        engine.register(self.task)
        engine.run(self.task_id)
        # The batteries passed before the block; a wait must not un-accept them.
        self.assertEqual(engine.final_verdict(self.task_id), FINAL_IN_PROGRESS)
        self.assertEqual(
            engine.latest_report(self.task_id, "gates_transform").verdict, VERDICT_PASS
        )


# ---------------------------------------------------------------------------
# 2. The inert-repair rule
# ---------------------------------------------------------------------------

class TestRepairFingerprint(RepairBudgetTestCase):
    def test_fingerprint_ignores_task_ir_lineage_bytes(self):
        """A repair round rewrites task_ir.json (new revision entry) without
        changing one semantic byte. If the fingerprint hashed those bytes the
        whole rule would be dead code — verified: with task_ir.json included,
        three provably inert rounds produced three different fingerprints."""
        engine = self.make_engine()
        engine.register(self.task)
        task = engine.load_task(self.task_id)
        before = repair.repair_fingerprint(self.workspace, task)

        repaired = repair.apply_repair(
            engine, task, RepairRoute.SPECIFICATION, "ambiguous grain"
        )
        after = repair.repair_fingerprint(self.workspace, repaired)

        self.assertEqual(before, after)
        self.assertNotEqual(repaired.current_revision, task.current_revision)
        # and the raw file really did move, which is the point of the exclusion
        ir = engine.task_dir(self.task_id) / "task_ir.json"
        self.assertIn("specification", ir.read_text(encoding="utf-8"))

    def test_fingerprint_moves_when_any_artifact_moves(self):
        engine = self.make_engine()
        engine.register(self.task)
        task = engine.load_task(self.task_id)
        before = repair.repair_fingerprint(self.workspace, task)

        out = engine.task_dir(self.task_id) / "populations" / "primary.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("a,b\n1,2\n", encoding="utf-8")

        self.assertNotEqual(before, repair.repair_fingerprint(self.workspace, task))

    def test_fingerprint_moves_when_content_hash_moves(self):
        engine = self.make_engine()
        engine.register(self.task)
        task = engine.load_task(self.task_id)
        before = repair.repair_fingerprint(self.workspace, task)
        edited = task.model_copy(update={"solver_prompt": "new prose"})
        self.assertNotEqual(before, repair.repair_fingerprint(self.workspace, edited))

    def test_reports_are_excluded(self):
        """reports/ are ledger echoes that move on EVERY round; if they counted
        no round could ever be proved inert."""
        engine = self.make_engine()
        engine.register(self.task)
        task = engine.load_task(self.task_id)
        before = repair.repair_fingerprint(self.workspace, task)
        engine.record_report(task, "generate", VERDICT_FAIL, StagePayload(detail="x"))
        self.assertEqual(before, repair.repair_fingerprint(self.workspace, task))

    def test_unrecorded_fingerprint_is_not_evidence_of_inertness(self):
        """A repairs row written before the column existed carries ''. That is
        'unprovable', not 'proven' — the ordinary budget still bounds it."""
        repair.assert_repair_not_inert(
            previous_fingerprint="",
            current_fingerprint="deadbeef",
            route=RepairRoute.SPECIFICATION,
            stage="review",
            rounds_used=1,
        )
        repair.assert_repair_not_inert(
            previous_fingerprint=None,
            current_fingerprint="deadbeef",
            route=RepairRoute.SPECIFICATION,
            stage="review",
            rounds_used=1,
        )
        with self.assertRaises(repair.InertRepairError):
            repair.assert_repair_not_inert(
                previous_fingerprint="deadbeef",
                current_fingerprint="deadbeef",
                route=RepairRoute.SPECIFICATION,
                stage="review",
                rounds_used=1,
            )

    def test_legacy_ledger_without_the_column_still_opens(self):
        """Forensic workspaces predating both added repair columns still open."""
        import sqlite3

        state = self.workspace / "state"
        state.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(state / "taskgen.sqlite"))
        con.executescript(
            "CREATE TABLE repairs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " task_id TEXT NOT NULL, revision INTEGER NOT NULL, route TEXT"
            " NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);"
        )
        con.execute(
            "INSERT INTO repairs (task_id, revision, route, reason, created_at)"
            " VALUES ('t', 2, 'specification', 'legacy', '2026-01-01T00:00:00Z')"
        )
        con.commit()
        con.close()

        engine = self.make_engine()
        row = engine.last_repair("t")
        self.assertIsNotNone(row)
        self.assertEqual(row.route, "specification")
        self.assertEqual(row.fingerprint, "")
        self.assertEqual(row.lineage_root_hash, "")


class TestReingestRepairLineage(RepairBudgetTestCase):
    def test_reingest_binds_unjournaled_legacy_row_to_the_old_lineage(self):
        engine = self.make_engine()
        engine.register(self.task)
        old = engine.load_task(self.task_id)
        old_root = old.revisions[0].content_hash
        engine._con.execute(
            "INSERT INTO repairs (task_id, revision, route, reason, created_at)"
            " VALUES (?,?,?,?,?)",
            (
                self.task_id,
                2,
                RepairRoute.SPECIFICATION.value,
                "pre-lineage legacy repair",
                "2026-01-01T00:00:00Z",
            ),
        )
        engine._con.commit()
        self.assertEqual(engine.repair_rounds_used(self.task_id), 1)

        replacement = self.task.model_copy(update={"title": "replacement identity"})
        engine.register(replacement, allow_overwrite=True)

        stored_root = engine._con.execute(
            "SELECT lineage_root_hash FROM repairs WHERE task_id=?",
            (self.task_id,),
        ).fetchone()[0]
        self.assertEqual(stored_root, old_root)
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        self.assertIsNone(engine.last_repair(self.task_id))

    def test_open_recovers_pre_column_lineage_from_repair_intent(self):
        engine = self.make_engine()
        engine.register(self.task)
        old = engine.load_task(self.task_id)
        repaired = repair.apply_repair(
            engine, old, RepairRoute.SPECIFICATION, "journaled old repair"
        )
        old_root = repaired.revisions[0].content_hash
        replacement = self.task.model_copy(update={"title": "replacement identity"})
        engine.register(replacement, allow_overwrite=True)

        # Simulate the additive-column state on first open: the repair intent
        # predates lineage_root_hash and therefore its new column is empty.
        engine._con.execute(
            "UPDATE repairs SET lineage_root_hash='' WHERE task_id=?",
            (self.task_id,),
        )
        engine._con.commit()
        engine.close()

        reopened = Engine(self.workspace)
        self.addCleanup(reopened.close)
        raw_root = reopened._con.execute(
            "SELECT lineage_root_hash FROM repairs WHERE task_id=?",
            (self.task_id,),
        ).fetchone()[0]
        self.assertEqual(raw_root, old_root)
        self.assertEqual(reopened.repair_rounds_used(self.task_id), 0)
        self.assertIsNone(reopened.last_repair(self.task_id))

    def test_fresh_reingest_gets_its_own_fully_bounded_repair_budget(self):
        """Old rows stay auditable but neither spend nor weaken the new budget."""
        attempts: list[int] = []

        def failing_but_productive(engine, task):
            attempts.append(len(attempts) + 1)
            out = engine.task_dir(task.task_id) / "populations" / "primary.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(f"replacement round {attempts[-1]}\n", encoding="utf-8")
            return StageOutcome(VERDICT_FAIL, StagePayload(error="gold mismatch"))

        engine = self.make_engine(
            {"reference": failing_but_productive}, max_repair_rounds=2
        )
        engine.register(self.task)
        old = engine.load_task(self.task_id)
        for ordinal in (1, 2):
            old = repair.apply_repair(
                engine,
                old,
                RepairRoute.SPECIFICATION,
                f"old identity round {ordinal}",
            )
        old_root = old.revisions[0].content_hash
        self.assertEqual(engine.repair_rounds_used(self.task_id), 2)

        replacement = self.task.model_copy(update={"title": "replacement identity"})
        engine.register(replacement, allow_overwrite=True)
        current = engine.load_task(self.task_id)
        current_root = current.revisions[0].content_hash
        self.assertNotEqual(current_root, old_root)
        self.assertEqual(current.current_revision, 1)
        self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
        self.assertIsNone(engine.last_repair(self.task_id))
        self.assertEqual(
            engine._con.execute(
                "SELECT COUNT(*) FROM repairs WHERE task_id=?", (self.task_id,)
            ).fetchone()[0],
            2,
            "re-ingest must not erase the obsolete lineage's audit rows",
        )

        finished = engine.run(self.task_id)

        self.assertEqual(finished.status, TaskStatus.REJECTED)
        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(engine.repair_rounds_used(self.task_id), 2)
        self.assertEqual(engine.last_repair(self.task_id).lineage_root_hash, current_root)
        lineage_counts = dict(
            engine._con.execute(
                "SELECT lineage_root_hash, COUNT(*) FROM repairs WHERE task_id=?"
                " GROUP BY lineage_root_hash",
                (self.task_id,),
            ).fetchall()
        )
        self.assertEqual(lineage_counts, {old_root: 2, current_root: 2})
        latest = engine.latest_report(self.task_id, "reference")
        self.assertEqual(latest.verdict, VERDICT_FATAL)
        self.assertIn("repair budget exhausted (2 rounds)", latest.payload_json)


class TestInertRepair(RepairBudgetTestCase):
    def test_inert_round_rejects_after_one_round_not_three(self):
        """THE MEASUREMENT THIS LANE EXISTS FOR.

        A stage that fails and a repair path that edits nothing: the budget
        used to buy three identical rounds (the four pools' behaviour). It now
        buys one, and the ledger says why."""
        attempts = []

        def failing(engine, task):
            attempts.append(task.content_hash())
            return StageOutcome(
                VERDICT_FAIL, StagePayload(error="gold mismatch on primary")
            )

        engine = self.make_engine({"reference": failing}, max_repair_rounds=3)
        engine.register(self.task)
        task = engine.run(self.task_id)

        self.assertEqual(task.status, TaskStatus.REJECTED)
        # OLD (pre-rule): rounds=3, attempts=4, "repair budget exhausted".
        # NEW: one round is spent, then the inertness of that round is proved.
        self.assertEqual(engine.repair_rounds_used(self.task_id), 1)
        self.assertEqual(len(attempts), 2)
        row = engine.latest_report(self.task_id, "reference")
        self.assertEqual(row.verdict, VERDICT_FATAL)
        self.assertIn("INERT REPAIR PATH", row.payload_json)
        self.assertIn("byte-identical", row.payload_json)
        self.assertNotIn("repair budget exhausted", row.payload_json)

    def test_a_productive_round_is_never_called_inert(self):
        """The rule must not fire on a repair path that IS doing work: three
        rounds that each change an artifact spend the whole budget as before."""
        rounds = []

        def failing_but_productive(engine, task):
            rounds.append(1)
            out = engine.task_dir(task.task_id) / "populations" / "primary.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(f"round {len(rounds)}\n", encoding="utf-8")
            return StageOutcome(VERDICT_FAIL, StagePayload(error="gold mismatch"))

        engine = self.make_engine(
            {"reference": failing_but_productive}, max_repair_rounds=3
        )
        engine.register(self.task)
        engine.run(self.task_id)

        self.assertEqual(engine.repair_rounds_used(self.task_id), 3)
        self.assertEqual(len(rounds), 4)
        row = engine.latest_report(self.task_id, "reference")
        self.assertIn("repair budget exhausted", row.payload_json)

    def test_repairs_row_records_the_state_the_round_started_from(self):
        engine = self.make_engine()
        engine.register(self.task)
        task = engine.load_task(self.task_id)
        expected = repair.repair_fingerprint(self.workspace, task)

        repair.apply_repair(engine, task, RepairRoute.SPECIFICATION, "prose")

        row = engine.last_repair(self.task_id)
        self.assertEqual(row.fingerprint, expected)
        # and every invalidation payload carries it too, so the evidence
        # survives in the reports/ copies a forensic reader gets
        invalidated = engine.latest_report(self.task_id, "author")
        self.assertIsNone(invalidated)  # author never ran: no phantom row


# ---------------------------------------------------------------------------
# 3. The prose path: the real cli author runner
# ---------------------------------------------------------------------------

class TestAuthorRepairPath(RepairBudgetTestCase):
    """Drives the REAL `cli.make_author_runner` — the stage that recorded
    'solver prose unchanged; fidelity gate green' as a PASS on every wasted
    round in every pool."""

    def setUp(self):
        super().setUp()
        self.prose = fixture_demo_prose()
        self.provider = CachedProvider(self.prose)
        self.sentinel = "Ties are broken by the lowest customer_id."

    def review_needs_sentinel(self, engine, task):
        if self.sentinel in (task.solver_prompt or ""):
            return StageOutcome(VERDICT_PASS, StagePayload(detail="review ok"))
        return StageOutcome(
            VERDICT_FAIL,
            StagePayload(error="ambiguity: the tie-break rule is undefined"),
            route=RepairRoute.SPECIFICATION,
        )

    def make_prose_engine(self, **kwargs) -> Engine:
        return self.make_engine(
            {
                "author": make_author_runner(self.provider),
                "review": self.review_needs_sentinel,
            },
            **kwargs,
        )

    # -- (B) the no-op is now honest ---------------------------------------

    def test_unchanged_prose_after_a_specification_repair_fails_loudly(self):
        engine = self.make_prose_engine(max_repair_rounds=3)
        engine.register(self.task)
        task = engine.run(self.task_id)

        self.assertEqual(task.status, TaskStatus.REJECTED)
        # ONE wasted round is reported, not three.
        self.assertEqual(engine.repair_rounds_used(self.task_id), 1)

        self.assertNotEqual(
            engine.latest_report(self.task_id, "author").verdict,
            VERDICT_PASS,
            "the re-authoring that changed nothing must not report success",
        )
        # the stage's own diagnostic (the FATAL rejection row shadows it under
        # MAX(id), so ask for the FAIL row the author runner wrote)
        author_row = engine.latest_report_with_verdict(
            self.task_id, "author", VERDICT_FAIL
        )
        self.assertIn("INERT SPECIFICATION REPAIR", author_row.payload_json)
        self.assertIn("BYTE-IDENTICAL", author_row.payload_json)
        # the ONLY author PASS in this ledger is the FIRST authoring run; the
        # old behaviour recorded a second one reading "solver prose unchanged"
        first_pass = engine.latest_report_with_verdict(
            self.task_id, "author", VERDICT_PASS
        )
        self.assertIn("solver prose authored", first_pass.payload_json)

        # and the engine's own rule then rejects rather than buying round 2
        fatal = engine.latest_report_with_verdict(
            self.task_id, "author", VERDICT_FATAL
        )
        self.assertIsNotNone(fatal)
        self.assertIn("INERT REPAIR PATH", fatal.payload_json)

    def test_first_authoring_records_the_prose_and_behavior_sha(self):
        from elt_taskgen.review import providers

        engine = self.make_prose_engine(max_repair_rounds=0)
        engine.register(self.task)
        engine.run(self.task_id, until="author")
        row = engine.latest_report_with_verdict(self.task_id, "author", VERDICT_PASS)
        payload = json.loads(row.payload_json)
        self.assertEqual(
            payload["data"][AUTHOR_PROSE_SHA_KEY],
            hashlib.sha256(self.prose.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            payload["data"][AUTHOR_BEHAVIOR_SHA_KEY],
            providers.role_behavior_sha256("semantic_author"),
        )

    def test_unchanged_prose_outside_a_repair_is_still_a_pass(self):
        """Idempotence is not the defect: re-running author at the same
        identity with no outstanding repair must stay green."""
        engine = self.make_prose_engine(max_repair_rounds=0)
        engine.register(self.task)
        engine.run(self.task_id, until="author")
        task = engine.load_task(self.task_id)
        outcome = make_author_runner(self.provider)(engine, task)
        self.assertEqual(outcome.verdict, VERDICT_PASS)
        self.assertIn("solver prose unchanged", outcome.payload.detail)

    # -- (A) a committed prose repair actually takes effect -----------------

    def test_initial_author_failure_is_repaired_from_its_persisted_draft(self):
        """The first fidelity-red author draft is a repairable artifact.

        Previously it lived only in a convenience session report: the TaskIR
        still had empty prose, so the specification proposer could neither see
        nor edit what failed and the ordinary fallback round was provably
        inert.  This drives the real engine, author runner, trial certifier and
        proposer from a red first draft to a preserved green repair.
        """
        from elt_taskgen.models import (
            RepairEdit,
            RepairEditOp,
            RepairPatch,
            canonical_json,
        )
        from elt_taskgen.review import metrology
        from elt_taskgen.review import repair_proposer as rp

        green = self.prose
        red = metrology.build_prose(self.task)
        self.provider = CachedProvider(red)
        patch = RepairPatch(
            route=RepairRoute.SPECIFICATION,
            artifact="task_ir.json",
            edits=(
                RepairEdit(
                    op=RepairEditOp.REPLACE,
                    locator="solver_prompt",
                    old=red,
                    new=green,
                ),
            ),
            rationale="replace the fidelity-red authored candidate with complete prose",
            proposer_role=rp.ROLE_NAME,
        )

        class PatchProvider:
            def __init__(self) -> None:
                self.prompts: list[str] = []

            def complete(self, role, prompt: str) -> str:
                self.prompts.append(prompt)
                return canonical_json(patch.model_dump(mode="json"))

        patch_provider = PatchProvider()
        engine = self.make_prose_engine(max_repair_rounds=3)
        engine.set_repair_proposer(rp.RepairProposer(patch_provider, max_attempts=1))
        engine.register(self.task)

        task = engine.run(self.task_id, until="author")

        self.assertEqual(task.solver_prompt, green)
        self.assertIsNot(task.status, TaskStatus.REJECTED)
        self.assertEqual(engine.repair_rounds_used(self.task_id), 1)
        self.assertEqual(
            len(self.provider.calls), 1,
            "trial and live author stages must preserve, not regenerate, the patch",
        )
        self.assertEqual(len(patch_provider.prompts), 1)
        self.assertIn(red, patch_provider.prompts[0])
        author = engine.latest_report(self.task_id, "author")
        self.assertEqual(author.verdict, VERDICT_PASS)
        self.assertIn("PRESERVED", author.payload_json)
        candidates = [
            json.loads(row.payload_json)["data"]
            for row in engine.report_history(self.task_id, "author")
            if row.verdict == VERDICT_FAIL
            and json.loads(row.payload_json).get("data", {}).get("source")
            == "authored_candidate"
        ]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0][AUTHOR_PROSE_SHA_KEY],
            hashlib.sha256(red.encode()).hexdigest(),
        )

    def test_repaired_prose_is_preserved_not_re_authored_away(self):
        """OPTION A, and the defect that made option A impossible.

        `council._author_view` does not contain `solver_prompt`, so re-
        authoring after a certified SPECIFICATION patch renders the SAME view,
        gets the SAME cached completion, and overwrites the patch — and
        `repair_proposer._commit` then copies the reverted bytes back as if
        they were the repair. Authoring must keep prose it did not write."""
        engine = self.make_prose_engine(max_repair_rounds=3)
        engine.register(self.task)
        engine.run(self.task_id, until="author")

        # a certified specification patch commits new prose (this is exactly
        # what repair_proposer.attempt_patch writes into task_ir.json)
        task = engine.load_task(self.task_id)
        patched = task.model_copy(
            update={"solver_prompt": task.solver_prompt + "\n\n" + self.sentinel}
        )
        engine.save_task(patched)
        repair.apply_repair(
            engine, patched, RepairRoute.SPECIFICATION, "tie-break undefined"
        )

        task = engine.run(self.task_id, until="review")

        # the patch survived the re-run of `author` ...
        self.assertIn(self.sentinel, task.solver_prompt)
        author_row = engine.latest_report(self.task_id, "author")
        self.assertEqual(author_row.verdict, VERDICT_PASS)
        self.assertIn("PRESERVED", author_row.payload_json)
        # ... the provider was not consulted again (its answer could only have
        # been the prose the patch replaced) ...
        self.assertEqual(len(self.provider.calls), 1)
        # ... and the finding is answered: review is green at this identity.
        review_row = engine.latest_report(self.task_id, "review")
        self.assertEqual(review_row.verdict, VERDICT_PASS)
        self.assertEqual(review_row.content_hash, task.content_hash())
        self.assertEqual(engine.repair_rounds_used(self.task_id), 1)

    def test_proposer_patch_survives_the_real_author_stage_end_to_end(self):
        """OPTION A, all the way through review/repair_proposer.py.

        The proposer's own tests wire a NO-OP author stub, so the revert this
        closes was invisible to them: with the real runner, re-validation on
        the trial copy re-ran `author`, which regenerated the pre-patch prose
        from a view that does not contain solver_prompt, and `_commit` copied
        THOSE bytes back — the certified 'repair' shipping the exact text it
        was written to replace. Here the patch survives the trial AND the live
        re-run, and the review finding it answers goes green."""
        from elt_taskgen.models import RepairEdit, RepairEditOp, RepairPatch, canonical_json
        from elt_taskgen.review import repair_proposer as rp

        patch = RepairPatch(
            route=RepairRoute.SPECIFICATION,
            artifact="task_ir.json",
            edits=(
                RepairEdit(
                    op=RepairEditOp.INSERT,
                    locator="solver_prompt",
                    old="",
                    new="\n\n" + self.sentinel,
                ),
            ),
            rationale="the review stage flagged an undefined tie-break",
            proposer_role=rp.ROLE_NAME,
        )
        patch_json = canonical_json(patch.model_dump(mode="json"))

        class PatchProvider:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def complete(self, role, prompt: str) -> str:
                self.calls.append(getattr(role, "value", str(role)))
                return patch_json

        proposer = rp.RepairProposer(PatchProvider(), max_attempts=2)
        engine = self.make_prose_engine(max_repair_rounds=3)
        engine.set_repair_proposer(proposer)
        engine.register(self.task)

        task = engine.run(self.task_id, until="review")

        self.assertIn(self.sentinel, task.solver_prompt)
        review_row = engine.latest_report(self.task_id, "review")
        self.assertEqual(review_row.verdict, VERDICT_PASS)
        self.assertEqual(review_row.content_hash, task.content_hash())
        author_row = engine.latest_report(self.task_id, "author")
        self.assertEqual(author_row.verdict, VERDICT_PASS)
        self.assertIn("PRESERVED", author_row.payload_json)
        self.assertEqual(engine.repair_rounds_used(self.task_id), 1)

    def test_preservation_is_sticky_across_a_later_re_attestation(self):
        """The one-stage-later version of the same revert.

        Once preserved, the recorded sha MATCHES the stored prose, so a naive
        'did the prose move since I wrote it?' test would answer no on the next
        re-attestation (any later stage moving the content hash re-runs every
        stage) and re-author the repair away then. The recorded SOURCE is what
        keeps preservation sticky while the bytes are unchanged."""
        engine = self.make_prose_engine(max_repair_rounds=3)
        engine.register(self.task)
        engine.run(self.task_id, until="author")

        task = engine.load_task(self.task_id)
        patched = task.model_copy(
            update={"solver_prompt": task.solver_prompt + "\n\n" + self.sentinel}
        )
        engine.save_task(patched)
        repaired = repair.apply_repair(
            engine, patched, RepairRoute.SPECIFICATION, "tie-break undefined"
        )
        first = make_author_runner(self.provider)(engine, repaired)
        self.assertEqual(first.verdict, VERDICT_PASS)
        engine.record_report(repaired, "author", VERDICT_PASS, first.payload)

        # some later stage moves the hash; author re-attests at the new identity
        again = make_author_runner(self.provider)(engine, repaired)
        self.assertEqual(again.verdict, VERDICT_PASS)
        self.assertIsNone(again.task, "must not hand back re-authored prose")
        self.assertIn("PRESERVED", again.payload.detail)
        self.assertEqual(len(self.provider.calls), 1)

    def test_a_repaired_prose_that_breaks_fidelity_still_fails(self):
        """Preserving repaired prose is not trusting it: the deterministic
        completeness gate still runs, and a patch that guts the prose fails
        the stage on the specification route."""
        engine = self.make_prose_engine(max_repair_rounds=3)
        engine.register(self.task)
        engine.run(self.task_id, until="author")

        task = engine.load_task(self.task_id)
        gutted = task.model_copy(update={"solver_prompt": "Do the thing."})
        engine.save_task(gutted)
        repaired = repair.apply_repair(
            engine, gutted, RepairRoute.SPECIFICATION, "tie-break undefined"
        )

        outcome = make_author_runner(self.provider)(engine, repaired)
        self.assertEqual(outcome.verdict, VERDICT_FAIL)
        self.assertIn("REPAIRED prose", outcome.payload.error)
        self.assertIs(outcome.route, RepairRoute.SPECIFICATION)


if __name__ == "__main__":
    unittest.main()

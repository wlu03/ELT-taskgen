"""Tests for the Phase-1 trial certifier (roadmap 1.P.0 and the
`trial_phase` / `_commit` split; certify addendum §0 F1, §3.3, §3.4, §3.6, §5).

WHY THIS EXISTS
The repair proposer certifies a patch by re-running the invalidated stages
on a trial copy. Two facts about that certifier were never pinned:

  * F1 — with the REAL wired runners no certification reaching `attack`
    could go green, because `_revalidate` recorded no ledger rows on the
    trial while `run_attack_stage` demands a `review` PASS at the current
    content hash (cli._require_pass_payload). Every proposer test used
    fixture runners that read no ledger row, so the suite never saw it.
    `test_revalidate_with_real_attack_runner_requires_review_evidence_at_new_hash`
    reproduces the defect with `build_stage_runners` and is its regression
    test; `test_trial_ledger_records_stage_rows_so_currency_checks_hold`
    pins the fix's mechanism (every member's outcome recorded on the TRIAL
    ledger, the wired prerequisite `review` prepended for lists that reach
    `attack` without it, the LIVE ledger untouched).
  * the split — `attempt_patch` is now `trial_phase` + `_commit`,
    byte-identical on every task-defect path, with `_commit` the only commit
    site; `trial_phase` substitutes the structural `calibrate` runner for the
    empirical one and re-raises harness faults instead of wrapping them (C7).

No live provider anywhere: the real runners are driven by offline doubles
(the recorded demo prose for the author, canned minor findings for the
critics) and the transcript-store path by `FakeTransport`.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

try:  # `python -m unittest tests.test_repair_proposer_agentic` from the repo root
    from tests import test_providers as provider_fixture
    from tests import test_repair_discrimination as discrimination_fixture
except ImportError:  # discovered from inside tests/ (no package on sys.path)
    import test_providers as provider_fixture
    import test_repair_discrimination as discrimination_fixture

from elt_taskgen import engine as engine_mod
from elt_taskgen import repair
from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.engine import (
    Engine,
    InfrastructureFailure,
    StageName,
    StageOutcome,
    StagePayload,
    VERDICT_FAIL,
    VERDICT_PASS,
)
from elt_taskgen.models import (
    AttackCase,
    AttackKind,
    CouncilRole,
    PopulationName,
    RepairEdit,
    RepairEditOp,
    RepairPatch,
    RepairRoute,
    TaskStatus,
    canonical_json,
)
from elt_taskgen.review import council
from elt_taskgen.review import repair_proposer as rp
from elt_taskgen.review.council import ProviderProtocolError

PROSE = (
    "Build the customer_summary mart: one row per customer, with the number of "
    "completed orders and the total completed order value."
)
SENTINEL = "Ties are broken by customer_id ascending."
REFERENCE_FILE_SQL = "SELECT 1 AS placeholder_reference;\n"
MART = "customer_summary"
REFERENCE_COMMENT = "-- clarified reference"

#: A minor finding with an executable probe: passes the review stage's
#: diligence rule (the shortcut attacker names a probe) and, being minor,
#: does not block the critic-to-mutation handoff at `attack`.
MINOR_FINDING = {
    "severity": "minor",
    "summary": "constants shortcut must lose reward",
    "detail": "compile a constants mutant",
    "route_hint": None,
    "suggested_attack": "constants",
    "proposed_case": {
        "kind": "constants",
        "params": "{}",
        "expected_pass_by_stage": {
            "extract_load": {
                population.value: True for population in PopulationName
            },
            "transform": {
                population.value: population is PopulationName.PRIMARY
                for population in PopulationName
            },
        },
        "rationale": (
            "the executable constants probe should preserve source loading "
            "and tests the hypothesis that only primary keeps full transform "
            "reward while the other populations defeat it"
        ),
    },
}
MINOR_FINDINGS_JSON = json.dumps({"findings": [MINOR_FINDING]})


def recorded_demo_prose() -> str:
    """Current fidelity-green demo prose for the offline author double.

    Historical semantic-author transcripts intentionally remain keyed to the
    behavior that produced them.  Loading their response here would launder
    stale prose across the stronger TaskIR-derived author contract, so this
    current-behavior fixture reads the independently maintained declarative
    prose oracle instead.
    """
    path = Path(__file__).parent / "fixtures" / "declarative_prose.txt"
    response = path.read_text(encoding="utf-8")
    if not response.strip():
        raise AssertionError(f"empty current demo prose fixture: {path}")
    return response


class ScriptedProvider:
    """Fixture proposer: hands back pre-built patches; records every call."""

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


class LadderProvider:
    """Offline double for the REAL stage runners: the recorded, fidelity-green
    demo prose for the author and one minor finding for every critic. Carries
    no `routing`, so the admission gate treats it as a non-live stub."""

    def __init__(self) -> None:
        self.prose = recorded_demo_prose()
        self.calls: list[str] = []

    def complete(self, role, prompt: str) -> str:
        name = getattr(role, "value", str(role))
        self.calls.append(name)
        return self.prose if name == "semantic_author" else MINOR_FINDINGS_JSON


def spec_patch(new_text: str = SENTINEL) -> RepairPatch:
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


def bad_anchor_patch() -> RepairPatch:
    """Applies nowhere: the replace anchor does not occur (PatchApplicationError)."""
    return RepairPatch(
        route=RepairRoute.SPECIFICATION,
        artifact="task_ir.json",
        edits=(
            RepairEdit(
                op=RepairEditOp.REPLACE,
                locator="solver_prompt",
                old="this sentence is not in the prose",
                new=SENTINEL,
            ),
        ),
        rationale="a proposal over text it never read",
        proposer_role=rp.ROLE_NAME,
    )


def out_of_scope_ir_patch() -> RepairPatch:
    """Claims SPECIFICATION, edits reference SQL inside task_ir.json."""
    return RepairPatch(
        route=RepairRoute.SPECIFICATION,
        artifact="task_ir.json",
        edits=(
            RepairEdit(
                op=RepairEditOp.REPLACE,
                locator=f"reference.sql_by_mart.{MART}",
                old="WITH completed_orders AS (",
                new="WITH completed_orders AS ( -- relaxed\n",
            ),
        ),
        rationale="'just a prose clarification'",
        proposer_role=rp.ROLE_NAME,
    )


def reference_comment_patch(task) -> RepairPatch:
    """An in-scope REFERENCE patch that changes the reference SQL's BYTES (so
    the content hash moves and `reference` re-freezes) but not its meaning:
    a leading comment. Gold and the attack matrix are unchanged, so a real
    certification that reaches `attack` must go green."""
    sql = task.reference.sql_by_mart[MART]
    head = sql.split("\n", 1)[0]
    return RepairPatch(
        route=RepairRoute.REFERENCE,
        artifact="task_ir.json",
        edits=(
            RepairEdit(
                op=RepairEditOp.REPLACE,
                locator=f"reference.sql_by_mart.{MART}",
                old=head,
                new=f"{REFERENCE_COMMENT}\n{head}",
            ),
        ),
        rationale="document the reference's join rule",
        proposer_role=rp.ROLE_NAME,
    )


POPULATION_CONDITION = "Cancelled orders are present."
POPULATION_CONDITION_REVISED = "Cancelled orders are present, and some are refunded."


def population_conditions_patch(task) -> RepairPatch:
    """An in-scope POPULATION patch on ONE population condition of the demo
    task's primary population: the bytes and the hash move, and — conditions
    being rendered into the population adversary's view alone (F3b,
    `council._population_summary(with_conditions=True)`) — exactly one
    critic view moves with them."""
    assert task.populations[1].conditions[1] == POPULATION_CONDITION
    return RepairPatch(
        route=RepairRoute.POPULATION,
        artifact="task_ir.json",
        edits=(
            RepairEdit(
                op=RepairEditOp.REPLACE,
                locator="populations.1.conditions.1",
                old=POPULATION_CONDITION,
                new=POPULATION_CONDITION_REVISED,
            ),
        ),
        rationale="state the cancelled-order condition precisely",
        proposer_role=rp.ROLE_NAME,
    )


def pass_runner(detail: str = "ok"):
    def run(engine, task):
        return StageOutcome(VERDICT_PASS, StagePayload(detail=detail))

    return run


LEDGER_FILE = "state/taskgen.sqlite"
LEDGER_FILES = frozenset({LEDGER_FILE, f"{LEDGER_FILE}-shm", f"{LEDGER_FILE}-wal"})


def tree_hash(root: Path, *, exclude_ledger: bool = False) -> dict[str, str]:
    """sha256 of every file under `root` (the whole workspace). Two
    workspaces built the same way differ only in the ledger's wall-clock
    `created_at`, so a CROSS-workspace comparison excludes the SQLite file
    and its live WAL/SHM sidecars and compares `ledger_rows` instead; a
    before/after comparison of ONE workspace includes the durable database and
    WAL. SQLite's SHM reader-lock page is always excluded: opening a consistent
    read snapshot changes those volatile lock bytes without changing ledger
    data."""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(root).rglob("*"))
        if p.is_file()
        and p.relative_to(root).as_posix() != f"{LEDGER_FILE}-shm"
        and not (exclude_ledger and p.relative_to(root).as_posix() in LEDGER_FILES)
    }


def ledger_rows(engine: Engine, task_id: str) -> list[tuple]:
    """Every report and repair row, wall-clock-free."""
    reports = engine._con.execute(
        "SELECT id, revision, stage, verdict, payload_json, content_hash FROM reports"
        " WHERE task_id=? ORDER BY id",
        (task_id,),
    ).fetchall()
    repairs = engine._con.execute(
        "SELECT id, revision, route, reason, fingerprint FROM repairs WHERE task_id=? ORDER BY id",
        (task_id,),
    ).fetchall()
    return [tuple(r) for r in reports] + [("repair", *r) for r in repairs]


def rows_by_stage(engine: Engine, task_id: str) -> dict[str, int]:
    return {
        str(stage): int(count)
        for stage, count in engine._con.execute(
            "SELECT stage, COUNT(*) FROM reports WHERE task_id=? GROUP BY stage",
            (task_id,),
        ).fetchall()
    }


class _CertifierFixture(unittest.TestCase):
    """The prose fixture of tests/test_repair_proposer.py: an author that
    passes and a review that is green only once the prose states the
    tie-break."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.task = demo_task().model_copy(update={"solver_prompt": PROSE})
        self.task_id = self.task.task_id

    def workspace(self, name: str) -> Path:
        return self.root / name

    def author_ok(self, engine, task):
        return StageOutcome(VERDICT_PASS, StagePayload(detail="author ok"))

    def review_needs_sentinel(self, engine, task):
        if SENTINEL in task.solver_prompt:
            return StageOutcome(VERDICT_PASS, StagePayload(detail="review ok"))
        return StageOutcome(VERDICT_FAIL, StagePayload(error="undefined tie-break in prose"))

    def make_engine(self, workspace: Path, *, review=None, proposer=None, **kwargs) -> Engine:
        engine = Engine(
            workspace,
            stage_runners={
                "author": self.author_ok,
                "review": review or self.review_needs_sentinel,
            },
            repair_proposer=proposer,
            **kwargs,
        )
        self.addCleanup(engine.close)
        engine.register(self.task)
        ref = engine.task_dir(self.task_id) / "answer_key" / "reference"
        ref.mkdir(parents=True, exist_ok=True)
        (ref / "solution.sql").write_text(REFERENCE_FILE_SQL, encoding="utf-8")
        return engine

    @staticmethod
    def review_raising_on_the_trial(exc):
        def review(engine, task):
            if SENTINEL in task.solver_prompt:  # the patched TRIAL copy
                raise exc
            return StageOutcome(VERDICT_FAIL, StagePayload(error="undefined tie-break in prose"))

        return review

    @staticmethod
    def review_blocked_on_the_trial(blocked_on: str):
        """A review that WAITS on the patched trial copy (`VERDICT_BLOCKED`
        with `blocked_on`, or naming no reason for '') exactly as the live
        gates / audit / select runners answer an environment hold, a human
        approval or stale evidence — and fails as before on the live prose."""

        def review(engine, task):
            if SENTINEL in task.solver_prompt:  # the patched TRIAL copy
                data = {engine_mod.BLOCKED_ON_KEY: blocked_on} if blocked_on else {}
                return StageOutcome(
                    engine_mod.VERDICT_BLOCKED,
                    StagePayload(detail="waiting: the warehouse at /Users/x/runs/live is down", data=data),
                )
            return StageOutcome(VERDICT_FAIL, StagePayload(error="undefined tie-break in prose"))

        return review


class _DiscriminationRunners:
    """The literal-rows guard fixture of tests/test_repair_discrimination.py:
    an `attack` runner that re-measures the inner_join mutant against the
    CURRENT counterfactual rows (it is discriminated while a 'completed'
    order exists) and a `generate` runner green only once the placeholder
    customer name is gone."""

    CASE = discrimination_fixture.CASE

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.attack_runs = 0

    def counterfactual(self, task):
        for pop in task.populations:
            if pop.name is PopulationName.COUNTERFACTUAL:
                return pop
        raise AssertionError("demo task has no counterfactual population")

    def write_rewards(self, workspace: Path, task, discriminates: bool) -> None:
        d = Path(workspace) / "tasks" / self.task_id / "attacks" / self.CASE
        d.mkdir(parents=True, exist_ok=True)
        (d / repair.ATTACK_REWARDS_FILENAME).write_text(
            json.dumps(
                {
                    "case": self.CASE,
                    "rewards": {"primary": 1.0, "counterfactual": 0.0 if discriminates else 1.0},
                    "task_content_hash": task.content_hash(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def attack(self, engine, task):
        self.attack_runs += 1
        rows = list(self.counterfactual(task).literal_rows.get("orders", ()))
        self.write_rewards(engine.workspace, task, any(r.get("status") == "completed" for r in rows))
        return StageOutcome(VERDICT_PASS, StagePayload(detail="attack ok"))

    def generate(self, engine, task):
        rows = self.counterfactual(task).literal_rows.get("customers", ())
        if rows and rows[0].get("customer_name") != "C10":
            return StageOutcome(VERDICT_PASS, StagePayload(detail="coverage ok"))
        return StageOutcome(VERDICT_FAIL, StagePayload(error="population coverage: placeholder rows"))

    def engine(self, workspace: Path, task, cleanup) -> Engine:
        engine = Engine(
            workspace,
            stage_runners={
                "generate": self.generate,
                "reference": pass_runner(),
                "attack": self.attack,
            },
            max_repair_rounds=0,
        )
        cleanup(engine.close)
        engine.register(task)
        self.write_rewards(workspace, engine.load_task(self.task_id), True)
        return engine


def _certify_with_attempt_patch(engine: Engine, task, stage: str, patch: RepairPatch):
    try:
        committed = rp.attempt_patch(engine, task, stage, patch)
    except rp.PatchRejected as exc:
        return ("raised", type(exc).__name__, str(exc), exc.code, exc.verdict)
    return ("committed", committed.content_hash())


def _certify_with_trial_phase_then_commit(engine: Engine, task, stage: str, patch: RepairPatch):
    """The split, composed by hand: exactly what `attempt_patch` does."""
    task_id = task.task_id
    with rp.trial_workspace(engine.workspace) as trial:
        before = repair.snapshot(trial, task_id)
        before_ir = (trial / "tasks" / task_id / "task_ir.json").read_text(encoding="utf-8")
        try:
            verdict = rp.trial_phase(
                engine, task, stage, patch, trial, before=before, before_ir=before_ir
            )
        except rp.PatchRejected as exc:
            return ("raised", type(exc).__name__, str(exc), exc.code, exc.verdict)
        rp._commit(trial, engine.workspace, verdict.diff.changed)
    return ("committed", engine.load_task(task_id).content_hash())


# ---------------------------------------------------------------------------
# attempt_patch = trial_phase + _commit
# ---------------------------------------------------------------------------

class TrialPhaseSplitTest(_CertifierFixture):
    def test_attempt_patch_equals_trial_phase_plus_commit_byte_identical(self):
        """Certify addendum §3.3: for each task-defect path (scope, application,
        red verdict, guard) and for a green patch, the live workspace bytes,
        the raised class, message and code, the attached `TrialVerdict` and
        the `ProposerAttempt` record are identical between `attempt_patch`
        and `trial_phase` followed by `_commit`."""
        discrimination = _DiscriminationRunners(self.task_id)
        base_task = demo_task()

        def prose_engine(name):
            engine = self.make_engine(self.workspace(name))
            return engine, engine.load_task(self.task_id)

        def guard_engine(name):
            engine = discrimination.engine(self.workspace(name), base_task, self.addCleanup)
            return engine, engine.load_task(self.task_id)

        paths = (
            ("scope", prose_engine, "review", out_of_scope_ir_patch(), "ScopeViolation"),
            ("application", prose_engine, "review", bad_anchor_patch(), "PatchApplicationError"),
            ("red", prose_engine, "review", spec_patch("cosmetic wording"), "RevalidationFailed"),
            (
                "guard",
                guard_engine,
                "generate",
                discrimination_fixture._strip_all_completed_patch(base_task),
                "DiscriminationWeakened",
            ),
            ("green", prose_engine, "review", spec_patch(), None),
        )
        def same_workspace(engine_a, engine_b):
            self.assertEqual(
                tree_hash(engine_a.workspace, exclude_ledger=True),
                tree_hash(engine_b.workspace, exclude_ledger=True),
            )
            self.assertEqual(ledger_rows(engine_a, self.task_id), ledger_rows(engine_b, self.task_id))

        for label, build, stage, patch, expected in paths:
            with self.subTest(path=label):
                engine_a, task_a = build(f"{label}-a")
                engine_b, task_b = build(f"{label}-b")
                same_workspace(engine_a, engine_b)
                untouched_a = tree_hash(engine_a.workspace)
                untouched_b = tree_hash(engine_b.workspace)
                whole = _certify_with_attempt_patch(engine_a, task_a, stage, patch)
                split = _certify_with_trial_phase_then_commit(engine_b, task_b, stage, patch)
                self.assertEqual(whole, split)
                same_workspace(engine_a, engine_b)
                if expected is not None:
                    # A rejection leaves each live workspace byte-identical.
                    self.assertEqual(tree_hash(engine_a.workspace), untouched_a)
                    self.assertEqual(tree_hash(engine_b.workspace), untouched_b)
                if expected is None:
                    self.assertEqual(whole[0], "committed")
                    self.assertNotEqual(whole[1], task_a.content_hash())
                    self.assertIn(SENTINEL, engine_a.load_task(self.task_id).solver_prompt)
                    continue
                self.assertEqual(whole[0], "raised")
                self.assertEqual(whole[1], expected)
                verdict = whole[4]
                if expected == "RevalidationFailed":
                    self.assertEqual(verdict, rp.TrialVerdict(False, "review", False, verdict.diff))
                    self.assertEqual(whole[3], "revalidation_red_review")
                elif expected == "DiscriminationWeakened":
                    self.assertEqual(verdict, rp.TrialVerdict(False, None, True, verdict.diff))
                    self.assertTrue(verdict.diff.changed)
                else:
                    self.assertIsNone(verdict)  # rejected before a diff existed
                # The one-shot proposer's attempt record is the same on both
                # sides: it is `attempt_patch`'s exception, verbatim.
                engine_c, task_c = build(f"{label}-c")
                outcome = rp.RepairProposer(ScriptedProvider([patch]), max_attempts=1).repair(
                    engine_c, task_c, stage, patch.route, "undefined tie-break in prose"
                )
                self.assertEqual(outcome.disposition, rp.DISPOSITION_NEEDS_ADJUDICATION)
                self.assertEqual(
                    outcome.record.attempts,
                    (
                        rp.ProposerAttempt(
                            index=1,
                            route=patch.route.value,
                            artifact=patch.artifact,
                            accepted=False,
                            error_type=split[1],
                            reason=split[2][:600],
                        ),
                    ),
                )

        # `_commit` stays the only commit site: a green `trial_phase` on its
        # own moves not one byte of the live workspace.
        engine, task = prose_engine("phase-only")
        before = tree_hash(engine.workspace)
        with rp.trial_workspace(engine.workspace) as trial:
            snap = repair.snapshot(trial, self.task_id)
            before_ir = (trial / "tasks" / self.task_id / "task_ir.json").read_text(encoding="utf-8")
            verdict = rp.trial_phase(
                engine, task, "review", spec_patch(), trial, before=snap, before_ir=before_ir
            )
            self.assertTrue(verdict.green)
            self.assertIsNone(verdict.failing_stage)
            self.assertFalse(verdict.discrimination_weakened)
            self.assertEqual(verdict.diff.changed, frozenset({f"tasks/{self.task_id}/task_ir.json"}))
            patched_ir = json.loads(
                (trial / "tasks" / self.task_id / "task_ir.json").read_text(encoding="utf-8")
            )
            self.assertIn(SENTINEL, patched_ir["solver_prompt"])
        self.assertEqual(tree_hash(engine.workspace), before)
        self.assertNotIn(SENTINEL, engine.load_task(self.task_id).solver_prompt)

    def test_nested_transport_fault_inside_trial_phase_halts_without_spending_a_round(self):
        """Certify addendum §3.3, the ONE pinned difference from the un-split
        certifier (C7): a runner exception on the trial whose MRO hits
        `_INFRA_EXCEPTION_NAMES` is re-raised out of `trial_phase` (a nested
        plain `ProviderProtocolError` under the engine's marker), never
        wrapped into a rejection; the proposer HALTS after one model call
        with nothing queued, and the engine raises `InfrastructureFailure`
        with zero repair rounds and no rejection."""
        from elt_taskgen.review import providers as providers_mod

        cases = (
            (ProviderProtocolError("critic returned no findings JSON"), InfrastructureFailure),
            (
                providers_mod.BudgetExceededError(
                    "per-task budget cannot absorb the next call", scope="task"
                ),
                providers_mod.BudgetExceededError,
            ),
            (
                providers_mod.TranscriptMissingError("replay-only mode: no recorded transcript"),
                providers_mod.TranscriptMissingError,
            ),
        )
        for exc, raised in cases:
            name = type(exc).__name__
            with self.subTest(fault=name):
                review = self.review_raising_on_the_trial(exc)
                engine = self.make_engine(self.workspace(f"phase-{name}"), review=review)
                task = engine.load_task(self.task_id)
                before = tree_hash(engine.workspace)
                with rp.trial_workspace(engine.workspace) as trial:
                    snap = repair.snapshot(trial, self.task_id)
                    before_ir = (trial / "tasks" / self.task_id / "task_ir.json").read_text(
                        encoding="utf-8"
                    )
                    with self.assertRaises(raised) as ctx:
                        rp.trial_phase(
                            engine, task, "review", spec_patch(), trial,
                            before=snap, before_ir=before_ir,
                        )
                self.assertNotIsInstance(ctx.exception, rp.PatchRejected)
                if raised is InfrastructureFailure:
                    self.assertEqual(ctx.exception.marker, "ProviderProtocolError")
                    self.assertIs(ctx.exception.__cause__, exc)
                else:
                    self.assertIs(ctx.exception, exc)
                self.assertEqual(rp.halting_marker(ctx.exception), name)
                self.assertEqual(tree_hash(engine.workspace), before)

                provider = ScriptedProvider([spec_patch(), spec_patch()])
                outcome = rp.RepairProposer(provider, max_attempts=2).repair(
                    engine, task, "review", RepairRoute.SPECIFICATION, "undefined tie-break"
                )
                self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)
                self.assertEqual(outcome.infrastructure, name)
                self.assertEqual(len(provider.calls), 1)
                self.assertIsNone(rp.load_repair_adjudication(engine.workspace, self.task_id))
                self.assertEqual(tree_hash(engine.workspace), before)

                run_engine = self.make_engine(
                    self.workspace(f"run-{name}"),
                    review=review,
                    proposer=rp.RepairProposer(ScriptedProvider([spec_patch()]), max_attempts=2),
                    max_repair_rounds=2,
                )
                for upstream in ("contamination_pre", "generate", "reference"):
                    run_engine.set_stage_runner(upstream, self.author_ok)
                with self.assertRaises(InfrastructureFailure) as halted:
                    run_engine.run(self.task_id, until="review")
                self.assertEqual(halted.exception.marker, name.lower())
                self.assertEqual(run_engine.repair_rounds_used(self.task_id), 0)
                self.assertIsNot(run_engine.load_task(self.task_id).status, TaskStatus.REJECTED)
                self.assertIsNone(rp.load_repair_adjudication(run_engine.workspace, self.task_id))

    def test_blocked_stage_inside_trial_phase_halts_without_spending_a_round(self):
        """A re-validated member that WAITS (`VERDICT_BLOCKED`: an environment
        or human-approval hold, stale evidence, another seat's session limit)
        is a NO-MEASURE outcome, never a red one (C7). Scored live the same
        verdict spends no round and rejects nothing (`Engine.run` records the
        BLOCKED row and returns); inside the certifier it is the same
        disposition: `trial_phase` raises `engine.StageBlocked` — an
        `InfrastructureFailure` under `blocked_on:<reason>`, never a
        `PatchRejected` — the one-shot proposer HALTS after one model call
        with nothing queued and the patch NOT rejected, and the engine raises
        `InfrastructureFailure` with the reason on the FAIL row, zero repair
        rounds, no rejection, the workspace byte-identical. Once the wait
        clears, the next run resumes at the failed stage and the same patch
        commits."""
        cases = (
            (engine_mod.BLOCKED_ON_ENVIRONMENT, "environment"),
            (engine_mod.BLOCKED_ON_HUMAN, "human"),
            (f"{engine_mod.SESSION_LIMIT_BLOCK_PREFIX}turns", "session_limit:turns"),
            ("", engine_mod.BLOCKED_ON_UNKNOWN),
        )
        for reason, expected in cases:
            marker = f"{engine_mod.BLOCKED_STAGE_MARKER_PREFIX}{expected}"
            with self.subTest(blocked_on=reason or "(none)"):
                review = self.review_blocked_on_the_trial(reason)
                engine = self.make_engine(self.workspace(f"phase-{expected}"), review=review)
                task = engine.load_task(self.task_id)
                before = tree_hash(engine.workspace)
                with rp.trial_workspace(engine.workspace) as trial:
                    snap = repair.snapshot(trial, self.task_id)
                    before_ir = (trial / "tasks" / self.task_id / "task_ir.json").read_text(
                        encoding="utf-8"
                    )
                    with self.assertRaises(engine_mod.StageBlocked) as ctx:
                        rp.trial_phase(
                            engine, task, "review", spec_patch(), trial,
                            before=snap, before_ir=before_ir,
                        )
                exc = ctx.exception
                self.assertNotIsInstance(exc, rp.PatchRejected)
                self.assertIsInstance(exc, InfrastructureFailure)
                self.assertEqual((exc.task_id, exc.stage, exc.blocked_on, exc.marker), (self.task_id, "review", expected, marker))
                self.assertEqual(rp.halting_marker(exc), marker)
                self.assertEqual(engine_mod._infra_marker_for(exc), marker)
                self.assertEqual(engine_mod.blocked_on_from_marker(marker), expected)
                self.assertNotIn("/Users", str(exc))  # the wait's payload stays on the trial
                self.assertEqual(tree_hash(engine.workspace), before)

                provider = ScriptedProvider([spec_patch(), spec_patch()])
                outcome = rp.RepairProposer(provider, max_attempts=2).repair(
                    engine, task, "review", RepairRoute.SPECIFICATION, "undefined tie-break"
                )
                self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)
                self.assertEqual(outcome.infrastructure, marker)
                self.assertEqual(len(provider.calls), 1)  # halted: no second attempt
                self.assertEqual(len(outcome.record.attempts), 1)
                self.assertEqual(outcome.record.attempts[0].error_type, "StageBlocked")
                self.assertFalse(outcome.record.attempts[0].accepted)
                self.assertIsNone(rp.load_repair_adjudication(engine.workspace, self.task_id))
                self.assertEqual(tree_hash(engine.workspace), before)

                run_engine = self.make_engine(
                    self.workspace(f"run-{expected}"),
                    review=review,
                    proposer=rp.RepairProposer(ScriptedProvider([spec_patch()]), max_attempts=2),
                    max_repair_rounds=2,
                )
                for upstream in ("contamination_pre", "generate", "reference"):
                    run_engine.set_stage_runner(upstream, self.author_ok)
                with self.assertRaises(InfrastructureFailure) as halted:
                    run_engine.run(self.task_id, until="review")
                self.assertEqual(halted.exception.marker, marker)
                self.assertEqual(run_engine.repair_rounds_used(self.task_id), 0)
                self.assertIsNot(run_engine.load_task(self.task_id).status, TaskStatus.REJECTED)
                self.assertIsNone(rp.load_repair_adjudication(run_engine.workspace, self.task_id))
                self.assertNotIn(SENTINEL, run_engine.load_task(self.task_id).solver_prompt)
                latest = run_engine.latest_report(self.task_id, "review")
                self.assertEqual(latest.verdict, VERDICT_FAIL)
                payload = json.loads(latest.payload_json)
                self.assertEqual(payload["infrastructure"], marker)
                self.assertEqual(payload["data"]["blocked_on"], expected)
                self.assertEqual(payload["data"]["status"], "halted")
                self.assertNotIn(
                    "/Users", latest.payload_json
                )  # the trial's BLOCKED payload never reaches the live ledger

                # The resume: the wait cleared, the next run re-executes the
                # stage, the same patch certifies green and commits.
                run_engine.set_stage_runner("review", self.review_needs_sentinel)
                resumed = run_engine.run(self.task_id, until="review")
                self.assertIsNot(resumed.status, TaskStatus.REJECTED)
                self.assertIn(SENTINEL, resumed.solver_prompt)
                self.assertEqual(run_engine.latest_report(self.task_id, "review").verdict, VERDICT_PASS)

    def test_trial_phase_substitutes_structural_calibrate_for_empirical_runner(self):
        """Certify addendum §3.4: with the engine wired for `calibrate
        --empirical`, a SPECIFICATION failure at `calibrate` certifies on the
        trial with the STRUCTURAL runner (over the same roster document) and
        never launches the solver campaign; the live ladder keeps the
        empirical runner for its own post-commit run. The tag survives the
        CLI's echo wrapper."""
        from elt_taskgen import cli
        from elt_taskgen.corpus import calibration as calibration_mod

        class NoSolver:
            def complete(self, role, prompt):
                raise AssertionError("a solver campaign was launched inside the trial")

        runners = cli.build_stage_runners(
            NoSolver(), echo=lambda line: None, calibrate_options={"empirical": True}
        )
        wired = runners[StageName.CALIBRATE]
        self.assertTrue(rp._is_empirical_calibrate_runner(wired))
        self.assertTrue(getattr(wired, rp.CALIBRATE_EMPIRICAL_TAG))
        self.assertFalse(rp._is_empirical_calibrate_runner(cli.run_calibrate))
        self.assertFalse(
            rp._is_empirical_calibrate_runner(
                cli.build_stage_runners(NoSolver(), echo=lambda line: None)[StageName.CALIBRATE]
            )
        )

        engine = Engine(self.workspace("empirical"), stage_runners=runners, max_repair_rounds=0)
        self.addCleanup(engine.close)
        engine.register(self.task)
        for stage in ("author", "attack", "gates", "gates_extract_load", "gates_transform"):
            engine.set_stage_runner(stage, pass_runner(f"{stage} ok"))
        engine.set_stage_runner("review", self.review_needs_sentinel)
        task = engine.load_task(self.task_id)

        ran_on: list[Path] = []
        real_make = cli.make_structural_calibrate_runner

        def spy_make(agents_config=None):
            structural = real_make(agents_config)

            def run(engine_, task_):
                ran_on.append(Path(engine_.workspace))
                return structural(engine_, task_)

            run.agents_config = agents_config
            return run

        with mock.patch.object(cli, "make_structural_calibrate_runner", spy_make), \
                mock.patch.object(
                    calibration_mod, "calibrate_task",
                    side_effect=AssertionError("the empirical campaign ran on the trial"),
                ):
            committed = rp.attempt_patch(engine, task, "calibrate", spec_patch())

        self.assertIn(SENTINEL, committed.solver_prompt)
        self.assertEqual(len(ran_on), 1)
        self.assertNotEqual(ran_on[0], engine.workspace)  # the trial, not the live tree
        self.assertIs(engine.stage_runners["calibrate"], wired)  # live ladder untouched
        self.assertTrue(rp._is_empirical_calibrate_runner(engine.stage_runners["calibrate"]))
        self.assertIsNone(getattr(wired, "agents_config"))

        # The substitute is built over the empirical runner's OWN roster document.
        custom = self.root / "roster.yaml"
        custom.write_text(
            "calibration:\n  roster:\n"
            "    - model_key: anthropic:weak-tier\n"
            "      provider: anthropic\n      model: weak-tier\n      k: 1\n",
            encoding="utf-8",
        )
        tagged = cli.make_calibrate_runner(NoSolver(), empirical=True, agents_config=custom)
        self.assertEqual(tagged.agents_config, custom)
        with mock.patch.object(cli, "make_structural_calibrate_runner", spy_make):
            self.assertEqual(rp._structural_calibrate_substitute(tagged).agents_config, custom)


# ---------------------------------------------------------------------------
# 1.P.0 — the F1 currency fix
# ---------------------------------------------------------------------------

class PopulationMaterialGuardTest(_CertifierFixture):
    def test_population_conditions_patch_reruns_attack_and_runs_the_guard(self):
        """Phase 3 review finding 2-1: `populations.*.conditions` drive row
        generation (`source_data.declares_dangling` arms the dangling-key
        lever `_generate_table` and the referential-integrity gate read), so
        a POPULATION patch to a condition is population material like
        literal rows. Over a MEASURED baseline (an attack record at the live
        hash) the guard arms on it (`discrimination_guard_armed`):
        `reference` and `attack` re-run on the trial WHATEVER the failed
        stage, and a condition edit that disarms the lever — the probe that
        discriminated only through it now keeps full reward — is
        `DiscriminationWeakened`, never a commit. An edit that keeps the
        lever re-measures a superset and commits. With NO baseline (a
        failure before `attack` at this hash) a condition repair stays
        expressible on the route's own stages, exactly as
        `test_in_scope_population_patch_commits` pins."""
        from elt_taskgen.generation.source_data import declares_dangling

        case = "dangling_parent_probe"
        cf_index = next(
            i for i, p in enumerate(self.task.populations) if p.name is PopulationName.COUNTERFACTUAL
        )
        armed = "Some child rows carry dangling parent keys (orphans)."
        disarmed = "No dangling parent keys: every child row has a parent."
        kept = armed + " The orphans are the C13 case."
        self.assertTrue(declares_dangling((armed,)))
        self.assertTrue(declares_dangling((kept,)))
        self.assertFalse(declares_dangling((disarmed,)))
        original = self.task.populations[cf_index]
        slot = len(original.conditions)
        task = self.task.model_copy(update={"populations": tuple(
            p.model_copy(update={"conditions": tuple(p.conditions) + (armed,)}) if i == cf_index else p
            for i, p in enumerate(self.task.populations)
        )})
        task = with_durable_attack_case(task, case)
        attack_runs: list[tuple[Path, bool]] = []

        def write_rewards(workspace: Path, t, discriminates: bool) -> None:
            d = Path(workspace) / "tasks" / self.task_id / "attacks" / case
            d.mkdir(parents=True, exist_ok=True)
            (d / repair.ATTACK_REWARDS_FILENAME).write_text(
                json.dumps(
                    {
                        "case": case,
                        "rewards": {"primary": 1.0, "counterfactual": 0.0 if discriminates else 1.0},
                        "task_content_hash": t.content_hash(),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

        def attack(engine, t):
            # The probe discriminates on counterfactual ONLY through the lever.
            discriminates = declares_dangling(t.populations[cf_index].conditions)
            attack_runs.append((Path(engine.workspace).resolve(), discriminates))
            write_rewards(engine.workspace, t, discriminates)
            return StageOutcome(VERDICT_PASS, StagePayload(detail="attack ok"))

        def make_engine(name: str, *, runners: dict) -> Engine:
            engine = Engine(self.root / name, stage_runners=runners, max_repair_rounds=0)
            self.addCleanup(engine.close)
            engine.register(task)
            return engine

        def conditions_patch(new: str) -> RepairPatch:
            return RepairPatch(
                route=RepairRoute.POPULATION,
                artifact="task_ir.json",
                edits=(
                    RepairEdit(
                        op=RepairEditOp.REPLACE,
                        locator=f"populations.{cf_index}.conditions.{slot}",
                        old=armed,
                        new=new,
                    ),
                ),
                rationale="restate the orphan condition",
                proposer_role=rp.ROLE_NAME,
            )

        full = {"generate": pass_runner(), "reference": pass_runner(), "attack": attack}

        # (a) A measured baseline at the live hash: the probe discriminates on
        # counterfactual. Disarming the lever at a `generate` failure re-runs
        # `attack` on the trial and the guard rejects the weakened matrix.
        engine = make_engine("weakened", runners=full)
        live = engine.load_task(self.task_id)
        write_rewards(engine.workspace, live, True)
        baseline = repair.discrimination_matrix(
            engine.workspace, self.task_id, task_content_hash=live.content_hash()
        )
        self.assertEqual(baseline, {case: frozenset({"counterfactual"})})
        before = tree_hash(engine.workspace, exclude_ledger=True)
        with self.assertRaises(rp.DiscriminationWeakened) as ctx:
            rp.attempt_patch(engine, live, "generate", conditions_patch(disarmed))
        self.assertEqual(ctx.exception.code, "discrimination_weakened")
        self.assertIn(case, str(ctx.exception))
        self.assertIn("conditions", str(ctx.exception))
        verdict = ctx.exception.verdict
        self.assertEqual(verdict, rp.TrialVerdict(False, None, True, verdict.diff))
        self.assertEqual(len(attack_runs), 1)
        trial_ws, discriminated = attack_runs[0]
        self.assertNotEqual(trial_ws, Path(engine.workspace).resolve())
        self.assertFalse(discriminated)
        self.assertEqual(tree_hash(engine.workspace, exclude_ledger=True), before)
        self.assertEqual(engine.load_task(self.task_id).populations[cf_index].conditions[slot], armed)

        # (b) An edit that KEEPS the lever: `attack` re-runs, the matrix is a
        # superset (identical), the patch commits.
        attack_runs.clear()
        engine = make_engine("kept", runners=full)
        live = engine.load_task(self.task_id)
        write_rewards(engine.workspace, live, True)
        committed = rp.attempt_patch(engine, live, "generate", conditions_patch(kept))
        self.assertEqual(committed.populations[cf_index].conditions[slot], kept)
        self.assertEqual([d for _ws, d in attack_runs], [True])

        # (c) NO baseline (nothing measured at this hash): the route's own
        # stages only — a condition repair at `generate` commits on
        # `generate` alone, `attack` is neither demanded nor run.
        attack_runs.clear()
        bare = make_engine("bare", runners={"generate": pass_runner()})
        bare_task = bare.load_task(self.task_id)
        self.assertEqual(
            repair.discrimination_matrix(bare.workspace, self.task_id, task_content_hash=bare_task.content_hash()),
            {},
        )
        committed = rp.attempt_patch(bare, bare_task, "generate", conditions_patch(disarmed))
        self.assertEqual(committed.populations[cf_index].conditions[slot], disarmed)
        self.assertEqual(attack_runs, [])

        # The predicate itself, and the two doors it composes.
        self.assertTrue(rp.discrimination_guard_armed(
            literal_rows_moved=False, population_moved=True, matrix_before=baseline))
        self.assertFalse(rp.discrimination_guard_armed(
            literal_rows_moved=False, population_moved=True, matrix_before={}))
        self.assertFalse(rp.discrimination_guard_armed(
            literal_rows_moved=False, population_moved=True, matrix_before={case: frozenset()}))
        self.assertFalse(rp.discrimination_guard_armed(
            literal_rows_moved=False, population_moved=False, matrix_before=baseline))
        self.assertTrue(rp.discrimination_guard_armed(
            literal_rows_moved=True, population_moved=True, matrix_before={}))
        with rp.trial_workspace(engine.workspace) as trial:
            before_ir = (trial / "tasks" / self.task_id / "task_ir.json").read_text(encoding="utf-8")
            snap = repair.snapshot(trial, self.task_id)
            target = trial / "tasks" / self.task_id / "task_ir.json"
            target.write_text(rp.apply_patch_text(before_ir, conditions_patch(disarmed)), encoding="utf-8")
            diff = repair.diff_snapshots(snap, repair.snapshot(trial, self.task_id))
            self.assertTrue(rp._moved_population_material(before_ir, trial, self.task_id, diff))
            self.assertFalse(rp._moved_counterfactual_rows(before_ir, trial, self.task_id, diff))
        # The in-session certify's `attack` member arms its guard bit by the
        # same predicate (a context written before the key applied the
        # deletion rule whenever armed).
        engine = make_engine("certify", runners=full)
        live = engine.load_task(self.task_id)
        engine.record_report(live, "review", VERDICT_PASS, cli_mod.ReviewPayload(findings=(), fatal_count=0, detail="ok"))
        write_rewards(engine.workspace, live, True)
        context = C.attack_member_context(engine.workspace, live, literal_rows_moved=False, population_moved=True)
        self.assertIsNotNone(context)
        self.assertTrue(context["guard"])
        self.assertFalse(context[C.ATTACK_CONTEXT_ROWS_KEY])
        unarmed = C.attack_member_context(engine.workspace, live, literal_rows_moved=False, population_moved=False)
        self.assertFalse(unarmed["guard"])
        literal = C.attack_member_context(engine.workspace, live, literal_rows_moved=True)
        self.assertEqual((literal["guard"], literal[C.ATTACK_CONTEXT_ROWS_KEY]), (True, True))
        legacy = C._attack_context({k: v for k, v in literal.items() if k in C.ATTACK_CONTEXT_KEYS})
        self.assertTrue(legacy[C.ATTACK_CONTEXT_ROWS_KEY])


class TrialLedgerCurrencyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.task = demo_task().model_copy(update={"solver_prompt": PROSE})
        self.task_id = self.task.task_id

    def test_trial_ledger_records_stage_rows_so_currency_checks_hold(self):
        """Certify addendum §3.6 (form i): every re-validated member's outcome
        is recorded on the TRIAL ledger at the new hash, and a member's wired
        currency prerequisite outside the route's rerun set (`review` for
        `attack` on the REFERENCE route) is prepended — so a runner that
        demands a PASS row at the current hash (the real `attack` and gates
        runners do, through `cli._require_pass_payload`) finds one. The LIVE
        ledger gains no row from the trial."""
        from elt_taskgen import cli

        order = [s.value for s in StageName]
        observed: list[dict] = []
        review_runs: list[Path] = []

        def review(engine, task):
            review_runs.append(Path(engine.workspace))
            return StageOutcome(VERDICT_PASS, StagePayload(detail="review ok"))

        def attack(engine, task):
            # THE real currency check, as `run_attack_stage` opens.
            cli._require_pass_payload(engine, task, StageName.REVIEW)
            reference = engine.latest_report(task.task_id, StageName.REFERENCE.value)
            observed.append(
                {
                    "workspace": Path(engine.workspace),
                    "reference": (reference.verdict, reference.content_hash == task.content_hash()),
                }
            )
            return StageOutcome(VERDICT_PASS, StagePayload(detail="attack ok"))

        def gates(engine, task):
            cli._require_pass_payload(engine, task, StageName.ATTACK)
            return StageOutcome(VERDICT_PASS, StagePayload(detail="gates ok"))

        runners = {s.value: pass_runner(f"{s.value} ok") for s in StageName}
        runners.update({"review": review, "attack": attack, "gates": gates})
        workspace = self.root / "ws"
        engine = Engine(workspace, stage_runners=runners, max_repair_rounds=0)
        self.addCleanup(engine.close)
        engine.register(self.task)
        task = engine.run(self.task_id, until="gates")
        self.assertIs(task.status, TaskStatus.ATTACKED)
        live_before = rows_by_stage(engine, self.task_id)
        self.assertEqual(len(observed), 1)
        self.assertEqual(review_runs, [workspace.resolve()])
        old_review = engine.latest_report(self.task_id, "review")

        committed = rp.attempt_patch(engine, task, "gates", reference_comment_patch(task))

        self.assertNotEqual(committed.content_hash(), task.content_hash())
        self.assertIn(REFERENCE_COMMENT, committed.reference.sql_by_mart[MART])
        # review re-ran on the trial although REFERENCE's rerun set lacks it...
        self.assertEqual(len(review_runs), 2)
        self.assertNotEqual(review_runs[1], workspace.resolve())
        # ...and attack found its rows — review AND reference — at the new hash there.
        self.assertEqual(len(observed), 2)
        self.assertNotEqual(observed[1]["workspace"], workspace.resolve())
        self.assertEqual(observed[1]["reference"], (VERDICT_PASS, True))
        # The live ledger learned of the patch only through `_commit`.
        self.assertEqual(rows_by_stage(engine, self.task_id), live_before)
        self.assertEqual(engine.latest_report(self.task_id, "review").id, old_review.id)
        self.assertEqual(engine.latest_report(self.task_id, "review").content_hash, task.content_hash())

        # The prerequisite map and its wiring rule.
        self.assertEqual(rp.CURRENCY_PREREQUISITES["attack"], ("review",))
        for stage in ("gates", "gates_extract_load", "gates_transform"):
            self.assertEqual(rp.CURRENCY_PREREQUISITES[stage], ("attack",))
        without_review = {"reference": runners["reference"], "attack": attack}
        self.assertEqual(
            rp._with_currency_prerequisites(["reference", "attack"], without_review, order),
            ["reference", "attack"],
        )
        self.assertEqual(
            rp._with_currency_prerequisites(["reference", "attack"], runners, order),
            ["reference", "review", "attack"],
        )
        self.assertEqual(  # transitive, in pipeline order
            rp._with_currency_prerequisites(["gates"], runners, order),
            ["review", "attack", "gates"],
        )
        self.assertEqual(
            rp._with_currency_prerequisites(["author", "review", "attack"], runners, order),
            ["author", "review", "attack"],
        )

        # Unchanged fixture semantics: a fixture `attack` that reads no row is
        # certified without a `review` runner being conjured, exactly as before.
        bare = Engine(
            self.root / "bare",
            stage_runners={"reference": pass_runner(), "attack": pass_runner("attack ok")},
            max_repair_rounds=0,
        )
        self.addCleanup(bare.close)
        bare.register(self.task)
        bare_task = bare.load_task(self.task_id)
        bare_committed = rp.attempt_patch(bare, bare_task, "attack", reference_comment_patch(bare_task))
        self.assertIn(REFERENCE_COMMENT, bare_committed.reference.sql_by_mart[MART])

    def test_population_patch_is_not_rejected_by_a_live_review_verdict(self):
        """Phase 3 review finding 2-4 (C4, C7): on the REFERENCE / POPULATION
        routes `review` is not a route member — `_with_currency_prerequisites`
        prepends it only so the real `attack` runner finds its PASS row at
        the new hash. When that prerequisite does not PASS on the trial (a
        live critic's FATAL finding at the moved adversary view, or the
        review runner's own SPECIFICATION-route pre-checks), the
        execution-route patch is NOT `revalidation_red_review`: the
        certification halts as `engine.StageBlocked` under
        `blocked_on:prerequisite_not_current_review` — a wait, no rejection,
        no round — and the one-shot proposer halts on its first attempt with
        nothing queued. A SPECIFICATION patch, where `review` IS a route
        member, keeps `revalidation_red_review`."""
        from elt_taskgen import cli

        review_runs: list[Path] = []

        def review(engine, task):
            review_runs.append(Path(engine.workspace).resolve())
            if task.populations[1].conditions[1] == POPULATION_CONDITION_REVISED:
                # The trial copy: a FATAL opinion on the moved adversary view.
                return StageOutcome(
                    VERDICT_FAIL,
                    StagePayload(error="1 finding(s), 1 fatal"),
                    route=RepairRoute.SPECIFICATION,
                )
            if SENTINEL in task.solver_prompt:
                return StageOutcome(VERDICT_FAIL, StagePayload(error="undefined tie-break in prose"))
            return StageOutcome(VERDICT_PASS, StagePayload(detail="review ok"))

        def attack(engine, task):
            cli._require_pass_payload(engine, task, StageName.REVIEW)
            return StageOutcome(VERDICT_PASS, StagePayload(detail="attack ok"))

        runners = {s.value: pass_runner(f"{s.value} ok") for s in StageName}
        runners.update({"review": review, "attack": attack})
        workspace = self.root / "ws"
        engine = Engine(workspace, stage_runners=runners, max_repair_rounds=0)
        self.addCleanup(engine.close)
        engine.register(self.task)
        task = engine.run(self.task_id, until="attack")
        self.assertIs(task.status, TaskStatus.ATTACKED)
        live_before = rows_by_stage(engine, self.task_id)
        before = tree_hash(workspace, exclude_ledger=True)
        self.assertEqual(len(review_runs), 1)

        with self.assertRaises(engine_mod.StageBlocked) as ctx:
            rp.attempt_patch(engine, task, "attack", population_conditions_patch(task))
        exc = ctx.exception
        self.assertNotIsInstance(exc, rp.PatchRejected)
        marker = f"{engine_mod.BLOCKED_STAGE_MARKER_PREFIX}{rp.BLOCKED_ON_PREREQUISITE_PREFIX}review"
        self.assertEqual((exc.stage, exc.marker), ("review", marker))
        self.assertEqual(engine_mod.blocked_on_from_marker(exc.marker), "prerequisite_not_current_review")
        self.assertEqual(rp.halting_marker(exc), marker)
        self.assertEqual(len(review_runs), 2)  # live, then the trial
        self.assertNotEqual(review_runs[1], workspace.resolve())
        self.assertEqual(rows_by_stage(engine, self.task_id), live_before)
        self.assertEqual(tree_hash(workspace, exclude_ledger=True), before)
        self.assertEqual(engine.load_task(self.task_id).populations[1].conditions[1], POPULATION_CONDITION)

        # The one-shot proposer: halted under the marker, one model call,
        # no RejectionCode, nothing queued.
        provider = ScriptedProvider([population_conditions_patch(task), population_conditions_patch(task)])
        outcome = rp.RepairProposer(provider, max_attempts=2).repair(
            engine, task, "attack", RepairRoute.POPULATION, "population coverage"
        )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)
        self.assertEqual(outcome.infrastructure, marker)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(outcome.record.attempts[0].error_type, "StageBlocked")
        self.assertEqual(outcome.record.attempts[0].rejection_code, "")
        self.assertIsNone(rp.load_repair_adjudication(workspace, self.task_id))
        self.assertEqual(len(review_runs), 3)

        # SPECIFICATION control: `review` is a route member, so its red
        # verdict on the trial is the patch's rejection, as before.
        with self.assertRaises(rp.RevalidationFailed) as red:
            rp.attempt_patch(engine, engine.load_task(self.task_id), "review", spec_patch())
        self.assertEqual(red.exception.code, "revalidation_red_review")
        self.assertEqual(rows_by_stage(engine, self.task_id), live_before)

    def test_revalidate_with_real_attack_runner_requires_review_evidence_at_new_hash(self):
        """THE F1 regression test (certify addendum §0 F1, §3.6): with
        `build_stage_runners` wired for real, a REFERENCE patch certified at
        `attack`.

        Before 1.P.0 no such certification could go green: the trial ledger
        is a whole-workspace copy holding `review` only at the OLD hash,
        `_revalidate` recorded nothing at the new one, and `run_attack_stage`
        raised "latest 'review' report is stale ... (fail closed)", which the
        `except Exception` wrap turned into
        `RevalidationFailed("re-validation stage 'attack' raised RuntimeError:
        latest 'review' report is stale ...")` — the assertion this test
        makes on the un-recorded trial. After 1.P.0 the same fixture
        certifies green, the trial ledger carries a `review` PASS at the new
        hash when `attack` reads it, and the live ledger is untouched."""
        from elt_taskgen import cli

        provider = LadderProvider()
        runners = cli.build_stage_runners(provider)
        workspace = self.root / "ws"
        engine = Engine(workspace, stage_runners=runners, max_repair_rounds=0)
        self.addCleanup(engine.close)
        engine.register(demo_task())
        task = engine.run(self.task_id, until="attack")
        self.assertIs(task.status, TaskStatus.ATTACKED)
        self.assertEqual(engine.latest_report(self.task_id, "attack").verdict, VERDICT_PASS)
        patch = reference_comment_patch(task)

        # (a) The defect: the real attack runner on a trial whose ledger holds
        # no review PASS at the patched hash.
        with rp.trial_workspace(workspace) as trial:
            target = trial / "tasks" / self.task_id / "task_ir.json"
            target.write_text(
                rp.apply_patch_text(target.read_text(encoding="utf-8"), patch), encoding="utf-8"
            )
            trial_engine = Engine(trial, max_repair_rounds=0)
            try:
                patched = trial_engine.load_task(self.task_id)
                self.assertNotEqual(patched.content_hash(), task.content_hash())
                self.assertEqual(
                    runners[StageName.REFERENCE](trial_engine, patched).verdict, VERDICT_PASS
                )
                with self.assertRaisesRegex(RuntimeError, "report is stale") as ctx:
                    runners[StageName.ATTACK](trial_engine, patched)
                self.assertIn("'review'", str(ctx.exception))
                self.assertIn("fail closed", str(ctx.exception))
            finally:
                trial_engine.close()

        # (b) After 1.P.0: the same fixture certifies green through
        # `attempt_patch`, with the review PASS row at the new hash on the
        # TRIAL ledger when `attack` reads it.
        seen: list[tuple[Path, str, bool]] = []
        real_attack = runners[StageName.ATTACK]

        def attack_with_currency_spy(engine_, task_):
            row = engine_.latest_report(task_.task_id, StageName.REVIEW.value)
            seen.append((Path(engine_.workspace), row.verdict, row.content_hash == task_.content_hash()))
            return real_attack(engine_, task_)

        engine.set_stage_runner(StageName.ATTACK, attack_with_currency_spy)
        live_before = rows_by_stage(engine, self.task_id)
        calls_before = len(provider.calls)

        committed = rp.attempt_patch(engine, task, StageName.ATTACK.value, patch)

        self.assertNotEqual(committed.content_hash(), task.content_hash())
        self.assertIn(REFERENCE_COMMENT, committed.reference.sql_by_mart[MART])
        self.assertEqual(len(seen), 1)
        trial_ws, verdict, current = seen[0]
        self.assertNotEqual(trial_ws, workspace.resolve())
        self.assertEqual(verdict, VERDICT_PASS)
        self.assertTrue(current)
        self.assertEqual(rows_by_stage(engine, self.task_id), live_before)
        self.assertEqual(
            engine.latest_report(self.task_id, "review").content_hash, task.content_hash()
        )
        # The prepended `review` consulted the four seats once more (a duck
        # double here; memo-served or live through a RoutedProvider, F2/F3b).
        self.assertEqual(
            provider.calls[calls_before:],
            ["ambiguity_critic", "population_adversary", "shortcut_attacker", "feasibility_reviewer"],
        )
        # And the live ladder re-attests at the committed identity as today.
        engine.set_stage_runner(StageName.ATTACK, real_attack)
        resumed = engine.run(self.task_id, until="attack")
        self.assertIs(resumed.status, TaskStatus.ATTACKED)
        self.assertEqual(engine.latest_report(self.task_id, "attack").content_hash, committed.content_hash())

    def test_review_is_memo_served_on_trial_when_critic_views_unchanged(self):
        """Certify addendum §3.6 / F2: on a REFERENCE patch the critic views
        are unchanged (the reference SQL is private to every seat), so the
        `review` the trial certification prepends for `attack` is served from
        the transcript store — zero transport calls, `replayed=True`
        evidence rows bound to the NEW hash — and costs $0.

        The served path rests on `RoutedProvider._lookup_bound`'s identity
        binding (item 1.P.0's blocker, now closed): in LIVE mode an entry of
        the same task under the same key (an identical rendered view and
        policy) whose response digest verifies is served across a
        content-hash move; the exact `(task_id, task_content_hash)` binding
        stays the rule under `--replay-only`
        (tests/test_providers.py `test_task_bound_replay_refuses_a_different_content_hash`).
        The post-commit live ladder then re-serves the same four exchanges at
        the committed hash: the certification's calls are the round's own
        re-attestation moved earlier, never additional spend."""
        from elt_taskgen import cli
        from elt_taskgen.models import CouncilRole
        from elt_taskgen.review import providers as providers_mod

        P = providers_mod
        transport = provider_fixture.FakeTransport(
            [provider_fixture.anthropic_tool_response([MINOR_FINDING]) for _ in range(4)]
        )
        workspace = self.root / "ws"
        provider = P.RoutedProvider(
            provider_fixture.make_routing(),
            P.TranscriptStore(workspace / "transcripts"),
            P.CostMeter(budget_per_task_usd=100.0),
            task_id=self.task_id,
            transports={"anthropic": transport},
        )

        review_runs: list[Path] = []
        real_review = cli.make_review_runner(provider)

        def review(engine, task):
            review_runs.append(Path(engine.workspace))
            return real_review(engine, task)

        def attack(engine, task):
            cli._require_pass_payload(engine, task, StageName.REVIEW)
            return StageOutcome(VERDICT_PASS, StagePayload(detail="attack ok"))

        runners = {s.value: pass_runner(f"{s.value} ok") for s in StageName}
        runners.update({"review": review, "attack": attack})
        engine = Engine(workspace, stage_runners=runners, max_repair_rounds=0)
        self.addCleanup(engine.close)
        engine.register(self.task)
        critics = {r.value for r in CouncilRole if r is not CouncilRole.SEMANTIC_AUTHOR}
        with mock.patch.object(cli, "_admission_gate", return_value=(None, {})):
            task = engine.run(self.task_id, until="attack")
            self.assertIs(task.status, TaskStatus.ATTACKED)
            self.assertEqual(len(transport.calls), 4)
            # Direct role entries are the replay cache. Agentic seats also
            # persist session and trajectory evidence below deeper
            # directories; those records are not additional exchanges.
            recorded = sorted((workspace / "transcripts").glob("*/*.json"))
            self.assertEqual(len(recorded), 4)
            self.assertTrue(all(not row["replayed"] for row in provider.exchange_evidence))
            usd_before = provider.meter.per_task_usd.get(self.task_id, 0.0)

            looked_up: list[tuple[str, str]] = []
            real_lookup = provider.store.lookup

            def lookup(role_name, prompt_sha):
                looked_up.append((role_name, prompt_sha))
                return real_lookup(role_name, prompt_sha)

            with mock.patch.object(provider.store, "lookup", lookup):
                committed = rp.attempt_patch(
                    engine, task, StageName.ATTACK.value, reference_comment_patch(task)
                )

            # The served path: zero new transport calls, every seat replayed
            # under the UNCHANGED critic key, evidence bound to the new hash,
            # nothing charged, nothing re-recorded.
            self.assertIn(REFERENCE_COMMENT, committed.reference.sql_by_mart[MART])
            self.assertNotEqual(committed.content_hash(), task.content_hash())
            self.assertEqual(len(transport.calls), 4)
            self.assertEqual(len(review_runs), 2)
            self.assertEqual(len(provider.exchange_evidence), 4)
            self.assertTrue(all(row["replayed"] for row in provider.exchange_evidence))
            self.assertEqual({row["role"] for row in provider.exchange_evidence}, critics)
            self.assertTrue(
                all(row["task_content_hash"] == committed.content_hash() for row in provider.exchange_evidence)
            )
            self.assertEqual({role for role, _ in looked_up}, critics)
            self.assertEqual({sha for _, sha in looked_up}, {p.stem for p in recorded})
            self.assertEqual(sorted((workspace / "transcripts").glob("*/*.json")), recorded)
            self.assertEqual(provider.meter.per_task_usd.get(self.task_id, 0.0), usd_before)
            # The trial's review PASS at the new hash is what `attack` read.
            self.assertEqual(
                engine.latest_report(self.task_id, "review").content_hash, task.content_hash()
            )

            # The post-commit ladder recycles the same exchanges at $0 (F2).
            resumed = engine.run(self.task_id, until="attack")
            self.assertIs(resumed.status, TaskStatus.ATTACKED)
            self.assertEqual(len(transport.calls), 4)
            self.assertEqual(len(review_runs), 3)
            self.assertEqual(
                engine.latest_report(self.task_id, "review").content_hash, committed.content_hash()
            )
            self.assertTrue(all(row["replayed"] for row in provider.exchange_evidence))
            self.assertEqual(provider.meter.per_task_usd.get(self.task_id, 0.0), usd_before)

    def test_population_conditions_patch_recalls_only_the_adversary_seat(self):
        """Certify addendum §3.6 / F3b: a POPULATION `conditions` patch moves
        exactly one critic view — population conditions are rendered into
        the adversary's view alone — so the `review` the trial certification
        prepends for `attack` memo-serves three seats across the hash move
        (`replayed=True`, $0) and makes exactly ONE live call, the
        adversary's, recorded under its new key and charged to the task
        meter; the patch commits, and the post-commit ladder recycles all
        four at $0."""
        from elt_taskgen import cli
        from elt_taskgen.models import CouncilRole
        from elt_taskgen.review import providers as providers_mod

        P = providers_mod
        transport = provider_fixture.FakeTransport(
            [provider_fixture.anthropic_tool_response([MINOR_FINDING]) for _ in range(5)]
        )
        workspace = self.root / "ws"
        provider = P.RoutedProvider(
            provider_fixture.make_routing(),
            P.TranscriptStore(workspace / "transcripts"),
            P.CostMeter(budget_per_task_usd=100.0),
            task_id=self.task_id,
            transports={"anthropic": transport},
        )
        real_review = cli.make_review_runner(provider)
        review_runs: list[Path] = []

        def review(engine, task):
            review_runs.append(Path(engine.workspace))
            return real_review(engine, task)

        def attack(engine, task):
            cli._require_pass_payload(engine, task, StageName.REVIEW)
            return StageOutcome(VERDICT_PASS, StagePayload(detail="attack ok"))

        runners = {s.value: pass_runner(f"{s.value} ok") for s in StageName}
        runners.update({"review": review, "attack": attack})
        engine = Engine(workspace, stage_runners=runners, max_repair_rounds=0)
        self.addCleanup(engine.close)
        engine.register(self.task)
        adversary = CouncilRole.POPULATION_ADVERSARY.value
        critics = {r.value for r in CouncilRole if r is not CouncilRole.SEMANTIC_AUTHOR}
        with mock.patch.object(cli, "_admission_gate", return_value=(None, {})):
            task = engine.run(self.task_id, until="attack")
            self.assertIs(task.status, TaskStatus.ATTACKED)
            self.assertEqual(len(transport.calls), 4)
            recorded = sorted((workspace / "transcripts").glob("*/*.json"))
            self.assertEqual(len(recorded), 4)
            usd_before = provider.meter.per_task_usd.get(self.task_id, 0.0)

            committed = rp.attempt_patch(
                engine, task, StageName.ATTACK.value, population_conditions_patch(task)
            )

            self.assertEqual(committed.populations[1].conditions[1], POPULATION_CONDITION_REVISED)
            self.assertNotEqual(committed.content_hash(), task.content_hash())
            self.assertEqual(len(transport.calls), 5)  # exactly one live call
            self.assertEqual(len(review_runs), 2)
            rows = {row["role"]: row for row in provider.exchange_evidence}
            self.assertEqual(set(rows), critics)
            self.assertFalse(rows[adversary]["replayed"])
            self.assertTrue(all(rows[role]["replayed"] for role in critics - {adversary}))
            self.assertTrue(all(row["task_content_hash"] == committed.content_hash() for row in rows.values()))
            after = sorted((workspace / "transcripts").glob("*/*.json"))
            self.assertEqual(len(after), 5)
            (new_entry,) = [p for p in after if p not in recorded]
            self.assertEqual(new_entry.parent.name, adversary)
            self.assertGreater(provider.meter.per_task_usd.get(self.task_id, 0.0), usd_before)

            # The post-commit ladder recycles all four at the committed hash.
            usd_after = provider.meter.per_task_usd.get(self.task_id, 0.0)
            resumed = engine.run(self.task_id, until="attack")
            self.assertIs(resumed.status, TaskStatus.ATTACKED)
            self.assertEqual(len(transport.calls), 5)
            self.assertEqual(len(review_runs), 3)
            self.assertTrue(all(row["replayed"] for row in provider.exchange_evidence))
            self.assertEqual(provider.meter.per_task_usd.get(self.task_id, 0.0), usd_after)



# ---------------------------------------------------------------------------
# 1.P — the bounded proposer through the real session runner (Design H)
# ---------------------------------------------------------------------------

from elt_taskgen import cli as cli_mod  # noqa: E402
from elt_taskgen.review import providers as P  # noqa: E402
from elt_taskgen.review import session as S  # noqa: E402
from elt_taskgen.review.tools import certify as C  # noqa: E402
from elt_taskgen.review.tools import projection as PJ  # noqa: E402
from elt_taskgen.review.tools import validators as V  # noqa: E402

try:
    from tests import test_bounded_session as BS
    from tests.test_providers import VALID_FINDING, FakeTransport, anthropic_tool_response, make_routing
    from tests.test_providers_session import tool_use_body
    from tests.test_redteam_round4 import REQUALIFIED_REFERENCE
except ImportError:  # pragma: no cover - discovered from inside tests/
    import test_bounded_session as BS  # type: ignore[no-redef]
    from test_providers import VALID_FINDING, FakeTransport, anthropic_tool_response, make_routing
    from test_providers_session import tool_use_body
    from test_redteam_round4 import REQUALIFIED_REFERENCE

SPEC = RepairRoute.SPECIFICATION
REF = RepairRoute.REFERENCE


def proposer_routing() -> P.RoleRouting:
    """The test routing plus a `repair_proposer` route (Opus, 8 192 tokens,
    `session.max_usd` 0.80 declared but disabled, exactly as agents.yaml)."""
    base = make_routing()
    roles = dict(base.roles)
    roles[rp.ROLE_NAME] = P.RoleRoute(
        rp.ROLE_NAME, "anthropic", "claude-opus-5", 8192, "high", max_usd=0.80
    )
    return P.RoleRouting(roles=roles, provider_config=base.provider_config, source=base.source)


_IDS = iter(range(1, 10_000))


def body(name: str, args: dict | None = None) -> dict:
    """One scripted proposer turn on the Anthropic wire."""
    return tool_use_body(name, dict(args or {}), id=f"toolu_{next(_IDS):04d}")


def spec_edit(text: str = SENTINEL) -> dict:
    return {
        "artifact": "task_ir.json", "op": "insert", "locator": "solver_prompt",
        "old": "", "new": " " + text, "rationale": "the review flagged an undefined tie-break",
    }


def reference_edit(task, comment: str = REFERENCE_COMMENT) -> dict:
    head = task.reference.sql_by_mart[MART].split("\n", 1)[0]
    return {
        "artifact": "task_ir.json", "op": "replace",
        "locator": f"reference.sql_by_mart.{MART}", "old": head, "new": f"{comment}\n{head}",
        "rationale": "document the reference's join rule",
    }


SUBMIT = [body(V.SUBMIT_TOOL)]


def payload_texts(transport: FakeTransport) -> list[str]:
    """Every message text a transport payload carried (what the model saw)."""
    out: list[str] = []
    for _url, _headers, payload in transport.calls:
        for message in payload.get("messages", ()):
            content = message.get("content")
            if isinstance(content, str):
                out.append(content)
                continue
            for block in content or ():
                if isinstance(block, dict):
                    for key in ("text", "content"):
                        if isinstance(block.get(key), str):
                            out.append(block[key])
    return out


def initial_views(transport: FakeTransport) -> list[str]:
    """The turn-0 user message of every session the transport served."""
    views: list[str] = []
    for _url, _headers, payload in transport.calls:
        first = payload["messages"][0]["content"]
        if isinstance(first, str) and first not in views:
            views.append(first)
    return views


class _BoundedFixture(_CertifierFixture):
    """The prose fixture, a live workspace and a `RoutedProvider` over a
    `FakeTransport` FIFO of the proposer's OWN turns: the real session runner,
    the real projections, the real proposer tools and the real `attempt_patch`
    at submit — no model anywhere."""

    def setUp(self):
        super().setUp()
        P.clear_behavior_caches()
        self.addCleanup(P.clear_behavior_caches)

    def provider(self, workspace: Path, bodies, *, budget: float = 100.0, replay_only: bool = False):
        transport = FakeTransport(list(bodies))
        provider = P.RoutedProvider(
            proposer_routing(),
            P.TranscriptStore(workspace / "transcripts"),
            P.CostMeter(budget_per_task_usd=budget),
            task_id=self.task_id,
            replay_only=replay_only,
            transports={"anthropic": transport},
        )
        return provider, transport

    def failure(self, route=SPEC, error: str = "undefined tie-break in prose") -> str:
        return rp.failure_detail(StagePayload(error=error), route=route, task=self.task, stage="review")

    def agents_config(self, **session_keys) -> Path:
        """An agents document whose `repair_proposer.session` block overrides
        the shipped limits (a test-only override, the way a pilot arm declares
        one): the shipped `max_turns: 5` binds a five-turn script."""
        block = {
            "enabled": False, "max_turns": 5, "max_tool_calls": 8, "max_certify": 2,
            "max_oracle_bits": 4, "max_usd": 0.80, "wall_clock_s": 900,
            "certify": {"attack_enabled": False, "deadline_s": 300},
        }
        block.update(session_keys)
        path = self.root / "agents-override.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "roles": {
                        rp.ROLE_NAME: {
                            "provider": "anthropic", "model": "claude-opus-5",
                            "max_tokens": 8192, "effort": "high", "session": block,
                        }
                    },
                    "repair": {
                        "max_attempts": 2,
                        "routes_bounded": ["specification", "reference"],
                        "nested_ceiling_usd": {"specification": 0.56, "population": 0.12, "reference": 0.05},
                        "max_usd_per_failure": 2.00,
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    def bounded(self, engine, bodies, *, route=SPEC, stage: str = "review", failure=None,
                budget: float = 100.0, replay_only: bool = False, **proposer_kw):
        provider, transport = self.provider(engine.workspace, bodies, budget=budget, replay_only=replay_only)
        proposer = rp.AgenticRepairProposer(provider, **proposer_kw)
        task = engine.load_task(self.task_id)
        outcome = proposer.repair(engine, task, stage, route, failure or self.failure(route))
        return outcome, provider, transport

    def task_tree(self, engine) -> tuple[dict, list]:
        """The task artifacts (`repair.snapshot`, what `_commit` could move)
        plus the ledger rows: what "the workspace is byte-identical" claims
        for a live workspace that also records transcripts and session
        records (both outside the snapshot and the ledger)."""
        return repair.snapshot(engine.workspace, self.task_id), ledger_rows(engine, self.task_id)

    def session_records(self, engine) -> list[dict]:
        root = rp.session_record_dir(engine.workspace, self.task_id)
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(root.glob("*.json"))]


class BoundedProposerTest(_BoundedFixture):
    def test_no_session_tool_makes_a_nested_model_call(self):
        """Certify addendum §5: the FIFO holds the proposer's own turns only;
        after read_view, read_field, apply_edit_trial, check_scope, check_cheap
        and two REAL certifies the transport popped exactly those turns, no
        `complete` was reached, `exchange_evidence` holds ONE session row bound
        to the live hash, and the accumulated patch commits through the
        unchanged `attempt_patch`."""
        engine = Engine(
            self.workspace("ref"),
            stage_runners={"generate": pass_runner("generate ok"), "reference": pass_runner("reference ok")},
        )
        self.addCleanup(engine.close)
        engine.register(demo_task())
        task = engine.load_task(self.task_id)
        self.assertEqual(cli_mod.run_generate(engine, task).verdict, VERDICT_PASS)  # populations for `reference`
        live_hash = task.content_hash()
        second = {
            "artifact": "task_ir.json", "op": "insert", "locator": f"reference.sql_by_mart.{MART}",
            "old": "", "new": "\n-- appended note", "rationale": "note the dedupe rule",
        }
        bodies = [
            body("read_view"), body("read_field", {"field": "tables"}),
            body("apply_edit_trial", reference_edit(task)), body("check_scope"), body("check_cheap"),
            body("certify"), body("apply_edit_trial", second), body("certify"), *SUBMIT,
        ]
        # The certify worker is a spawned child process: the parent-side
        # spy is the SPAWN site (`certify_disposable_copy`), one per executed certify.
        with mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as runs, \
                mock.patch.object(P.RoutedProvider, "complete", side_effect=AssertionError("nested model call")) as complete:
            outcome, provider, transport = self.bounded(
                engine, bodies, route=REF, stage="reference", failure=self.failure(REF, "reference red"),
                config_path=self.agents_config(max_turns=12, max_tool_calls=16),
            )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        self.assertEqual(transport.responses, [])
        self.assertEqual(len(transport.calls), 9)
        self.assertEqual(complete.call_count, 0)
        self.assertEqual(runs.call_count, 2)
        self.assertEqual(len(provider.exchange_evidence), 1)
        row = provider.exchange_evidence[0]
        self.assertEqual((row["role"], row["task_content_hash"], row["entry_schema"]), (rp.ROLE_NAME, live_hash, 3))
        self.assertEqual((row["model_call_count"], row["tool_call_count"], row["terminal"]), (9, 8, "SUBMITTED"))
        attempt = outcome.record.attempts[0]
        self.assertEqual((attempt.terminal, attempt.accepted, attempt.artifact), ("SUBMITTED", True, "task_ir.json"))
        self.assertEqual([s.tool for s in attempt.steps], [
            "read_view", "read_field", "apply_edit_trial", "check_scope", "check_cheap",
            "certify", "apply_edit_trial", "certify",
        ])
        self.assertEqual([s.code for s in attempt.steps if s.tool == "certify"], [C.CODE_GREEN, C.CODE_GREEN])
        committed = engine.load_task(self.task_id)
        self.assertNotEqual(committed.content_hash(), live_hash)
        self.assertIn(REFERENCE_COMMENT, committed.reference.sql_by_mart[MART])
        self.assertIn("-- appended note", committed.reference.sql_by_mart[MART])
        # Every tool_result the model saw was a rendered, digit-free projection.
        results = [
            block["content"]
            for message in transport.calls[-1][2]["messages"]  # the last payload carries the whole prefix
            if message["role"] == "user" and isinstance(message["content"], list)
            for block in message["content"] if block.get("type") == "tool_result"
        ]
        self.assertEqual(len(results), 8)
        for text in results:
            self.assertRegex(text, r"^\[(trial|field|cheap|certify|rejection)\] ")
            self.assertIsNone(re.search(r"\d", text), text)
        (record,) = self.session_records(engine)
        self.assertEqual(record["terminal"], "SUBMITTED")
        self.assertEqual(record["submit"]["outcome"], "committed")
        self.assertEqual([c["observation"]["code"] for c in record["certify"]], [C.CODE_GREEN, C.CODE_GREEN])

    def test_nested_budget_breach_inside_trial_phase_is_infrastructure_not_rejection(self):
        """Certify addendum §5: a task-scope budget breach raised by the trial's
        review runner at submit is re-raised out of the certifier (never a
        rejection): the session's outcome is HALTED with
        `infrastructure == "BudgetExceededError"`, no second session runs, no
        adjudication is queued, and the engine raises `InfrastructureFailure`
        with zero rounds and no rejection, the task tree byte-identical."""
        breach = P.BudgetExceededError("per-task budget cannot absorb the next call", scope="task")
        review = self.review_raising_on_the_trial(breach)
        engine = self.make_engine(self.workspace("breach"), review=review)
        before = self.task_tree(engine)
        bodies = [body("apply_edit_trial", spec_edit()), *SUBMIT, body("apply_edit_trial", spec_edit()), *SUBMIT]
        outcome, provider, transport = self.bounded(engine, bodies)
        self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)
        self.assertEqual(outcome.infrastructure, "BudgetExceededError")
        self.assertEqual(len(transport.calls), 2)  # one session, no second
        self.assertEqual(len(outcome.record.attempts), 1)
        self.assertEqual(outcome.record.attempts[0].error_type, "BudgetExceededError")
        self.assertEqual(outcome.record.attempts[0].rejection_code, "")
        self.assertIsNone(rp.load_repair_adjudication(engine.workspace, self.task_id))
        self.assertEqual(self.task_tree(engine), before)
        self.assertNotIn(SENTINEL, engine.load_task(self.task_id).solver_prompt)

        provider, transport = self.provider(self.workspace("run"), bodies)
        run_engine = self.make_engine(
            self.workspace("run"), review=review,
            proposer=rp.AgenticRepairProposer(provider), max_repair_rounds=2,
        )
        for upstream in ("contamination_pre", "generate", "reference"):
            run_engine.set_stage_runner(upstream, self.author_ok)
        with self.assertRaises(InfrastructureFailure) as halted:
            run_engine.run(self.task_id, until="review")
        self.assertEqual(halted.exception.marker, "budgetexceedederror")
        self.assertEqual(halted.exception.budget_scope, "task")
        halted_payload = json.loads(
            run_engine.latest_report(self.task_id, "review").payload_json
        )
        self.assertEqual(halted_payload["budget_scope"], "task")
        self.assertEqual(run_engine.repair_rounds_used(self.task_id), 0)
        self.assertIsNot(run_engine.load_task(self.task_id).status, TaskStatus.REJECTED)
        self.assertIsNone(rp.load_repair_adjudication(run_engine.workspace, self.task_id))

    def test_a_human_hold_inside_the_certifier_is_an_adjudication_not_a_halt(self):
        """batch10 run F 2026-09-11 (dlt__personio, lefty02w): the certifier
        re-ran review on the PATCHED trial, the critic held it for human
        adjudication on a new finding, and the task was reported as an
        infrastructure failure — "the harness failed" — although nothing
        had. A human hold inside the certification is the same
        needs-adjudication outcome an uncertifiable patch gets, with the
        hold's reason on the record; an environment wait still halts."""
        review = self.review_blocked_on_the_trial("human")
        engine = self.make_engine(self.workspace("human-hold"), review=review)
        before = self.task_tree(engine)
        bodies = [body("apply_edit_trial", spec_edit()), *SUBMIT]
        outcome, provider, transport = self.bounded(engine, bodies)
        self.assertEqual(outcome.disposition, rp.DISPOSITION_NEEDS_ADJUDICATION)
        self.assertEqual(outcome.infrastructure, "")
        self.assertIn("waiting on human adjudication", outcome.record.detail)
        self.assertIsNotNone(rp.load_repair_adjudication(engine.workspace, self.task_id))
        self.assertEqual(self.task_tree(engine), before)
        self.assertNotIn(SENTINEL, engine.load_task(self.task_id).solver_prompt)
        # The wait's payload (a path) never reached a turn or the record.
        self.assertNotIn("/Users", outcome.record.detail)
        self.assertNotIn("/Users", "\n".join(payload_texts(transport)))

    def test_blocked_stage_inside_certifier_halts_bounded_session_without_a_round(self):
        """The bounded mode of the same rule: a member that WAITS
        (`VERDICT_BLOCKED`) at the submit-time certification is a no-measure
        outcome — the session's outcome is HALTED with
        `infrastructure == "blocked_on:environment"`, NO rejection code is
        carried into a next session (none runs), nothing is queued, the task
        tree is byte-identical; the engine halts with the reason on the FAIL
        row, zero rounds, no rejection (C7)."""
        review = self.review_blocked_on_the_trial(engine_mod.BLOCKED_ON_ENVIRONMENT)
        marker = f"{engine_mod.BLOCKED_STAGE_MARKER_PREFIX}{engine_mod.BLOCKED_ON_ENVIRONMENT}"
        engine = self.make_engine(self.workspace("blocked"), review=review)
        before = self.task_tree(engine)
        bodies = [body("apply_edit_trial", spec_edit()), *SUBMIT, body("apply_edit_trial", spec_edit()), *SUBMIT]
        outcome, provider, transport = self.bounded(engine, bodies)
        self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)
        self.assertEqual(outcome.infrastructure, marker)
        self.assertEqual(len(transport.calls), 2)  # one session, no second
        self.assertEqual(len(outcome.record.attempts), 1)
        attempt = outcome.record.attempts[0]
        self.assertEqual((attempt.error_type, attempt.rejection_code, attempt.accepted), ("StageBlocked", "", False))
        self.assertEqual(attempt.terminal, "SUBMITTED")
        self.assertIsNone(rp.load_repair_adjudication(engine.workspace, self.task_id))
        self.assertEqual(self.task_tree(engine), before)
        self.assertNotIn(SENTINEL, engine.load_task(self.task_id).solver_prompt)
        (record,) = self.session_records(engine)
        self.assertEqual((record["submit"]["outcome"], record["submit"]["halt_marker"]), ("halted", marker))
        self.assertEqual(record["submit"]["rejection_code"], "")
        # The wait's payload (a path) never reached a turn or the record.
        self.assertNotIn("/Users", canonical_json(record))
        self.assertNotIn("/Users", "\n".join(payload_texts(transport)))

        provider, transport = self.provider(self.workspace("run"), bodies)
        run_engine = self.make_engine(
            self.workspace("run"), review=review,
            proposer=rp.AgenticRepairProposer(provider), max_repair_rounds=2,
        )
        for upstream in ("contamination_pre", "generate", "reference"):
            run_engine.set_stage_runner(upstream, self.author_ok)
        with self.assertRaises(InfrastructureFailure) as halted:
            run_engine.run(self.task_id, until="review")
        self.assertEqual(halted.exception.marker, marker)
        self.assertEqual(run_engine.repair_rounds_used(self.task_id), 0)
        self.assertIsNot(run_engine.load_task(self.task_id).status, TaskStatus.REJECTED)
        self.assertIsNone(rp.load_repair_adjudication(run_engine.workspace, self.task_id))
        latest = run_engine.latest_report(self.task_id, "review")
        payload = json.loads(latest.payload_json)
        self.assertEqual((latest.verdict, payload["infrastructure"]), (VERDICT_FAIL, marker))
        self.assertEqual(payload["data"]["blocked_on"], engine_mod.BLOCKED_ON_ENVIRONMENT)
        self.assertEqual(payload["data"]["status"], "halted")

    def test_resource_budget_exhaustion_in_session_certify_is_scored_not_a_halt(self):
        """Phase 3 re-check verdict (state machine §2 "paid outcomes versus
        harness faults"): inside a bounded session, an in-session `certify`
        whose stage the model's own edit feeds runs out of the worker's
        DuckDB memory budget answers the PAID `certify_refused_resource_budget`
        — an ordinary tool turn the model reads (2 oracle bits, one of the
        two executed certifies), recorded with the session and replayable —
        and the session goes on to submit through the unchanged
        `attempt_patch`; nothing halts, no `InfrastructureFailure`, no
        harness marker, the exception's text never reaches a message."""
        import duckdb

        def oom_reference(engine, task):
            raise duckdb.OutOfMemoryException(
                "Out of Memory Error: failed to allocate 512MB at /Users/x/answer_key (4711 rows)"
            )

        engine = Engine(self.workspace("oom"), stage_runners={"reference": pass_runner("reference ok")})
        self.addCleanup(engine.close)
        engine.register(demo_task())
        task = engine.load_task(self.task_id)
        self.assertEqual(cli_mod.run_generate(engine, task).verdict, VERDICT_PASS)
        bodies = [body("apply_edit_trial", reference_edit(task)), body("certify"), *SUBMIT]
        with mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as runs:
            outcome, provider, transport = self.bounded(
                engine, bodies, route=REF, stage="reference", failure=self.failure(REF, "reference red"),
                certify_runners={"generate": pass_runner("generate ok"), "reference": oom_reference},
            )
        self.assertEqual(runs.call_count, 1)
        self.assertEqual(runs.call_args.kwargs["touched_stages"], ("reference", "attack"))
        # Scored, not halted: the session submitted and the patch committed.
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        self.assertEqual(outcome.infrastructure, "")
        self.assertEqual(len(transport.calls), 3)
        (attempt,) = outcome.record.attempts
        self.assertEqual((attempt.terminal, attempt.accepted, attempt.error_type), ("SUBMITTED", True, ""))
        certify_steps = [s for s in attempt.steps if s.tool == "certify"]
        self.assertEqual([(s.kind, s.code, s.refused) for s in certify_steps], [("tool", C.CODE_REFUSED_RESOURCE_BUDGET, False)])
        # The model saw the code and nothing of the worker's text.
        texts = payload_texts(transport)
        self.assertTrue(any(C.CODE_REFUSED_RESOURCE_BUDGET in t for t in texts))
        for secret in ("Out of Memory", "512MB", "4711", "/Users", "answer_key", "OutOfMemoryException"):
            for text in texts:
                self.assertNotIn(secret, text)
        # Recorded with the session as an executed (paid) certify.
        (record,) = self.session_records(engine)
        (entry,) = record["certify"]
        self.assertEqual(entry["observation"]["code"], C.CODE_REFUSED_RESOURCE_BUDGET)
        self.assertEqual((entry["served"], entry["verified"]), (False, False))
        self.assertEqual(entry["observation_sha256"], PJ.Diagnostic.model_validate(entry["observation"]).sha256)
        self.assertNotIn("/Users", canonical_json(record))
        self.assertNotIn("4711", canonical_json(record))
        self.assertEqual(len(provider.exchange_evidence), 1)

    def test_certify_result_is_recorded_and_replay_serves_it_without_reexecution(self):
        """Certify addendum §3.1 "Record / replay": the executed `certify`'s
        Diagnostic and digest are recorded with the session; a replay-only run
        from the same pre-repair state pops zero transport calls and spawns
        zero certify workers (the result is served for the same trial bytes);
        a `verify_tools` replay re-executes and matches the recorded digest,
        and a tampered record is a harness fault, never a red verdict. The
        spy sits at the SPAWN site (`certify_disposable_copy`): the worker
        itself is a child process."""
        pristine = self.workspace("pristine")
        engine = Engine(pristine, stage_runners={"reference": pass_runner("reference ok")})
        engine.register(demo_task())
        task = engine.load_task(self.task_id)
        self.assertEqual(cli_mod.run_generate(engine, task).verdict, VERDICT_PASS)
        engine.close()

        def clone(name: str) -> Engine:
            target = self.workspace(name)
            shutil.copytree(pristine, target)
            clone_engine = Engine(target, stage_runners={"reference": pass_runner("reference ok")})
            self.addCleanup(clone_engine.close)
            return clone_engine

        bodies = [body("apply_edit_trial", reference_edit(task)), body("certify"), *SUBMIT]
        live = clone("live")
        with mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as runs:
            outcome, _provider, transport = self.bounded(
                live, bodies, route=REF, stage="reference", failure=self.failure(REF, "reference red"),
            )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        self.assertEqual(runs.call_count, 1)
        self.assertEqual(len(transport.calls), 3)
        (record,) = self.session_records(live)
        (entry,) = record["certify"]
        self.assertEqual(entry["observation"]["code"], C.CODE_GREEN)
        self.assertEqual((entry["served"], entry["verified"]), (False, False))
        self.assertEqual(entry["observation_sha256"], PJ.Diagnostic.model_validate(entry["observation"]).sha256)
        self.assertEqual(
            [s for s in outcome.record.attempts[0].steps if s.tool == "certify"][0].observation_sha256,
            entry["observation_sha256"],
        )

        def transplant(name: str) -> Engine:
            target = clone(name)
            shutil.copytree(live.workspace / "transcripts", target.workspace / "transcripts", dirs_exist_ok=True)
            shutil.copytree(
                rp.session_record_dir(live.workspace, self.task_id),
                rp.session_record_dir(target.workspace, self.task_id), dirs_exist_ok=True,
            )
            return target

        # Replay: zero HTTP, zero workers spawned, the served result recorded as such.
        replay = transplant("replay")
        with mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as runs:
            outcome, provider, transport = self.bounded(
                replay, [], route=REF, stage="reference", failure=self.failure(REF, "reference red"),
                replay_only=True,
            )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        self.assertEqual(runs.call_count, 0)
        self.assertEqual(transport.calls, [])
        self.assertEqual(provider.meter.total_usd, 0.0)
        self.assertTrue(provider.exchange_evidence[0]["replayed"])
        # The replay lands on the SAME session_sha256 path as the executed
        # record it was transplanted from: the executed certify evidence
        # (`served: false`) stands and is never overwritten by a merely
        # served copy, and the served copy's digest was verified against it.
        replayed = [r for r in self.session_records(replay) if r["certify"]]
        self.assertEqual(len(replayed), 1)
        self.assertEqual((replayed[0]["certify"][0]["served"], replayed[0]["certify"][0]["verified"]), (False, False))
        self.assertEqual(replayed[0]["certify"][0]["observation_sha256"], entry["observation_sha256"])
        self.assertEqual(replayed[0]["session"]["session_sha256"], record["session"]["session_sha256"])
        # A replay whose record path is NEW (another salt) writes its served entry.
        fresh_replay = transplant("replay-fresh")
        for path in rp.session_record_dir(fresh_replay.workspace, self.task_id).glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            path.rename(path.with_name(path.name.replace(path.name.split(".")[-2], "f" * 64)))
            self.assertEqual(data["certify"][0]["served"], False)
        outcome, _provider, _transport = self.bounded(
            fresh_replay, [], route=REF, stage="reference", failure=self.failure(REF, "reference red"),
            replay_only=True,
        )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        served_records = [r for r in self.session_records(fresh_replay) if r["certify"] and r["certify"][0]["served"]]
        self.assertEqual(len(served_records), 1)
        self.assertEqual(served_records[0]["certify"][0]["observation_sha256"], entry["observation_sha256"])

        # A served record whose observation was FLIPPED (a green body under
        # the digest of another observation) is refused as a harness fault
        # on the served path too — never served as the runner's verdict and
        # never re-hashed into consistency.
        flipped = transplant("flipped")
        for path in rp.session_record_dir(flipped.workspace, self.task_id).glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            data["certify"][0]["observation"]["code"] = "certify_red_reference"
            data["certify"][0]["observation"]["ok"] = False
            path.write_text(canonical_json(data), encoding="utf-8")
        with mock.patch.object(C, "certify_disposable_copy", side_effect=AssertionError("a worker was spawned")):
            outcome, _provider, _transport = self.bounded(
                flipped, [], route=REF, stage="reference", failure=self.failure(REF, "reference red"),
                replay_only=True,
            )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)
        self.assertEqual(outcome.infrastructure, "ToolHarnessFault")
        self.assertEqual(outcome.record.attempts[0].error_type, "ToolHarnessFault")
        self.assertNotIn(REFERENCE_COMMENT, flipped.load_task(self.task_id).reference.sql_by_mart[MART])

        # --verify-tools: re-executed once, digest matches.
        verify = transplant("verify")
        with mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as runs:
            outcome, _provider, transport = self.bounded(
                verify, [], route=REF, stage="reference", failure=self.failure(REF, "reference red"),
                replay_only=True, verify_tools=True,
            )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        self.assertEqual(runs.call_count, 1)
        self.assertEqual(transport.calls, [])
        verified = [r for r in self.session_records(verify) if r["certify"] and r["certify"][0]["verified"]]
        self.assertEqual(len(verified), 1)

        # A tampered record under verification is a harness fault (halt), never red.
        tampered = transplant("tampered")
        for path in rp.session_record_dir(tampered.workspace, self.task_id).glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            data["certify"][0]["observation_sha256"] = "0" * 64
            path.write_text(canonical_json(data), encoding="utf-8")
        outcome, _provider, _transport = self.bounded(
            tampered, [], route=REF, stage="reference", failure=self.failure(REF, "reference red"),
            replay_only=True, verify_tools=True,
        )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)
        self.assertEqual(outcome.infrastructure, "ToolHarnessFault")
        self.assertNotIn(REFERENCE_COMMENT, tampered.load_task(self.task_id).reference.sql_by_mart[MART])

    def test_submit_patch_calls_attempt_patch_exactly_once_with_the_accumulated_patch(self):
        """Certify addendum §3.2: `submit_patch` ends the session and the
        harness hands the accumulated single-artifact patch (every
        `apply_edit_trial` in order) to the UNCHANGED `attempt_patch` exactly
        once, on the live engine's workspace, under the supervisor deadline —
        an expired deadline halts the certification before it can commit."""
        engine = self.make_engine(self.workspace("once"))
        first = spec_edit("First clarification.")
        bodies = [body("apply_edit_trial", first), body("apply_edit_trial", spec_edit()), *SUBMIT]
        with mock.patch.object(rp, "attempt_patch", wraps=rp.attempt_patch) as spy:
            outcome, _provider, _transport = self.bounded(engine, bodies)
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        self.assertEqual(spy.call_count, 1)
        supervised, task_arg, stage_arg, patch = spy.call_args.args
        self.assertEqual(supervised.workspace, engine.workspace)
        self.assertEqual((task_arg.task_id, stage_arg), (self.task_id, "review"))
        self.assertEqual((patch.route, patch.artifact, patch.proposer_role), (SPEC, "task_ir.json", rp.ROLE_NAME))
        self.assertEqual([(e.op.value, e.locator, e.new) for e in patch.edits], [
            ("insert", "solver_prompt", first["new"]), ("insert", "solver_prompt", spec_edit()["new"]),
        ])
        prose = engine.load_task(self.task_id).solver_prompt
        self.assertIn("First clarification.", prose)
        self.assertIn(SENTINEL, prose)

        # The supervised engine keeps every runner tag through its guard (the
        # empirical `calibrate` tag `trial_phase` substitutes on).
        tagged = cli_mod.make_calibrate_runner(ScriptedProvider([]), empirical=True)
        engine.set_stage_runner("calibrate", tagged)
        supervised = rp._SupervisedEngine(engine, deadline_s=rp.SUBMIT_DEADLINE_S, clock=time.monotonic)
        self.assertTrue(rp._is_empirical_calibrate_runner(supervised.stage_runners["calibrate"]))
        self.assertIs(supervised.stage_runners["calibrate"].__wrapped__, tagged)
        self.assertEqual(supervised.workspace, engine.workspace)
        self.assertEqual(supervised.load_task(self.task_id).task_id, self.task_id)

        # The supervisor deadline: a certifier whose review outlives 30 min is
        # a ToolDeadlineExceeded halt (no round), and nothing commits.
        clock = BS_FakeClock()

        def slow_review(engine_, task_):
            if SENTINEL in task_.solver_prompt:
                clock.advance(rp.SUBMIT_DEADLINE_S + 1)
                return StageOutcome(VERDICT_PASS, StagePayload(detail="review ok, late"))
            return StageOutcome(VERDICT_FAIL, StagePayload(error="undefined tie-break in prose"))

        slow = self.make_engine(self.workspace("slow"), review=slow_review)
        before = self.task_tree(slow)
        outcome, _provider, _transport = self.bounded(
            slow, [body("apply_edit_trial", spec_edit()), *SUBMIT], clock=clock,
        )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)
        self.assertEqual(outcome.infrastructure, "ToolDeadlineExceeded")
        self.assertEqual(self.task_tree(slow), before)

    def test_accumulated_patch_replays_to_held_trial_bytes(self):
        """Certify addendum §5: applying the accumulated patch to the LIVE
        artifact text yields exactly the held trial's artifact bytes at submit
        (the trial holds only validated writes; the patch is their replay)."""
        engine = self.make_engine(self.workspace("replay"))
        observed: list[tuple] = []
        real = V.ProposerSession.accumulated_patch

        def spy(session, *args, **kwargs):
            patch = real(session, *args, **kwargs)
            observed.append((patch, session.before_ir, session.trial_text()))
            return patch

        bodies = [
            body("apply_edit_trial", spec_edit("First clarification.")),
            body("check_scope"),
            body("apply_edit_trial", spec_edit()),
            *SUBMIT,
        ]
        with mock.patch.object(V.ProposerSession, "accumulated_patch", spy):
            outcome, _provider, _transport = self.bounded(engine, bodies)
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        patch, before_ir, trial_text = observed[-1]
        self.assertEqual(len(patch.edits), 2)
        self.assertEqual(rp.apply_patch_text(before_ir, patch), trial_text)
        self.assertEqual(
            rp.apply_patch_text(before_ir, patch),
            (engine.task_dir(self.task_id) / "task_ir.json").read_text(encoding="utf-8"),
        )

    def test_revalidation_failed_payload_dump_never_appears_in_any_turn(self):
        """Certify addendum §3.2 / §5: the certifier's `RevalidationFailed`
        (whose message embeds a payload dump) is stored in `ProposerAttempt`
        for humans and never enters any turn of any session: the second
        session's view carries the projected code only."""
        engine = self.make_engine(self.workspace("dump"))
        bodies = [
            body("apply_edit_trial", spec_edit("Cosmetic wording only.")), *SUBMIT,
            body("apply_edit_trial", spec_edit()), *SUBMIT,
        ]
        outcome, _provider, transport = self.bounded(engine, bodies)
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        first, second = outcome.record.attempts
        self.assertEqual((first.accepted, first.error_type, first.rejection_code), (False, "RevalidationFailed", "revalidation_red_review"))
        self.assertIn("undefined tie-break in prose", first.reason)  # humans keep the text
        self.assertTrue(second.accepted)
        seen = "\n".join(payload_texts(transport))
        for forbidden in ("undefined tie-break", "RevalidationFailed", "verdict 'fail'", "re-validation stage"):
            self.assertNotIn(forbidden, seen)
        self.assertIn("revalidation_red_review", seen)

    def test_second_session_view_carries_rejection_code_not_retry_sentence(self):
        """Certify addendum §2 item 2: the second session's initial view
        carries the first session's rejection as a `RejectionCode` projection
        (class and stage) in place of the one-shot `RETRY n` sentence, and
        never the certifier's message text."""
        engine = self.make_engine(self.workspace("second"))
        bodies = [
            body("apply_edit_trial", spec_edit("Cosmetic wording only.")), *SUBMIT,
            body("apply_edit_trial", spec_edit()), *SUBMIT,
        ]
        outcome, _provider, transport = self.bounded(engine, bodies)
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        views = initial_views(transport)
        self.assertEqual(len(views), 2)
        self.assertNotIn("PREVIOUS SESSION", views[0])
        self.assertIn(f"PREVIOUS SESSION 1 OF {rp.proposer_attempt_budget()}", views[1])
        self.assertIn("[rejection] revalidation_red_review ok=false", views[1])
        for view in views:
            self.assertNotIn("RETRY", view)
            self.assertNotIn("undefined tie-break", view)
            self.assertNotIn("verdict", view)
        # The one-shot path keeps its sentence; the builder is the only difference.
        one_shot = rp.propose_patch(self.task, SPEC, self.failure(), ScriptedProvider([spec_patch()]), attempt=1)
        self.assertIsInstance(one_shot, RepairPatch)
        rejection = rp.project_rejection(rp.RevalidationFailed("x", code="revalidation_red_review"))
        built = rp.session_view(self.task, SPEC, self.failure(), previous=rejection, index=2, max_sessions=rp.proposer_attempt_budget())
        self.assertEqual(built, views[1])
        self.assertEqual(rp.session_view(self.task, SPEC, self.failure()), views[0])
        self.assertIn("SESSION RE-RUN 1", rp.session_view(self.task, SPEC, self.failure(), salt=1))

    def test_population_route_is_enabled_by_routes_bounded(self):
        """Roadmap Phase 3 Table 7 `repair.routes_bounded` (replaces the Phase 1
        pin `test_population_route_is_refused_in_phase_1`, review finding
        1-2): the SHIPPED key names SPECIFICATION, REFERENCE and POPULATION,
        so under `--repair-proposer-mode bounded` a POPULATION failure runs a
        session on a held trial and its `conditions` edit commits through the
        UNCHANGED `attempt_patch` exactly once. The refusal the Phase 1 pin
        proved — no session, no transcript, no adjudication, the workspace
        unchanged, the engine's ordinary bounded round — belongs to a route
        the key OMITS: the documented rollback (a document without
        `population`, the Phase 1 pair) still refuses a POPULATION failure
        before any model call. RUNTIME and FATAL stay `not_applicable`."""
        self.assertEqual(
            rp.repair_settings().routes_bounded, ("specification", "reference", "population")
        )
        self.assertEqual(rp.DEFAULT_ROUTES_BOUNDED, ("specification", "reference", "population"))
        self.assertTrue(rp.repair_settings().route_is_bounded(RepairRoute.POPULATION))
        self.assertTrue(rp.RepairSettings().route_is_bounded(RepairRoute.POPULATION))

        # (a) Enabled by the shipped key: a session runs and commits.
        demanded = "Cancelled orders are present, and some are refunded."

        def generate_demanding(engine, task):
            if any(demanded in c for c in task.populations[1].conditions):
                return StageOutcome(VERDICT_PASS, StagePayload(detail="coverage ok"))
            return StageOutcome(VERDICT_FAIL, StagePayload(error="population coverage: planted"))

        engine = Engine(
            self.workspace("enabled"),
            stage_runners={"generate": generate_demanding, "reference": pass_runner("reference ok")},
            max_repair_rounds=0,
        )
        self.addCleanup(engine.close)
        engine.register(self.task)
        live_hash = engine.load_task(self.task_id).content_hash()
        edit = {
            "artifact": "task_ir.json", "op": "replace", "locator": "populations.1.conditions.1",
            "old": POPULATION_CONDITION, "new": demanded, "rationale": "state the cancelled-order condition",
        }
        bodies = [body("read_view"), body("apply_edit_trial", edit), body("check_cheap"), *SUBMIT]
        with mock.patch.object(rp, "attempt_patch", wraps=rp.attempt_patch) as certifier:
            outcome, provider, transport = self.bounded(
                engine, bodies, route=RepairRoute.POPULATION, stage="generate",
                failure=self.failure(RepairRoute.POPULATION, "population coverage: planted"),
            )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        self.assertEqual(outcome.record.route, "population")
        self.assertEqual(certifier.call_count, 1)
        self.assertEqual(transport.responses, [])
        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(len(provider.exchange_evidence), 1)
        attempt = outcome.record.attempts[0]
        self.assertEqual((attempt.terminal, attempt.accepted, attempt.route), ("SUBMITTED", True, "population"))
        self.assertEqual([s.tool for s in attempt.steps], ["read_view", "apply_edit_trial", "check_cheap"])
        committed = engine.load_task(self.task_id)
        self.assertNotEqual(committed.content_hash(), live_hash)
        self.assertEqual(committed.populations[1].conditions[1], demanded)
        (record,) = self.session_records(engine)
        self.assertEqual((record["route"], record["terminal"], record["submit"]["outcome"]),
                         ("population", "SUBMITTED", "committed"))
        self.assertIn("REPAIR ROUTE: population", initial_views(transport)[0])

        # (b) The documented rollback: a document without `population`.
        rollback = self.agents_config()  # repair.routes_bounded: [specification, reference]
        settings = rp.repair_settings(rollback)
        self.assertEqual(settings.routes_bounded, ("specification", "reference"))
        self.assertFalse(settings.route_is_bounded(RepairRoute.POPULATION))
        engine = self.make_engine(self.workspace("pop"))
        before = self.task_tree(engine)
        outcome, provider, transport = self.bounded(
            engine, [], route=RepairRoute.POPULATION, stage="generate",
            failure=self.failure(RepairRoute.POPULATION, "population coverage"),
            config_path=rollback,
        )
        self.assertEqual(outcome.record.status, rp.STATUS_ROUTE_NOT_BOUNDED)
        self.assertIn("without", outcome.record.detail.replace("['specification', 'reference']", "without"))
        self.assertEqual(outcome.disposition, rp.DISPOSITION_NEEDS_ADJUDICATION)
        self.assertFalse(outcome.committed)
        self.assertIsNone(outcome.adjudication)
        self.assertEqual(outcome.record.attempts, ())
        self.assertEqual(transport.calls, [])
        self.assertEqual(provider.exchange_evidence, [])
        self.assertEqual(self.session_records(engine), [])
        self.assertEqual(self.task_tree(engine), before)
        # RUNTIME and FATAL stay the one-shot's not_applicable under the
        # shipped key too.
        for route in (RepairRoute.RUNTIME, RepairRoute.FATAL):
            refused = rp.AgenticRepairProposer(provider).repair(engine, self.task, "generate", route, "x")
            self.assertEqual(refused.record.status, "not_applicable")
            self.assertEqual(transport.calls, [])

    def test_revalidate_runner_raising_oserror_is_infrastructure_not_red(self):
        """Phase 3 review finding 2-0 (C7; certify addendum C7 row): a stage
        runner that RAISES an exception the engine does not name — DuckDB
        out of memory or I/O out of the `reference` runner, ENOSPC while
        gold is frozen, a locked ledger — could not MEASURE. `_revalidate`
        no longer wraps it into `RevalidationFailed(revalidation_red_<stage>)`:
        it applies the in-session certify's rule
        (`certify.classify_runner_exception`) — a `ToolHarnessFault`
        carrying the exception CLASS only, halted as `InfrastructureFailure`.
        Both proposers halt on the first attempt with no `RejectionCode`,
        nothing queued and no second model call; the engine raises
        `InfrastructureFailure` with zero rounds and no rejection; only a
        runner that RETURNS a non-PASS outcome is red."""
        import errno
        import sqlite3

        import duckdb

        cases = (
            duckdb.OutOfMemoryException("Out of Memory Error: failed to allocate 512MB"),
            duckdb.IOException("IO Error: could not read block 4711 of /Users/x/runs/live/gold.duckdb"),
            OSError(errno.ENOSPC, "No space left on device", "/Users/x/runs/live/answer_key/gold.parquet"),
            sqlite3.OperationalError("database is locked"),
            MemoryError("cannot allocate the mart"),
        )
        for exc in cases:
            name = type(exc).__name__
            with self.subTest(exc=name):
                self.assertEqual(rp.halting_marker(exc), "")  # unnamed by itself: the wrap classifies it
                review = self.review_raising_on_the_trial(exc)
                engine = self.make_engine(self.workspace(f"os-{name}"), review=review)
                task = engine.load_task(self.task_id)
                before = self.task_tree(engine)
                # (a) The certifier: infrastructure, never a rejection, the text withheld.
                with self.assertRaises(InfrastructureFailure) as ctx:
                    rp.attempt_patch(engine, task, "review", spec_patch())
                self.assertNotIsInstance(ctx.exception, rp.PatchRejected)
                self.assertEqual((ctx.exception.stage, ctx.exception.marker), ("review", "ToolHarnessFault"))
                fault = ctx.exception.__cause__
                self.assertIsInstance(fault, S.ToolHarnessFault)
                self.assertEqual((fault.tool, fault.cause_type), ("review", name))
                self.assertNotIn(str(exc), str(fault))
                self.assertNotIn("/Users/x", str(fault) + str(ctx.exception))
                self.assertEqual(rp.halting_marker(ctx.exception), "ToolHarnessFault")
                self.assertEqual(self.task_tree(engine), before)
                # (b) The one-shot proposer halts: one model call, nothing queued.
                provider = ScriptedProvider([spec_patch(), spec_patch()])
                outcome = rp.RepairProposer(provider, max_attempts=2).repair(
                    engine, task, "review", SPEC, "undefined tie-break"
                )
                self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)
                self.assertEqual(outcome.infrastructure, "ToolHarnessFault")
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(outcome.record.attempts[0].error_type, "InfrastructureFailure")
                self.assertIsNone(rp.load_repair_adjudication(engine.workspace, self.task_id))
                # (c) The bounded proposer halts: one session, no RejectionCode,
                # no `revalidation_red_*` anywhere in its record.
                bodies = [body("apply_edit_trial", spec_edit()), *SUBMIT, body("apply_edit_trial", spec_edit()), *SUBMIT]
                bounded, _provider, transport = self.bounded(engine, bodies)
                self.assertEqual(bounded.disposition, rp.DISPOSITION_HALTED)
                self.assertEqual(bounded.infrastructure, "ToolHarnessFault")
                self.assertEqual(len(transport.calls), 2)
                self.assertEqual(len(bounded.record.attempts), 1)
                self.assertEqual(bounded.record.attempts[0].rejection_code, "")
                self.assertEqual(bounded.record.attempts[0].error_type, "InfrastructureFailure")
                records = canonical_json(self.session_records(engine))
                self.assertNotIn("revalidation_red", records)
                self.assertNotIn("/Users/x", records)
                self.assertIsNone(rp.load_repair_adjudication(engine.workspace, self.task_id))
                self.assertEqual(self.task_tree(engine), before)
                # (d) The engine: InfrastructureFailure, zero rounds, no rejection.
                provider2, _transport2 = self.provider(self.workspace(f"run-{name}"), bodies)
                run_engine = self.make_engine(
                    self.workspace(f"run-{name}"), review=review,
                    proposer=rp.AgenticRepairProposer(provider2), max_repair_rounds=2,
                )
                for upstream in ("contamination_pre", "generate", "reference"):
                    run_engine.set_stage_runner(upstream, self.author_ok)
                with self.assertRaises(InfrastructureFailure) as halted:
                    run_engine.run(self.task_id, until="review")
                self.assertEqual(halted.exception.marker, "toolharnessfault")
                self.assertEqual(run_engine.repair_rounds_used(self.task_id), 0)
                self.assertIsNot(run_engine.load_task(self.task_id).status, TaskStatus.REJECTED)
                self.assertIsNone(rp.load_repair_adjudication(run_engine.workspace, self.task_id))
        # A runner that RETURNS red is still red (the fixture's own review).
        engine = self.make_engine(self.workspace("returned-red"))
        with self.assertRaises(rp.RevalidationFailed) as red:
            rp.attempt_patch(engine, engine.load_task(self.task_id), "review", spec_patch("Cosmetic wording only."))
        self.assertEqual(red.exception.code, "revalidation_red_review")

    def test_view_leak_tripwire_halts_without_spending_a_round(self):
        """Phase 3 review finding 2-2 (04 §2 LEAK_TRIPWIRE; SoT T6): a view
        that still carries private material — here the paraphrased reference
        SQL the anonymized-AST detector catches — is a security incident,
        not a failed proposal. `_assert_scope` raises `DiagnosticTripwire`
        (code `view_private_ast`, the SQL only in the quarantined bytes), so
        the one-shot proposer halts BEFORE its model call (no second
        attempt, nothing queued), the bounded proposer halts inside its
        session fault boundary (a record, no model call), and the engine
        raises `InfrastructureFailure` under the tripwire's marker with zero
        rounds and no rejection."""
        leaky = "evidence:\n" + REQUALIFIED_REFERENCE
        with self.assertRaises(PJ.DiagnosticTripwire) as ctx:
            rp.view_for_route(self.task, SPEC, leaky)
        trip = ctx.exception
        self.assertEqual((trip.code, trip.source), (rp.VIEW_PRIVATE_AST_CODE, "repair_view"))
        self.assertIsInstance(trip, RuntimeError)  # the Round-4 pin still reads
        self.assertNotIn("SELECT", str(trip))
        self.assertEqual(rp.halting_marker(trip), "DiagnosticTripwire")
        # (A LITERAL copy of the reference is scrubbed to "[withheld: private
        # material]" before the tripwire, as tests/test_redteam_round4.py
        # pins; the paraphrase is what only the AST detector catches.)
        self.assertIn(
            "[withheld: private material]",
            rp.view_for_route(self.task, SPEC, "evidence:\n" + self.task.reference.sql_by_mart[MART]),
        )

        engine = self.make_engine(self.workspace("leak"))
        task = engine.load_task(self.task_id)
        before = self.task_tree(engine)
        # One-shot: halted before the model is called.
        provider = ScriptedProvider([spec_patch(), spec_patch()])
        outcome = rp.RepairProposer(provider, max_attempts=2).repair(engine, task, "review", SPEC, leaky)
        self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)
        self.assertEqual(outcome.infrastructure, "DiagnosticTripwire")
        self.assertEqual(provider.calls, [])
        self.assertEqual(len(outcome.record.attempts), 1)
        self.assertEqual(outcome.record.attempts[0].error_type, "DiagnosticTripwire")
        self.assertIsNone(rp.load_repair_adjudication(engine.workspace, self.task_id))
        self.assertEqual(self.task_tree(engine), before)
        # Bounded: the view is built inside the session's fault boundary.
        bounded, _provider, transport = self.bounded(
            engine, [body("apply_edit_trial", spec_edit()), *SUBMIT], failure=leaky,
        )
        self.assertEqual(bounded.disposition, rp.DISPOSITION_HALTED)
        self.assertEqual(bounded.infrastructure, "DiagnosticTripwire")
        self.assertEqual(transport.calls, [])
        self.assertEqual(len(bounded.record.attempts), 1)
        self.assertEqual(bounded.record.attempts[0].error_type, "DiagnosticTripwire")
        self.assertEqual(bounded.record.attempts[0].rejection_code, "")
        (record,) = self.session_records(engine)
        self.assertIsNotNone(record["fault"])
        self.assertIn("DiagnosticTripwire", canonical_json(record["fault"]))
        self.assertNotIn("SELECT", canonical_json(record))
        self.assertIsNone(rp.load_repair_adjudication(engine.workspace, self.task_id))
        self.assertEqual(self.task_tree(engine), before)
        # The engine: the proposer's halt marker, zero rounds, no rejection.
        provider2, transport2 = self.provider(self.workspace("leak-run"), [body("apply_edit_trial", spec_edit()), *SUBMIT])
        run_engine = self.make_engine(
            self.workspace("leak-run"), proposer=rp.AgenticRepairProposer(provider2), max_repair_rounds=2,
        )
        for upstream in ("contamination_pre", "generate", "reference"):
            run_engine.set_stage_runner(upstream, self.author_ok)
        with mock.patch.object(rp, "failure_detail", return_value=leaky):
            with self.assertRaises(InfrastructureFailure) as halted:
                run_engine.run(self.task_id, until="review")
        self.assertEqual(halted.exception.marker, "diagnostictripwire")
        self.assertEqual(transport2.calls, [])
        self.assertEqual(run_engine.repair_rounds_used(self.task_id), 0)
        self.assertIsNot(run_engine.load_task(self.task_id).status, TaskStatus.REJECTED)

    def test_limit_stop_is_blocked_not_a_round(self):
        """SoT T4 / roadmap 0.C: a session that stops at a harness-imposed
        limit without a validator-green draft is `blocked_limit` (the engine
        writes a BLOCKED row with `session_limit:<kind>` and a salt; no round,
        no fatal row, no rejection); with a validator-green draft the limit
        stop auto-submits it to the certifier instead."""
        engine = self.make_engine(self.workspace("limit"))
        from elt_taskgen.review import providers as providers_mod

        # Exactly `max_turns` reads: the last is the forced turn. Read the
        # shipped budget rather than repeating it (8 since 2026-09-10, when
        # 30 recorded proposer turns produced zero patch submissions).
        turns = providers_mod.role_loop_limits("repair_proposer")["max_turns"]
        reads = [body("read_view") for _ in range(turns)]
        outcome, _provider, transport = self.bounded(engine, reads)
        self.assertEqual(outcome.disposition, rp.DISPOSITION_BLOCKED_LIMIT)
        self.assertEqual((outcome.limit, outcome.limit_scope), ("turns", ""))
        self.assertIsNone(outcome.adjudication)
        self.assertEqual(len(transport.calls), turns)
        self.assertEqual(outcome.record.attempts[0].terminal, "LIMIT_TURNS")
        self.assertNotIn(SENTINEL, engine.load_task(self.task_id).solver_prompt)

        provider, _transport = self.provider(self.workspace("blocked-run"), [body("read_view") for _ in range(turns)])
        run_engine = self.make_engine(
            self.workspace("blocked-run"), proposer=rp.AgenticRepairProposer(provider), max_repair_rounds=2,
        )
        for upstream in ("contamination_pre", "generate", "reference"):
            run_engine.set_stage_runner(upstream, self.author_ok)
        task = run_engine.run(self.task_id, until="review")
        self.assertIsNot(task.status, TaskStatus.REJECTED)
        self.assertEqual(run_engine.repair_rounds_used(self.task_id), 0)
        latest = run_engine.latest_report(self.task_id, "review")
        self.assertEqual(latest.verdict, engine_mod.VERDICT_BLOCKED)
        data = json.loads(latest.payload_json)["data"]
        self.assertEqual(data["blocked_on"], "session_limit:turns")
        self.assertEqual(data["session_salt"], "1")
        self.assertEqual(run_engine.session_limit_reruns(self.task_id, "review"), 1)

        # A limit stop WITH a validator-green draft auto-submits it.
        green = self.make_engine(self.workspace("green"))
        # Two working turns then reads up to `max_turns`, so the limit binds.
        bodies = ([body("apply_edit_trial", spec_edit()), body("check_scope")]
                  + [body("read_view") for _ in range(turns - 2)])
        outcome, _provider, _transport = self.bounded(green, bodies)
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        self.assertEqual(outcome.record.attempts[0].terminal, "LIMIT_TURNS")
        self.assertIn("auto-submitted", outcome.record.attempts[0].reason)
        self.assertIn(SENTINEL, green.load_task(self.task_id).solver_prompt)

    def test_abort_queues_adjudication_and_workspace_is_byte_identical(self):
        """SoT T4 ABSTAINED for the proposer: `abort(reason_code)` ends the
        failure's sessions, queues NEEDS_ADJUDICATION with the reason code, and
        the task tree and ledger are byte-identical. Through Engine.run the
        same disposition becomes a human BLOCKED resume point with no repair
        row, revision, invalidation or rejection."""
        engine = self.make_engine(self.workspace("abort"))
        before = self.task_tree(engine)
        bodies = [body("read_view"), body("abort", {"reason_code": "cannot_repair"})]
        outcome, _provider, transport = self.bounded(engine, bodies)
        self.assertEqual(outcome.disposition, rp.DISPOSITION_NEEDS_ADJUDICATION)
        self.assertEqual(outcome.record.status, rp.STATUS_NEEDS_ADJUDICATION)
        self.assertEqual(len(transport.calls), 2)  # no second session after an abort
        attempt = outcome.record.attempts[0]
        self.assertEqual((attempt.terminal, attempt.abort_reason, attempt.error_type), ("ABSTAINED", "cannot_repair", "Abstained"))
        self.assertIsNotNone(outcome.adjudication)
        queued = rp.load_repair_adjudication(engine.workspace, self.task_id)
        self.assertEqual(queued["status"], rp.STATUS_NEEDS_ADJUDICATION)
        self.assertEqual(queued["attempts"][0]["abort_reason"], "cannot_repair")
        self.assertEqual(queued["task_content_hash"], self.task.content_hash())
        self.assertEqual(self.task_tree(engine), before)
        # A proposer call alone has not made this a live queue item: the
        # engine must also map the disposition onto a BLOCKED stage row.
        self.assertEqual(cli_mod._audit_queue(engine, include_repair=True), [])
        self.assertEqual(cli_mod._audit_queue(engine), [])  # the triage's predicate is unchanged

        run_workspace = self.workspace("abort-run")
        provider, run_transport = self.provider(
            run_workspace,
            [body("read_view"), body("abort", {"reason_code": "cannot_repair"})],
        )
        run_engine = self.make_engine(
            run_workspace,
            proposer=rp.AgenticRepairProposer(provider),
            max_repair_rounds=2,
        )
        for upstream in ("contamination_pre", "generate", "reference"):
            run_engine.set_stage_runner(upstream, self.author_ok)
        before_task = run_engine.load_task(self.task_id)

        blocked = run_engine.run(self.task_id, until="review")

        self.assertEqual(len(run_transport.calls), 2)
        self.assertIsNot(blocked.status, TaskStatus.REJECTED)
        self.assertEqual(blocked.current_revision, before_task.current_revision)
        self.assertEqual(blocked.content_hash(), before_task.content_hash())
        self.assertEqual(run_engine.repair_rounds_used(self.task_id), 0)
        self.assertEqual(run_engine.final_verdict(self.task_id), engine_mod.FINAL_IN_PROGRESS)
        latest = run_engine.latest_report(self.task_id, "review")
        self.assertEqual(latest.verdict, engine_mod.VERDICT_BLOCKED)
        latest_data = json.loads(latest.payload_json)["data"]
        self.assertEqual(latest_data["blocked_on"], engine_mod.BLOCKED_ON_HUMAN)
        self.assertEqual(latest_data["status"], rp.STATUS_NEEDS_ADJUDICATION)
        entries = cli_mod._audit_queue(run_engine, include_repair=True)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][3]["status"], rp.STATUS_NEEDS_ADJUDICATION)

    def test_session_instructions_cover_batching_and_a_green_cheap_check(self):
        """Run 11 (2026-09-11) transcripts: across 50 read_view, 87 read_field
        and 55 check_cheap calls the proposer made ZERO edits. Two causes in
        the observations. It batched two tool calls in one turn and the policy
        refused BOTH (`multiple_tool_use`), spending turns for nothing. And
        `check_cheap` answered `cheap_green ok=true` — those gates read the
        prose and IR shape only, so they cannot see the critic's claim the
        proposer was called to repair — which it read as "nothing is wrong"
        and aborted."""
        text = rp._SESSION_INSTRUCTIONS
        lowered = text.lower()
        self.assertIn("one tool call per turn", lowered)
        self.assertIn("multiple_tool_use", lowered)
        self.assertIn("a green check_cheap does not mean there is nothing to repair", lowered)
        self.assertIn("never abort merely because check_cheap is", lowered)
        # It must say WHY the gates are blind, not merely that they are.
        for blind in ("ambiguity a critic found", "attack case that failed to promote"):
            self.assertIn(blind, lowered, blind)

    def test_session_instructions_say_how_to_place_a_mart_local_passage(self):
        """Batch 2026-09-10: on prose-fidelity failures the proposer read the
        view and the named missing items, never once called apply_edit_trial,
        and aborted `insufficient_information`. Prose coverage is scored per
        mart SECTION, so `insert` — which appends to the end of the field —
        lands in the LAST mart's section and cannot fix an earlier mart's
        item. The instructions must say that, and must say which abort code
        actually means what. The prose recipe is now rendered for the
        SPECIFICATION route only, so the assertions run against the rendered
        text rather than the template."""
        text = rp._session_instructions(RepairRoute.SPECIFICATION)
        lowered = text.lower()
        self.assertIn("repairing solver prose, concretely", lowered)
        self.assertIn("per mart section", lowered)
        self.assertIn("lands in the last mart's section", lowered)
        self.assertIn("use `replace`", lowered)
        self.assertIn("occurs exactly once in the whole field", lowered)
        # The abort discipline: not because the edit is fiddly.
        self.assertIn("not because the edit is long or fiddly", lowered)
        self.assertIn("never that you have not looked yet", lowered)
        # Still one artifact, still scope-checked: the older contract stands.
        self.assertIn("one edit per apply_edit_trial call", lowered)

    def test_the_prose_recipe_is_not_shown_on_the_population_route(self):
        """A recipe for the wrong surface costs turns. On the population route
        the proposer cannot edit any mart field, so per-mart-section advice
        about `insert` and `replace` is noise."""
        text = rp._session_instructions(RepairRoute.POPULATION).lower()
        self.assertNotIn("repairing solver prose, concretely", text)
        self.assertNotIn("lands in the last mart's section", text)

    def test_the_population_route_is_told_how_to_write_a_population_repair(self):
        """batch10 2026-09-11: on the population route the proposer spent five
        of its six tool calls on `read_field` over `marts.*` — two of them
        refused `field_outside_allowlist` — and then aborted
        `insufficient_information`, with the mart plan, the tie-break rule and
        the population conditions all already in its view. It was never told
        that a population repair is written by STATING A CONDITION."""
        text = rp._session_instructions(RepairRoute.POPULATION).lower()
        self.assertIn("repairing a population, concretely", text)
        self.assertIn("the conditions are the repair surface", text)
        self.assertIn("stating a condition", text)
        self.assertIn("`populations.<i>.conditions`", text)
        # The witness form the conditions already use, so a new one matches.
        self.assertIn("tie witness:", text)
        # And the turn sink it fell into.
        self.assertIn("do not spend turns reading `marts.*` on this route", text)

    def test_the_readable_and_editable_lists_are_both_true(self):
        """One block named the EDITABLE paths under a "READ AND EDIT ONLY"
        heading and called everything else refused. On the population route
        that is false twice over: `marts` and `tables` answer `read_field`
        while `marts.*.plan` and `marts.*.grain` do not. Both lists are
        rendered from the validators that actually decide."""
        from elt_taskgen.review.tools.validators import (
            editable_field_paths,
            field_is_readable,
        )

        for route in (RepairRoute.POPULATION, RepairRoute.SPECIFICATION):
            with self.subTest(route=route.value):
                text = rp._session_instructions(route)
                for path in editable_field_paths(route):
                    if "literal_rows" in path:
                        # Editable but never readable: naming it invites a
                        # `forbidden_argument` security event.
                        self.assertNotIn(path, text)
                        continue
                    self.assertIn(f"`{path}`", text)
                # Every path the text calls unreadable really is unreadable.
                head, _, tail = text.partition("but NOT ")
                named = tail.split(", which come back")[0] if tail else ""
                for quoted in [p.strip(" `") for p in named.split("`, `") if p]:
                    quoted = quoted.strip(" `")
                    if not quoted:
                        continue
                    self.assertFalse(
                        field_is_readable(quoted, route),
                        f"{quoted} is readable on {route.value}",
                    )
                # `marts.*.plan` is readable on no route and is the field the
                # proposer actually burned a turn on.
                self.assertIn("`marts.*.plan`", text)

    def test_proposer_turns_send_auto_and_the_last_turn_forces_a_terminal(self):
        """2026-09-11: under `tool_choice: any` the Messages API allows no
        extended thinking, so the proposer picked a tool blind, read six
        fields and aborted on every blocked task of three runs (48 tool
        calls, zero edits); with the same prompt and no forced choice it
        reasoned and named the exact replace. Ordinary turns now send
        `auto`; the last permitted turn, whose wire is submit and abort
        only, still sends `any` so the session cannot end without one."""
        engine = self.make_engine(self.workspace("auto-choice"))
        bodies = [body("apply_edit_trial", spec_edit()), *SUBMIT]
        provider, transport = self.provider(engine.workspace, bodies)
        rp.AgenticRepairProposer(provider).repair(engine, self.task, "review", SPEC, self.failure())
        choices = [payload.get("tool_choice") for _u, _h, payload in transport.calls]
        self.assertGreaterEqual(len(choices), 2)
        self.assertEqual(choices[0], {"type": "auto"})
        from elt_taskgen.review import providers as providers_mod

        policy = providers_mod.session_policy_for(rp.ROLE_NAME)
        for index, (_u, _h, payload) in enumerate(transport.calls):
            names = {t.get("name") for t in payload.get("tools", ())}
            if names and names <= set(policy.terminal_tool_names):
                self.assertEqual(payload.get("tool_choice"), {"type": "any"}, index)
            else:
                self.assertEqual(payload.get("tool_choice"), {"type": "auto"}, index)

    def test_an_edit_carrying_tool_call_markup_is_a_correction_not_a_write(self):
        """batch10 run D 2026-09-11, dlt__personio: three apply_edit_trial
        calls arrived with `new` = "</parameter>\n<parameter name=\"rationale\">
        Roll back ..." — the model's own call framing, leaked into the
        argument — and were written into the solver prompt. Such a call is
        refused as invalid_arguments (a correction), the session goes on,
        and the leaked text never lands."""
        engine = self.make_engine(self.workspace("leaked-markup"))
        leaked = spec_edit()
        leaked["new"] = '</parameter>\n<parameter name="rationale">Roll back the inserted paragraph'
        bodies = [body("apply_edit_trial", leaked), body("apply_edit_trial", spec_edit()), *SUBMIT]
        provider, transport = self.provider(engine.workspace, bodies)
        outcome = rp.AgenticRepairProposer(provider).repair(engine, self.task, "review", SPEC, self.failure())
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        texts = "\n".join(payload_texts(transport))
        self.assertIn("invalid_arguments", texts)
        committed = engine.load_task(self.task.task_id)
        self.assertNotIn("<parameter", committed.solver_prompt)
        self.assertNotIn("</parameter>", committed.solver_prompt)
        self.assertIn(SENTINEL, committed.solver_prompt)

    def test_a_delete_whose_new_is_only_markup_is_taken_as_empty(self):
        """lavestima, batch10 run E: three `delete` calls in a row arrived
        with `new` = "</antml：parameter>\n" (a fullwidth colon), were each
        refused, and the third exhausted the session — a halt over an
        argument that is empty by definition. Markup-only `new` on a delete
        is the empty string; the markup is never written."""
        engine = self.make_engine(self.workspace("delete-markup"))
        first = spec_edit()                      # insert the sentinel ...
        gone = {
            # Spelled as a REPLACE, the way synsql__3d_object's session did
            # it (run F): the markup-only `new` makes it a delete of `old`.
            "artifact": "task_ir.json", "op": "replace", "locator": "solver_prompt",
            "old": " " + SENTINEL, "new": "</antml\u0903parameter>\n<parameter name=\"rationale\">restore",
            "rationale": "remove the passage the cheap gate rejected",
        }
        again = spec_edit(SENTINEL + " restated")  # ... then put a variant back
        bodies = [body("apply_edit_trial", first), body("apply_edit_trial", gone),
                  body("apply_edit_trial", again), *SUBMIT]
        provider, transport = self.provider(engine.workspace, bodies)
        outcome = rp.AgenticRepairProposer(provider).repair(engine, self.task, "review", SPEC, self.failure())
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        texts = "\n".join(payload_texts(transport))
        self.assertNotIn("new_carries_tool_call_syntax", texts)
        committed = engine.load_task(self.task.task_id)
        self.assertNotIn("parameter>", committed.solver_prompt)
        self.assertIn(SENTINEL + " restated", committed.solver_prompt)

    def test_a_leaked_parameter_tail_is_cut_off_the_argument(self):
        """synsql__3d_motion, batch10 run K 2026-09-11: three apply_edit_trial
        calls arrived with `new` = "</antmlःparameter>\n<parameter
        name=\"rationale\">Diagnostic revert ..." — an empty `new` whose
        closing tag broke, so the rationale landed inside it. Each was refused
        as markup-bearing and the third exhausted the session (an
        infrastructure halt). The text before a leaked closing tag is the
        value the model meant: empty here, so the replace is a delete; a
        non-empty prefix is kept as the replacement."""
        engine = self.make_engine(self.workspace("leaked-tail"))
        first = spec_edit()
        revert = {
            "artifact": "task_ir.json", "op": "replace", "locator": "solver_prompt",
            "old": " " + SENTINEL,
            "new": "</antmlःparameter>\n<parameter name=\"rationale\">Diagnostic "
                   "revert of the second clarification sentence.",
            "rationale": "isolate which edit the prose gate objects to",
        }
        again = spec_edit(SENTINEL + " restated")
        trimmed = {
            "artifact": "task_ir.json", "op": "replace", "locator": "solver_prompt",
            "old": SENTINEL + " restated",
            "new": SENTINEL + " restated twice</parameter>\n<parameter name=\"rationale\">keep",
            "rationale": "restate once more",
        }
        bodies = [body("apply_edit_trial", first), body("apply_edit_trial", revert),
                  body("apply_edit_trial", again), body("apply_edit_trial", trimmed), *SUBMIT]
        provider, transport = self.provider(engine.workspace, bodies)
        outcome = rp.AgenticRepairProposer(provider).repair(engine, self.task, "review", SPEC, self.failure())
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        texts = "\n".join(payload_texts(transport))
        self.assertNotIn("carries_tool_call_syntax", texts)
        committed = engine.load_task(self.task.task_id)
        self.assertNotIn("parameter>", committed.solver_prompt)
        self.assertNotIn("Diagnostic revert", committed.solver_prompt)
        self.assertIn(SENTINEL + " restated twice", committed.solver_prompt)

    def test_check_cheap_names_the_item_and_the_operator_word_it_refused(self):
        """batch10 run G 2026-09-11 (lavestima, dlt__workable): after an edit
        the proposer's check_cheap answered only `prose_operator_vocabulary=true`
        and a mart name — not which rule, not which operator word — and the
        proposer, unable to tell what to change, aborted `cannot_repair`. It
        now carries the same codes-only projection the author's check_prose
        gets: the per-item flags and the public names, including the
        operator kind the declarative gate refused."""
        engine = self.make_engine(self.workspace("cheap-names"))
        banned = spec_edit("A producers row whose id_users is NULL satisfies this join condition for no users row.")
        bodies = [body("apply_edit_trial", banned), body("check_cheap"), body("abort", {"reason_code": "cannot_repair"})]
        provider, transport = self.provider(engine.workspace, bodies)
        rp.AgenticRepairProposer(provider).repair(engine, self.task, "review", SPEC, self.failure())
        texts = "\n".join(payload_texts(transport))
        cheap = [line for line in texts.splitlines() if "[cheap]" in line]
        self.assertTrue(cheap, texts[-800:])
        observation = cheap[-1]
        self.assertIn("prose_operator_vocabulary=true", observation)
        self.assertIn("prose_operator_join_operator=true", observation)
        self.assertIn("prose_item_operator=true", observation)
        self.assertIn("join", observation.split("names=")[-1] if "names=" in observation else observation)

    def test_session_init_reserves_turn_cap_plus_nested_ceiling(self):
        """Certify addendum §3.2 / §5: INIT calls `CostMeter.reserve(max_usd +
        nested_ceiling[route])` on the task meter; a task budget that cannot
        cover the session AND its certification refuses before any call —
        a `usd` limit stop of the task scope (BLOCKED, no round), which the
        engine halts on as infrastructure."""
        settings = rp.repair_settings()
        self.assertEqual(dict(settings.nested_ceiling_usd), {"specification": 0.56, "population": 0.12, "reference": 0.05})
        engine = self.make_engine(self.workspace("reserve"))
        bodies = [body("apply_edit_trial", spec_edit()), *SUBMIT]
        provider, transport = self.provider(engine.workspace, bodies, budget=1.00)
        proposer = rp.AgenticRepairProposer(provider)
        # The session cap is the shipped `session.max_usd` (2.00 since the
        # 2026-09-10 rerun, where 13 of 50 tasks stopped at the old 0.80).
        from elt_taskgen.review import providers as providers_mod

        session_cap = providers_mod.role_loop_limits("repair_proposer")["max_usd"]
        self.assertAlmostEqual(proposer.session_reserve_usd(SPEC), session_cap + 0.56)
        self.assertAlmostEqual(proposer.session_reserve_usd(REF), session_cap + 0.05)
        with mock.patch.object(provider.meter, "reserve", wraps=provider.meter.reserve) as reserve:
            outcome = proposer.repair(engine, self.task, "review", SPEC, self.failure())
        self.assertEqual(outcome.disposition, rp.DISPOSITION_BLOCKED_LIMIT)
        self.assertEqual((outcome.limit, outcome.limit_scope), (rp.LIMIT_USD, "task"))
        self.assertEqual(reserve.call_count, 1)
        kwargs = reserve.call_args.kwargs
        self.assertAlmostEqual(kwargs["est_usd"], session_cap + 0.56)
        self.assertEqual((kwargs["task_id"], kwargs["role_name"]), (self.task_id, rp.ROLE_NAME))
        self.assertEqual(transport.calls, [])
        self.assertEqual(provider.meter.total_usd, 0.0)
        self.assertIsNone(rp.load_repair_adjudication(engine.workspace, self.task_id))

        provider, transport = self.provider(engine.workspace, bodies, budget=100.0)
        with mock.patch.object(provider.meter, "reserve", wraps=provider.meter.reserve) as reserve:
            outcome = rp.AgenticRepairProposer(provider).repair(engine, self.task, "review", SPEC, self.failure())
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        self.assertAlmostEqual(reserve.call_args_list[0].kwargs["est_usd"], session_cap + 0.56)
        self.assertGreater(len(transport.calls), 0)

        provider, transport = self.provider(self.workspace("halt"), bodies, budget=1.00)
        run_engine = self.make_engine(
            self.workspace("halt"), proposer=rp.AgenticRepairProposer(provider), max_repair_rounds=2,
        )
        for upstream in ("contamination_pre", "generate", "reference"):
            run_engine.set_stage_runner(upstream, self.author_ok)
        with self.assertRaises(InfrastructureFailure) as halted:
            run_engine.run(self.task_id, until="review")
        self.assertEqual(halted.exception.marker, "budgetexceedederror")
        self.assertEqual(run_engine.repair_rounds_used(self.task_id), 0)
        self.assertIsNot(run_engine.load_task(self.task_id).status, TaskStatus.REJECTED)
        self.assertEqual(transport.calls, [])

    def test_cost_meter_episode_record_includes_nested_certification_spend(self):
        """Certify addendum §5: `RepairAttemptRecord.usd_session` is the
        proposer role's meter delta across the session and `usd_certification`
        the task meter's delta across `attempt_patch`, with the nested critic
        charge attributed to its own role and summed into the episode."""
        engine = self.make_engine(self.workspace("meter"))
        holder: dict = {}

        def review_with_critic(engine_, task_):
            if SENTINEL in task_.solver_prompt:  # the trial: one nested live critic call
                holder["provider"].complete("ambiguity_critic", "critic view " + task_.content_hash())
                return StageOutcome(VERDICT_PASS, StagePayload(detail="review ok"))
            return StageOutcome(VERDICT_FAIL, StagePayload(error="undefined tie-break in prose"))

        engine.set_stage_runner("review", review_with_critic)
        bodies = [body("apply_edit_trial", spec_edit()), *SUBMIT, anthropic_tool_response([VALID_FINDING])]
        provider, transport = self.provider(engine.workspace, bodies)
        holder["provider"] = provider
        outcome = rp.AgenticRepairProposer(provider).repair(engine, self.task, "review", SPEC, self.failure())
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        self.assertEqual(len(transport.calls), 3)
        meter = provider.meter
        proposer_usd = meter.per_role[rp.ROLE_NAME]["usd"]
        critic_usd = meter.per_role["ambiguity_critic"]["usd"]
        self.assertGreater(proposer_usd, 0.0)
        self.assertGreater(critic_usd, 0.0)
        self.assertAlmostEqual(outcome.record.usd_session, proposer_usd)
        self.assertAlmostEqual(outcome.record.usd_certification, critic_usd)
        self.assertAlmostEqual(meter.per_task_usd[self.task_id], proposer_usd + critic_usd)
        attempt = outcome.record.attempts[0]
        self.assertAlmostEqual(attempt.usd_session, proposer_usd)
        self.assertAlmostEqual(attempt.usd_certification, critic_usd)
        (record,) = self.session_records(engine)
        self.assertAlmostEqual(record["submit"]["usd_certification"], critic_usd)
        self.assertAlmostEqual(record["usd_session"], proposer_usd)

    def test_explicit_one_shot_rollback_is_byte_identical(self):
        """The shipped default and explicit `bounded` mode construct the
        agentic proposer. Explicit `one_shot`, with or without the redundant
        enable flag, retains the legacy proposer and produces byte-identical
        ledgers and task trees; `--no-repair-proposer` keeps the off switch."""
        parser = cli_mod.build_parser()
        default = parser.parse_args(["generate", "--task-id", "t"])
        bounded = parser.parse_args([
            "generate", "--task-id", "t", "--repair-proposer-mode", "bounded",
        ])
        one_shot = parser.parse_args([
            "generate", "--task-id", "t", "--repair-proposer-mode", "one_shot",
        ])
        explicit_one_shot = parser.parse_args([
            "generate", "--task-id", "t", "--repair-proposer",
            "--repair-proposer-mode", "one_shot",
        ])
        disabled = parser.parse_args([
            "generate", "--task-id", "t", "--no-repair-proposer",
        ])
        self.assertEqual(default.repair_proposer_mode, "bounded")
        self.assertIsInstance(cli_mod._make_repair_proposer(default, object()), rp.AgenticRepairProposer)
        self.assertIsInstance(cli_mod._make_repair_proposer(bounded, object()), rp.AgenticRepairProposer)
        self.assertIsNone(cli_mod._make_repair_proposer(disabled, object()))
        for args in (one_shot, explicit_one_shot):
            proposer = cli_mod._make_repair_proposer(args, ScriptedProvider([spec_patch()]))
            self.assertIs(type(proposer), rp.RepairProposer)
            self.assertEqual(proposer.max_attempts, rp.proposer_attempt_budget())

        def run(name: str, args):
            engine = self.make_engine(
                self.workspace(name),
                proposer=cli_mod._make_repair_proposer(args, ScriptedProvider([spec_patch()])),
                max_repair_rounds=2,
            )
            for upstream in ("contamination_pre", "generate", "reference"):
                engine.set_stage_runner(upstream, self.author_ok)
            task = engine.run(self.task_id, until="review")
            self.assertIn(SENTINEL, task.solver_prompt)
            return tree_hash(engine.workspace, exclude_ledger=True), ledger_rows(engine, self.task_id)

        self.assertEqual(run("one-shot", one_shot), run("explicit-one-shot", explicit_one_shot))
        with tempfile.TemporaryDirectory() as tmp:
            for args, cls in ((one_shot, rp.RepairProposer), (bounded, rp.AgenticRepairProposer)):
                args.workspace = tmp
                args.replay_only = True
                engine = cli_mod._make_engine(args, provider=object())
                try:
                    self.assertIsInstance(engine._repair_proposer, cls)
                finally:
                    engine.close()

    def test_audit_list_renders_repair_adjudication(self):
        """Roadmap 1.P cli.py row: `_audit_queue` reads
        `<ws>/audit/<task>.repair_adjudication.json` (bound to the current
        hash) and `audit list` renders it as a non-sign-off item with the
        stage, route, attempts and rejection codes; a stale entry is not queued."""
        import contextlib
        import io

        engine = self.make_engine(self.workspace("audit"))
        record = rp.RepairAttemptRecord(
            task_id=self.task_id, task_content_hash=self.task.content_hash(), stage="review",
            route="specification", status=rp.STATUS_NEEDS_ADJUDICATION, committed=False,
            attempts=(
                rp.ProposerAttempt(index=1, route="specification", artifact="task_ir.json", error_type="RevalidationFailed",
                                   reason="re-validation stage 'review' verdict 'fail'", terminal="SUBMITTED",
                                   rejection_code="revalidation_red_review"),
                rp.ProposerAttempt(index=2, route="specification", error_type="Abstained", terminal="ABSTAINED",
                                   abort_reason="cannot_repair"),
            ),
            detail="repair proposer ABSTAINED after 2 session(s)",
        )
        rp.queue_adjudication(engine.workspace, self.task, record)
        engine.record_report(
            self.task,
            "review",
            engine_mod.VERDICT_BLOCKED,
            engine_mod.StagePayload(detail="repair proposer awaiting adjudication"),
        )
        entries = cli_mod._audit_queue(engine, include_repair=True)
        self.assertEqual(len(entries), 1)
        task, pending, adjudication, repair_entry = entries[0]
        self.assertEqual((task.task_id, pending, adjudication), (self.task_id, {}, None))
        self.assertEqual(repair_entry["kind"], rp.ADJUDICATION_KIND)
        self.assertEqual(cli_mod._audit_queue(engine), [])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_mod.main(["audit", "list", "--workspace", str(engine.workspace)])
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn(self.task_id, out)
        self.assertIn("REPAIR ADJUDICATION", out)
        self.assertIn("stage=review", out)
        self.assertIn("route=specification", out)
        self.assertIn("attempts=2", out)
        self.assertIn("revalidation_red_review", out)
        self.assertIn("ABSTAINED after 2 session(s)", out)
        # A later repair that moved the identity leaves the entry stale, not queued.
        engine.save_task(self.task.model_copy(update={"solver_prompt": PROSE + " " + SENTINEL}))
        self.assertEqual(cli_mod._audit_queue(engine, include_repair=True), [])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli_mod.main(["audit", "list", "--workspace", str(engine.workspace)]), 0)
        self.assertIn("audit queue empty", buf.getvalue())


class BS_FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class OneShotCertifierLedgerTest(_CertifierFixture):
    """docs/plans/bounded_agents_phase1.md §2: the certifier changes that are
    properties of `trial_phase` reach the ONE-SHOT proposer too, and are
    pinned here for that mode."""

    def test_one_shot_proposer_certification_substitutes_structural_calibrate(self):
        """Finding 1-3 (certify addendum §3.4): under `--empirical` a
        one-shot proposer's certification of a `calibrate` failure runs the
        STRUCTURAL calibrate runner on the trial (a structural green commits)
        and never launches the campaign there; the live ladder runs the
        empirical runner once, post-commit, at the new hash."""
        from elt_taskgen import cli

        empirical_calls: list[tuple[Path, str]] = []

        def empirical(engine, task):
            empirical_calls.append((Path(engine.workspace), task.content_hash()))
            if SENTINEL in task.solver_prompt:
                return StageOutcome(VERDICT_PASS, StagePayload(detail="campaign ok"))
            return StageOutcome(VERDICT_FAIL, StagePayload(error="impossible variant: minimal"))

        setattr(empirical, rp.CALIBRATE_EMPIRICAL_TAG, True)
        self.assertTrue(rp._is_empirical_calibrate_runner(empirical))
        engine = self.make_engine(
            self.workspace("one-shot-calibrate"), review=self.author_ok,
            proposer=rp.RepairProposer(ScriptedProvider([spec_patch()]), max_attempts=1),
            max_repair_rounds=2,
        )
        for stage in ("contamination_pre", "generate", "reference", "attack", "gates", "gates_extract_load", "gates_transform"):
            engine.set_stage_runner(stage, pass_runner(f"{stage} ok"))
        engine.set_stage_runner("calibrate", empirical)
        before_hash = engine.load_task(self.task_id).content_hash()
        structural_runs: list[Path] = []
        real_make = cli.make_structural_calibrate_runner

        def spy_make(agents_config=None):
            structural = real_make(agents_config)

            def run(engine_, task_):
                structural_runs.append(Path(engine_.workspace))
                return structural(engine_, task_)

            return run

        with mock.patch.object(cli, "make_structural_calibrate_runner", spy_make):
            engine.run(self.task_id, until="calibrate")
        task = engine.load_task(self.task_id)
        self.assertIn(SENTINEL, task.solver_prompt)
        self.assertEqual(engine.repair_rounds_used(self.task_id), 1)
        self.assertEqual(len(structural_runs), 1)
        self.assertNotEqual(structural_runs[0], engine.workspace)  # the trial only
        self.assertEqual([ws for ws, _ in empirical_calls], [engine.workspace, engine.workspace])
        self.assertEqual([h for _, h in empirical_calls], [before_hash, task.content_hash()])
        self.assertEqual(engine.latest_report(self.task_id, "calibrate").verdict, VERDICT_PASS)
        self.assertEqual(engine.latest_report(self.task_id, "calibrate").content_hash, task.content_hash())



    def test_one_shot_blocked_revalidation_halts_exit_2_and_rejects_nothing(self):
        """Finding 1-1 (C7; docs/plans/bounded_agents_phase1.md §2): through
        the CLI with the ONE-SHOT proposer, a re-validated member that WAITS
        (`VERDICT_BLOCKED`) on the trial halts the run as infrastructure —
        exit 2, "could not measure", the reason on the FAIL row — with no
        repair round spent, nothing queued and the task not rejected; at the
        Phase 0 end this was `RevalidationFailed`, a failed proposal, an
        adjudication entry and the ordinary round (exit 0/1)."""
        import contextlib
        import io

        from elt_taskgen import cli
        from elt_taskgen.engine import STAGE_ORDER

        review = self.review_blocked_on_the_trial(engine_mod.BLOCKED_ON_HUMAN)
        engine = self.make_engine(self.workspace("one-shot-blocked"), review=review)
        workspace = engine.workspace
        engine.close()

        def stub_runners(provider, **kwargs):
            runners = {
                stage: pass_runner(f"{stage.value} ok")
                for stage in STAGE_ORDER if stage is not StageName.INTAKE
            }
            runners[StageName.REVIEW] = review
            return runners

        proposers: list = []

        def stub_proposer(args, provider):
            self.assertEqual((args.repair_proposer, args.repair_proposer_mode), (True, "one_shot"))
            proposers.append(rp.RepairProposer(ScriptedProvider([spec_patch()]), max_attempts=2))
            return proposers[-1]

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "build_stage_runners", stub_runners), \
                mock.patch.object(cli, "_make_repair_proposer", stub_proposer), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([
                "review", "--task-id", self.task_id, "--replay-only", "--repair-proposer",
                "--repair-proposer-mode", "one_shot",
                "--workspace", str(workspace),
            ])
        printed = out.getvalue() + err.getvalue()
        self.assertEqual(code, 2, printed)
        self.assertIn("could not measure", printed)
        self.assertEqual(len(proposers), 1)
        self.assertEqual(len(proposers[0].provider.calls), 1)  # halted after one proposal
        reopened = Engine(workspace)
        self.addCleanup(reopened.close)
        task = reopened.load_task(self.task_id)
        self.assertIsNot(task.status, TaskStatus.REJECTED)
        self.assertNotIn(SENTINEL, task.solver_prompt)
        self.assertEqual(reopened.repair_rounds_used(self.task_id), 0)
        self.assertIsNone(rp.load_repair_adjudication(workspace, self.task_id))
        latest = reopened.latest_report(self.task_id, "review")
        self.assertEqual(latest.verdict, VERDICT_FAIL)
        payload = json.loads(latest.payload_json)
        self.assertEqual(payload["infrastructure"], engine_mod.blocked_stage_marker(engine_mod.BLOCKED_ON_HUMAN))
        self.assertEqual(payload["data"][engine_mod.BLOCKED_ON_KEY], engine_mod.BLOCKED_ON_HUMAN)

    def test_one_shot_reference_certification_halts_when_prepended_review_cannot_measure(self):
        """Finding 1-2 (F1 fix form (i); docs/plans/bounded_agents_phase1.md
        §2): a ONE-SHOT REFERENCE certification that reaches `attack`
        prepends the wired `review` on the trial; a review that cannot
        measure there — the council not admitted (`infrastructure =
        not_admitted`), or a replay miss (`TranscriptMissingError`) — HALTS
        the proposal as infrastructure (no round, nothing queued, the
        workspace byte-identical) instead of failing red at `attack` as the
        Phase 0 end did; a review that measures green lets the same patch
        commit, because `attack` then finds the PASS row at the new hash."""
        from elt_taskgen import cli
        from elt_taskgen.review.providers import TranscriptMissingError

        live = self.workspace("one-shot-currency")
        seen: list[tuple[str, bool]] = []

        def on_trial(engine) -> bool:
            return Path(engine.workspace).resolve() != live.resolve()

        def review_not_admitted(engine, task):
            seen.append(("review", on_trial(engine)))
            if on_trial(engine):
                return StageOutcome(
                    VERDICT_FAIL,
                    StagePayload(error="council not admitted (fail closed)", infrastructure="not_admitted"),
                )
            return StageOutcome(VERDICT_PASS, StagePayload(detail="review ok"))

        def review_replay_miss(engine, task):
            seen.append(("review", on_trial(engine)))
            if on_trial(engine):
                raise TranscriptMissingError("no recorded transcript at this hash (--replay-only)")
            return StageOutcome(VERDICT_PASS, StagePayload(detail="review ok"))

        def attack(engine, task):
            cli._require_pass_payload(engine, task, StageName.REVIEW)  # the real currency check
            seen.append(("attack", on_trial(engine)))
            return StageOutcome(VERDICT_PASS, StagePayload(detail="attack ok"))

        runners = {s.value: pass_runner(f"{s.value} ok") for s in StageName}
        runners.update({"review": review_not_admitted, "attack": attack})
        engine = Engine(live, stage_runners=runners, max_repair_rounds=0)
        self.addCleanup(engine.close)
        engine.register(self.task)
        task = engine.run(self.task_id, until="attack")
        self.assertIs(task.status, TaskStatus.ATTACKED)
        self.assertEqual(seen, [("review", False), ("attack", False)])

        for name, review in (("not_admitted", review_not_admitted), ("TranscriptMissingError", review_replay_miss)):
            with self.subTest(review=name):
                seen.clear()
                engine.set_stage_runner("review", review)
                before = tree_hash(engine.workspace, exclude_ledger=True)
                artifacts_before = repair.snapshot(engine.workspace, self.task_id)
                rows_before = ledger_rows(engine, self.task_id)
                provider = ScriptedProvider([reference_comment_patch(task), reference_comment_patch(task)])
                outcome = rp.RepairProposer(provider, max_attempts=2).repair(
                    engine, task, "attack", RepairRoute.REFERENCE, "reference failure"
                )
                self.assertEqual((outcome.disposition, outcome.infrastructure), (rp.DISPOSITION_HALTED, name))
                self.assertEqual(len(provider.calls), 1)  # halted: no second proposal
                self.assertEqual(seen, [("review", True)])  # the prepended review ran on the trial; attack never did
                self.assertEqual(len(outcome.record.attempts), 1)
                self.assertFalse(outcome.record.attempts[0].accepted)
                self.assertIn(outcome.record.attempts[0].error_type, ("InfrastructureFailure", name))
                self.assertIsNone(rp.load_repair_adjudication(engine.workspace, self.task_id))
                # The proposer wrote nothing: not a task artifact, not a ledger row.
                self.assertEqual(tree_hash(engine.workspace, exclude_ledger=True), before)
                self.assertEqual(ledger_rows(engine, self.task_id), rows_before)
                # The engine's mapping of a halted outcome (the FAIL row with
                # the marker, then InfrastructureFailure -> exit 2).
                with self.assertRaises(InfrastructureFailure) as halted:
                    engine._halt_on_proposer_fault(
                        task, StageName.ATTACK, RepairRoute.REFERENCE, outcome.infrastructure,
                        outcome.record.detail,
                    )
                self.assertEqual(halted.exception.marker, name)
                self.assertEqual(
                    json.loads(engine.latest_report(self.task_id, "attack").payload_json)["infrastructure"], name
                )
                self.assertEqual(engine.repair_rounds_used(self.task_id), 0)
                self.assertIsNot(engine.load_task(self.task_id).status, TaskStatus.REJECTED)
                # The halt row (and its `reports/` echo) is the engine's; no task artifact moved.
                self.assertEqual(repair.snapshot(engine.workspace, self.task_id), artifacts_before)

        # An admitted council that measures green: the same patch commits.
        seen.clear()
        engine.set_stage_runner("review", pass_runner("review ok"))
        provider = ScriptedProvider([reference_comment_patch(task)])
        outcome = rp.RepairProposer(provider, max_attempts=1).repair(
            engine, task, "attack", RepairRoute.REFERENCE, "reference failure"
        )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        self.assertEqual(seen, [("attack", True)])
        self.assertIn(REFERENCE_COMMENT, engine.load_task(self.task_id).reference.sql_by_mart[MART])


# Population repair route and certify's attack member (Phase 3).

POP = RepairRoute.POPULATION

#: The dangling-key lever fixture of `PopulationMaterialGuardTest`: a probe
#: that discriminates on the counterfactual ONLY while its conditions arm
#: `source_data.declares_dangling`, so a `conditions` edit can weaken the
#: measured matrix (disarm) or keep it (a superset).
DANGLING_CASE = "dangling_parent_probe"
DANGLING_ARMED = "Some child rows carry dangling parent keys (orphans)."
DANGLING_DISARMED = "No dangling parent keys: every child row has a parent."
DANGLING_KEPT = DANGLING_ARMED + " The orphans are the C13 case."

#: ONE counterfactual condition naming every REQUIRED non-load attack kind
#: of the demo task: what `validate_population_coverage` demands of a
#: counterfactual WITHOUT literal rows (`counterfactual_untargeted`), so a
#: real `generate` failure on the POPULATION route is repaired by a
#: `conditions` edit alone — no row, seed or count is written by anyone.
TARGETING_CONDITION = (
    "Targets the inner_join, constants, no_dedup and no_null_default attack kinds."
)


def counterfactual_index(task) -> int:
    return next(i for i, p in enumerate(task.populations) if p.name is PopulationName.COUNTERFACTUAL)


def with_condition(task, index: int, condition: str):
    """`task` with `condition` appended to population `index`'s conditions."""
    return task.model_copy(update={"populations": tuple(
        p.model_copy(update={"conditions": tuple(p.conditions) + (condition,)}) if i == index else p
        for i, p in enumerate(task.populations)
    )})


def with_durable_attack_case(task, name: str):
    """Declare a fixture probe as reconstructable TaskIR attack evidence."""

    if any(case.name == name for case in task.attack_cases):
        return task
    probe = AttackCase(
        name=name,
        kind=AttackKind.CUSTOM,
        description=(
            "Fixture probe reconstructed by dangling_attack_runner from the "
            "counterfactual's public dangling-link condition."
        ),
        mutation="SELECT 1",
        expected_pass={PopulationName.COUNTERFACTUAL: False},
        required=False,
    )
    return task.model_copy(update={"attack_cases": task.attack_cases + (probe,)})


def untargeted_counterfactual(task):
    """`task` with the counterfactual's literal rows removed: the REAL
    `run_generate` fails on `counterfactual_untargeted` until a condition
    names every required attack kind (`TARGETING_CONDITION`)."""
    cf = counterfactual_index(task)
    return task.model_copy(update={"populations": tuple(
        p.model_copy(update={"literal_rows": {}}) if i == cf else p
        for i, p in enumerate(task.populations)
    )})


def write_rewards(workspace: Path, task_id: str, task, *, case: str, discriminates: bool) -> None:
    """A measured attack record at `task`'s hash: the probe lost reward on
    the counterfactual (discriminates) or kept it."""
    d = Path(workspace) / "tasks" / task_id / "attacks" / case
    d.mkdir(parents=True, exist_ok=True)
    (d / repair.ATTACK_REWARDS_FILENAME).write_text(
        json.dumps(
            {
                "case": case,
                "rewards": {"primary": 1.0, "counterfactual": 0.0 if discriminates else 1.0},
                "task_content_hash": task.content_hash(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def dangling_attack_runner(task_id: str, runs: list | None = None):
    """A fixture `attack` runner whose probe discriminates on the
    counterfactual only through the dangling-key lever of the
    counterfactual's conditions; records (workspace, discriminated)."""
    from elt_taskgen.generation.source_data import declares_dangling

    def attack(engine, task):
        discriminates = declares_dangling(task.population(PopulationName.COUNTERFACTUAL).conditions)
        if runs is not None:
            runs.append((Path(engine.workspace).resolve(), discriminates))
        write_rewards(engine.workspace, task_id, task, case=DANGLING_CASE, discriminates=discriminates)
        return StageOutcome(VERDICT_PASS, StagePayload(detail="attack ok"))

    return attack


def literal_rows_attack_runner(task_id: str, runs: list | None = None):
    """The literal-rows guard fixture of tests/test_repair_discrimination.py:
    the `inner_join` mutant is discriminated while a 'completed' order exists
    among the CURRENT counterfactual rows."""
    def attack(engine, task):
        rows = list(task.population(PopulationName.COUNTERFACTUAL).literal_rows.get("orders", ()))
        discriminates = any(r.get("status") == "completed" for r in rows)
        if runs is not None:
            runs.append((Path(engine.workspace).resolve(), discriminates))
        write_rewards(engine.workspace, task_id, task, case=discrimination_fixture.CASE, discriminates=discriminates)
        return StageOutcome(VERDICT_PASS, StagePayload(detail="attack ok"))

    return attack


def recording_runner(stage: str, invoked: list, inner=None):
    """A stage runner that records (stage, workspace) before answering PASS
    (or delegating to `inner`)."""
    def run(engine, task):
        invoked.append((stage, Path(engine.workspace).resolve()))
        if inner is not None:
            return inner(engine, task)
        return StageOutcome(VERDICT_PASS, StagePayload(detail=f"{stage} ok"))

    return run


def condition_edit(index: int, slot: int, old: str, new: str, rationale: str = "restate the condition") -> dict:
    return {
        "artifact": "task_ir.json", "op": "replace",
        "locator": f"populations.{index}.conditions.{slot}", "old": old, "new": new, "rationale": rationale,
    }


def targeting_edit(index: int) -> dict:
    """Append `TARGETING_CONDITION` to the counterfactual's first condition."""
    return {
        "artifact": "task_ir.json", "op": "insert", "locator": f"populations.{index}.conditions.0",
        "old": "", "new": " " + TARGETING_CONDITION,
        "rationale": "name the required attack kinds the counterfactual targets",
    }


def literal_rows_edit(locator: str, old: str, new: str) -> RepairEdit:
    return RepairEdit(op=RepairEditOp.REPLACE, locator=locator, old=old, new=new)


#: The literal-rows guard patches of tests/test_repair_discrimination.py: a
#: name change keeps the discrimination; a status change neuters the mutant.
KEEP_ROWS_EDIT = literal_rows_edit("populations.3.literal_rows.customers.0.customer_name", "C10", "Acme Holdings")
KEEP_ROWS_EDIT_2 = literal_rows_edit("populations.3.literal_rows.customers.1.customer_name", "C11", "Beta Corp")
NEUTER_ROWS_EDIT = literal_rows_edit("populations.3.literal_rows.orders.0.status", "completed", "cancelled")


def move_literal_rows(session, *edits: RepairEdit) -> RepairPatch:
    """Move `populations.*.literal_rows` on the HELD trial by the harness's
    own hand — the tool refuses the write as a `ForbiddenArgument` (SoT T4;
    OQ-21 stays declined), so the only way rows move under a session is
    outside the model's surface. Bumps the state epoch like a write does
    and returns the patch the harness would certify at submit."""
    patch = RepairPatch(
        route=POP, artifact="task_ir.json", edits=tuple(edits),
        rationale="the counterfactual carries placeholder identifiers", proposer_role=rp.ROLE_NAME,
    )
    target = session.trial / "tasks" / session.task_id / "task_ir.json"
    target.write_text(rp.apply_patch_text(target.read_text(encoding="utf-8"), patch), encoding="utf-8")
    session.state_epoch += 1
    return patch


class PopulationRouteTest(_BoundedFixture):
    """The POPULATION route of the bounded proposer (Phase 3 item 1) through
    the real session runner, the real proposer tools, the real (fixture-run)
    provider-free `certify` — its `attack` member behind
    `repair.certify.attack_enabled` — and the unchanged `attempt_patch`."""

    def population_config(self, *, attack_enabled: bool = False, **session_keys) -> Path:
        """An agents document whose `repair.routes_bounded` names population
        (the shipped key) and whose `repair.certify.attack_enabled` is
        `attack_enabled`. A pilot arm that turns the flag on also declares
        `max_oracle_bits: 12` and `wall_clock_s: 2700` in the proposer block
        (certify addendum §4.6)."""
        block = {
            "enabled": False, "max_turns": 8, "max_tool_calls": 16, "max_certify": 2,
            "max_oracle_bits": 12 if attack_enabled else 4, "max_usd": 0.80,
            "wall_clock_s": 2700 if attack_enabled else 900,
            "certify": {"attack_enabled": False, "deadline_s": 300},
        }
        block.update(session_keys)
        path = self.root / f"agents-population-{'attack' if attack_enabled else 'plain'}.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "roles": {
                        rp.ROLE_NAME: {
                            "provider": "anthropic", "model": "claude-opus-5",
                            "max_tokens": 8192, "effort": "high", "session": block,
                        }
                    },
                    "repair": {
                        "max_attempts": 2,
                        "routes_bounded": ["specification", "reference", "population"],
                        "nested_ceiling_usd": {"specification": 0.56, "population": 0.12, "reference": 0.05},
                        "max_usd_per_failure": 2.00,
                        "certify": {"attack_enabled": bool(attack_enabled)},
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    def population_engine(self, name: str, task, *, runners: dict) -> Engine:
        engine = Engine(self.root / name, stage_runners=runners, max_repair_rounds=0)
        self.addCleanup(engine.close)
        engine.register(task)
        return engine

    def population_failure(self, task, *, stage: str = "generate", error: str = "population coverage: planted") -> str:
        return rp.failure_detail(StagePayload(error=error), route=POP, task=task, stage=stage)

    @staticmethod
    def review_pass_row(engine: Engine, task) -> None:
        """A `review` PASS at the live hash: what the `attack` member's
        memo-served review is carried from."""
        engine.record_report(
            task, "review", VERDICT_PASS, cli_mod.ReviewPayload(findings=(), fatal_count=0, detail="ok")
        )

    def assert_clean(self, diag, task, route=POP) -> None:
        payload = PJ.serialize_for_transport(diag, task=task, route=route)
        PJ.assert_value_free(payload.encode("utf-8"), task=task, route=route)
        self.assertIsNone(re.search(r"\d", diag.render()), diag.render())

    @staticmethod
    def tool_results(transport) -> list[str]:
        return [
            block["content"]
            for _u, _h, payload in transport.calls[-1:]
            for message in payload["messages"]
            if message["role"] == "user" and isinstance(message["content"], list)
            for block in message["content"] if block.get("type") == "tool_result"
        ]

    # -- read_field ---------------------------------------------------------------

    def test_population_route_read_field_excludes_literal_rows(self):
        """SoT T3 `read_field` row; roadmap Table 7 (C5 not reopened, OQ-21
        stays declined): on the POPULATION route `read_field` resolves
        `ROUTE_IR_PATHS[POPULATION]` plus the public schema paths MINUS
        `populations.*.literal_rows`, on every route. Through the real
        session runner a read naming literal rows — at the table, the row
        or the cell — is a `ForbiddenArgument`: a POLICY_VIOLATION halt with
        a security event, no round, no commit, the workspace unchanged, and
        no row value in any message. The route's own conditions are
        acknowledged (the view already shows them), a declared scale is
        withheld (a number), a schema path projects identifiers, an
        off-route public field is a refusal code."""
        route_paths = tuple(p for p in rp.ROUTE_IR_PATHS[POP] if p not in V.LITERAL_ROWS_PATHS)
        self.assertEqual(V.readable_field_paths(POP), route_paths + V.PUBLIC_SCHEMA_PATHS)
        self.assertEqual(set(rp.ROUTE_IR_PATHS[POP]) - set(route_paths), set(V.LITERAL_ROWS_PATHS))
        self.assertEqual(route_paths, ("populations.*.conditions", "populations.*.conditions.*", "populations.*.scale.*"))
        for route in (SPEC, REF, POP):
            for path in (*V.LITERAL_ROWS_PATHS, "populations.3.literal_rows.orders", "populations.3.literal_rows.orders.0.status"):
                self.assertFalse(V.field_is_readable(path, route), (route, path))
                self.assertNotIn(path, V.readable_field_paths(route))
        self.assertTrue(rp._ir_path_allowed("populations.3.literal_rows.orders.0.status", POP))  # editable by a patch, never readable

        # (a) The halt, through the real runner: the second read names a
        # literal-row cell; the third (a whole table) is never reached.
        engine = self.population_engine("read-halt", self.task, runners={"generate": pass_runner("generate ok")})
        before = self.task_tree(engine)
        bodies = [
            body("read_view"),
            body("read_field", {"field": "populations.3.literal_rows.orders.0.status"}),
            body("read_field", {"field": "populations.3.literal_rows"}),
            *SUBMIT,
        ]
        with mock.patch.object(rp, "attempt_patch", side_effect=AssertionError("nothing to certify")) as certifier:
            outcome, provider, transport = self.bounded(
                engine, bodies, route=POP, stage="generate", failure=self.population_failure(self.task),
                config_path=self.population_config(),
            )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_HALTED)
        self.assertEqual(outcome.infrastructure, "ProviderProtocolError")  # SessionPolicyViolation's engine name
        attempt = outcome.record.attempts[0]
        self.assertEqual((attempt.error_type, attempt.accepted, attempt.rejection_code), ("SessionPolicyViolation", False, ""))
        self.assertEqual(certifier.call_count, 0)
        self.assertEqual(len(transport.calls), 2)  # read_view answered; the read that halted
        self.assertEqual(len(transport.responses), 2)  # the whole-table read and the submit never ran
        (record,) = self.session_records(engine)
        self.assertEqual(record["fault"]["terminal"], "POLICY_VIOLATION")
        (event,) = record["session"]["security_events"]
        self.assertEqual((event["code"], event["tool"], event["detail"]), ("forbidden_argument", "read_field", "literal_rows"))
        self.assertIsNone(rp.load_repair_adjudication(engine.workspace, self.task_id))
        self.assertEqual(self.task_tree(engine), before)
        texts = "\n".join(payload_texts(transport) + [json.dumps(record)])
        # No literal-row VALUE reached a message or the record (the
        # conditions the view shows name C10..C12 by design; the rows'
        # own order ids, statuses, column names and prices never travel —
        # the security event names the FIELD class `literal_rows`, nothing
        # of what it holds).
        for value in ("1101", "1201", '"status"', '"completed"', "customer_name", "unit_price", "Acme"):
            self.assertIsNone(re.search(rf"(?<![\w]){re.escape(value)}(?![\w])", texts), value)
        self.assertNotIn("literal_rows", "\n".join(payload_texts(transport)).replace(
            "populations.*.literal_rows is never readable", ""  # the tool's own description sentence
        ))
        self.assertIn("scale [literal constructed rows]", texts)  # the view's own label

        # (b) What the route may read, through the same runner: the
        # projections and nothing else, digit-free, then an abort.
        engine = self.population_engine("read-ok", self.task, runners={"generate": pass_runner("generate ok")})
        bodies = [
            body("read_field", {"field": "populations.1.conditions.1"}),
            body("read_field", {"field": "populations.1.scale.orders"}),
            body("read_field", {"field": "tables.1.columns"}),
            body("read_field", {"field": "solver_prompt"}),
            body("abort", {"reason_code": "cannot_repair"}),
        ]
        outcome, provider, transport = self.bounded(
            engine, bodies, route=POP, stage="generate", failure=self.population_failure(self.task),
            config_path=self.population_config(),
        )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_NEEDS_ADJUDICATION)
        self.assertEqual(outcome.record.attempts[0].terminal, "ABSTAINED")
        steps = [(s.tool, s.code) for s in outcome.record.attempts[0].steps if s.tool == "read_field"]
        self.assertEqual(steps, [
            ("read_field", "field_text_in_view"), ("read_field", "field_withheld"),
            ("read_field", "field_value"), ("read_field", "field_outside_allowlist"),
        ])
        results = self.tool_results(transport)
        self.assertEqual(len(results), 4)
        for text in results:
            self.assertRegex(text, r"^\[field\] ")
            self.assertIsNone(re.search(r"\d", text), text)
        self.assertIn("names=", results[2])
        self.assertTrue(set(results[2].split("names=")[1].split(",")) <= set(PJ.PublicIdentifierSet(self.task)))
        self.assertEqual(transport.responses, [])

    # -- the discrimination guard ----------------------------------------------------

    def test_population_patch_commits_only_when_discrimination_matrix_is_superset(self):
        """Roadmap Table 7 / §6 "behind the discrimination guard"; review
        finding 2-1: through the bounded proposer a POPULATION `conditions`
        patch over a MEASURED baseline re-runs `reference` and `attack` on
        the certifier's trial and commits ONLY when the re-measured matrix is
        a superset. Session 1 disarms the dangling-key lever the probe
        discriminated through: `attempt_patch` answers
        `DiscriminationWeakened`, the live tree is byte-unchanged and the
        next session's view carries the `discrimination_weakened` code (never
        the guard's sentence or the case name). Session 2 keeps the lever:
        the matrix is a superset and the patch commits through the unchanged
        `attempt_patch`. No re-measurement ever lands on the live tree."""
        cf = counterfactual_index(self.task)
        task = with_durable_attack_case(
            with_condition(self.task, cf, DANGLING_ARMED), DANGLING_CASE
        )
        slot = len(self.task.populations[cf].conditions)
        runs: list[tuple[Path, bool]] = []
        runners = {
            "generate": pass_runner("generate ok"), "reference": pass_runner("reference ok"),
            "review": pass_runner("review ok"), "attack": dangling_attack_runner(self.task_id, runs),
        }
        engine = self.population_engine("superset", task, runners=runners)
        live = engine.load_task(self.task_id)
        write_rewards(engine.workspace, self.task_id, live, case=DANGLING_CASE, discriminates=True)
        baseline = repair.discrimination_matrix(engine.workspace, self.task_id, task_content_hash=live.content_hash())
        self.assertEqual(baseline, {DANGLING_CASE: frozenset({"counterfactual"})})
        artifacts_before = repair.snapshot(engine.workspace, self.task_id)
        bodies = [
            body("read_view"), body("apply_edit_trial", condition_edit(cf, slot, DANGLING_ARMED, DANGLING_DISARMED)),
            body("check_scope"), *SUBMIT,
            body("apply_edit_trial", condition_edit(cf, slot, DANGLING_ARMED, DANGLING_KEPT)), *SUBMIT,
        ]
        with mock.patch.object(rp, "attempt_patch", wraps=rp.attempt_patch) as certifier:
            outcome, provider, transport = self.bounded(
                engine, bodies, route=POP, stage="attack",
                failure=self.population_failure(live, stage="attack", error="mutant kept full reward"),
                config_path=self.population_config(),
            )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        self.assertEqual(certifier.call_count, 2)
        self.assertEqual(transport.responses, [])
        first, second = outcome.record.attempts
        self.assertEqual(
            (first.accepted, first.error_type, first.rejection_code),
            (False, "DiscriminationWeakened", PJ.RejectionCode.DISCRIMINATION_WEAKENED.value),
        )
        self.assertEqual((second.accepted, second.rejection_code), (True, ""))
        # `attack` re-ran on a TRIAL each time (never the live tree): the
        # disarmed edit measured no discrimination, the kept one did.
        self.assertEqual([d for _ws, d in runs], [False, True])
        self.assertTrue(all(ws != Path(engine.workspace).resolve() for ws, _ in runs))
        committed = engine.load_task(self.task_id)
        self.assertEqual(committed.populations[cf].conditions[slot], DANGLING_KEPT)
        self.assertNotEqual(committed.content_hash(), live.content_hash())
        # The commit moved task_ir.json alone; the live attack record is
        # still the baseline at the OLD hash (the post-commit ladder
        # re-measures at the new one).
        changed = repair.diff_snapshots(artifacts_before, repair.snapshot(engine.workspace, self.task_id)).changed
        self.assertEqual(changed, frozenset({f"tasks/{self.task_id}/task_ir.json"}))
        self.assertEqual(
            repair.discrimination_matrix(engine.workspace, self.task_id, task_content_hash=committed.content_hash()), {}
        )
        self.assertEqual(
            repair.discrimination_matrix(engine.workspace, self.task_id, task_content_hash=live.content_hash()), baseline
        )
        views = initial_views(transport)
        self.assertEqual(len(views), 2)
        self.assertIn("[rejection] discrimination_weakened ok=false", views[1])
        for view in views:
            self.assertNotIn(DANGLING_CASE, view)
            self.assertNotIn("not at least as strong", view)
        records = sorted(self.session_records(engine), key=lambda r: r["session_index"])
        self.assertEqual(
            [(r["session_index"], r["submit"]["outcome"], r["submit"]["rejection_code"]) for r in records],
            [(1, "rejected", "discrimination_weakened"), (2, "committed", "")],
        )

    # -- regeneration ----------------------------------------------------------------

    def test_regenerate_after_population_patch_rederives_and_byte_compares(self):
        """Roadmap §6 "every `generate` re-derives and byte-compares the
        tree": a REAL `generate` failure (`counterfactual_untargeted`: the
        counterfactual has no literal rows and its conditions name no
        required attack kind) is repaired on the POPULATION route by a
        `conditions` edit alone — `check_cheap` reports the class before and
        green after, the in-session `certify` runs the real `generate` on a
        disposable copy, the unchanged `attempt_patch` commits — and the
        live `generate` then re-derives every population from the committed
        IR, and a second run finds all five byte-identical (`reused`) with no
        drift. No model wrote a row, a seed or a scale."""
        task = untargeted_counterfactual(self.task)
        cf = counterfactual_index(task)
        runners = {"generate": cli_mod.run_generate}
        engine = self.population_engine("regenerate", task, runners=runners)
        live = engine.load_task(self.task_id)
        failed = cli_mod.run_generate(engine, live)
        self.assertEqual((failed.verdict, failed.route), (VERDICT_FAIL, POP))
        self.assertIn("counterfactual population has no literal rows", failed.payload.error)
        self.assertFalse((engine.task_dir(self.task_id) / "populations").exists())
        failure = rp.failure_detail(failed.payload, route=POP, task=live, stage="generate")
        self.assertNotIn("literal rows", failure)  # the class travels as a code, never the sentence
        bodies = [
            body("check_cheap"), body("apply_edit_trial", targeting_edit(cf)), body("check_cheap"),
            body("certify"), *SUBMIT,
        ]
        with mock.patch.object(rp, "attempt_patch", wraps=rp.attempt_patch) as certifier, \
                mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as spawn:
            outcome, provider, transport = self.bounded(
                engine, bodies, route=POP, stage="generate", failure=failure,
                config_path=self.population_config(), certify_runners=runners,
            )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        self.assertEqual((certifier.call_count, spawn.call_count), (1, 1))
        self.assertEqual(transport.responses, [])
        steps = outcome.record.attempts[0].steps
        self.assertEqual([s.code for s in steps if s.tool == "certify"], [C.CODE_GREEN])
        cheap_before, cheap_after = [t for t in self.tool_results(transport) if t.startswith("[cheap]")]
        self.assertIn("counterfactual_untargeted=true", cheap_before)
        self.assertIn("population_ok=false", cheap_before)
        self.assertIn("counterfactual_untargeted=false", cheap_after)
        self.assertIn("population_ok=true", cheap_after)
        self.assertIn("witness_ok=true", cheap_after)
        for text in self.tool_results(transport):
            self.assertIsNone(re.search(r"\d", text), text)

        committed = engine.load_task(self.task_id)
        self.assertNotEqual(committed.content_hash(), live.content_hash())
        self.assertIn(TARGETING_CONDITION, committed.populations[cf].conditions[0])
        for before_pop, after_pop in zip(live.populations, committed.populations):
            self.assertEqual((before_pop.name, before_pop.scale, before_pop.seed, before_pop.literal_rows),
                             (after_pop.name, after_pop.scale, after_pop.seed, after_pop.literal_rows))
        self.assertEqual(committed.population(PopulationName.COUNTERFACTUAL).literal_rows, {})
        # The copy's `generate` side effects never reached the live tree.
        self.assertFalse((engine.task_dir(self.task_id) / "populations").exists())

        # The live `generate` re-derives from the committed IR ...
        first = cli_mod.run_generate(engine, committed)
        self.assertEqual(first.verdict, VERDICT_PASS)
        self.assertEqual(first.payload.data["built"], "counterfactual,development,primary,resampled,stress")
        self.assertEqual((first.payload.data["reused"], first.payload.data["verified"]), ("", "rederived"))
        populations = engine.task_dir(self.task_id) / "populations"
        tree = tree_hash(populations)
        self.assertTrue(tree)
        # ... and a second run re-derives again and finds every population
        # byte-identical: nothing rebuilt, no drift.
        second = cli_mod.run_generate(engine, engine.load_task(self.task_id))
        self.assertEqual(second.verdict, VERDICT_PASS)
        self.assertEqual(
            second.payload.data,
            {"built": "", "reused": "counterfactual,development,primary,resampled,stress", "verified": "rederived"},
        )
        self.assertEqual(tree_hash(populations), tree)
        self.assertIsNone(cli_mod._population_drift_failure(engine, engine.load_task(self.task_id)))
        # The counterfactual materialized EMPTY tables: the repair named the
        # kinds it targets; it minted no row.
        for table in ("customers", "orders", "order_items"):
            rows = populations / "counterfactual" / "rows" / f"{table}.jsonl"
            self.assertTrue(rows.is_file(), table)
            self.assertEqual(rows.read_text(encoding="utf-8").strip(), "", table)

    # -- realized row counts ---------------------------------------------------------

    def test_realized_row_count_never_appears_in_any_turn(self):
        """Roadmap §6: `realized_row_count` is private (source_data.py). A
        POPULATION session whose in-session `certify` runs the REAL
        `generate` on a disposable copy (materializing every hidden
        population at its realized count) and whose reads name a declared
        scale never carries a realized count of any hidden population — as a
        token of any message, of the session record or of a transcript —
        while the declared scales the route may edit ARE in the view (so the
        test discriminates) and every tool_result is digit-free."""
        from elt_taskgen.generation import source_data

        task = untargeted_counterfactual(self.task)
        cf = counterfactual_index(task)
        hidden = {
            (pop.name.value, table): source_data.realized_row_count(self.task_id, table, declared)
            for pop in task.populations
            if pop.name not in (PopulationName.DEVELOPMENT, PopulationName.COUNTERFACTUAL)
            for table, declared in pop.scale.items()
        }
        realized = sorted({str(n) for n in hidden.values()})
        declared = sorted({str(n) for pop in task.populations for n in pop.scale.values()})
        self.assertTrue(realized)
        self.assertTrue(set(realized).isdisjoint(declared))  # the band moved every hidden count
        runners = {"generate": cli_mod.run_generate}
        engine = self.population_engine("realized", task, runners=runners)
        live = engine.load_task(self.task_id)
        failed = cli_mod.run_generate(engine, live)
        self.assertEqual(failed.verdict, VERDICT_FAIL)
        bodies = [
            body("read_view"),
            body("read_field", {"field": "populations.1.scale.customers"}),
            body("read_field", {"field": "populations.1.conditions.0"}),
            body("read_field", {"field": "tables"}),
            body("check_cheap"), body("apply_edit_trial", targeting_edit(cf)), body("check_cheap"),
            body("certify"), *SUBMIT,
        ]
        with mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as spawn:
            outcome, provider, transport = self.bounded(
                engine, bodies, route=POP, stage="generate",
                failure=rp.failure_detail(failed.payload, route=POP, task=live, stage="generate"),
                config_path=self.population_config(max_turns=12), certify_runners=runners,
            )
        self.assertEqual(outcome.disposition, rp.DISPOSITION_COMMITTED)
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(transport.responses, [])
        steps = outcome.record.attempts[0].steps
        self.assertEqual([s.code for s in steps if s.tool == "certify"], [C.CODE_GREEN])
        self.assertEqual(
            [s.code for s in steps if s.tool == "read_field"],
            ["field_withheld", "field_text_in_view", "field_value"],
        )
        # The copy was torn down and nothing of it reached the live tree.
        self.assertFalse((engine.task_dir(self.task_id) / "populations").exists())

        texts = payload_texts(transport)
        (record,) = self.session_records(engine)
        transcripts = [p.read_text(encoding="utf-8") for p in sorted((engine.workspace / "transcripts").rglob("*.json"))]
        self.assertTrue(transcripts)
        everything = texts + [json.dumps(record, sort_keys=True)] + transcripts
        for text in everything:
            self.assertNotIn("realized", text.lower())
            for count in realized:
                self.assertIsNone(re.search(rf"(?<![\w.]){count}(?![\w.])", text), (count, text[:200]))
        # The declared scales the route edits ARE in the view.
        self.assertTrue(any("customers~1000" in t and "orders~20000" in t for t in texts))
        for text in self.tool_results(transport):
            self.assertRegex(text, r"^\[(trial|field|cheap|certify|rejection)\] ")
            self.assertIsNone(re.search(r"\d", text), text)

    # -- the `attack` member behind the flag ----------------------------------------

    def test_population_session_certify_reruns_reference_and_attack_when_literal_rows_move(self):
        """Certify addendum §3.1 / §4.6 (behind `repair.certify.attack_enabled`):
        with the flag on, a review PASS at the live hash and a measured
        baseline, a POPULATION session whose literal rows MOVED (by the
        harness's own hand: the tool refuses the write as a
        `ForbiddenArgument`) runs `reference` and `attack` on the disposable
        copy WHATEVER the failed stage (the proof stages), with the 1 200 s
        deadline and 6 oracle bits per call, and the `discrimination_weakened`
        bit is LIVE: a rows move that keeps the mutant discriminated is
        green; one that neuters it answers the bit `trial_phase` would
        reject on at submit. Every side effect lands on the copy: the held
        trial, the live ledger and the live tree are unchanged. Off the flag
        the Phase 1 stage set and its pinned-False bit are byte-identical."""
        invoked: list[tuple[str, Path]] = []
        runners = {
            "generate": recording_runner("generate", invoked),
            "reference": recording_runner("reference", invoked),
            "attack": recording_runner("attack", invoked, literal_rows_attack_runner(self.task_id)),
            "review": recording_runner("review", invoked),
        }
        engine = self.population_engine("literal-rows", self.task, runners=runners)
        live = engine.load_task(self.task_id)
        self.review_pass_row(engine, live)
        write_rewards(engine.workspace, self.task_id, live, case=discrimination_fixture.CASE, discriminates=True)
        artifacts_before = repair.snapshot(engine.workspace, self.task_id)
        ledger_before = ledger_rows(engine, self.task_id)
        registry = V.proposer_registry(attack_enabled=True)
        tool = V.proposer_tool("certify", attack_enabled=True)
        self.assertEqual((tool.cost.oracle_bits, tool.cost.wall_s, tool.cost.per_session),
                         (C.CERTIFY_ATTACK_ORACLE_BITS, C.CERTIFY_ATTACK_DEADLINE_S, C.MAX_CERTIFY_PER_SESSION))
        self.assertEqual((C.CERTIFY_ATTACK_ORACLE_BITS, C.CERTIFY_ATTACK_DEADLINE_S), (6, 1200.0))
        with V.ProposerSession.open(
            engine.workspace, live, POP, "generate", failure="stage failed",
            certify_runners=runners, attack_enabled=True, certify_deadline_s=C.CERTIFY_ATTACK_DEADLINE_S,
        ) as session:
            self.assertTrue(session.attack_member_available())
            self.assertEqual(session.certify_oracle_bits, 6)
            # Before any move: the failed stage's own member only.
            self.assertFalse(session.literal_rows_moved())
            self.assertEqual(session.certify_stages(), ("generate",))
            # The tool refuses the write (SoT T4); the harness moves the rows.
            with self.assertRaises(S.ForbiddenArgument) as forbidden:
                registry.dispatch(session.context(), "apply_edit_trial", {
                    "artifact": "task_ir.json", "op": "replace",
                    "locator": KEEP_ROWS_EDIT.locator, "old": KEEP_ROWS_EDIT.old, "new": KEEP_ROWS_EDIT.new,
                    "rationale": "rename the placeholder customer",
                })
            self.assertEqual(forbidden.exception.detail, "private_field")
            self.assertEqual(session.edits, [])
            move_literal_rows(session, KEEP_ROWS_EDIT)
            self.assertTrue(session.literal_rows_moved())
            self.assertTrue(session.population_moved())
            self.assertTrue(session.guard_armed())
            self.assertEqual(session.critic_views_changed(), ())  # rows move no critic view
            self.assertEqual(session.certify_stages(), ("generate", "reference", "attack"))
            self.assertFalse(session.certify_deferred())
            self.assertEqual(tool.permit(session.context(), {}), "")
            context = session.attack_context()
            self.assertEqual((context["guard"], context[C.ATTACK_CONTEXT_ROWS_KEY]), (True, True))
            self.assertEqual(context["matrix_before"], {discrimination_fixture.CASE: ["counterfactual"]})
            with mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as spawn:
                green = registry.dispatch(session.context(), "certify", {})
            self.assertEqual(spawn.call_count, 1)
            self.assertEqual(spawn.call_args.kwargs["deadline_s"], 1200.0)
            self.assertEqual([stage for stage, _ws in invoked], ["generate", "reference", "attack"])
            copies = {ws for _stage, ws in invoked}
            self.assertEqual(len(copies), 1)
            self.assertNotIn(session.trial.resolve(), copies)
            self.assertNotIn(Path(engine.workspace).resolve(), copies)
            self.assertEqual((green.code, green.ok), (C.CODE_GREEN, True))
            self.assertEqual(dict(green.flags), {"model_stages_deferred": False, "discrimination_weakened": False})
            self.assert_clean(green, live)
            self.assertEqual((session.certify_calls, session.oracle_bits_used, session.certified_epoch), (1, 6, 1))
            receipt = session.certify_receipts[-1]
            self.assertEqual((receipt.stages, receipt.worker), (("generate", "reference", "attack"), C.CERTIFY_WORKER_THREAD))
            self.assertFalse(receipt.copy_path.exists())
            # The held trial moved task_ir.json alone: no attack record at
            # its new hash (the live baseline it was copied with is at the
            # OLD hash), no answer key, no population tree from the copy's
            # stages, no ledger row.
            held = repair.diff_snapshots(session.before, repair.snapshot(session.trial, self.task_id)).changed
            self.assertEqual(held, frozenset({f"tasks/{self.task_id}/task_ir.json"}))
            self.assertEqual(
                repair.discrimination_matrix(
                    session.trial, self.task_id, task_content_hash=session.trial_task().content_hash()
                ),
                {},
            )
            self.assertFalse((session.trial / "tasks" / self.task_id / "populations").exists())
            trial_engine = Engine(session.trial, max_repair_rounds=0)
            self.addCleanup(trial_engine.close)
            self.assertIsNone(trial_engine.latest_report(self.task_id, "attack"))
            # A neutering move: the bit goes live (green stages, weakened matrix).
            invoked.clear()
            move_literal_rows(session, NEUTER_ROWS_EDIT)
            weakened = registry.dispatch(session.context(), "certify", {})
            self.assertEqual([stage for stage, _ws in invoked], ["generate", "reference", "attack"])
            self.assertEqual((weakened.code, weakened.ok), (C.CODE_GREEN, False))
            self.assertEqual(dict(weakened.flags), {"model_stages_deferred": False, "discrimination_weakened": True})
            self.assert_clean(weakened, live)
            self.assertEqual((session.certify_calls, session.oracle_bits_used), (2, 12))
        # The live tree and the live ledger are untouched by both certifies.
        self.assertEqual(repair.snapshot(engine.workspace, self.task_id), artifacts_before)
        self.assertEqual(ledger_rows(engine, self.task_id), ledger_before)

        # Off the flag: the Phase 1 stage set, 2 bits, 300 s, the bit pinned False.
        invoked.clear()
        with V.ProposerSession.open(
            engine.workspace, live, POP, "generate", failure="stage failed", certify_runners=runners,
        ) as off:
            self.assertFalse(off.attack_enabled)
            self.assertFalse(off.attack_member_available())
            move_literal_rows(off, KEEP_ROWS_EDIT)
            self.assertTrue(off.literal_rows_moved())
            self.assertEqual(off.certify_stages(), ("generate",))
            self.assertIsNone(off.attack_context())
            diag = V.proposer_registry().dispatch(off.context(), "certify", {})
            self.assertEqual([stage for stage, _ws in invoked], ["generate"])
            self.assertEqual((diag.code, diag.ok), (C.CODE_GREEN, True))
            self.assertEqual(dict(diag.flags), {"model_stages_deferred": False, "discrimination_weakened": False})
            self.assertEqual((off.certify_calls, off.oracle_bits_used), (1, 2))
            self.assertEqual(off.certify_receipts[-1].stages, ("generate",))
        phase1 = V.proposer_tool("certify")
        self.assertEqual((phase1.cost.oracle_bits, phase1.cost.wall_s), (2, 300.0))
        self.assertNotIn(C.CODE_REFUSED_REVIEW_VIEW_CHANGED, phase1.cost.no_cost_codes)
        with self.assertRaises(ValueError):
            PJ.project_certify(C.CODE_GREEN, model_stages_deferred=False, discrimination_weakened=True)

    def test_second_certify_after_attack_rerun_does_not_raise_scope_violation(self):
        """Certify addendum §3.1 "Copy discipline" (behind the flag): after a
        certify whose `attack` member re-ran on the disposable copy (writing
        `attacks/*/rewards.json`, the copy's ledger rows, the reference's
        outputs THERE), the held trial is byte-unchanged, so the session's
        next write, its `check_scope` and a second executed certify see
        nothing model-touched, and the accumulated patch commits through the
        unchanged `attempt_patch` behind the guard with no `ScopeViolation`.
        A `conditions` write after the rerun is applied and scope-clean too,
        and only then is the next certify refused — at no cost — because a
        critic view moved, never because of the copy's side effects."""
        invoked: list[tuple[str, Path]] = []
        runs: list[tuple[Path, bool]] = []
        runners = {
            "generate": recording_runner("generate", invoked),
            "reference": recording_runner("reference", invoked),
            "attack": recording_runner("attack", invoked, literal_rows_attack_runner(self.task_id, runs)),
            "review": recording_runner("review", invoked),
        }
        engine = self.population_engine("second-certify", self.task, runners=runners)
        live = engine.load_task(self.task_id)
        self.review_pass_row(engine, live)
        write_rewards(engine.workspace, self.task_id, live, case=discrimination_fixture.CASE, discriminates=True)
        registry = V.proposer_registry(attack_enabled=True)
        with V.ProposerSession.open(
            engine.workspace, live, POP, "generate", failure="stage failed",
            certify_runners=runners, attack_enabled=True, certify_deadline_s=C.CERTIFY_ATTACK_DEADLINE_S,
        ) as session:
            patch = move_literal_rows(session, KEEP_ROWS_EDIT)
            with mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as spawn:
                first = registry.dispatch(session.context(), "certify", {})
                self.assertEqual((first.code, first.ok, first.flags["discrimination_weakened"]), (C.CODE_GREEN, True, False))
                self.assertEqual([stage for stage, _ws in invoked], ["generate", "reference", "attack"])
                (copy_after_first,) = {ws for _stage, ws in invoked}
                self.assertFalse(copy_after_first.exists())
                # The attack rerun's record went to the copy, never the held
                # trial: at the trial's new hash there is no measurement.
                self.assertEqual(
                    repair.discrimination_matrix(
                        session.trial, self.task_id, task_content_hash=session.trial_task().content_hash()
                    ),
                    {},
                )
                # A second harness move after the rerun, then the scope check
                # `submit_patch` would run over the accumulated rows patch:
                # the diff is task_ir.json alone.
                second_patch = move_literal_rows(session, KEEP_ROWS_EDIT_2)
                rows_patch = patch.model_copy(update={"edits": (*patch.edits, *second_patch.edits)})
                after = repair.snapshot(session.trial, self.task_id)
                diff = rp.validate_scope(
                    session.trial, self.task_id, rows_patch, session.before, after, before_ir=session.before_ir
                )
                self.assertEqual(diff.changed, frozenset({f"tasks/{self.task_id}/task_ir.json"}))
                self.assertEqual(session.critic_views_changed(), ())
                self.assertEqual(V.proposer_tool("certify", attack_enabled=True).permit(session.context(), {}), "")
                invoked.clear()
                second = registry.dispatch(session.context(), "certify", {})
                self.assertEqual(spawn.call_count, 2)
                self.assertEqual((second.code, second.ok, second.flags["discrimination_weakened"]), (C.CODE_GREEN, True, False))
                self.assertEqual([stage for stage, _ws in invoked], ["generate", "reference", "attack"])
                (copy_after_second,) = {ws for _stage, ws in invoked}
                self.assertNotEqual(copy_after_first, copy_after_second)
                self.assertEqual(session.certify_receipts[-1].stages, ("generate", "reference", "attack"))
                self.assertEqual((session.certify_calls, session.oracle_bits_used), (2, 12))
                # A `conditions` write through the tool after the reruns:
                # applied, scope-clean (the copies' side effects are invisible).
                applied = registry.dispatch(session.context(), "apply_edit_trial", {
                    "artifact": "task_ir.json", "op": "replace", "locator": "populations.1.conditions.1",
                    "old": POPULATION_CONDITION, "new": POPULATION_CONDITION_REVISED,
                    "rationale": "state the cancelled-order condition precisely",
                })
                self.assertEqual((applied.code, applied.ok), ("applied", True))
                scope = registry.dispatch(session.context(), "check_scope", {})
                self.assertEqual((scope.code, scope.ok), ("scope_ok", True))
                # Two certifies consume max_certify, so the next request is a
                # per_tool_cap refusal even when its attack member changed.
                self.assertEqual(session.critic_views_changed(), ("population_adversary",))
                self.assertFalse(session.attack_member_admitted())
                self.assertEqual(session.certify_stages(), ("generate",))
                self.assertEqual(V.proposer_tool("certify", attack_enabled=True).permit(session.context(), {}), "")
                with self.assertRaises(S.OracleCapExceeded):
                    registry.dispatch(session.context(), "certify", {})
                self.assertEqual(spawn.call_count, 2)
                self.assertEqual((session.certify_calls, session.oracle_bits_used), (2, 12))
            held = repair.diff_snapshots(session.before, repair.snapshot(session.trial, self.task_id)).changed
            self.assertEqual(held, frozenset({f"tasks/{self.task_id}/task_ir.json"}))
            accumulated = RepairPatch(
                route=POP, artifact="task_ir.json", edits=(*rows_patch.edits, *session.edits),
                rationale="rows and a condition", proposer_role=rp.ROLE_NAME,
            )
        # The commit: the unchanged `attempt_patch` on the live engine, the
        # proof stages re-run on its own trial, the guard passes (the rows
        # keep the mutant discriminated), no `ScopeViolation` anywhere.
        runs.clear()
        committed = rp.attempt_patch(engine, live, "generate", accumulated)
        self.assertEqual(committed.population(PopulationName.COUNTERFACTUAL).literal_rows["customers"][0]["customer_name"], "Acme Holdings")
        self.assertEqual(committed.population(PopulationName.COUNTERFACTUAL).literal_rows["customers"][1]["customer_name"], "Beta Corp")
        self.assertEqual(committed.populations[1].conditions[1], POPULATION_CONDITION_REVISED)
        self.assertEqual([d for _ws, d in runs], [True])
        self.assertNotIn(Path(engine.workspace).resolve(), {ws for ws, _d in runs})

    def test_certify_attack_member_refused_when_critic_view_changed(self):
        """Certify addendum §3.1 "Refusals" (3), F3b, as reconciled by Phase
        3 review finding 1-1 (addendum O3): with the flag on, a POPULATION
        `conditions` edit moves exactly the adversary's view, so the `attack`
        MEMBER cannot be memo-served at the new hash and is DROPPED
        (`ProposerSession.attack_member_admitted` false): the Phase 1
        members still run on the copy — a paid call at the flag's 6 bits —
        the projection answers `model_stages_deferred=True` with the bit
        pinned False, and `attack` is never invoked; through the tool's
        `permit` hook (no refusal) and through the bounded runner (an
        executed turn). The CALL is refused as
        `certify_refused_review_view_changed` (0 bits, no worker) only when
        no other member remains. Off the flag the same edit certifies the
        Phase 1 stage set and the code does not exist in the tool's
        declaration; a literal-rows move alone moves no view and keeps the
        member admitted. Turning the flag on therefore never removes a
        capability the flag-off session had."""
        cf = counterfactual_index(self.task)
        task = with_durable_attack_case(
            with_condition(self.task, cf, DANGLING_ARMED), DANGLING_CASE
        )
        slot = len(self.task.populations[cf].conditions)
        invoked: list[tuple[str, Path]] = []
        runners = {
            "generate": recording_runner("generate", invoked),
            "reference": recording_runner("reference", invoked),
            "attack": recording_runner("attack", invoked, dangling_attack_runner(self.task_id)),
            "review": recording_runner("review", invoked),
        }
        engine = self.population_engine("view-changed", task, runners=runners)
        live = engine.load_task(self.task_id)
        self.review_pass_row(engine, live)
        write_rewards(engine.workspace, self.task_id, live, case=DANGLING_CASE, discriminates=True)
        registry = V.proposer_registry(attack_enabled=True)
        tool = V.proposer_tool("certify", attack_enabled=True)
        self.assertIn(C.CODE_REFUSED_REVIEW_VIEW_CHANGED, tool.cost.no_cost_codes)
        self.assertEqual(C.ATTACK_NO_COST_REFUSAL_CODES - C.NO_COST_REFUSAL_CODES, {C.CODE_REFUSED_REVIEW_VIEW_CHANGED})
        edit = condition_edit(cf, slot, DANGLING_ARMED, DANGLING_KEPT)
        with V.ProposerSession.open(
            engine.workspace, live, POP, "attack", failure="stage failed",
            certify_runners=runners, attack_enabled=True,
        ) as session:
            self.assertTrue(session.attack_member_available())
            applied = registry.dispatch(session.context(), "apply_edit_trial", edit)
            self.assertEqual((applied.code, applied.ok), ("applied", True))
            self.assertEqual(session.critic_views_changed(), ("population_adversary",))
            self.assertEqual(C.critic_views_changed(live, session.trial_task()), ("population_adversary",))
            self.assertTrue(session.population_moved())
            self.assertFalse(session.literal_rows_moved())
            self.assertTrue(session.guard_armed())  # a measured baseline exists
            # The MEMBER is refused, not the call: dropped from the set, the
            # Phase 1 members remain, the deferral bit is raised, no context.
            self.assertTrue(session.attack_member_available())
            self.assertFalse(session.attack_member_admitted())
            self.assertTrue(session.attack_member_dropped_for_view())
            self.assertEqual(session.certify_stages(), ("generate", "reference"))
            self.assertTrue(session.certify_deferred())
            self.assertIsNone(session.attack_context())
            self.assertEqual(tool.permit(session.context(), {}), "")
            with mock.patch.object(C, "certify_disposable_copy", wraps=C.certify_disposable_copy) as spawn:
                green = registry.dispatch(session.context(), "certify", {})
            self.assertEqual(spawn.call_count, 1)
            self.assertIsNone(spawn.call_args.kwargs["attack"])
            self.assertEqual([stage for stage, _ws in invoked], ["generate", "reference"])
            self.assertEqual((green.source, green.code, green.ok), (PJ.DiagnosticSource.CERTIFY, C.CODE_GREEN, True))
            self.assertEqual(dict(green.flags), {"model_stages_deferred": True, "discrimination_weakened": False})
            self.assert_clean(green, live)
            # A paid call at the flag's declared price (the tool's cost is
            # the session's, whichever members ran), never a refusal.
            self.assertEqual((session.certify_calls, session.certify_refusals, session.oracle_bits_used), (1, 0, 6))
            self.assertEqual(session.certify_receipts[-1].stages, ("generate", "reference"))
            # Through the bounded runner: an EXECUTED tool turn under the
            # green code, the declared bits charged, then the submit terminal.
            invoked.clear()
            session.state_epoch += 1  # a fresh epoch: the unchanged-trial refusal does not apply
            limits = S.SessionLimits.declare(max_turns=6, max_tool_calls=8, max_certify=2, max_oracle_bits=12)
            script = [[BS.tool_use("certify", {}, "c0")], [BS.tool_use(V.SUBMIT_TOOL, {}, "s0")]]
            result = S.run_bounded_session(
                rp.ROLE_NAME, session.view, V.PROPOSER_TOOLS_ATTACK,
                V.proposer_policy(limits, attack_enabled=True), limits,
                provider=BS.ScriptedProvider(script), ctx=session.context(),
                worker=V.proposer_validator_worker(),
            )
            self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
            certify_turns = [t for t in result.turns if t.kind in ("tool", "refused") and t.tool_name == "certify"]
            self.assertEqual([(t.kind, t.outcome_code, t.refused) for t in certify_turns],
                             [("tool", C.CODE_GREEN, False)])
            self.assertEqual(result.oracle_bits_used, 6)
            self.assertEqual([stage for stage, _ws in invoked], ["generate", "reference"])
            self.assertEqual((session.certify_calls, session.oracle_bits_used), (2, 12))
            # The CALL-level refusal survives for the one case it names: no
            # member but the dropped `attack` (an empty Phase 1 set), at 0
            # bits with no worker — and the Phase 1 tool names it never.
            with mock.patch.object(V.ProposerSession, "certify_stages", return_value=()):
                self.assertEqual(tool.permit(session.context(), {}), C.CODE_REFUSED_REVIEW_VIEW_CHANGED)
                with mock.patch.object(C, "certify_disposable_copy", side_effect=AssertionError("worker spawned")):
                    refused = registry.dispatch(session.context(), "certify", {})
                self.assertEqual(V.proposer_tool("certify").permit(session.context(), {}), C.CODE_REFUSED_NO_PROVIDER_FREE_STAGE)
            self.assertEqual((refused.source, refused.code, refused.ok), (PJ.DiagnosticSource.CERTIFY, C.CODE_REFUSED_REVIEW_VIEW_CHANGED, False))
            self.assertEqual(dict(refused.flags), {"model_stages_deferred": True, "discrimination_weakened": False})
            self.assert_clean(refused, live)
            self.assertEqual((session.certify_calls, session.certify_refusals, session.oracle_bits_used), (2, 1, 12))

        # Off the flag: the Phase 1 stage set runs (no `attack`), the bit is
        # pinned False and the refusal code is not declared.
        invoked.clear()
        with V.ProposerSession.open(
            engine.workspace, live, POP, "attack", failure="stage failed", certify_runners=runners,
        ) as off:
            registry_off = V.proposer_registry()
            self.assertEqual(registry_off.dispatch(off.context(), "apply_edit_trial", edit).code, "applied")
            self.assertEqual(off.critic_views_changed(), ("population_adversary",))
            self.assertEqual(off.certify_stages(), ("generate", "reference"))
            self.assertTrue(off.certify_deferred())
            self.assertEqual(V.proposer_tool("certify").permit(off.context(), {}), "")
            diag = registry_off.dispatch(off.context(), "certify", {})
            self.assertEqual([stage for stage, _ws in invoked], ["generate", "reference"])
            self.assertEqual((diag.code, diag.ok), (C.CODE_GREEN, True))
            self.assertEqual(dict(diag.flags), {"model_stages_deferred": True, "discrimination_weakened": False})
            self.assertEqual((off.certify_calls, off.oracle_bits_used), (1, 2))
        self.assertNotIn(C.CODE_REFUSED_REVIEW_VIEW_CHANGED, V.proposer_tool("certify").cost.no_cost_codes)
        self.assertNotIn(C.CODE_REFUSED_REVIEW_VIEW_CHANGED, PJ.CERTIFY_CODES)
        self.assertIn(C.CODE_REFUSED_REVIEW_VIEW_CHANGED, PJ.CERTIFY_ATTACK_CODES)

        # A literal-rows move alone: no view moved, the member stays admitted.
        with V.ProposerSession.open(
            engine.workspace, live, POP, "attack", failure="stage failed",
            certify_runners=runners, attack_enabled=True,
        ) as rows:
            move_literal_rows(rows, KEEP_ROWS_EDIT)
            self.assertEqual(rows.critic_views_changed(), ())
            self.assertIn(C.CERTIFY_ATTACK_STAGE, rows.certify_stages())
            self.assertEqual(tool.permit(rows.context(), {}), "")

    # -- witness_problem_codes ---------------------------------------------------------

    def test_witness_problem_codes_name_only_public_shapes(self):
        """Roadmap "New interfaces": `witness_problem_codes(task)` wraps
        `mart_plan._witness_problems` over the witnesses the counterfactual's
        conditions declare and answers CODES from the closed vocabulary
        `projection.WITNESS_PROBLEM_CODES`, in its order — never the
        sentence, a witness label, a table or a `FactRoles` attribute; the
        demo's hand-written counterfactual declares no witness; prose outside
        the witness vocabulary declares none; and `check_population_cheap`
        carries the codes as flags through the gatekeeper, digit-free, with
        no name."""
        from elt_taskgen.generation import mart_plan
        from elt_taskgen.generation.populations import _WITNESS_PROSE

        self.assertEqual(rp.witness_problem_codes(self.task), ())
        self.assertEqual(rp._declared_witnesses(self.task), ((), "", "", ""))
        cf = counterfactual_index(self.task)

        def declaring(*conditions: str):
            return self.task.model_copy(update={"populations": tuple(
                p.model_copy(update={"conditions": tuple(conditions)}) if i == cf else p
                for i, p in enumerate(self.task.populations)
            )})

        # A fan-out witness declared over a one-hop shape (no child): the
        # rows control no child key and there is no second hop.
        one_hop = declaring(
            "Anchor rows live in customers; their linked rows live in orders.",
            _WITNESS_PROSE[mart_plan.WITNESS_SAME_CHILD],
        )
        # A bridge witness declared over a shape that joins nothing.
        no_bridge = declaring(
            "Anchor rows live in customers; their linked rows live in (none).",
            _WITNESS_PROSE[mart_plan.WITNESS_DUPLICATE],
        )
        # A witness the rows CAN construct: no problem.
        constructible = declaring(
            "Anchor rows live in customers; their linked rows live in orders.",
            _WITNESS_PROSE[mart_plan.WITNESS_CHILDLESS],
        )
        # Prose outside the vocabulary declares no witness at all.
        foreign = declaring("row Z: a witness this library never named.")
        expected = {
            "one_hop": ("witness_role_missing", "witness_no_second_hop"),
            "no_bridge": ("witness_no_bridge", "witness_role_missing"),
            "constructible": (),
            "foreign": (),
        }
        for label, task in (("one_hop", one_hop), ("no_bridge", no_bridge), ("constructible", constructible), ("foreign", foreign)):
            with self.subTest(label=label):
                codes = rp.witness_problem_codes(task)
                self.assertEqual(codes, expected[label])
                self.assertTrue(set(codes) <= set(PJ.WITNESS_PROBLEM_CODES))
                self.assertEqual(codes, tuple(c for c in PJ.WITNESS_PROBLEM_CODES if c in codes))  # vocabulary order
                for code in codes:
                    self.assertRegex(code, r"^[a-z][a-z0-9_]{0,127}$")
                witnesses, anchor, bridge, child = rp._declared_witnesses(task)
                raw = mart_plan._witness_problems(
                    witnesses, rp._roles_from_counterfactual(task, anchor, bridge, child), fact=bridge, child=child,
                )
                self.assertEqual(bool(raw), bool(codes))
                joined = " ".join(raw)
                for code in codes:
                    self.assertNotIn(code, joined)
                # The wrapper answers a code where the sentence names the
                # witness LABEL, the table or the FactRoles attribute.
                if label == "one_hop":
                    self.assertIn(mart_plan.WITNESS_SAME_CHILD, joined)
                    self.assertIn("FactRoles.child_key", joined)
                    self.assertIn("SECOND hop", joined)
                if label == "no_bridge":
                    self.assertIn("needs a bridge table", joined)
                for code in codes:
                    for private in (mart_plan.WITNESS_SAME_CHILD, mart_plan.WITNESS_DUPLICATE, "FactRoles", "customers", "orders"):
                        self.assertNotIn(private, code)
                diag = rp.check_population_cheap(self.root, task)
                self.assertEqual(diag.source, PJ.DiagnosticSource.CHEAP)
                self.assertEqual(diag.names, ())
                self.assertEqual(diag.subject, "")
                self.assertEqual({k: v for k, v in diag.flags.items() if k in PJ.WITNESS_PROBLEM_CODES},
                                 {c: c in codes for c in PJ.WITNESS_PROBLEM_CODES})
                self.assertEqual(diag.flags["witness_ok"], not codes)
                self.assertEqual(diag.flags["population_ok"], not codes)  # the coverage half is green on the demo
                self.assertEqual(diag.code, codes[0] if codes else "cheap_green")
                self.assertEqual(diag.ok, not codes)
                self.assert_clean(diag, task)
                rendered = diag.render()
                self.assertNotIn("FactRoles", rendered)
                self.assertNotIn(mart_plan.WITNESS_SAME_CHILD, rendered)
                # Nothing on disk is read: the IR is the whole input.
                self.assertEqual(rp.check_population_cheap(self.root / "nowhere", task), diag)
        # The vocabulary is what SoT T3 / the roadmap name, and the cheap
        # gate's own vocabulary carries it.
        self.assertEqual(PJ.WITNESS_PROBLEM_CODES, ("witness_unknown", "witness_no_bridge", "witness_role_missing", "witness_no_second_hop"))
        self.assertEqual(PJ.POPULATION_CHEAP_CODES, ("missing_population", "scale_drift", "no_scale_no_rows", "counterfactual_untargeted"))
        for code in (*PJ.WITNESS_PROBLEM_CODES, *PJ.POPULATION_CHEAP_CODES):
            self.assertIn(code, PJ.CHEAP_CODES)

    # -- the view's private-material gate (review findings 0-0, 0-2) --------------------

    @staticmethod
    def _coverage_degraded_task(base, domain_size: int, task_id: str):
        """tests/test_fix_F.py's `_enum_chain` shape under the real coverage
        budget: a STRESS population whose declared child scale and coverage
        notes derive from `realized_row_count` of its parent (the generator's
        `coverage_budget_scale`)."""
        from elt_taskgen.generation import populations as pops
        from elt_taskgen.models import Backend, BackendAssignment, ColumnSpec, ColumnType, Relationship, TableSpec

        domain = tuple(f"v{i}" for i in range(domain_size))
        parents = TableSpec(
            name="parents",
            columns=(ColumnSpec(name="p_id", type=ColumnType.INTEGER), ColumnSpec(name="p_name", type=ColumnType.TEXT)),
            primary_key=("p_id",),
        )
        facts = TableSpec(
            name="facts",
            columns=(
                ColumnSpec(name="f_id", type=ColumnType.INTEGER), ColumnSpec(name="p_id", type=ColumnType.INTEGER),
                ColumnSpec(name="kind", type=ColumnType.TEXT, enum_values=domain),
                ColumnSpec(name="amount", type=ColumnType.INTEGER),
            ),
            primary_key=("f_id",),
        )
        grand = TableSpec(
            name="grand",
            columns=(
                ColumnSpec(name="g_id", type=ColumnType.INTEGER), ColumnSpec(name="f_id", type=ColumnType.INTEGER),
                ColumnSpec(name="v", type=ColumnType.INTEGER),
            ),
            primary_key=("g_id",),
        )
        tables = (parents, facts, grand)
        rels = (
            Relationship(child_table="facts", child_columns=("p_id",), parent_table="parents", parent_columns=("p_id",)),
            Relationship(child_table="grand", child_columns=("f_id",), parent_table="facts", parent_columns=("f_id",)),
        )
        specs = tuple(pops.default_populations(
            task_id, {"parents": 60, "facts": 240, "grand": 240}, tables=tables, relationships=rels,
        ))
        return base.model_copy(update={
            "task_id": task_id, "tables": tables, "relationships": rels,
            "backends": tuple(BackendAssignment(table=t.name, backend=Backend.MONGODB) for t in tables),
            "populations": specs, "attack_cases": (),
        })

    def test_population_view_withholds_hidden_realized_counts_and_literal_values(self):
        """Phase 3 review findings 0-0 and 0-2: the POPULATION view is the one
        place the route shows hidden-population prose, and a condition line
        is a producer string nothing projected. The view now passes every
        hidden population's conditions through the projector's gate
        (`projection.population_condition_private_shape`; `repair_proposer.
        withheld_population_conditions`): a standalone number that is not
        the population's own declared scale, a count vector, a key tuple, a
        digit run equal to a private scalar or a hidden REALIZED count, a
        path, a secret or a measured value prints as
        `[withheld: private material]`. On a coverage-degraded generated
        task the generator's `COVERAGE SHORTFALL` notes — `budgeted for N
        rows` with N = the realized parent count, or that count times the
        public enum domain — are withheld and no hidden realized count is
        a token of the view; on the demo the hand-written counterfactual's
        `C11: ... 1*10 + 2*15 + 1*5 = 45` / `C12: ... worth 100 -> (0, 0)`
        lines (literal-row values and the counterfactual gold) are withheld
        while the digit-free `C10/C11/C12` label, every DEVELOPMENT note
        (solver-visible, OQ-23 option C) and every declared scale stay.
        A withheld slot is never a substring oracle: `read_field` answers
        `field_withheld`, a replace / delete anchor answers ONE code for a
        hit and a miss without reading a byte, `insert` stays open."""
        from elt_taskgen.generation import source_data

        # (a) The demo's hand-written counterfactual.
        cf = counterfactual_index(self.task)
        withheld = rp.withheld_population_conditions(self.task)
        self.assertEqual(withheld, frozenset({(cf, 1), (cf, 2), (cf, 3)}))
        view = rp.view_for_route(self.task, POP, self.population_failure(self.task))
        self.assertEqual(view.count(f"condition: {rp.CONDITION_WITHHELD}"), 3)
        for private in ("= 45", "1*10", "2*15", "worth 100", "(0, 0)", "COUNT would be 3"):
            self.assertNotIn(private, view, private)
        self.assertIn("exactly the C10/C11/C12 case.", view)
        self.assertIn("scale [literal constructed rows]", view)
        for pop in self.task.populations:
            for table, scale in pop.scale.items():
                self.assertIn(f"{table}~{int(scale)}", view)
            if pop.name is PopulationName.DEVELOPMENT:
                for condition in pop.conditions:
                    self.assertIn(f"condition: {condition}", view)
        self.assertEqual(rp.condition_path_is_withheld(f"populations.{cf}.conditions.1", withheld), True)
        self.assertEqual(rp.condition_path_is_withheld(f"populations.{cf}.conditions.0", withheld), False)
        self.assertEqual(rp.condition_path_is_withheld(f"populations.{cf}.conditions", withheld), True)
        self.assertEqual(rp.condition_path_is_withheld("populations.1.conditions", withheld), False)
        self.assertEqual(rp.condition_path_is_withheld(f"populations.{cf}.literal_rows", withheld), False)
        # The adversary's council view is not this function: byte-identical
        # to the verbatim rendering (its digest is admission evidence).
        self.assertEqual(
            council._population_summary(self.task, with_conditions=True),
            council._population_summary(self.task, with_conditions=True, condition_text=None),
        )
        self.assertIn("= 45", council.render_view(CouncilRole.POPULATION_ADVERSARY, self.task))

        # (b) The gate's rules, per population class.
        gate = PJ.population_condition_private_shape
        self.assertIsNone(gate("Approximately 1000 customers.", task=self.task, population=PopulationName.PRIMARY))
        self.assertEqual(gate("budgeted for 613 rows, short of whale headroom", task=self.task, population=PopulationName.STRESS), PJ.CONDITION_STANDALONE_NUMBER)
        self.assertEqual(gate("C10: customer with no orders -> (0, 0).", task=self.task, population=PopulationName.COUNTERFACTUAL), "key_tuple")
        self.assertEqual(gate("stage-1 counts customers=4711", task=self.task, population=PopulationName.PRIMARY), "count_vector")
        self.assertEqual(gate("rows under /Users/x/runs/live/answer_key", task=self.task, population=PopulationName.DEVELOPMENT), "path")
        self.assertEqual(gate("inner_join must lose reward on primary, got 1.0", task=self.task, population=PopulationName.DEVELOPMENT), "measured_value")
        self.assertIsNone(gate("Tiny C1/C2 debug data: exactly two customers, 8 items.", task=self.task, population=PopulationName.DEVELOPMENT))
        self.assertIsNone(gate("Literal constructed rows only — exactly the C10/C11/C12 case.", task=self.task, population=PopulationName.COUNTERFACTUAL))

        # (c) A coverage-degraded generated task: the realized hidden counts
        # (and their enum-domain products) never enter the view.
        for domain_size, task_id in ((2000, "t__cov2"), (80, "t__cov")):
            with self.subTest(domain=domain_size):
                task = self._coverage_degraded_task(self.task, domain_size, task_id)
                stress = task.population(PopulationName.STRESS)
                notes = [c for c in stress.conditions if "COVERAGE SHORTFALL" in c]
                self.assertTrue(notes)
                realized = {
                    (pop.name.value, table): source_data.realized_row_count(task_id, table, declared)
                    for pop in task.populations
                    if pop.name not in (PopulationName.DEVELOPMENT, PopulationName.COUNTERFACTUAL)
                    for table, declared in pop.scale.items()
                }
                stress_parents = realized[(PopulationName.STRESS.value, "parents")]
                self.assertTrue(any(str(n) in note for n in realized.values() for note in notes)
                                or any(str(stress_parents * domain_size) in note for note in notes))
                self.assertEqual(PJ.hidden_realized_counts(task), frozenset(str(n) for n in realized.values()))
                view = rp.view_for_route(task, POP, self.population_failure(task))
                for note in notes:
                    self.assertNotIn(note, view)
                self.assertNotIn("budgeted for", view.split("- population stress")[1])
                self.assertNotIn("needs at least", view.split("- population stress")[1])
                for count in realized.values():
                    self.assertIsNone(re.search(rf"(?<![\w.]){count}(?![\w.])", view), count)
                # The STRESS conditions block carries no product of a realized
                # count and the public domain either (the declared scale line
                # `facts~N` is the generator's own choice of N — a
                # `coverage_budget_scale` residual the generation owner owns,
                # outside this view's gate).
                conditions_block = "\n".join(line for line in view.splitlines() if "condition:" in line)
                self.assertNotIn(str(stress_parents * domain_size), conditions_block)
                # DEVELOPMENT's own shortfall notes stay (option C).
                dev_notes = [c for c in task.population(PopulationName.DEVELOPMENT).conditions if "COVERAGE SHORTFALL" in c]
                for note in dev_notes:
                    self.assertIn(f"condition: {note}", view)
                # The tripwire over private SQL still guards the whole view.
                leaky = task.model_copy(update={"populations": tuple(
                    p.model_copy(update={"conditions": tuple(p.conditions) + (
                        "prose contains " + " ".join(next(iter(task.reference.sql_by_mart.values())).split()[:12]),
                    )}) if p.name is PopulationName.PRIMARY else p
                    for p in task.populations
                )})
                with self.assertRaises(PJ.DiagnosticTripwire):
                    rp.view_for_route(leaky, POP, self.population_failure(leaky))

        # (d) Through the session: no oracle over a withheld slot.
        engine = self.population_engine("withheld", self.task, runners={"generate": pass_runner("generate ok")})
        live = engine.load_task(self.task_id)
        registry = V.proposer_registry()
        with V.ProposerSession.open(engine.workspace, live, POP, "generate", failure=self.population_failure(live)) as session:
            self.assertEqual(session.withheld_conditions, withheld)
            self.assertNotIn("= 45", session.view)
            read = registry.dispatch(session.context(), "read_field", {"field": f"populations.{cf}.conditions.2"})
            self.assertEqual((read.code, read.ok), ("field_withheld", False))
            self.assertEqual(registry.dispatch(session.context(), "read_field", {"field": f"populations.{cf}.conditions"}).code, "field_withheld")
            self.assertEqual(registry.dispatch(session.context(), "read_field", {"field": f"populations.{cf}.conditions.0"}).code, "field_text_in_view")
            self.assertEqual(registry.dispatch(session.context(), "read_field", {"field": "populations.1.conditions.1"}).code, "field_text_in_view")
            before = session.trial_text()
            secret = live.populations[cf].conditions[2]
            with mock.patch.object(rp, "apply_patch_text", side_effect=AssertionError("anchor applied")):
                for op in ("replace", "delete"):
                    for old in (secret[: len(secret) // 2], "zzz-never-in-any-condition"):
                        diag = registry.dispatch(session.context(), "apply_edit_trial", {
                            "artifact": "task_ir.json", "op": op, "locator": f"populations.{cf}.conditions.2",
                            "old": old, "new": "" if op == "delete" else "x", "rationale": "a probe",
                        })
                        self.assertEqual((diag.source.value, diag.code), ("rejection", "patch_anchor_not_found"))
                        self.assert_clean(diag, live)
            self.assertEqual((session.trial_text(), session.state_epoch, session.edits), (before, 0, []))
            inserted = registry.dispatch(session.context(), "apply_edit_trial", {
                "artifact": "task_ir.json", "op": "insert", "locator": f"populations.{cf}.conditions.2",
                "old": "", "new": " Appended by the session.", "rationale": "append",
            })
            self.assertEqual((inserted.code, session.state_epoch), ("applied", 1))
            visible = registry.dispatch(session.context(), "apply_edit_trial", condition_edit(
                cf, 0, "exactly the C10/C11/C12 case.", "exactly the C10/C11/C12 case (three customers)."))
            self.assertEqual(visible.code, "applied")
        # Nothing withheld reaches a message or a session record through the runner.
        bodies = [body("read_view"), body("read_field", {"field": f"populations.{cf}.conditions.2"}), body("abort", {"reason_code": "cannot_repair"})]
        outcome, _provider, transport = self.bounded(
            engine, bodies, route=POP, stage="generate", failure=self.population_failure(live),
            config_path=self.population_config(),
        )
        self.assertEqual(outcome.record.status, rp.STATUS_NEEDS_ADJUDICATION)
        texts = "\n".join(payload_texts(transport) + [json.dumps(r) for r in self.session_records(engine)])
        for private in ("= 45", "1*10", "worth 100", "(0, 0)"):
            self.assertNotIn(private, texts, private)
        self.assertIn(rp.CONDITION_WITHHELD, texts)

    def test_check_population_cheap_is_invariant_under_literal_row_content(self):
        """Phase 3 review finding 0-1: `witness_problem_codes` derives its
        `FactRoles` from the PUBLIC schema and the declared anchor / bridge /
        child only — never from the hidden `literal_rows` — so the
        `check_population_cheap` Diagnostic a POPULATION session reads (and
        the bytes the projector delivers) are invariant under the CONTENT of
        the hidden rows for a fixed IR-minus-rows, whatever anchor line the
        model's `conditions` edit points at; the roles name schema columns
        and declared enum values, never a row value (OQ-21 stays declined)."""
        from elt_taskgen.generation import mart_plan
        from elt_taskgen.generation.populations import _WITNESS_PROSE

        cf = counterfactual_index(self.task)
        anchor = "Anchor rows live in customers; their linked rows live in orders."

        def with_conditions(task, conditions, literal=None):
            update = {"conditions": tuple(conditions)}
            if literal is not None:
                update["literal_rows"] = literal
            return task.model_copy(update={"populations": tuple(
                p.model_copy(update=update) if i == cf else p for i, p in enumerate(task.populations)
            )})

        rows = {t: tuple(dict(r) for r in v) for t, v in self.task.populations[cf].literal_rows.items()}
        # Schema-valid variants of the hidden rows: other values in the enum
        # domain, other names, one item line fewer, a nullable key blanked.
        variants = {
            "real": rows,
            "statuses_flipped": {**rows, "orders": tuple(
                {**r, "status": "cancelled" if r.get("status") == "completed" else "completed"} for r in rows["orders"]
            )},
            "names_changed": {**rows, "customers": tuple({**r, "customer_name": f"Zed {i}"} for i, r in enumerate(rows["customers"]))},
            "one_item_fewer": {**rows, "order_items": rows["order_items"][:-1]},
        }
        for witness in (mart_plan.WITNESS_ALL_FAIL, mart_plan.WITNESS_SECOND_PERIOD, mart_plan.WITNESS_TIE, mart_plan.WITNESS_SAME_CHILD):
            with self.subTest(witness=witness):
                conditions = (anchor, _WITNESS_PROSE[witness])
                rendered = set()
                roles = set()
                for label, literal in variants.items():
                    task = with_conditions(self.task, conditions, literal=literal)
                    diag = rp.check_population_cheap(self.root, task)
                    payload = PJ.serialize_for_transport(diag, task=task, route=POP)
                    PJ.assert_value_free(payload.encode("utf-8"), task=task, route=POP)
                    rendered.add(payload)
                    witnesses, a, b, c = rp._declared_witnesses(task)
                    self.assertEqual((witnesses, a, b, c), ((witness,), "customers", "orders", ""))
                    roles.add(rp._roles_from_counterfactual(task, a, b, c))
                    self.assertEqual(rp.witness_problem_codes(task), rp.witness_problem_codes(with_conditions(self.task, conditions)))
                self.assertEqual(len(rendered), 1, witness)
                self.assertEqual(len(roles), 1, witness)
        # The roles are the schema's: column names and declared enum values.
        task = with_conditions(self.task, (anchor,))
        roles = rp._roles_from_counterfactual(task, "customers", "orders", "")
        columns = {c.name for t in task.tables for c in t.columns}
        enum_values = {v for t in task.tables for c in t.columns for v in (c.enum_values or ())}
        for attr in ("link_key", "child_key", "measure", "label", "predicate_column", "period_column", "child_primary_key", "domain_column"):
            self.assertIn(getattr(roles, attr), columns | {""}, attr)
        for value in (*roles.predicate_pass, *roles.predicate_fail, *roles.domain, roles.out_of_domain):
            self.assertIn(value, enum_values | {""})
        self.assertNotIn(roles.out_of_domain, {r.get("customer_name") for r in rows["customers"]})
        # A two-hop anchor over the schema: the child key and its target come
        # from the relationships, whatever the rows carry.
        two_hop = rp._roles_from_counterfactual(task, "customers", "order_items", "orders")
        orders = task.table("orders")
        self.assertEqual(
            (two_hop.child_key, two_hop.child_primary_key),
            ("order_id", orders.primary_key[0] if orders.primary_key else ""),
        )
        self.assertEqual(two_hop, rp._roles_from_counterfactual(
            with_conditions(self.task, (anchor,), literal={}), "customers", "order_items", "orders"
        ))

    # -- the flag's own budget (review finding 1-2) --------------------------------------

    def test_attack_flag_requires_the_declared_oracle_and_wall_budget(self):
        """Phase 3 review finding 1-2: `repair.certify.attack_enabled` (or
        the proposer block's own `session.certify.attack_enabled`) with the
        shipped `max_oracle_bits: 4` would trip LIMIT_ORACLE at the FIRST
        certify's PERMIT (6 bits > 4: no worker, `blocked_limit`, no round)
        — the flag silently disabling certify. The proposer refuses to START
        on such a document (`validate_attack_flag_limits`, a `ValueError`
        naming the key, like `repair_settings`' malformed-value checks):
        `max_oracle_bits >= max_certify * 6` and `wall_clock_s >=
        max_certify * certify deadline`; the arm the certify addendum §4.6
        describes (12 bits, 2 700 s) constructs, and the flag off keeps the
        Phase 1 block."""
        provider, _transport = self.provider(self.root / "flag", [])

        def proposer(**config_keys):
            # The role block is read through a per-path cache: each document
            # this test writes under the same name is a fresh read.
            path = self.population_config(**config_keys)
            P.clear_behavior_caches()
            return rp.AgenticRepairProposer(provider, config_path=path)

        with self.assertRaises(ValueError) as bits:
            proposer(attack_enabled=True, max_oracle_bits=4)
        self.assertIn("max_oracle_bits >= 12", str(bits.exception))
        self.assertIn("repair.certify.attack_enabled", str(bits.exception))
        with self.assertRaises(ValueError) as wall:
            proposer(attack_enabled=True, wall_clock_s=900)
        self.assertIn("wall_clock_s >= 2400", str(wall.exception))
        # The block's own declaration of the flag is validated the same way.
        with self.assertRaises(ValueError):
            proposer(attack_enabled=False, certify={"attack_enabled": True, "deadline_s": 300})
        # A deadline override raises the wall the block must declare.
        with self.assertRaises(ValueError):
            rp.validate_attack_flag_limits(
                attack_enabled=True, max_oracle_bits=12, wall_clock_s=2700.0, max_certify=2, certify_deadline_s=2000.0,
            )
        rp.validate_attack_flag_limits(
            attack_enabled=True, max_oracle_bits=12, wall_clock_s=2700.0, max_certify=2, certify_deadline_s=1200.0,
        )
        rp.validate_attack_flag_limits(
            attack_enabled=True, max_oracle_bits=6, wall_clock_s=None, max_certify=1, certify_deadline_s=1200.0,
        )
        rp.validate_attack_flag_limits(
            attack_enabled=False, max_oracle_bits=4, wall_clock_s=900.0, max_certify=2, certify_deadline_s=1200.0,
        )
        armed = proposer(attack_enabled=True)
        self.assertTrue(armed.attack_enabled)
        self.assertEqual((armed.limits.max_oracle_bits, armed.limits.session_wall_seconds), (12, 2700.0))
        self.assertEqual(armed._certify_deadline_s(), C.CERTIFY_ATTACK_DEADLINE_S)
        plain = proposer()
        self.assertFalse(plain.attack_enabled)
        self.assertEqual((plain.limits.max_oracle_bits, plain.limits.session_wall_seconds), (4, 900.0))
        self.assertGreaterEqual(2 * C.CERTIFY_ATTACK_ORACLE_BITS, 12)

    # -- a model-caused certifier exception is a rejection (review finding 1-0) ----------

    def test_model_caused_certifier_exception_is_a_rejection_not_a_halt(self):
        """Phase 3 review finding 1-0 (C7 read the right way round): a stage
        runner exception the MODEL caused — the REAL reference runner's
        `duckdb.ParserException` on reference SQL the patch broke — is what
        the live ladder records as a scored FAIL row, so the submit-time
        certifier (`_revalidate` inside the unchanged `attempt_patch`)
        answers `RevalidationFailed(revalidation_red_reference)`: a
        rejection both proposers pay for (a second attempt, then
        adjudication), never an `InfrastructureFailure` that halts the
        session, the certifier and the whole `repair()` for free. The
        bounded proposer's `_submit` treats a certifier exception no
        `PatchRejected` class named the same way (`revalidation_red_unknown`)
        and halts only on a harness fault. `test_revalidate_runner_raising_oserror_is_infrastructure_not_red`
        keeps the OS / storage-engine classes on the halting side."""
        import duckdb

        runners = C.provider_free_stage_runners()
        engine = Engine(self.root / "parser", stage_runners=dict(runners), max_repair_rounds=0)
        self.addCleanup(engine.close)
        engine.register(self.task)
        live = engine.load_task(self.task_id)
        self.assertEqual(cli_mod.run_generate(engine, live).verdict, VERDICT_PASS)
        sql = live.reference.sql_by_mart[MART]
        anchor = "SELECT DISTINCT order_id, customer_id"
        self.assertEqual(sql.count(anchor), 1)
        broken = RepairPatch(
            route=REF, artifact="task_ir.json",
            edits=(RepairEdit(op=RepairEditOp.REPLACE, locator=f"reference.sql_by_mart.{MART}", old=anchor,
                              new="SELEKT DISTINCT order_id, customer_id"),),
            rationale="a typo the model made", proposer_role=rp.ROLE_NAME,
        )
        before = self.task_tree(engine)
        # (a) The certifier: a rejection naming the stage, caused by the
        # runner's own exception, never infrastructure.
        with self.assertRaises(rp.RevalidationFailed) as ctx:
            rp.attempt_patch(engine, live, "reference", broken)
        self.assertEqual(ctx.exception.code, "revalidation_red_reference")
        self.assertIsInstance(ctx.exception.__cause__, duckdb.ParserException)
        self.assertEqual(rp.halting_marker(ctx.exception), "")
        self.assertEqual(rp.project_rejection(ctx.exception).code, "revalidation_red_reference")
        self.assertEqual(self.task_tree(engine), before)
        # (b) The one-shot proposer: two failed attempts, adjudication queued.
        provider = ScriptedProvider([broken, broken])
        outcome = rp.RepairProposer(provider, max_attempts=2).repair(engine, live, "reference", REF, "stage failed")
        self.assertEqual(outcome.record.status, rp.STATUS_NEEDS_ADJUDICATION)
        self.assertNotEqual(outcome.disposition, rp.DISPOSITION_HALTED)
        self.assertEqual(outcome.infrastructure, "")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual([a.error_type for a in outcome.record.attempts], ["RevalidationFailed"] * 2)
        self.assertIsNotNone(rp.load_repair_adjudication(engine.workspace, self.task_id))
        rp.repair_adjudication_path(engine.workspace, self.task_id).unlink()
        self.assertEqual(self.task_tree(engine), before)
        # (c) The bounded proposer: session 1's patch is rejected under the
        # code, session 2 sees the code and nothing else, then adjudication;
        # the certifier's text never reaches a message.
        edit = {"artifact": "task_ir.json", "op": "replace", "locator": f"reference.sql_by_mart.{MART}",
                "old": anchor, "new": "SELEKT DISTINCT order_id, customer_id", "rationale": "a typo the model made"}
        bodies = [body("apply_edit_trial", edit), *SUBMIT, body("apply_edit_trial", edit), *SUBMIT]
        bounded, _provider, transport = self.bounded(
            engine, bodies, route=REF, stage="reference", failure="stage failed",
            config_path=self.agents_config(), certify_runners=runners,
        )
        self.assertEqual(bounded.record.status, rp.STATUS_NEEDS_ADJUDICATION)
        self.assertNotEqual(bounded.disposition, rp.DISPOSITION_HALTED)
        self.assertEqual(bounded.infrastructure, "")
        self.assertEqual(len(transport.calls), 4)
        self.assertEqual([a.rejection_code for a in bounded.record.attempts], ["revalidation_red_reference"] * 2)
        self.assertEqual([a.error_type for a in bounded.record.attempts], ["RevalidationFailed"] * 2)
        views = initial_views(transport)
        self.assertIn("revalidation_red_reference", views[-1])
        for text in payload_texts(transport):
            self.assertNotIn("Parser Error", text)
            self.assertNotIn("syntax error", text)
        self.assertEqual(self.task_tree(engine), before)
        # (d) `_submit` on a certifier exception no rejection class named:
        # a `revalidation_red_unknown` rejection, never a halt — and a
        # harness fault still halts under the certify rule's marker.
        proposer = rp.AgenticRepairProposer(_provider, config_path=self.agents_config())
        with mock.patch.object(rp, "attempt_patch", side_effect=ValueError("the certifier choked")):
            submission = proposer._submit(engine, live, "reference", broken)
        self.assertIsNone(submission.task)
        self.assertEqual(submission.halt_marker, "")
        self.assertEqual((submission.rejection.code, submission.error_type), ("revalidation_red_unknown", "ValueError"))
        with mock.patch.object(rp, "attempt_patch", side_effect=OSError(28, "No space left on device")):
            halted = proposer._submit(engine, live, "reference", broken)
        self.assertEqual((halted.halt_marker, halted.error_type), ("ToolHarnessFault", "OSError"))
        self.assertIsNone(halted.rejection)


if __name__ == "__main__":
    unittest.main()


class LeakedParameterRecoveryTest(unittest.TestCase):
    """batch10 run L (2026-09-11), wikidbs__c20112 and dlt__personio: the
    proposer's `apply_edit_trial` arrived with every text value one field
    over — `old` = "</antmlःparameter>\n<parameter name=\"new\">THE
    CONDITION", `new` = "</antmlःparameter>\n<parameter
    name=\"rationale\">THE RATIONALE", `rationale` = a bare leaked closing
    tag — three times, and the session halted as infrastructure. The text
    after a leaked `<parameter name="Y">` is Y's value."""

    def test_values_shifted_one_field_over_are_put_back(self):
        args = {
            "artifact": "task_ir.json", "op": "insert", "locator": "populations.0.conditions",
            "old": "</antmlःparameter>\n<parameter name=\"new\">REPEATED-VALUE WITNESS: at least two rows share a value.",
            "new": "</antmlःparameter>\n<parameter name=\"rationale\">development lacks the witness.",
            "rationale": "</antml：parameter>\n",
        }
        recovered = V._recover_leaked_parameters(args)
        self.assertEqual(recovered["old"], "")
        self.assertEqual(recovered["new"], "REPEATED-VALUE WITNESS: at least two rows share a value.")
        self.assertEqual(recovered["rationale"], "development lacks the witness.")
        self.assertEqual((recovered["op"], recovered["locator"]), ("insert", "populations.0.conditions"))
        for value in recovered.values():
            self.assertIsNone(V._LEAKED_TOOL_SYNTAX_RE.search(str(value)))

    def test_a_kept_prefix_and_a_clean_call_are_untouched(self):
        shifted = V._recover_leaked_parameters({
            "op": "replace",
            "old": "keep me</parameter><parameter name=\"new\">the new text</antml：parameter>\n",
            "new": "</antmlःparameter>\n<parameter name=\"rationale\">why",
            "rationale": "</antml：parameter>\n",
        })
        self.assertEqual(shifted, {"op": "replace", "old": "keep me", "new": "the new text", "rationale": "why"})
        clean = {"op": "replace", "old": "a sentence.", "new": "another sentence.", "rationale": "because"}
        self.assertEqual(V._recover_leaked_parameters(clean), clean)
        # A carried value never overwrites a field that holds real text.
        kept = V._recover_leaked_parameters({
            "op": "replace", "old": "x</parameter><parameter name=\"new\">carried",
            "new": "real replacement text", "rationale": "r",
        })
        self.assertEqual((kept["old"], kept["new"]), ("x", "real replacement text"))


class CertificationOnADifferentFindingTest(unittest.TestCase):
    """lavestima, batch10 run L (2026-09-11): the proposer's patch resolved
    the ambiguity claim it was handed, the certifier re-ran the attack
    stage, a DIFFERENT critic claim held the patched task, and the patch was
    abstained as "waiting on human adjudication". A re-run that fails on a
    finding other than the one under repair is not evidence about the patch:
    the certification stops there, the patch commits, and the live re-run
    decides the new claim on its own round."""

    def _descriptor(self, *identifiers):
        from elt_taskgen.cli import AttackFindingDescriptor
        from elt_taskgen.models import CouncilRole, Severity
        return AttackFindingDescriptor(role=CouncilRole.AMBIGUITY_CRITIC, severity=Severity.MAJOR, identifiers=tuple(identifiers))

    def test_the_descriptor_is_read_off_the_failure_evidence(self):
        import json
        repaired = rp._repaired_finding_of(json.dumps({"codes": {"attack": "x"}, "failing_gates": [], "blocking_finding": self._descriptor("customer_summary").model_dump(mode="json")}))
        self.assertEqual(repaired["identifiers"], ["customer_summary"])
        self.assertIsNone(rp._repaired_finding_of(json.dumps({"codes": {"stage": "error"}, "failing_gates": []})))
        self.assertIsNone(rp._repaired_finding_of("not json"))

    def test_a_payload_naming_another_finding_is_a_different_finding(self):
        from elt_taskgen.cli import AttackPayload
        repaired = self._descriptor("customer_summary", "total_spend").model_dump(mode="json")
        other = AttackPayload(blocking_finding=self._descriptor("customer_summary", "order_count"))
        same = AttackPayload(blocking_finding=self._descriptor("customer_summary", "total_spend"))
        none = AttackPayload()
        self.assertTrue(rp._holds_on_a_different_finding(other, repaired))
        self.assertFalse(rp._holds_on_a_different_finding(same, repaired))
        self.assertFalse(rp._holds_on_a_different_finding(none, repaired))

    def test_revalidation_stops_green_on_a_different_finding_and_stays_red_on_the_same(self):
        from elt_taskgen.cli import AttackPayload
        from elt_taskgen.engine import StageOutcome, StagePayload, VERDICT_FAIL, VERDICT_PASS
        from elt_taskgen.models import RepairRoute

        helper = BoundedProposerTest("test_a_leaked_parameter_tail_is_cut_off_the_argument")
        helper.setUp()
        engine = helper.make_engine(helper.workspace("cert-different"))
        task_id = helper.task.task_id
        repaired = self._descriptor("customer_summary", "total_spend").model_dump(mode="json")

        def runners(attack_outcome):
            def passing(engine, task):
                return StageOutcome(VERDICT_PASS, StagePayload())
            def attack(engine, task):
                return attack_outcome
            table = {stage.value: passing for stage in rp.StageName} if hasattr(rp, "StageName") else {}
            from elt_taskgen.engine import StageName
            table = {stage.value: passing for stage in StageName}
            table["attack"] = attack
            return table

        different = StageOutcome(VERDICT_FAIL, AttackPayload(detail="handoff BLOCKED", blocking_finding=self._descriptor("customer_summary", "order_count")))
        rp._revalidate(engine.workspace, task_id, RepairRoute.SPECIFICATION, "attack", runners(different), repaired_finding=repaired)
        same = StageOutcome(VERDICT_FAIL, AttackPayload(detail="handoff BLOCKED", blocking_finding=self._descriptor("customer_summary", "total_spend")))
        with self.assertRaises(rp.RevalidationFailed):
            rp._revalidate(engine.workspace, task_id, RepairRoute.SPECIFICATION, "attack", runners(same), repaired_finding=repaired)
        with self.assertRaises(rp.RevalidationFailed):
            rp._revalidate(engine.workspace, task_id, RepairRoute.SPECIFICATION, "attack", runners(different))

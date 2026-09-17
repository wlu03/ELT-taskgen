"""Tests for review/loopguard.py and review/trajectory.py (Phase 1.R).

WHY THIS EXISTS
A repeated call that IS executed spends validator time for a byte-identical
observation (up to twenty minutes for `certify`), and a detector whose
fingerprint ignores the edit-versus-validate distinction either nudges a
legitimate `edit, validate, edit, validate` loop or lets `validate, validate,
validate` run. These tests pin SoT T5: identical (action, observation) pairs
nudge at the third attempt and halt at the fourth; error streaks the same;
the check runs BEFORE dispatch so the flagged repeat is never executed; a
no-op write does not bump the epoch and counts as an error; idempotent reads
are exempt but still count toward the call cap; correction turns are
excluded; one nudge per session. The trajectory chain tests pin that any
edited, dropped or re-ordered turn — or another task, tool manifest or
policy — moves the digest.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.models import sha256_hex
from elt_taskgen.review import loopguard as L
from elt_taskgen.review import session as S
from elt_taskgen.review import trajectory as T
from elt_taskgen.review.tools import projection as PJ
from elt_taskgen.review.tools import registry as RG

ROLE = "repair_proposer"
TASK = demo_task()
SUBMIT = "submit_patch"


def diag(ok: bool = True, code: str = "ok") -> PJ.Diagnostic:
    return PJ.Diagnostic(source=PJ.DiagnosticSource.GATE, ok=ok, code=code)


def _schema(*names: str) -> dict:
    return {
        "type": "object",
        "properties": {n: {"type": "string"} for n in names},
        "required": list(names),
        "additionalProperties": False,
    }


@dataclass
class FakeTool:
    name: str
    permitted_roles: frozenset = frozenset({ROLE})
    input_schema: dict = field(default_factory=_schema)
    cost: RG.ToolCost = field(default_factory=RG.ToolCost)
    description: str = "a fake tool"
    surface_write: bool = False
    validator: bool = False
    impl: Callable[[Any, dict], Any] | None = None
    calls: list = field(default_factory=list)

    def run(self, ctx, args):
        self.calls.append(dict(args))
        return diag() if self.impl is None else self.impl(ctx, args)


def tool_use(name: str, args: dict | None = None, id_: str = "tu") -> dict:
    return {"type": "tool_use", "id": id_, "name": name, "input": dict(args or {})}


@dataclass
class Turn:
    content: list
    stop_reason: str = "tool_use"
    usage: dict = field(default_factory=lambda: {"input_tokens": 10, "output_tokens": 5})
    usd: float = 0.0
    memo_key: str = ""


class ScriptedProvider:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def _turn(self, role, messages, policy, turn_index, *, tools=None):
        self.calls += 1
        item = self.script.pop(0)
        return Turn(content=list(item))


class DetectorCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name) / "attempt"
        root.mkdir()
        self.ctx = RG.ToolContext(root=root, task_id=TASK.task_id, role=ROLE, task=TASK)

    def run_session(self, script, tools, *, worker=None, max_turns=12, max_tool_calls=20, detector=None):
        submit = FakeTool(SUBMIT, input_schema=_schema("patch"))
        tools = list(tools) + [submit]
        limits = S.SessionLimits.declare(max_turns=max_turns, max_tool_calls=max_tool_calls)
        policy = S.SessionPolicy(role=ROLE, tools=tuple(tools), submit_tool=SUBMIT, limits=limits, mode="model_driven")
        provider = ScriptedProvider(script)
        result = S.run_bounded_session(
            ROLE, "view", tools, policy, limits, provider=provider, ctx=self.ctx,
            worker=worker or S.InProcessValidatorWorker(), detector=detector,
        )
        return result, provider


# ---------------------------------------------------------------------------
# The detector alone
# ---------------------------------------------------------------------------

class StuckDetectorUnitTest(unittest.TestCase):
    def test_three_identical_action_observation_pairs_nudge_fourth_halts(self):
        d = L.StuckDetector.from_policy(S.STUCK_DETECTOR_THRESHOLDS)
        fp = d.fingerprint("check_scope", {})
        obs = sha256_hex("same")
        # Attempts 1 and 2 execute (two identical pairs); the third identical
        # attempt is nudged and NOT executed; the fourth halts.
        self.assertEqual(d.check(fp), "proceed")
        d.observe(fp, obs, ok=True, code="ok")
        self.assertEqual(d.check(fp), "proceed")
        d.observe(fp, obs, ok=True, code="ok")
        self.assertEqual(d.check(fp), "nudge")
        self.assertEqual(d.nudge_count, 1)
        self.assertEqual(d.check(fp), "halt")
        self.assertEqual([(e.rule, e.verdict, e.attempt) for e in d.events], [("r1", "nudge", 3), ("r1", "halt", 4)])
        # A validator whose OBSERVATION changed is never "the same".
        d = L.StuckDetector.from_policy(None)
        for i in range(5):
            self.assertEqual(d.check(fp), "proceed", i)
            d.observe(fp, sha256_hex(f"obs-{i}"), ok=True, code="ok")
        self.assertEqual(d.events, [])
        # Thresholds and nudge text are part of policy_sha256.
        policy = S.SessionPolicy(role=ROLE)
        other = S.SessionPolicy(role=ROLE, stuck_thresholds={**dict(S.STUCK_DETECTOR_THRESHOLDS), "identical_pairs_nudge": 2, "identical_pairs_halt": 3})
        self.assertNotEqual(policy.sha256(), other.sha256())
        self.assertNotEqual(policy.sha256(), S.SessionPolicy(role=ROLE, nudge_text="other").sha256())
        self.assertEqual(policy.sha256(), S.SessionPolicy(role=ROLE, session_salt=7).sha256())
        self.assertEqual(L.StuckThresholds.from_mapping(S.STUCK_DETECTOR_THRESHOLDS), L.StuckThresholds(3, 4, 3, 4, 3))

    def test_error_streak_nudges_at_three_halts_at_four(self):
        d = L.StuckDetector.from_policy(None)
        fp = d.fingerprint("apply_edit_trial", {"patch": "x"})
        # Same action, ok=false, the same first code — observations may differ.
        self.assertEqual(d.check(fp), "proceed")
        d.observe(fp, sha256_hex("o1"), ok=False, code="anchor_not_found")
        self.assertEqual(d.check(fp), "proceed")
        d.observe(fp, sha256_hex("o2"), ok=False, code="anchor_not_found")
        self.assertEqual(d.check(fp), "nudge")
        self.assertEqual(d.check(fp), "halt")
        self.assertEqual([(e.rule, e.verdict) for e in d.events], [("r2", "nudge"), ("r2", "halt")])
        # A different first code breaks the streak.
        d = L.StuckDetector.from_policy(None)
        d.observe(fp, sha256_hex("o1"), ok=False, code="anchor_not_found")
        d.observe(fp, sha256_hex("o2"), ok=False, code="anchor_ambiguous")
        self.assertEqual(d.check(fp), "proceed")
        # ...and a clean step with another action resets every counter.
        d = L.StuckDetector.from_policy(None)
        d.observe(fp, sha256_hex("o1"), ok=False, code="anchor_not_found")
        d.observe(fp, sha256_hex("o2"), ok=False, code="anchor_not_found")
        other = d.fingerprint("check_scope", {})
        d.observe(other, sha256_hex("clean"), ok=True, code="ok")
        self.assertEqual(d.check(fp), "proceed")

    def test_r3_cycles_are_pol_only_and_disabled_for_council_roles(self):
        # A 2-cycle repeated five times in the ring trips R3 only when enabled.
        a, b = sha256_hex("a"), sha256_hex("b")
        disabled = L.StuckDetector.from_policy(None, cycles_enabled=False)
        enabled = L.StuckDetector.from_policy(None, cycles_enabled=True)
        for step in range(9):
            fp = a if step % 2 == 0 else b
            self.assertEqual(disabled.check(fp), "proceed", step)
            disabled.observe(fp, sha256_hex(f"o{step}"), ok=True, code="ok")
            self.assertEqual(enabled.check(fp), "proceed", step)
            enabled.observe(fp, sha256_hex(f"o{step}"), ok=True, code="ok")
        self.assertEqual(disabled.check(b), "proceed")
        self.assertEqual(enabled.check(b), "nudge")
        self.assertEqual(enabled.events[-1].rule, "r3")
        self.assertEqual(L.RING_SIZE, 25)
        self.assertEqual(L.PAIR_WINDOW, 8)
        # The runner's default detector runs with cycles disabled.
        d = L.StuckDetector.from_policy(S.STUCK_DETECTOR_THRESHOLDS)
        self.assertFalse(d.cycles_enabled)


# ---------------------------------------------------------------------------
# The detector inside the runner (STUCK_CHECK before dispatch)
# ---------------------------------------------------------------------------

class StuckDetectorRunnerTest(DetectorCase):
    def test_edit_between_validations_resets_via_state_epoch(self):
        drafts = {"text": ""}

        def write(ctx, args):
            drafts["text"] = args["text"]
            return diag()

        edit = FakeTool("replace_prose", input_schema=_schema("text"), surface_write=True, impl=write)
        check = FakeTool("check_prose", validator=True, impl=lambda ctx, args: diag(ok=False, code="failed"))
        worker = S.InProcessValidatorWorker(surface_fingerprint=lambda ctx: sha256_hex(drafts["text"]))
        # edit, validate, edit, validate, edit, validate: every validate
        # carries a new epoch, so nothing repeats and the session submits.
        script = []
        for i in range(3):
            script.append([tool_use("replace_prose", {"text": f"draft {i}"}, f"e{i}")])
            script.append([tool_use("check_prose", {}, f"v{i}")])
        script.append([tool_use(SUBMIT, {"patch": "p"}, "s")])
        result, _ = self.run_session(script, [edit, check], worker=worker)
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(len(check.calls), 3)
        self.assertEqual(result.nudge_count, 0)
        self.assertEqual(result.detector_events, ())
        self.assertEqual(result.state_epoch, 3)
        epochs = [t.state_epoch for t in result.turns if t.kind == "tool" and t.tool_name == "check_prose"]
        self.assertEqual(epochs, [1, 2, 3])
        fps = [t.action_fingerprint for t in result.turns if t.kind == "tool" and t.tool_name == "check_prose"]
        self.assertEqual(len(set(fps)), 3)
        self.assertEqual(fps[0], L.action_fingerprint("check_prose", {}, 1))
        # validate, validate, validate with NO edit between: the third is nudged.
        check = FakeTool("check_prose", validator=True, impl=lambda ctx, args: diag(ok=False, code="failed"))
        script = [[tool_use("replace_prose", {"text": "d"}, "e")]] + [[tool_use("check_prose", {}, f"v{i}")] for i in range(3)] + [[tool_use(SUBMIT, {"patch": "p"}, "s")]]
        result, _ = self.run_session(script, [FakeTool("replace_prose", input_schema=_schema("text"), surface_write=True, impl=write), check], worker=worker)
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(len(check.calls), 2)
        self.assertEqual(result.nudge_count, 1)

    def test_noop_write_does_not_bump_epoch_and_counts_as_error(self):
        drafts = {"text": "unchanged"}

        def write(ctx, args):
            drafts["text"] = args["text"]
            return diag()

        edit = FakeTool("apply_edit_trial", input_schema=_schema("text"), surface_write=True, impl=write)
        worker = S.InProcessValidatorWorker(surface_fingerprint=lambda ctx: sha256_hex(drafts["text"]))
        # Writing the same bytes twice leaves the surface unchanged: `noop`,
        # ok=false, epoch stays 0; two no-ops then a third identical attempt
        # is the R2 nudge, the fourth the STUCK halt.
        script = [[tool_use("apply_edit_trial", {"text": "unchanged"}, f"w{i}")] for i in range(4)] + [[tool_use(SUBMIT, {"patch": "p"}, "s")]]
        result, _ = self.run_session(script, [edit], worker=worker)
        self.assertIs(result.terminal, S.TerminalState.STUCK)
        self.assertEqual(result.state_epoch, 0)
        noop_turns = [t for t in result.turns if t.kind == "tool"]
        self.assertEqual([t.outcome_code for t in noop_turns], ["noop", "noop"])
        self.assertEqual(len(edit.calls), 2)
        self.assertEqual([e.rule for e in result.detector_events], ["r2", "r2"])
        # A REAL write bumps the epoch and is never `noop`.
        drafts["text"] = "before"
        script = [[tool_use("apply_edit_trial", {"text": "after"}, "w")], [tool_use(SUBMIT, {"patch": "p"}, "s")]]
        result, _ = self.run_session(script, [FakeTool("apply_edit_trial", input_schema=_schema("text"), surface_write=True, impl=write)], worker=worker)
        self.assertEqual(result.state_epoch, 1)
        self.assertEqual([t.outcome_code for t in result.turns if t.kind == "tool"], ["ok"])
        # A tool that reports `noop` itself gets the same treatment, even
        # without a surface fingerprint.
        def self_noop(ctx, args):
            return PJ.Diagnostic(source=PJ.DiagnosticSource.REJECTION, ok=False, code="patch_noop")

        drafts["text"] = "x"
        reporting = FakeTool("apply_edit_trial", input_schema=_schema("text"), surface_write=True, impl=self_noop)
        script = [[tool_use("apply_edit_trial", {"text": "x"}, "w")], [tool_use(SUBMIT, {"patch": "p"}, "s")]]
        result, _ = self.run_session(script, [reporting], worker=S.InProcessValidatorWorker())
        self.assertEqual(result.state_epoch, 0)
        self.assertFalse([t for t in result.turns if t.kind == "tool"][0].outcome_code == "ok")

    def test_idempotent_reads_are_exempt_but_count_toward_call_cap(self):
        read = FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True))
        script = [[tool_use("read_view", {}, f"r{i}")] for i in range(6)] + [[tool_use(SUBMIT, {"patch": "p"}, "s")]]
        # Six identical reads: no nudge, no halt (exempt from R1)...
        result, _ = self.run_session(script, [read], max_tool_calls=20)
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(len(read.calls), 6)
        self.assertEqual(result.nudge_count, 0)
        self.assertEqual(result.detector_events, ())
        # ...but every one counts toward max_tool_calls: after the fourth
        # the model still gets its submit-or-abort turn (last-turn forcing),
        # and spending it on a fifth read is the cap binding.
        read = FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True))
        result, provider = self.run_session(list(script), [read], max_tool_calls=4)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_TOOL_CALLS)
        self.assertEqual(len(read.calls), 4)
        self.assertEqual(result.tool_call_count, 4)
        self.assertEqual(provider.calls, 5)
        # A non-idempotent tool with the same repetition is nudged then halted.
        check = FakeTool("check_scope")
        script = [[tool_use("check_scope", {}, f"c{i}")] for i in range(6)]
        result, _ = self.run_session(script, [check], max_tool_calls=20)
        self.assertIs(result.terminal, S.TerminalState.STUCK)
        self.assertEqual(len(check.calls), 2)
        # An idempotent read still counts toward R2 error streaks (a failing
        # read repeated is a failing read repeated).
        failing = FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True), impl=lambda ctx, args: diag(ok=False, code="failed"))
        script = [[tool_use("read_view", {}, f"r{i}")] for i in range(6)]
        result, _ = self.run_session(script, [failing], max_tool_calls=20)
        self.assertIs(result.terminal, S.TerminalState.STUCK)
        self.assertEqual(len(failing.calls), 2)

    def test_stuck_check_runs_before_dispatch_so_repeat_is_never_executed(self):
        executed: list[str] = []

        def record(ctx, args):
            executed.append("run")
            return diag()

        check = FakeTool("check_scope", impl=record)
        script = [[tool_use("check_scope", {}, f"c{i}")] for i in range(4)]
        result, provider = self.run_session(script, [check])
        self.assertIs(result.terminal, S.TerminalState.STUCK)
        # Four proposals, two executions: the nudged third and the halted
        # fourth never reached the tool.
        self.assertEqual(provider.calls, 4)
        self.assertEqual(executed, ["run", "run"])
        kinds = [(t.kind, t.category or t.outcome_code) for t in result.turns]
        self.assertEqual(
            kinds,
            [("model", "tool_call"), ("tool", "ok"), ("model", "tool_call"), ("tool", "ok"),
             ("model", "nudge"), ("nudge", "repeated_call"), ("model", "limit_stop")],
        )
        nudge = next(t for t in result.turns if t.kind == "nudge")
        self.assertTrue(nudge.refused)
        self.assertEqual(nudge.outcome_code, "repeated_call")
        self.assertEqual(nudge.output_sha256, sha256_hex(S.NUDGE_TEXT))
        self.assertEqual(result.nudge_count, 1)
        self.assertEqual(result.limit_stop_count, 1)
        self.assertEqual(result.tool_call_count, 2)
        self.assertEqual(result.action_trace[2].outcome_code, "repeated_call")

    def test_correction_turns_excluded_from_detectors(self):
        check = FakeTool("check_scope")
        # Two identical executed calls, then two corrections (no tool call,
        # unknown tool): the corrections neither extend nor reset the R1
        # streak, so the next identical call is still the third attempt (nudge).
        script = [
            [tool_use("check_scope", {}, "c0")],
            [tool_use("check_scope", {}, "c1")],
            [{"type": "text", "text": "hmm"}],
            [tool_use("no_such_tool", {}, "x")],
            [tool_use("check_scope", {}, "c2")],
            [tool_use(SUBMIT, {"patch": "p"}, "s")],
        ]
        result, _ = self.run_session(script, [check])
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.correction_count, 2)
        self.assertEqual(result.nudge_count, 1)
        self.assertEqual(len(check.calls), 2)
        self.assertEqual([e.verdict for e in result.detector_events], ["nudge"])
        # Corrections are model turns without a tool-side record.
        self.assertEqual([t.category for t in result.turns if t.kind == "model"], ["tool_call", "tool_call", "correction", "correction", "nudge", "terminal"])
        # The detector itself: note_correction changes nothing.
        d = L.StuckDetector.from_policy(None)
        fp = d.fingerprint("check_scope", {})
        d.observe(fp, sha256_hex("o"), ok=True, code="ok")
        d.observe(fp, sha256_hex("o"), ok=True, code="ok")
        d.note_correction()
        d.note_correction()
        self.assertEqual(d.check(fp), "nudge")

    def test_stuck_threshold_override_changes_the_nudge_ordinal(self):
        """SoT T5 "Exemptions": the thresholds are versioned in
        `policy_sha256` AND enforced by the runner — a `stuck:` override in
        the role's session block moves the nudge from the third identical
        proposal to the second (and the halt from the fourth to the third)
        through the detector `run_bounded_session` builds from the policy."""
        submit = FakeTool(SUBMIT, input_schema=_schema("patch"))
        check = FakeTool("check_scope")
        tools = [check, submit]
        override = {"identical_pairs_nudge": 2, "identical_pairs_halt": 3}
        limits = S.SessionLimits.declare(max_turns=12, max_tool_calls=20, stuck=override)
        policy = S.SessionPolicy(role=ROLE, tools=tuple(tools), submit_tool=SUBMIT, limits=limits, mode="model_driven")
        self.assertEqual(policy.stuck_thresholds["identical_pairs_nudge"], 2)
        self.assertEqual(L.StuckThresholds.from_mapping(policy.stuck_thresholds).identical_pairs_halt, 3)
        script = [[tool_use("check_scope", {}, f"c{i}")] for i in range(4)]
        result = S.run_bounded_session(
            ROLE, "view", tools, policy, limits, provider=ScriptedProvider(script), ctx=self.ctx,
            worker=S.InProcessValidatorWorker(), detector=None,
        )
        self.assertIs(result.terminal, S.TerminalState.STUCK)
        self.assertEqual(len(check.calls), 1)
        self.assertEqual([(e.rule, e.verdict, e.attempt) for e in result.detector_events],
                         [("r1", "nudge", 2), ("r1", "halt", 3)])
        # The default block: third nudged, fourth halted (two executed pairs).
        check = FakeTool("check_scope")
        result, _ = self.run_session([[tool_use("check_scope", {}, f"c{i}")] for i in range(4)], [check])
        self.assertEqual(len(check.calls), 2)
        self.assertEqual([(e.verdict, e.attempt) for e in result.detector_events], [("nudge", 3), ("halt", 4)])

    def test_flagged_repeat_is_never_executed(self):
        # Across every rule: the proposal that triggers a nudge or a halt does
        # not reach the tool (R1 identical pairs, R2 error streak, R4 no-ops).
        r1 = FakeTool("check_scope")
        result, _ = self.run_session([[tool_use("check_scope", {}, f"c{i}")] for i in range(4)], [r1])
        self.assertEqual(len(r1.calls), 2)
        self.assertIs(result.terminal, S.TerminalState.STUCK)

        r2 = FakeTool("check_scope", impl=lambda ctx, args: diag(ok=False, code="failed"))
        result, _ = self.run_session([[tool_use("check_scope", {}, f"c{i}")] for i in range(4)], [r2])
        self.assertEqual(len(r2.calls), 2)
        self.assertIs(result.terminal, S.TerminalState.STUCK)

        drafts = {"text": "same"}

        def write(ctx, args):
            drafts["text"] = args["text"]
            return diag()

        r4 = FakeTool("apply_edit_trial", input_schema=_schema("text"), surface_write=True, impl=write)
        worker = S.InProcessValidatorWorker(surface_fingerprint=lambda ctx: sha256_hex(drafts["text"]))
        result, _ = self.run_session([[tool_use("apply_edit_trial", {"text": "same"}, f"w{i}")] for i in range(4)], [r4], worker=worker)
        self.assertEqual(len(r4.calls), 2)
        self.assertIs(result.terminal, S.TerminalState.STUCK)
        # One nudge per session: after it, a trigger of ANY rule halts.
        d = L.StuckDetector.from_policy(None)
        a = d.fingerprint("check_scope", {})
        b = d.fingerprint("apply_edit_trial", {"text": "x"})
        d.observe(a, sha256_hex("o"), ok=True, code="ok")
        d.observe(a, sha256_hex("o"), ok=True, code="ok")
        self.assertEqual(d.check(a), "nudge")
        d.observe(b, sha256_hex("e"), ok=False, code="noop")
        d.observe(b, sha256_hex("e"), ok=False, code="noop")
        self.assertEqual(d.check(b), "halt")
        self.assertEqual(d.nudge_count, 1)


# ---------------------------------------------------------------------------
# The trajectory hash chain
# ---------------------------------------------------------------------------

def _turns() -> list[S.TurnRecord]:
    return [
        S.TurnRecord(turn_index=0, kind="model", model_turn=0, category="tool_call", memo_key="m0",
                     prompt_sha256=sha256_hex("p0"), response_sha256=sha256_hex("r0"), tool_name="check_scope"),
        S.TurnRecord(turn_index=1, kind="tool", model_turn=0, tool_name="check_scope", output_sha256=sha256_hex("o0"),
                     outcome_code="ok", elapsed_tool_ms=12),
        S.TurnRecord(turn_index=2, kind="model", model_turn=1, category="terminal", memo_key="m1",
                     prompt_sha256=sha256_hex("p1"), response_sha256=sha256_hex("r1"), tool_name=SUBMIT, outcome_code="submitted"),
    ]


IDENTITY = dict(task_content_hash=TASK.content_hash(), tools_sha256=sha256_hex("tools"), policy_sha256=sha256_hex("policy"), role=ROLE)


def swapped_turns(turns):
    return [turns[0], turns[2], turns[1]]


def _record(turns, chain, *, trial_nonce="", behavior_sha256="", prompt_sha256=""):
    """A Phase 4 trajectory record over `turns` under `chain` (the shape
    `providers.RoutedProvider._trajectory_record` writes: the chain fields
    of every turn, the chain hashes, the identity and the trial binding)."""
    session_sha = chain.digest
    return {
        **IDENTITY,
        "trial_nonce": trial_nonce,
        "behavior_sha256": behavior_sha256,
        "prompt_sha256": prompt_sha256,
        "turns": [t.as_dict() for t in turns],
        "chain_hashes": list(chain.hashes),
        "session_sha256": session_sha,
        "trajectory_sha256": T.trial_trajectory_sha256(
            session_sha, task_content_hash=IDENTITY["task_content_hash"], role=ROLE,
            trial_nonce=trial_nonce, behavior_sha256=behavior_sha256, prompt_sha256=prompt_sha256,
        ),
    }


class TrajectoryChainTest(unittest.TestCase):
    def test_trajectory_chain_rejects_any_edited_turn(self):
        turns = _turns()
        chain = T.TrajectoryChain(**IDENTITY)
        links = [chain.append(t) for t in turns]
        self.assertEqual(len(links), 3)
        self.assertEqual(chain.digest, links[-1])
        self.assertTrue(chain.verify(turns))
        self.assertEqual(T.verify_chain(turns, links, **IDENTITY), chain.digest)
        # Editing ANY chained field of ANY turn breaks the chain from that link on.
        for index, edit in ((0, {"response_sha256": sha256_hex("forged")}), (1, {"outcome_code": "failed"}),
                            (1, {"output_sha256": sha256_hex("other")}), (2, {"tool_name": "abort"}),
                            (0, {"route": {"provider": "anthropic", "model": "other-model"}}),
                            (0, {"route": {"policy_sha256": sha256_hex("other policy")}}),
                            (1, {"state_epoch": 1}), (2, {"fresh": False})):
            with self.subTest(index=index, edit=edit):
                edited = list(turns)
                edited[index] = S.TurnRecord(**{**edited[index].as_dict(), **edit})
                self.assertFalse(chain.verify(edited))
                with self.assertRaises(T.ChainError) as ctx:
                    T.verify_chain(edited, links, **IDENTITY)
                self.assertIn(f"link {index}", str(ctx.exception))
        # Timing, cost, replay/cache metadata, admission provenance, and
        # recorded-only diagnostics do not identify a trajectory. The chain
        # binds only stable route identity, so these edits preserve its digest.
        for edit in ({"elapsed_tool_ms": 999}, {"elapsed_model_ms": 999}, {"usd": 0.5}, {"replayed": True},
                     {"usage": {"input": 999, "output": 0, "cache_read": 900, "cache_write": 0}},
                     {"admission": {"admission_record_path": "/Users/alice/council/state/council.live_admitted",
                                    "admission_evidence_sha256": sha256_hex("x")}},
                     {"route": {"diagnostics_version": "99", "entry_schema": 3}}):
            with self.subTest(unchained=edit):
                timed = list(turns)
                timed[1] = S.TurnRecord(**{**timed[1].as_dict(), **edit})
                self.assertTrue(chain.verify(timed))
                self.assertEqual(T.trajectory_sha256(timed, **IDENTITY), chain.digest)
        self.assertEqual(
            T._UNCHAINED_FIELDS,
            frozenset({"elapsed_model_ms", "elapsed_tool_ms", "usd", "replayed", "usage", "admission"}),
        )
        self.assertEqual(T.CHAIN_VERSION, "2")
        # The chained projection of a route block, from a record and from a mapping.
        full_route = {"provider": "anthropic", "model": "m", "max_tokens": 8, "effort": "high",
                      "behavior_sha256": "b", "tools_sha256": "t", "policy_sha256": "p",
                      "diagnostics_version": "1", "entry_schema": 3}
        self.assertEqual(list(T.chained_route(full_route)), list(T.ROUTE_CHAIN_KEYS))
        record = S.TurnRecord(**{**turns[0].as_dict(), "route": full_route})
        self.assertEqual(record.chain_fields()["route"], T.chained_route(full_route))
        self.assertEqual(T.turn_chain_fields(record.as_dict()), T.turn_chain_fields(record))
        self.assertNotIn("admission", T.turn_chain_fields(record))
        self.assertNotIn("usage", T.turn_chain_fields(record))
        self.assertEqual(dict(record.as_dict()["route"]), full_route)
        # Dropping a turn is a length mismatch; an empty chain is h0.
        self.assertFalse(chain.verify(turns[:2]))
        self.assertEqual(T.TrajectoryChain(**IDENTITY).digest, T.chain_seed(**IDENTITY))
        self.assertEqual(len(T.chain_seed(**IDENTITY)), 64)
        # Phase 4: the content-addressed trajectory RECORD carries the turns,
        # the chain and the trial binding; `verify_trajectory_record`
        # recomputes both and refuses ANY edited turn in the record.
        record = _record(turns, chain)
        self.assertEqual(T.verify_trajectory_record(record), record["trajectory_sha256"])
        for index, edit in ((0, {"response_sha256": sha256_hex("forged")}), (1, {"output_sha256": sha256_hex("other")}),
                            (2, {"outcome_code": "abstained"}), (1, {"fresh": False})):
            with self.subTest(record_edit=(index, edit)):
                edited = _record(turns, chain)
                edited["turns"][index] = {**edited["turns"][index], **edit}
                with self.assertRaises(T.ChainError) as ctx:
                    T.verify_trajectory_record(edited)
                self.assertIn(f"link {index}", str(ctx.exception))
        # The measured / accounting fields of a recorded turn are not evidence
        # in the record either.
        timed = _record(turns, chain)
        timed["turns"][1] = {**timed["turns"][1], "elapsed_tool_ms": 999, "usd": 0.5, "replayed": True}
        self.assertEqual(T.verify_trajectory_record(timed), record["trajectory_sha256"])
        # A record whose stated digests disagree with its turns is refused.
        for field, needle in (("session_sha256", "session_sha256"), ("trajectory_sha256", "trajectory_sha256")):
            with self.subTest(forged=field):
                forged = _record(turns, chain)
                forged[field] = sha256_hex("forged")
                with self.assertRaises(T.ChainError) as ctx:
                    T.verify_trajectory_record(forged)
                self.assertIn(needle, str(ctx.exception))
        self.assertEqual(T.TRAJECTORY_RECORD_VERSION, "elt-trajectory-v5")

    def test_trajectory_hash_chain_detects_reorder(self):
        turns = _turns()
        digest = T.trajectory_sha256(turns, **IDENTITY)
        swapped = [turns[0], turns[2], turns[1]]
        self.assertNotEqual(T.trajectory_sha256(swapped, **IDENTITY), digest)
        reversed_turns = list(reversed(turns))
        self.assertNotEqual(T.trajectory_sha256(reversed_turns, **IDENTITY), digest)
        chain = T.TrajectoryChain(**IDENTITY)
        for t in turns:
            chain.append(t)
        self.assertFalse(chain.verify(swapped))
        with self.assertRaises(T.ChainError) as ctx:
            T.verify_chain(swapped, chain.hashes, **IDENTITY)
        self.assertIn("link 1", str(ctx.exception))
        # Two turns with identical content in swapped positions still differ,
        # because the position (turn_index) is a chained field.
        a = S.TurnRecord(turn_index=0, kind="tool", tool_name="x", output_sha256=sha256_hex("same"))
        b = S.TurnRecord(turn_index=1, kind="tool", tool_name="x", output_sha256=sha256_hex("same"))
        self.assertNotEqual(T.trajectory_sha256([a, b], **IDENTITY), T.trajectory_sha256([b, a], **IDENTITY))
        # The ATIF stub carries the chain and the terminal.
        atif = T.to_atif(turns, terminal="SUBMITTED", **IDENTITY)
        self.assertEqual(atif["format"], "atif-stub")
        self.assertEqual(atif["trajectory_sha256"], digest)
        self.assertEqual([s["h"] for s in atif["steps"]], chain.hashes)
        self.assertEqual(atif["terminal"], "SUBMITTED")
        # Phase 4: a trajectory record whose turns were re-ordered (the
        # recorded chain hashes left as they were) is refused at the first
        # moved link, and the trial-bound digest of the re-ordered turns is
        # another address.
        record = _record(turns, chain)
        swapped_record = _record(turns, chain)
        swapped_record["turns"] = [swapped_record["turns"][0], swapped_record["turns"][2], swapped_record["turns"][1]]
        with self.assertRaises(T.ChainError) as ctx:
            T.verify_trajectory_record(swapped_record)
        self.assertIn("link 1", str(ctx.exception))
        swapped_chain = T.TrajectoryChain(**IDENTITY)
        for t in swapped:
            swapped_chain.append(t)
        rebound = _record(swapped, swapped_chain)
        self.assertEqual(T.verify_trajectory_record(rebound), rebound["trajectory_sha256"])
        self.assertNotEqual(rebound["trajectory_sha256"], record["trajectory_sha256"])

    def test_trajectory_hash_binds_task_content_hash_and_tool_manifest(self):
        turns = _turns()
        digest = T.trajectory_sha256(turns, **IDENTITY)
        self.assertNotEqual(T.trajectory_sha256(turns, **{**IDENTITY, "task_content_hash": sha256_hex("other task")}), digest)
        self.assertNotEqual(T.trajectory_sha256(turns, **{**IDENTITY, "tools_sha256": sha256_hex("other tools")}), digest)
        self.assertNotEqual(T.trajectory_sha256(turns, **{**IDENTITY, "policy_sha256": sha256_hex("other policy")}), digest)
        self.assertNotEqual(T.trajectory_sha256(turns, **{**IDENTITY, "role": "semantic_author"}), digest)
        self.assertEqual(T.trajectory_sha256(list(turns), **IDENTITY), digest)  # deterministic
        # A chain recorded under one identity is refused under another.
        chain = T.TrajectoryChain(**IDENTITY)
        for t in turns:
            chain.append(t)
        with self.assertRaises(T.ChainError):
            T.verify_chain(turns, chain.hashes, **{**IDENTITY, "task_content_hash": sha256_hex("other task")})
        # Phase 4 (metrology redesign §7): the trial-bound `trajectory_sha256`
        # binds the chain digest to the task content hash, the role, the
        # trial nonce, the seat's behaviour digest and the prompt digest —
        # each moves it — and a record claiming another binding is refused.
        binding = dict(task_content_hash=TASK.content_hash(), role=ROLE, trial_nonce="0123456789abcdef",
                       behavior_sha256=sha256_hex("behaviour"), prompt_sha256=sha256_hex("prompt"))
        bound = T.trial_trajectory_sha256(digest, **binding)
        self.assertEqual(len(bound), 64)
        self.assertEqual(T.trial_trajectory_sha256(digest, **binding), bound)  # deterministic
        for key, other in (("task_content_hash", sha256_hex("other task")), ("role", "semantic_author"),
                           ("trial_nonce", "fedcba9876543210"), ("behavior_sha256", sha256_hex("other behaviour")),
                           ("prompt_sha256", sha256_hex("other prompt"))):
            with self.subTest(moves=key):
                self.assertNotEqual(T.trial_trajectory_sha256(digest, **{**binding, key: other}), bound)
        self.assertNotEqual(T.trial_trajectory_sha256(T.trajectory_sha256(swapped_turns(turns), **IDENTITY), **binding), bound)
        record = _record(turns, chain, trial_nonce=binding["trial_nonce"],
                         behavior_sha256=binding["behavior_sha256"], prompt_sha256=binding["prompt_sha256"])
        self.assertEqual(record["trajectory_sha256"], bound)
        self.assertEqual(T.verify_trajectory_record(record), bound)
        for key, other in (("task_content_hash", sha256_hex("other task")), ("tools_sha256", sha256_hex("other tools")),
                           ("policy_sha256", sha256_hex("other policy")), ("role", "semantic_author"),
                           ("trial_nonce", "fedcba9876543210"), ("behavior_sha256", sha256_hex("other")),
                           ("prompt_sha256", sha256_hex("other"))):
            with self.subTest(record_claims=key):
                claimed = dict(record)
                claimed[key] = other
                with self.assertRaises(T.ChainError):
                    T.verify_trajectory_record(claimed)
        # The runner records the same binding on its result and the result verifies.
        from tests.test_bounded_session import ScriptedProvider as Provider, submit_tool, tool_use as tu

        policy = S.SessionPolicy(role=ROLE, tools=(submit_tool(),), submit_tool=SUBMIT,
                                 limits=S.SessionLimits.declare(max_turns=2, max_tool_calls=2), mode="model_driven")
        with tempfile.TemporaryDirectory() as tmp:
            ctx = RG.ToolContext(root=Path(tmp), task_id=TASK.task_id, role=ROLE, task=TASK)
            result = S.run_bounded_session(ROLE, "v", policy.tools, policy, policy.limits, provider=Provider([[tu(SUBMIT, {"patch": "p"})]]), ctx=ctx, worker=S.InProcessValidatorWorker())
        self.assertEqual(result.task_content_hash, TASK.content_hash())
        self.assertEqual(result.tools_sha256, policy.tools_sha256())
        self.assertEqual(result.policy_sha256, policy.sha256())
        self.assertEqual(result.session_sha256, T.trajectory_sha256(result.turns, task_content_hash=result.task_content_hash, tools_sha256=result.tools_sha256, policy_sha256=result.policy_sha256, role=ROLE))
        self.assertTrue(result.verify_chain())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

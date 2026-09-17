"""Tests for review/session.py — the bounded-session runner (Phase 1.R).

WHY THIS EXISTS
The runner is the ONE state machine every agentic role shares
(04_agent_state_machine.md). These tests pin, against a scripted fake
provider (no transport, no model, no network) and the real projections on
the demo task, that: every model turn ends in exactly one of the six SoT T5
categories and the count identities hold; a limit binds to ITS terminal state
and never another; the last permitted turn admits only submit or abort; a
limit stop auto-submits the last validator-green draft or waits (BLOCKED) —
never a repair round; harness faults, tripwires, budget breaches of the task
and the third protocol fault RAISE through the classes the engine already
halts on (C7), while the session's OWN cap is its LIMIT_USD stop; tool output
is capped at 8 KiB for codes and 16 KiB for DEVELOPMENT rows; and the action
trace seals straight into a workspace. Names follow the roadmap (§4) and the
SoT (T4, T5, T7, T8) exactly.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from unittest import mock

import yaml

from elt_taskgen import cli, engine as engine_mod
from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.models import canonical_json, sha256_hex
from elt_taskgen.review import loopguard as L
from elt_taskgen.review import providers as P
from elt_taskgen.review import session as S
from elt_taskgen.review.council import ProviderProtocolError
from elt_taskgen.review.tools import projection as PJ
from elt_taskgen.review.tools import registry as RG
from elt_taskgen.training import (
    install_workspace,
    load_sealed_workspace,
    load_workspace_package,
    seal_workspace,
)
from elt_taskgen.training.contract import MAX_WORKSPACE_ACTIONS
from elt_taskgen.training.models import WorkspaceActionTraceEntry
from tests.test_providers import (
    VALID_FINDING,
    FakeTransport,
    anthropic_text_response,
    anthropic_tool_response,
    make_routing,
    openai_text_response,
    openai_tool_response,
)
from tests.test_providers_session import (
    _AgenticRoles,
    _RunningTool,
    _bound,
    _provider,
    _session_ctx,
    _session_policy,
    tool_use_body,
)

ROLE = "repair_proposer"
TASK = demo_task()
SUBMIT = "submit_patch"
FIXTURE_RELEASE = Path(__file__).resolve().parent / "fixtures" / "semantic_gate" / "release"
GATE_TASK_ID = "gate__five_backend_probe"


# ---------------------------------------------------------------------------
# Doubles: tools over the real projections, a scripted provider, a fake clock
# ---------------------------------------------------------------------------

def diag(ok: bool = True, code: str = "ok", **kw) -> PJ.Diagnostic:
    return PJ.Diagnostic(source=PJ.DiagnosticSource.GATE, ok=ok, code=code, **kw)


def _empty_schema() -> dict:
    return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def _text_schema() -> dict:
    return {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }


@dataclass
class FakeTool:
    """A model-facing tool over the real projection types."""

    name: str
    permitted_roles: frozenset = frozenset({ROLE})
    input_schema: dict = field(default_factory=_empty_schema)
    cost: RG.ToolCost = field(default_factory=RG.ToolCost)
    description: str = "a fake tool"
    surface_write: bool = False
    validator: bool = False
    auto_validators: tuple = ()
    impl: Callable[[Any, dict], Any] | None = None
    calls: list = field(default_factory=list)

    def run(self, ctx, args):
        self.calls.append(dict(args))
        if self.impl is None:
            return diag()
        return self.impl(ctx, args)


def tool_use(name: str, args: dict | None = None, id_: str = "tu_1") -> dict:
    return {"type": "tool_use", "id": id_, "name": name, "input": dict(args or {})}


def text_block(text: str = "thinking out loud") -> dict:
    return {"type": "text", "text": text}


@dataclass
class ScriptedTurn:
    """What `RoutedProvider._turn` hands back: the assistant content blocks,
    the wire stop reason, usage, the metered USD and the memo key."""

    content: list
    stop_reason: str = "tool_use"
    usage: dict = field(default_factory=lambda: {"input_tokens": 100, "output_tokens": 40})
    usd: float = 0.01
    elapsed_ms: int = 7
    memo_key: str = ""
    replayed: bool = False


class ScriptedProvider:
    """A FIFO of turns. Each item is a list of content blocks, an exception
    to raise INSTEAD of answering, or a callable(turn_index) -> blocks. Every
    served turn is recorded once under a memo key over (role, policy,
    message prefix), the way the transcript store keys a session turn."""

    def __init__(self, script, *, stop_reason: str = "tool_use"):
        self.script = list(script)
        self.stop_reason = stop_reason
        self.calls: list[dict] = []
        self.store: dict[str, dict] = {}

    def _turn(self, role, messages, policy, turn_index, *, tools=None, observations=None):
        # `tools` is the runner's per-turn wire override (the terminal tools
        # once `max_tool_calls` is spent); absent, the policy's own rule.
        # `observations` are the full digests of the tool-side turns since
        # the previous model turn (folded into the real provider's memo key).
        wire = policy.wire_tools_for_turn(turn_index) if tools is None else tools
        self.calls.append(
            {
                "role": role,
                "messages": json.loads(json.dumps(messages)),
                "turn_index": turn_index,
                "last": policy.is_last_permitted_turn(turn_index),
                "wire_tools": [t["name"] for t in wire],
                "narrowed": tools is not None,
                "observations": list(observations or ()),
            }
        )
        if not self.script:
            raise AssertionError("ScriptedProvider ran out of turns")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(turn_index)
        stop_reason = self.stop_reason
        if isinstance(item, ScriptedTurn):
            turn = item
        else:
            turn = ScriptedTurn(content=list(item), stop_reason=stop_reason)
        key = sha256_hex(role + "\n" + policy.sha256() + "\n" + canonical_json(S.plain_messages(messages)))
        if key in self.store:
            raise AssertionError("a session turn was recorded twice under one memo key")
        self.store[key] = {"role": role, "turn_index": turn_index, "content": turn.content}
        turn.memo_key = key
        return turn


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_policy(tools, *, role: str = ROLE, submit: str = SUBMIT, max_turns: int = 5,
                max_tool_calls: int = 8, **limit_keys) -> S.SessionPolicy:
    limits = S.SessionLimits.declare(max_turns=max_turns, max_tool_calls=max_tool_calls, **limit_keys)
    wire = tuple(
        {"name": t.name, "description": t.description, "strict": True, "input_schema": dict(t.input_schema)}
        for t in tools
    ) + (S.abort_tool_wire(),)
    return S.SessionPolicy(
        role=role, tools=tuple(tools), submit_tool=submit, limits=limits, mode="model_driven", wire_tools=wire
    )


def submit_tool(name: str = SUBMIT) -> FakeTool:
    return FakeTool(name=name, input_schema={
        "type": "object", "properties": {"patch": {"type": "string"}},
        "required": ["patch"], "additionalProperties": False,
    })


class SessionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "attempt"
        self.root.mkdir()
        self.ctx = RG.ToolContext(root=self.root, task_id=TASK.task_id, role=ROLE, task=TASK)

    def run_session(self, script, tools, *, policy=None, worker=None, clock=None, ctx=None,
                    detector=None, **policy_kw):
        provider = ScriptedProvider(script)
        policy = policy or make_policy(tools, **policy_kw)
        result = S.run_bounded_session(
            ROLE, "the view", tools, policy, policy.limits,
            provider=provider, ctx=ctx or self.ctx, worker=worker or S.InProcessValidatorWorker(),
            detector=detector, clock=clock or time.monotonic,
        )
        return result, provider

    def assert_identities(self, result: S.SessionResult):
        self.assertEqual(
            result.model_call_count,
            result.tool_call_count + result.refused_count + result.nudge_count
            + result.correction_count + result.terminal_count + result.limit_stop_count,
        )
        self.assertEqual(
            len(result.turns),
            result.model_call_count + result.tool_call_count + result.refused_count
            + result.nudge_count + result.validator_run_count,
        )
        self.assertTrue(result.verify_chain())


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

class ReplayObservationTest(SessionCase):
    """Finding 0-1 at the runner: the FULL digest of every tool-side turn is
    handed to the next `provider._turn(observations=)` (the memo key folds
    it; the prefix carries only the capped text), and under a replay the
    executed observations are verified against the recorded session's."""

    def test_runner_folds_tool_observation_digests_into_the_next_turn_key(self):
        check = FakeTool("check_scope")
        script = [
            [tool_use("check_scope", {}, "a")],
            [tool_use("check_scope", {}, "b")],
            [tool_use(SUBMIT, {"patch": "p1"}, "c")],
        ]
        result, provider = self.run_session(script, [check, submit_tool()])
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        tool_turns = [t for t in result.turns if t.kind == "tool"]
        self.assertTrue(all(t.output_sha256 for t in tool_turns))
        self.assertEqual(
            [c["observations"] for c in provider.calls],
            [[], [tool_turns[0].output_sha256], [tool_turns[1].output_sha256]],
            "turn 0 is fed by nothing; every later turn by the observations since the previous one",
        )
        self.assertEqual(result.stale_tool_result_count, 0)
        # A refused call's fixed text is a tool-side turn too: its digest is queued.
        capped = FakeTool("check_scope", cost=RG.ToolCost(per_session=1))
        script = [
            [tool_use("check_scope", {}, "a")],
            [tool_use("check_scope", {}, "b")],
            [tool_use(SUBMIT, {"patch": "p1"}, "c")],
        ]
        result, provider = self.run_session(script, [capped, submit_tool()])
        refused = [t for t in result.turns if t.kind == "refused"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(provider.calls[2]["observations"], [refused[0].output_sha256])
        # A double that predates the keyword is called without it.
        self.assertTrue(S._accepts_keyword(provider._turn, "observations"))
        self.assertTrue(S._accepts_keyword(lambda *a, **kw: None, "observations"))
        self.assertFalse(S._accepts_keyword(lambda role, messages, policy, turn_index, *, tools=None: None, "observations"))
        self.assertFalse(S._accepts_keyword(None, "observations"))

    def test_replay_observation_mismatch_halts_as_replay_mismatch_and_counts_stale(self):
        check = FakeTool("check_scope")
        tools = [check, submit_tool()]
        policy = make_policy(tools)

        def run(expected):
            provider = ScriptedProvider([[tool_use("check_scope", {}, "a")], [tool_use(SUBMIT, {"patch": "p1"}, "b")]])
            return S.run_bounded_session(
                ROLE, "the view", tools, policy, policy.limits, provider=provider, ctx=self.ctx,
                worker=S.InProcessValidatorWorker(), expected_observations=expected,
            )

        live = run(None)
        digests = [t.output_sha256 for t in live.turns if t.kind == "tool"]
        self.assertEqual(len(digests), 1)
        same = run(digests)
        self.assertIs(same.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual((same.stale_tool_result_count, same.session_sha256), (0, live.session_sha256))
        # A recording with MORE observations than this run executes leaves
        # the remainder to the chain-digest check at the end of the session.
        longer = run(digests + ["f" * 64])
        self.assertEqual((longer.stale_tool_result_count, longer.session_sha256), (0, live.session_sha256))
        with self.assertRaises(P.SessionReplayMismatchError) as ctx:
            run(["0" * 64])
        exc = ctx.exception
        self.assertIsInstance(exc, S.ToolHarnessFault)
        self.assertIsInstance(exc, P.TranscriptMissingError)
        self.assertEqual(
            (exc.code, exc.tool, exc.boundary, exc.terminal), ("replay_mismatch", "check_scope", "tool", "HARNESS_FAULT")
        )
        partial = exc.session_result
        self.assertIs(partial.terminal, S.TerminalState.HARNESS_FAULT)
        self.assertEqual(partial.stale_tool_result_count, 1)
        self.assertEqual(partial.as_dict()["stale_tool_result_count"], 1)
        self.assertEqual((partial.fault.code, partial.fault.tool, partial.fault.turn_index), ("replay_mismatch", "check_scope", 0))
        self.assertEqual(partial.fault.exception_type, "SessionReplayMismatchError")
        self.assertEqual((partial.model_call_count, partial.tool_call_count), (1, 1))
        self.assertEqual((partial.turns[-1].kind, partial.turns[-1].output_sha256), ("tool", digests[0]))
        self.assertTrue(partial.verify_chain(), "the diverged observation is on the chain before the halt")
        self.assertIn("ToolHarnessFault", engine_mod._INFRA_EXCEPTION_NAMES)
        self.assertIn("TranscriptMissingError", engine_mod._INFRA_EXCEPTION_NAMES)


class RunnerTest(SessionCase):
    def test_interruptible_worker_stops_timed_out_write(self):
        events: list[str] = []

        def late_write(ctx, args):
            events.append("started")
            time.sleep(0.20)
            events.append("late-mutation")
            return diag()

        tool = FakeTool(
            "replace_prose",
            input_schema=_text_schema(),
            surface_write=True,
            impl=late_write,
        )
        worker = S.InterruptibleValidatorWorker()
        with self.assertRaises(S.ToolDeadlineExceeded):
            worker.run(tool, self.ctx, {"text": "draft"}, deadline_s=0.02)
        time.sleep(0.25)
        self.assertEqual(events, ["started"])

    def test_unclassified_worker_exception_leaves_the_runner_as_a_harness_fault(self):
        """Finding 2-1: a raw exception from the worker (a serialize crash, a
        post-projection I/O error) must be wrapped as a `ToolHarnessFault`
        before it leaves the runner — never a raw exception the engine would
        keyword-route on its text. The class name is infrastructure and the
        message (which may echo a `/gold/` path) is withheld."""
        from elt_taskgen import engine as engine_mod

        sentinel = "/private/answer_key/gold.csv"

        class RawWorker:
            def run(self, tool, ctx, args, *, deadline_s):
                raise ValueError(f"boom reading {sentinel}")

        check = FakeTool("check_scope")
        script = [[tool_use("check_scope", {}, "a")], [tool_use(SUBMIT, {"patch": "p"}, "b")]]
        with self.assertRaises(S.ToolHarnessFault) as ctx:
            self.run_session(script, [check, submit_tool()], worker=RawWorker())
        fault = ctx.exception
        self.assertEqual(fault.cause_type, "ValueError")  # the CLASS, never the text
        self.assertNotIn(sentinel, str(fault))
        self.assertIn(type(fault).__name__, S.INFRASTRUCTURE_FAULT_NAMES)
        self.assertEqual(engine_mod._infra_marker_for(fault), "ToolHarnessFault")
        # The partial trajectory rides along for the caller to persist.
        self.assertIsNotNone(getattr(fault, "session_result", None))

    def test_scripted_tool_loop_records_one_transcript_entry_per_turn(self):
        check = FakeTool("check_scope")
        script = [
            [tool_use("check_scope", {}, "a")],
            [tool_use("check_scope", {}, "b")],
            [tool_use(SUBMIT, {"patch": "p1"}, "c")],
        ]
        result, provider = self.run_session(script, [check, submit_tool()])
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.final, {"patch": "p1"})
        # One transcript entry per MODEL turn, keyed on the exact prefix; the
        # model records carry the same memo keys, in order.
        self.assertEqual(len(provider.store), 3)
        self.assertEqual(result.model_call_count, 3)
        model_turns = [t for t in result.turns if t.kind == "model"]
        self.assertEqual([t.memo_key for t in model_turns], list(provider.store))
        self.assertEqual([t.category for t in model_turns], ["tool_call", "tool_call", "terminal"])
        self.assertEqual(result.tool_call_count, 2)
        self.assertEqual(len(check.calls), 2)
        # Each prefix grows by the assistant turn and its tool_result.
        self.assertEqual([len(c["messages"]) for c in provider.calls], [1, 3, 5])
        answered = provider.calls[1]["messages"][2]["content"][0]
        self.assertEqual(answered["type"], "tool_result")
        self.assertEqual(answered["tool_use_id"], "a")
        # The canonical `tool_result` shape of `providers._with_tool_results`
        # (the ONE builder on both wires): `is_error` is present only when true.
        self.assertFalse(answered.get("is_error", False))
        self.assertNotIn("is_error", answered)
        self.assertIn("[gate] ok", answered["content"])
        # The chain binds every turn and the counters agree (SoT T5).
        self.assert_identities(result)
        self.assertEqual(len(result.chain_hashes), len(result.turns))
        self.assertEqual(result.session_sha256, result.chain_hashes[-1])
        self.assertEqual(result.usd, 0.03)
        self.assertEqual(result.usage["input"], 300)
        self.assertEqual(result.live_model_call_count, 3)
        self.assertIsNone(result.fault)
        self.assertEqual(result.blocked_on, "")

    def test_session_usd_cap_is_limit_usd_but_task_budget_is_halt(self):
        check = FakeTool("check_scope")
        # The session's OWN cap (RoleCapExceeded, scope role) refused the next
        # turn before transport: LIMIT_USD, a stop, blocked with the kind.
        script = [[tool_use("check_scope")], P.RoleCapExceeded("role cap", scope="role")]
        result, provider = self.run_session(script, [check, submit_tool()])
        self.assertIs(result.terminal, S.TerminalState.LIMIT_USD)
        self.assertEqual((result.limit, result.limit_scope), ("usd", "role"))
        self.assertEqual(result.blocked_on, "session_limit:usd")
        self.assertIsNone(result.fault)
        self.assertEqual(result.model_call_count, 1)
        self.assert_identities(result)
        # The TASK budget (scope task) is the transport's PROVIDER_FAULT: it
        # re-raises the engine's own class, with the partial result attached.
        for scope in ("task", "total"):
            with self.subTest(scope=scope):
                script = [[tool_use("check_scope")], P.BudgetExceededError("budget", scope=scope)]
                with self.assertRaises(P.BudgetExceededError) as ctx:
                    self.run_session(script, [FakeTool("check_scope"), submit_tool()])
                exc = ctx.exception
                self.assertEqual(exc.scope, scope)
                self.assertEqual(engine_mod._infra_marker_for(exc), "BudgetExceededError")
                partial = exc.session_result
                self.assertIs(partial.terminal, S.TerminalState.PROVIDER_FAULT)
                self.assertEqual(partial.fault.exception_type, "BudgetExceededError")
                self.assertEqual(partial.fault.terminal, "PROVIDER_FAULT")
                self.assertEqual(partial.model_call_count, 1)
                self.assert_identities(partial)
        self.assertIs(S.terminal_for_exception(P.RoleCapExceeded("x")), S.TerminalState.LIMIT_USD)
        self.assertIs(S.terminal_for_exception(P.BudgetExceededError("x", scope="task")), S.TerminalState.PROVIDER_FAULT)

    def test_limits_produce_distinct_terminal_states(self):
        read = FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True))
        submit = submit_tool()
        endless = [[tool_use("read_view", {}, f"t{i}")] for i in range(12)]
        seen = {}
        # turns
        result, _ = self.run_session(list(endless), [read, submit], max_turns=3, max_tool_calls=20)
        seen["turns"] = result.terminal
        self.assertEqual(result.blocked_on, "session_limit:turns")
        # tool calls
        result, _ = self.run_session(list(endless), [read, submit], max_turns=20, max_tool_calls=2)
        seen["tool_calls"] = result.terminal
        self.assertEqual(result.tool_call_count, 2)
        # tokens
        result, _ = self.run_session(list(endless), [read, submit], max_turns=20, max_tool_calls=20, max_output_tokens=100)
        seen["tokens"] = result.terminal
        self.assertEqual(result.blocked_on, "session_limit:tokens")
        # usd (the session's own cap)
        result, _ = self.run_session([[tool_use("read_view")], P.RoleCapExceeded("cap")], [read, submit], max_turns=20, max_tool_calls=20)
        seen["usd"] = result.terminal
        # wall
        clock = FakeClock()

        def slow(turn_index):
            clock.advance(400.0)
            return [tool_use("read_view", {}, f"w{turn_index}")]

        result, _ = self.run_session([slow, slow, slow], [read, submit], max_turns=20, max_tool_calls=20, wall_clock_s=900, clock=clock)
        seen["wall"] = result.terminal
        self.assertEqual(result.blocked_on, "session_limit:wall")
        # oracle bits
        certify = FakeTool("certify", cost=RG.ToolCost(oracle_bits=2))
        result, _ = self.run_session(
            [[tool_use("certify", {}, "c1")], [tool_use("certify", {}, "c2")], [tool_use("certify", {}, "c3")]],
            [certify, submit], max_turns=20, max_tool_calls=20, max_oracle_bits=4,
        )
        seen["oracle"] = result.terminal
        self.assertEqual(result.oracle_bits_used, 4)
        self.assertEqual(len(certify.calls), 2)
        self.assertEqual(
            seen,
            {
                "turns": S.TerminalState.LIMIT_TURNS,
                "tool_calls": S.TerminalState.LIMIT_TOOL_CALLS,
                "tokens": S.TerminalState.LIMIT_TOKENS,
                "usd": S.TerminalState.LIMIT_USD,
                "wall": S.TerminalState.LIMIT_WALL,
                "oracle": S.TerminalState.LIMIT_ORACLE,
            },
        )
        self.assertEqual(len(set(seen.values())), 6)
        for kind, terminal in seen.items():
            self.assertEqual(terminal.limit_kind, kind)
            self.assertTrue(terminal.is_limit_stop)
            self.assertFalse(terminal.is_halt)

    def test_last_turn_forces_submit_or_abort(self):
        check = FakeTool("check_scope")
        submit = submit_tool()
        # Turn 1 (of 2) is the last permitted turn: the wire offers only the
        # terminal tools, and a non-terminal call is the turn limit binding —
        # refused, never executed.
        result, provider = self.run_session(
            [[tool_use("check_scope", {}, "a")], [tool_use("check_scope", {}, "b")]],
            [check, submit], max_turns=2,
        )
        self.assertEqual([c["last"] for c in provider.calls], [False, True])
        self.assertEqual(provider.calls[1]["wire_tools"], [SUBMIT, "abort"])
        self.assertIs(result.terminal, S.TerminalState.LIMIT_TURNS)
        self.assertEqual(len(check.calls), 1)
        self.assertEqual(result.limit_stop_count, 1)
        self.assertTrue(result.turns[-1].refused)
        self.assert_identities(result)
        # ...while submit on the last turn is SUBMITTED and abort is ABSTAINED.
        result, _ = self.run_session(
            [[tool_use("check_scope", {}, "a")], [tool_use(SUBMIT, {"patch": "x"}, "b")]],
            [FakeTool("check_scope"), submit], max_turns=2,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        result, _ = self.run_session(
            [[tool_use("check_scope", {}, "a")], [tool_use("abort", {"reason_code": "cannot_repair"}, "b")]],
            [FakeTool("check_scope"), submit], max_turns=2,
        )
        self.assertIs(result.terminal, S.TerminalState.ABSTAINED)
        policy = make_policy([check, submit], max_turns=2)
        self.assertFalse(policy.is_last_permitted_turn(0))
        self.assertTrue(policy.is_last_permitted_turn(1))
        self.assertEqual(policy.terminal_tool_names, (SUBMIT, "abort"))

    def test_unpermitted_tool_is_terminal_policy_violation_and_spends_no_round(self):
        foreign = FakeTool("dev_query", permitted_roles=frozenset({"independent_implementer"}))
        check = FakeTool("check_scope")
        with mock.patch.dict(RG._ROLE_TOOLS, {"independent_implementer": (foreign,)}):
            with self.assertRaises(S.SessionPolicyViolation) as ctx:
                self.run_session(
                    [[tool_use("dev_query", {}, "a")], [tool_use(SUBMIT, {"patch": "x"}, "b")]],
                    [check, submit_tool()],
                )
        exc = ctx.exception
        self.assertEqual(exc.terminal, "POLICY_VIOLATION")
        self.assertEqual(exc.code, "tool_not_permitted")
        self.assertTrue(exc.security_event)
        self.assertIsInstance(exc, ProviderProtocolError)
        # The engine halts on the name it already classifies: no round, no
        # rejection (test_policy_violation_halts_without_spending_a_round_or_rejecting_task).
        self.assertEqual(engine_mod._infra_marker_for(exc), "ProviderProtocolError")
        self.assertEqual(foreign.calls, [])
        self.assertEqual(check.calls, [])
        partial = exc.session_result
        self.assertIs(partial.terminal, S.TerminalState.POLICY_VIOLATION)
        self.assertEqual(partial.fault.terminal, "POLICY_VIOLATION")
        self.assertEqual(partial.fault.code, "tool_not_permitted")
        self.assertEqual([e["code"] for e in partial.security_events], ["tool_not_permitted"])
        self.assertEqual(partial.model_call_count, 1)
        self.assert_identities(partial)
        # An UNKNOWN name (no role registers it) is a correction, not a violation.
        result, _ = self.run_session(
            [[tool_use("no_such_tool", {}, "a")], [tool_use(SUBMIT, {"patch": "x"}, "b")]],
            [FakeTool("check_scope"), submit_tool()],
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.correction_count, 1)
        self.assertEqual(result.security_events, ())

    def test_oracle_bits_cap_terminates_limit_oracle(self):
        certify = FakeTool("certify", cost=RG.ToolCost(oracle_bits=2, per_session=2))
        script = [[tool_use("certify", {}, "c1")], [tool_use("certify", {}, "c2")], [tool_use("certify", {}, "c3")]]
        result, _ = self.run_session(script, [certify, submit_tool()], max_oracle_bits=3)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_ORACLE)
        # The second call WOULD exceed 3 bits: refused before dispatch.
        self.assertEqual(len(certify.calls), 1)
        self.assertEqual(result.oracle_bits_used, 2)
        self.assertEqual(result.blocked_on, "session_limit:oracle")
        self.assertFalse(result.auto_submitted)
        self.assertEqual(result.limit_stop_count, 1)
        self.assertEqual(result.turns[-1].outcome_code, "oracle_cap_exceeded")
        self.assertEqual(result.security_events, ())  # a limit, not a violation
        self.assert_identities(result)
        # A tool raising OracleCapExceeded itself ends the session the same way.
        def cap(ctx, args):
            raise S.OracleCapExceeded(tool="certify")

        raising = FakeTool("certify", cost=RG.ToolCost(oracle_bits=0), impl=cap)
        result, _ = self.run_session([[tool_use("certify")]], [raising, submit_tool()], max_oracle_bits=4)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_ORACLE)

    def test_dev_population_is_not_an_argument(self):
        # 1. The context pins the population; no tool takes one.
        self.assertEqual(RG.ToolContext.population, "development")
        self.assertEqual(self.ctx.population, "development")
        with self.assertRaises(ValueError):
            make_policy([FakeTool("dev_query", input_schema={
                "type": "object", "properties": {"population": {"type": "string"}},
                "required": [], "additionalProperties": False,
            }), submit_tool()])
        # 2. A call that smuggles one is a terminal ForbiddenArgument: a
        # POLICY_VIOLATION with a security event, and the tool never runs.
        query = FakeTool("dev_query", input_schema={
            "type": "object", "properties": {"sql": {"type": "string"}},
            "required": ["sql"], "additionalProperties": False,
        })
        with self.assertRaises(S.SessionPolicyViolation) as ctx:
            self.run_session(
                [[tool_use("dev_query", {"sql": "select 1", "population": "eval_hidden"})]],
                [query, submit_tool()],
            )
        self.assertEqual(ctx.exception.code, "forbidden_argument")
        self.assertEqual(query.calls, [])
        self.assertEqual(ctx.exception.session_result.security_events[0]["detail"], "population_argument")
        # 3. Forbidden SQL and a path into a private tree are the same class.
        for args, detail in (
            ({"sql": "ATTACH 'x.duckdb'"}, "forbidden_sql"),
            ({"sql": "select * from read_csv('answer_key/gold.csv')"}, "forbidden_sql"),
            ({"sql": "select 'answer_key/gold.csv'"}, "denied_tree"),
        ):
            with self.subTest(detail=detail):
                with self.assertRaises(S.SessionPolicyViolation) as ctx:
                    self.run_session([[tool_use("dev_query", args)]], [FakeTool("dev_query", input_schema=query.input_schema), submit_tool()])
                self.assertEqual(ctx.exception.session_result.security_events[0]["detail"], detail)

    def test_tool_output_capped_at_8kib_codes_16kib_rows(self):
        self.assertEqual(S.TOOL_OUTPUT_CAP_BYTES["diagnostic"], 8 * 1024)
        self.assertEqual(S.TOOL_OUTPUT_CAP_BYTES["diagnostic_text"], 8 * 1024)
        self.assertEqual(S.TOOL_OUTPUT_CAP_BYTES["dev_rows"], 16 * 1024)
        text, truncated = S.cap_tool_output("diagnostic", "x" * (8 * 1024))
        self.assertFalse(truncated)
        text, truncated = S.cap_tool_output("diagnostic", "x" * (8 * 1024 + 1))
        self.assertTrue(truncated)
        self.assertLessEqual(len(text.encode("utf-8")), 8 * 1024)
        self.assertTrue(text.endswith("(truncated)"))
        text, truncated = S.cap_tool_output("dev_rows", "y" * (16 * 1024))
        self.assertFalse(truncated)
        text, truncated = S.cap_tool_output("dev_rows", "y" * (16 * 1024 + 1))
        self.assertTrue(truncated)
        self.assertLessEqual(len(text.encode("utf-8")), 16 * 1024)

        # End to end: 200 DEVELOPMENT rows of long strings exceed 16 KiB; the
        # delivered tool_result is cut at the cap, the observation digest is
        # over the FULL canonical bytes, and a code-only result of 8 KiB+ is
        # cut at 8 KiB.
        rows = tuple(("customer_id", "c" * 400) for _ in range(200))

        def big_rows(ctx, args):
            return PJ.DevRows(columns=("customer_id", "customer_name"), rows=rows, truncated=False)

        wide = FakeTool("dev_query", impl=big_rows, cost=RG.ToolCost(idempotent_read=True))

        def big_codes(ctx, args):
            return diag(names=("customer_id",) * 900)

        chatty = FakeTool("check_scope", impl=big_codes)
        result, provider = self.run_session(
            [[tool_use("dev_query", {}, "a")], [tool_use("check_scope", {}, "b")], [tool_use(SUBMIT, {"patch": "p"}, "c")]],
            [wide, chatty, submit_tool()],
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        delivered_rows = provider.calls[1]["messages"][2]["content"][0]["content"]
        self.assertLessEqual(len(delivered_rows.encode("utf-8")), 16 * 1024)
        self.assertTrue(delivered_rows.endswith("(truncated)"))
        self.assertGreater(len(delivered_rows.encode("utf-8")), 8 * 1024)
        delivered_codes = provider.calls[2]["messages"][4]["content"][0]["content"]
        self.assertLessEqual(len(delivered_codes.encode("utf-8")), 8 * 1024)
        self.assertTrue(delivered_codes.endswith("(truncated)"))
        full = PJ.DevRows(columns=("customer_id", "customer_name"), rows=rows, truncated=False)
        rows_turn = next(t for t in result.turns if t.kind == "tool" and t.tool_name == "dev_query")
        self.assertEqual(rows_turn.output_sha256, full.sha256)

    def test_three_protocol_faults_halt_via_provider_protocol_error(self):
        check = FakeTool("check_scope")
        # no tool call, two tool_use blocks, an unknown tool: three
        # consecutive protocol faults -> PROTOCOL_EXHAUSTED (R-C: a
        # ProviderProtocolError halt, exit 2, no round).
        script = [
            [text_block("no call")],
            [tool_use("check_scope", {}, "a"), tool_use("check_scope", {}, "b")],
            [tool_use("nope", {}, "c")],
            [tool_use(SUBMIT, {"patch": "never reached"}, "d")],
        ]
        with self.assertRaises(S.SessionProtocolError) as ctx:
            self.run_session(script, [check, submit_tool()])
        exc = ctx.exception
        self.assertIsInstance(exc, ProviderProtocolError)
        self.assertEqual(exc.terminal, "PROTOCOL_EXHAUSTED")
        self.assertEqual(exc.faults, S.PROTOCOL_FAULT_LIMIT)
        self.assertEqual(exc.last_code, "unknown_tool")
        self.assertEqual(engine_mod._infra_marker_for(exc), "ProviderProtocolError")
        self.assertEqual(check.calls, [])
        partial = exc.session_result
        self.assertIs(partial.terminal, S.TerminalState.PROTOCOL_EXHAUSTED)
        self.assertEqual(partial.correction_count, 3)
        self.assertEqual(partial.correction_kinds["schema"], 3)
        self.assertEqual([t.outcome_code for t in partial.turns], ["no_tool_call", "multiple_tool_use", "unknown_tool"])
        self.assert_identities(partial)
        # Every tool_use id of the parallel turn was answered (wire legality).
        provider = ScriptedProvider(script[:3])
        with self.assertRaises(S.SessionProtocolError):
            policy = make_policy([FakeTool("check_scope"), submit_tool()])
            S.run_bounded_session(ROLE, "v", policy.tools, policy, policy.limits, provider=provider, ctx=self.ctx, worker=S.InProcessValidatorWorker())
        answered = provider.calls[2]["messages"][4]["content"]
        self.assertEqual([b["tool_use_id"] for b in answered], ["a", "b"])
        self.assertTrue(all(b["is_error"] for b in answered))
        # A clean step between faults resets the streak: three faults spread
        # over a session with executed calls between them never exhaust it.
        script = [
            [text_block()], [tool_use("check_scope", {}, "a")],
            [text_block()], [tool_use("check_scope", {}, "b")],
            [text_block()], [tool_use(SUBMIT, {"patch": "p"}, "c")],
        ]
        result, _ = self.run_session(script, [FakeTool("check_scope"), submit_tool()], max_turns=8)
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.correction_count, 3)
        # `session.format_error_disposition: stage_fail` (declared, default
        # off) returns the terminal instead of halting; the shipped default
        # is `halt`.
        self.assertEqual(S.SessionLimits().format_error_disposition, "halt")
        result, _ = self.run_session([[text_block()]] * 3, [submit_tool()], format_error_disposition="stage_fail")
        self.assertIs(result.terminal, S.TerminalState.PROTOCOL_EXHAUSTED)
        self.assertEqual(result.fault.exception_type, "SessionProtocolError")
        self.assertEqual(result.correction_count, 3)
        self.assertIsNone(result.final)
        self.assert_identities(result)

    def test_output_truncated_halts_on_first_session_turn(self):
        check = FakeTool("check_scope")
        script = [ScriptedTurn(content=[tool_use("check_scope", {"": ""})], stop_reason="max_tokens")]
        with self.assertRaises(S.OutputTruncated) as ctx:
            self.run_session(script + [[tool_use(SUBMIT, {"patch": "p"})]], [check, submit_tool()])
        exc = ctx.exception
        self.assertEqual(exc.terminal, "OUTPUT_TRUNCATED")
        self.assertIsInstance(exc, S.ProviderFault)
        self.assertIsInstance(exc, S.SessionFault)
        self.assertEqual(exc.code, "output_truncated")
        self.assertFalse(exc.label_eligible)  # reward None: a configuration fault
        self.assertEqual(engine_mod._infra_marker_for(exc), "ProviderFault")
        self.assertEqual(check.calls, [])
        partial = exc.session_result
        self.assertIs(partial.terminal, S.TerminalState.OUTPUT_TRUNCATED)
        self.assertEqual(partial.model_call_count, 1)
        self.assertEqual(partial.correction_count, 0)  # never a protocol fault
        self.assertEqual(partial.fault.exception_type, "OutputTruncated")
        self.assert_identities(partial)
        # The chat-completions spelling halts the same way.
        script = [ScriptedTurn(content=[text_block("cut off")], stop_reason="length")]
        with self.assertRaises(S.OutputTruncated):
            self.run_session(script, [FakeTool("check_scope"), submit_tool()])
        # The one-shot complete() keeps its three attempts: the correction
        # text still names the fix and `SCHEMA_RETRIES` is untouched.
        self.assertEqual(P.SCHEMA_RETRIES, 2)
        self.assertIn("raise this role's max_tokens", P.AnthropicBackend.complete.__code__.co_consts.__repr__())

    def test_limit_stop_auto_submits_last_validator_green_draft(self):
        drafts: dict = {}

        def write(ctx, args):
            drafts["text"] = args["text"]
            return PJ.Diagnostic(source=PJ.DiagnosticSource.GATE, ok=True, code="ok")

        def check(ctx, args):
            return diag(ok=len(drafts.get("text", "")) > 3, code="ok" if len(drafts.get("text", "")) > 3 else "failed")

        replace = FakeTool("replace_prose", input_schema=_text_schema(), surface_write=True, impl=write)
        check_prose = FakeTool("check_prose", validator=True, impl=check)
        read = FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True))
        worker = S.InProcessValidatorWorker(
            surface_fingerprint=lambda ctx: sha256_hex(drafts.get("text", "")),
            current_draft=lambda ctx: {"text": drafts["text"]} if drafts else None,
        )
        # write, validate green, then read until the turn cap binds.
        script = [
            [tool_use("replace_prose", {"text": "one row per customer"}, "a")],
            [tool_use("check_prose", {}, "b")],
            [tool_use("read_view", {}, "c")],
            [tool_use("read_view", {}, "d")],
        ]
        result, _ = self.run_session(script, [replace, check_prose, read, submit_tool()], worker=worker, max_turns=4)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_TURNS)
        self.assertTrue(result.auto_submitted)
        self.assertEqual(result.final, {"text": "one row per customer"})
        self.assertEqual(result.blocked_on, "")
        self.assertEqual(result.state_epoch, 1)
        self.assert_identities(result)
        # No green draft: the stop WAITS (blocked_on carries the kind) and
        # never spends a round — the engine maps it to VERDICT_BLOCKED.
        drafts.clear()
        script = [
            [tool_use("replace_prose", {"text": "no"}, "a")],
            [tool_use("check_prose", {}, "b")],
            [tool_use("read_view", {}, "c")],
            [tool_use("read_view", {}, "d")],
        ]
        result, _ = self.run_session(script, [FakeTool("replace_prose", input_schema=_text_schema(), surface_write=True, impl=write),
                                              FakeTool("check_prose", validator=True, impl=check), FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True)), submit_tool()],
                                     worker=worker, max_turns=4)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_TURNS)
        self.assertFalse(result.auto_submitted)
        self.assertIsNone(result.final)
        self.assertEqual(result.blocked_on, "session_limit:turns")
        # An edit AFTER the green validation does not demote it: the green
        # draft is still the LAST validator-green one (SoT T4; 04 §2), and it
        # — not the edited surface — is what auto-submits.
        drafts.clear()
        script = [
            [tool_use("replace_prose", {"text": "one row per customer"}, "a")],
            [tool_use("check_prose", {}, "b")],
            [tool_use("replace_prose", {"text": "one row per customer, edited"}, "c")],
            [tool_use("read_view", {}, "d")],
        ]
        result, _ = self.run_session(script, [FakeTool("replace_prose", input_schema=_text_schema(), surface_write=True, impl=write),
                                              FakeTool("check_prose", validator=True, impl=check), FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True)), submit_tool()],
                                     worker=worker, max_turns=4)
        self.assertTrue(result.auto_submitted)
        self.assertEqual(result.final, {"text": "one row per customer"})
        self.assertEqual(result.blocked_on, "")
        self.assertEqual(result.state_epoch, 2)
        self.assertEqual(drafts["text"], "one row per customer, edited")
        # LIMIT_WALL alone never auto-submits (BLOCKED with a salt: its clock
        # includes transport latency); every agent-attributable limit stop,
        # LIMIT_ORACLE included, auto-submits the last green draft.
        self.assertFalse(S.TerminalState.LIMIT_WALL.auto_submits)
        for state in (S.TerminalState.LIMIT_TURNS, S.TerminalState.LIMIT_TOOL_CALLS, S.TerminalState.LIMIT_TOKENS,
                      S.TerminalState.LIMIT_USD, S.TerminalState.LIMIT_ORACLE, S.TerminalState.STUCK):
            self.assertTrue(state.auto_submits, state)

    def test_harness_validated_green_draft_is_recorded_without_an_epoch_bump(self):
        """A harness-validated seat whose validator INSTALLS the draft itself
        (the author's `check_prose`: `validator = True`, not a
        `surface_write`) never bumps the state epoch; its green run still
        records the draft the worker holds, so a later limit stop
        auto-submits it instead of waiting."""
        held = {"text": "one row per customer"}
        validator = FakeTool("check_prose", validator=True, impl=lambda ctx, args: diag(ok=True, code="ok"))
        read = FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True))
        worker = S.InProcessValidatorWorker(current_draft=lambda ctx: dict(held))
        script = [
            [tool_use("check_prose", {}, "a")],
            [tool_use("read_view", {}, "b")],
            [tool_use("read_view", {}, "c")],
        ]
        result, _ = self.run_session(script, [validator, read, submit_tool()], worker=worker, max_turns=3)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_TURNS)
        self.assertEqual(result.state_epoch, 0)
        self.assertTrue(result.auto_submitted)
        self.assertEqual(result.final, {"text": "one row per customer"})
        self.assertEqual(result.blocked_on, "")
        self.assert_identities(result)
        # A RED validator run records nothing: the stop waits.
        red = FakeTool("check_prose", validator=True, impl=lambda ctx, args: diag(ok=False, code="failed"))
        result, _ = self.run_session(list(script), [red, FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True)), submit_tool()],
                                     worker=worker, max_turns=3)
        self.assertFalse(result.auto_submitted)
        self.assertEqual(result.blocked_on, "session_limit:turns")

    def test_session_result_action_trace_seals_into_workspace(self):
        check = FakeTool("check_scope")
        read = FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True, per_session=1))
        script = [
            [tool_use("read_view", {}, "a")],
            [tool_use("read_view", {}, "b")],   # per-tool cap: refused
            [tool_use("check_scope", {}, "c")],
            [tool_use(SUBMIT, {"patch": "p"}, "d")],
        ]
        result, _ = self.run_session(script, [check, read, submit_tool()])
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.refused_count, 1)
        trace = result.action_trace
        self.assertTrue(all(isinstance(e, WorkspaceActionTraceEntry) for e in trace))
        self.assertEqual([e.sequence for e in trace], list(range(len(trace))))
        self.assertEqual([e.action for e in trace], ["read_view", "read_view", "check_scope", SUBMIT])
        self.assertEqual([e.outcome_code for e in trace], ["ok", "per_tool_cap", "ok", "submitted"])
        self.assertEqual([e.success for e in trace], [True, False, True, True])
        # 1:1 with the tool-side turns' observation digests.
        self.assertEqual(trace[0].observation_sha256, result.turns[1].output_sha256)
        self.assertEqual(trace[2].observation_sha256, diag().sha256)
        self.assertLessEqual(len(trace), MAX_WORKSPACE_ACTIONS)
        # It seals straight into a workspace and reads back byte-identical.
        package = load_workspace_package(FIXTURE_RELEASE, GATE_TASK_ID)
        root = Path(self._tmp.name) / "ws"
        attempt = install_workspace(package, root / "attempt")
        (attempt.elt_dir / "dbt_project.yml").write_text(
            "name: gate_task\nversion: '1.0'\nprofile: elt_training\nmodel-paths: ['models']\n", encoding="utf-8"
        )
        models = attempt.elt_dir / "models"
        models.mkdir()
        (models / "sources.yml").write_text(
            "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: customers\n", encoding="utf-8"
        )
        (models / "customer_rollup.sql").write_text(
            "{{ config(materialized='table') }}\nselect * from {{ source('raw', 'customers') }}\n", encoding="utf-8"
        )
        sealed = seal_workspace(attempt, package, root / "sealed", action_trace=result.action_trace)
        self.assertEqual(sealed.submission.action_trace, tuple(result.action_trace))
        reloaded = load_sealed_workspace(root / "sealed")
        self.assertEqual(reloaded.submission.action_trace, tuple(result.action_trace))
        # The persisted form round-trips the same entries.
        dumped = result.as_dict()
        self.assertEqual(len(dumped["action_trace"]), len(trace))
        self.assertEqual(dumped["terminal"], "submitted")
        json.dumps(dumped)

    def test_session_abort_tool_is_legitimate_stop(self):
        check = FakeTool("check_scope")
        result, _ = self.run_session(
            [[tool_use("check_scope", {}, "a")], [tool_use("abort", {"reason_code": "infeasible"}, "b")]],
            [check, submit_tool()],
        )
        self.assertIs(result.terminal, S.TerminalState.ABSTAINED)
        self.assertTrue(result.terminal.is_scored)
        self.assertEqual(result.final, {"reason_code": "infeasible"})
        self.assertIsNone(result.fault)
        self.assertEqual(result.security_events, ())
        self.assertEqual(result.blocked_on, "")
        self.assertEqual(result.terminal_count, 1)
        self.assertEqual(result.action_trace[-1].outcome_code, "abstained")
        self.assert_identities(result)
        # The reason vocabulary is closed and carries no free text: an
        # unknown reason is a correction, then abort succeeds.
        result, _ = self.run_session(
            [[tool_use("abort", {"reason_code": "because"}, "a")], [tool_use("abort", {"reason_code": "cannot_repair", "note": "x"}, "b")],
             [tool_use("abort", {"reason_code": "cannot_repair"}, "c")]],
            [FakeTool("check_scope"), submit_tool()],
        )
        self.assertIs(result.terminal, S.TerminalState.ABSTAINED)
        self.assertEqual(result.correction_count, 2)
        self.assertEqual(S.ABORT_REASON_CODES, ("infeasible", "spec_conflict", "out_of_scope", "insufficient_information", "cannot_repair"))
        self.assertEqual(S.abort_tool_wire()["input_schema"]["properties"]["reason_code"]["enum"], list(S.ABORT_REASON_CODES))

    def test_harness_validators_run_on_every_submit_and_are_not_model_callable(self):
        """SoT T1.1: a declared harness validator runs on EVERY submitted
        payload, recorded as a tool turn with fresh = True and counted in
        validator_run_count; a red result is a compile correction while
        `max_compile_corrections` allows, then the payload is accepted as is;
        the model cannot call a `harness_only` validator by name."""
        seen: list[dict] = []

        def check(ctx, args):
            seen.append(dict(args))
            return diag(ok=len(args.get("patch", "")) > 2, code="ok" if len(args.get("patch", "")) > 2 else "failed")

        validator = FakeTool("check_prose", input_schema={
            "type": "object", "properties": {"patch": {"type": "string"}}, "required": ["patch"], "additionalProperties": False,
        }, validator=True, impl=check)
        validator.harness_only = True
        script = [
            [tool_use("check_prose", {"patch": "x"}, "a")],       # not callable: correction
            [tool_use(SUBMIT, {"patch": "no"}, "b")],             # red: one compile correction
            [tool_use(SUBMIT, {"patch": "still"}, "c")],          # green: submitted
        ]
        result, provider = self.run_session(
            script, [validator, submit_tool()], max_turns=5, harness_validators=["check_prose"], max_compile_corrections=1,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.final, {"patch": "still"})
        self.assertEqual(seen, [{"patch": "no"}, {"patch": "still"}])
        self.assertEqual(result.validator_run_count, 2)
        self.assertEqual(result.correction_count, 2)
        self.assertEqual(dict(result.correction_kinds), {"schema": 1, "compile": 1})
        validator_turns = [t for t in result.turns if t.kind == "validator"]
        self.assertEqual([t.outcome_code for t in validator_turns], ["failed", "ok"])
        self.assertTrue(all(t.fresh for t in validator_turns))
        red = provider.calls[2]["messages"][-1]["content"][0]
        self.assertTrue(red["is_error"])
        self.assertIn("failed", red["content"])
        self.assert_identities(result)
        # Corrections exhausted: the second red submit is accepted as is.
        seen.clear()
        validator = FakeTool("check_prose", input_schema=validator.input_schema, validator=True, impl=check)
        result, _ = self.run_session(
            [[tool_use(SUBMIT, {"patch": "no"}, "a")], [tool_use(SUBMIT, {"patch": "no"}, "b")]],
            [validator, submit_tool()], harness_validators=["check_prose"], max_compile_corrections=1,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.final, {"patch": "no"})
        self.assertEqual(result.validator_run_count, 2)
        self.assertEqual(result.correction_count, 1)


# PERMIT accounting covers execution/refusal charges, caps, validator bits,
# parse order, worker protocol faults, and forced tool limits.

@dataclass
class HookTool(FakeTool):
    """A `FakeTool` with the optional PERMIT-time `permit(ctx, args)` hook."""

    permit_impl: Callable[[Any, dict], str] | None = None

    def permit(self, ctx, args):
        return self.permit_impl(ctx, dict(args)) if self.permit_impl is not None else ""


class PermitAccountingTest(SessionCase):
    def test_no_cost_refusal_codes_charge_no_oracle_bits(self):
        """Oracle bits are charged for what the tool MEASURED: an outcome in
        its declared `cost.no_cost_codes` charges nothing, so two no-cost
        refusals never exhaust `max_oracle_bits: 4` ahead of the two real
        calls it admits."""
        seen: list[int] = []
        # Distinct arguments per call keep the stuck detector out of the way.
        numbered = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"], "additionalProperties": False}

        def certify(ctx, args):
            seen.append(1)
            return diag(ok=False, code="failed") if len(seen) <= 2 else diag()

        tool = FakeTool("certify", input_schema=numbered, cost=RG.ToolCost(oracle_bits=2, no_cost_codes=frozenset({"failed"})), impl=certify)
        script = [[tool_use("certify", {"n": i}, f"c{i}")] for i in range(4)] + [[tool_use(SUBMIT, {"patch": "p"}, "s")]]
        result, _ = self.run_session(script, [tool, submit_tool()], max_turns=8, max_oracle_bits=4)
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(len(seen), 4)
        self.assertEqual(result.oracle_bits_used, 4)
        codes = [t.outcome_code for t in result.turns if t.kind == "tool"]
        self.assertEqual(codes, ["failed", "failed", "ok", "ok"])
        self.assert_identities(result)
        # Without the declaration the same refusals ARE charged (the tool
        # ran and reported a measured failure), and the cap binds early.
        seen.clear()
        priced = FakeTool("certify", input_schema=numbered, cost=RG.ToolCost(oracle_bits=2), impl=certify)
        result, _ = self.run_session(list(script), [priced, submit_tool()], max_turns=8, max_oracle_bits=4)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_ORACLE)
        self.assertEqual((len(seen), result.oracle_bits_used), (2, 4))

    def test_permit_hook_refusal_spends_no_bits_and_no_worker(self):
        """`Tool.permit(ctx, args)` returning a declared no-cost code at PERMIT
        is a `refused` turn answered with the fixed refusal text: no worker
        spawned, no bits charged, counted toward `max_tool_calls`, and — as an
        observed error — toward R2, so the same refused call repeated is
        nudged on its third proposal. An UNDECLARED code from the hook is a
        wiring defect: a `ToolHarnessFault` halt, never something the model
        sees."""
        calls: list[dict] = []
        proposals: list[int] = []
        tool = HookTool(
            "certify",
            cost=RG.ToolCost(oracle_bits=2, no_cost_codes=frozenset({"failed"})),
            # The FIRST proposal is refused at PERMIT; the second dispatches.
            permit_impl=lambda ctx, args: "failed" if not proposals.append(1) and len(proposals) == 1 else "",
            impl=lambda ctx, args: calls.append(dict(args)) or diag(),
        )
        script = [[tool_use("certify", {}, "c0")], [tool_use("certify", {}, "c1")], [tool_use(SUBMIT, {"patch": "p"}, "s")]]
        result, provider = self.run_session(script, [tool, submit_tool()], max_oracle_bits=4)
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.refused_count, 1)
        self.assertEqual(result.tool_call_count, 1)
        self.assertEqual(result.oracle_bits_used, 2)
        refused = [t for t in result.turns if t.kind == "refused"]
        self.assertEqual([(t.tool_name, t.outcome_code, t.refused) for t in refused], [("certify", "failed", True)])
        self.assertEqual(refused[0].output_sha256, sha256_hex(S.REFUSAL_TEXT.format(code="failed")))
        answered = provider.calls[1]["messages"][-1]["content"][0]
        self.assertEqual((answered["is_error"], answered["content"]), (True, S.REFUSAL_TEXT.format(code="failed")))
        self.assertEqual([e.action for e in result.action_trace][:1], ["certify"])
        self.assert_identities(result)
        # Repeated verbatim, the cheap refusal counts toward R2: the third
        # identical proposal is nudged, not refused again.
        always = HookTool("certify", cost=RG.ToolCost(oracle_bits=2, no_cost_codes=frozenset({"failed"})),
                          permit_impl=lambda ctx, args: "failed")
        script = [[tool_use("certify", {}, f"c{i}")] for i in range(3)] + [[tool_use(SUBMIT, {"patch": "p"}, "s")]]
        result, _ = self.run_session(script, [always, submit_tool()], max_oracle_bits=4)
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual((result.refused_count, result.nudge_count, result.oracle_bits_used), (2, 1, 0))
        self.assertEqual([e.rule for e in result.detector_events], ["r2"])
        self.assertEqual(always.calls, [])
        # An undeclared refusal code is the harness's defect: a halt.
        bogus = HookTool("certify", cost=RG.ToolCost(oracle_bits=2), permit_impl=lambda ctx, args: "failed")
        with self.assertRaises(S.ToolHarnessFault) as ctx:
            self.run_session([[tool_use("certify", {}, "c0")]], [bogus, submit_tool()], max_oracle_bits=4)
        self.assertEqual(ctx.exception.code, "undeclared_refusal_code")
        partial = ctx.exception.session_result
        self.assertIs(partial.terminal, S.TerminalState.HARNESS_FAULT)
        self.assertEqual(bogus.calls, [])
        self.assert_identities(partial)

    def test_per_session_cap_and_max_certify_refuse_the_next_call_at_permit(self):
        """The per-tool ceiling (`cost.per_session`) and the role's
        `max_certify` (the certify verb's ceiling, SoT T1 RPR) are PERMIT-time
        `per_tool_cap` refusals, judged BEFORE the oracle accounting: a call
        the ceiling denies spawns no worker and charges no bit, and the
        session goes on to submit — never an in-worker stop."""
        certify = FakeTool("certify", cost=RG.ToolCost(oracle_bits=2))
        script = [[tool_use("certify", {}, f"c{i}")] for i in range(3)] + [[tool_use(SUBMIT, {"patch": "p"}, "s")]]
        result, _ = self.run_session(script, [certify, submit_tool()], max_certify=2, max_oracle_bits=10)
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(len(certify.calls), 2)
        self.assertEqual((result.refused_count, result.oracle_bits_used), (1, 4))
        self.assertEqual([t.outcome_code for t in result.turns if t.kind == "refused"], ["per_tool_cap"])
        self.assert_identities(result)
        # The tighter of the two ceilings binds.
        certify = FakeTool("certify", cost=RG.ToolCost(oracle_bits=2, per_session=1))
        result, _ = self.run_session(list(script), [certify, submit_tool()], max_certify=2, max_oracle_bits=10)
        self.assertEqual((len(certify.calls), result.refused_count), (1, 2))
        # The oracle cap still binds first when the bits run out before the ceiling.
        certify = FakeTool("certify", cost=RG.ToolCost(oracle_bits=2, per_session=3))
        result, _ = self.run_session(list(script), [certify, submit_tool()], max_certify=3, max_oracle_bits=3)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_ORACLE)
        self.assertEqual(len(certify.calls), 1)

    def test_harness_validator_runs_charge_oracle_bits_and_stop_limit_oracle(self):
        """A priced harness validator (`compile_proposal` at 3 bits) charges
        its `cost.oracle_bits` on every run, so `max_oracle_bits: 6` binds:
        the run that would exceed it is not made and the session ends as
        LIMIT_ORACLE (04 §5 item 5) — the last green draft auto-submitting
        when one exists, else BLOCKED with the kind."""
        compile_proposal = FakeTool(
            "compile_proposal", input_schema=submit_tool().input_schema, validator=True,
            cost=RG.ToolCost(oracle_bits=3), impl=lambda ctx, args: diag(ok=False, code="failed"),
        )
        compile_proposal.harness_only = True
        script = [[tool_use(SUBMIT, {"patch": f"p{i}"}, f"s{i}")] for i in range(3)]
        result, _ = self.run_session(
            script, [compile_proposal, submit_tool()], max_turns=6, harness_validators=["compile_proposal"],
            max_compile_corrections=3, max_oracle_bits=6,
        )
        self.assertIs(result.terminal, S.TerminalState.LIMIT_ORACLE)
        self.assertEqual((result.validator_run_count, result.oracle_bits_used), (2, 6))
        self.assertEqual(len(compile_proposal.calls), 2)
        self.assertEqual(result.correction_count, 2)
        self.assertEqual(result.limit_stop_count, 1)
        self.assertEqual((result.turns[-1].category, result.turns[-1].outcome_code), ("limit_stop", "oracle_cap_exceeded"))
        self.assertEqual(result.blocked_on, "session_limit:oracle")
        self.assert_identities(result)
        # With the bits to spare the same validators run and the third,
        # corrections spent, is accepted as is.
        compile_proposal = FakeTool(
            "compile_proposal", input_schema=submit_tool().input_schema, validator=True,
            cost=RG.ToolCost(oracle_bits=3), impl=lambda ctx, args: diag(ok=False, code="failed"),
        )
        compile_proposal.harness_only = True
        result, _ = self.run_session(
            list(script), [compile_proposal, submit_tool()], max_turns=6, harness_validators=["compile_proposal"],
            max_compile_corrections=2, max_oracle_bits=9,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual((result.validator_run_count, result.oracle_bits_used), (3, 9))
        # A priced auto-validator of a write charges the same way.
        check = FakeTool("check_load_plan", validator=True, cost=RG.ToolCost(oracle_bits=3))
        check.harness_only = True
        replace = FakeTool("replace_load_plan", input_schema=_text_schema(), surface_write=True, auto_validators=("check_load_plan",))
        worker = S.InProcessValidatorWorker(surface_fingerprint=lambda ctx: sha256_hex(str(len(replace.calls))))
        script = [[tool_use("replace_load_plan", {"text": "a"}, "w0")], [tool_use("replace_load_plan", {"text": "b"}, "w1")]]
        result, _ = self.run_session(script, [replace, check, submit_tool()], worker=worker, max_oracle_bits=3)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_ORACLE)
        self.assertEqual((len(check.calls), result.oracle_bits_used, result.validator_run_count), (1, 3, 1))
        self.assert_identities(result)

    def test_stuck_override_in_the_limits_reaches_the_detector(self):
        """A `stuck:` override declared in the role's session block is what
        the runner's detector enforces (SoT T5): `SessionPolicy` derives its
        `stuck_thresholds` from `limits.stuck` when the caller left the module
        default, and the nudge ordinal moves with it."""
        override = {"identical_pairs_nudge": 2, "identical_pairs_halt": 3, "error_streak_nudge": 2, "error_streak_halt": 3}
        check = FakeTool("check_scope")
        policy = make_policy([check, submit_tool()], stuck=override)
        self.assertEqual(dict(policy.stuck_thresholds), {**S.STUCK_DETECTOR_THRESHOLDS, **override})
        self.assertNotEqual(policy.sha256(), make_policy([check, submit_tool()]).sha256())
        script = [[tool_use("check_scope", {}, f"c{i}")] for i in range(3)]
        result, _ = self.run_session(list(script), [check, submit_tool()], policy=policy)
        self.assertIs(result.terminal, S.TerminalState.STUCK)
        self.assertEqual(len(check.calls), 1)
        self.assertEqual([(e.rule, e.verdict, e.attempt) for e in result.detector_events], [("r1", "nudge", 2), ("r1", "halt", 3)])
        # The module default: nudge on the third proposal, halt on the fourth.
        check = FakeTool("check_scope")
        result, _ = self.run_session([[tool_use("check_scope", {}, f"c{i}")] for i in range(4)], [check, submit_tool()])
        self.assertEqual([(e.verdict, e.attempt) for e in result.detector_events], [("nudge", 3), ("halt", 4)])
        self.assertEqual(dict(make_policy([check]).stuck_thresholds), dict(S.STUCK_DETECTOR_THRESHOLDS))
        # An explicit `stuck_thresholds=` still wins over the block.
        explicit = S.SessionPolicy(role=ROLE, tools=(check,), submit_tool=SUBMIT, limits=policy.limits,
                                   stuck_thresholds={**S.STUCK_DETECTOR_THRESHOLDS, "identical_pairs_halt": 6})
        self.assertEqual(explicit.stuck_thresholds["identical_pairs_halt"], 6)
        self.assertEqual(explicit.stuck_thresholds["identical_pairs_nudge"], 3)

    def test_schema_invalid_args_are_a_correction_before_the_argument_rules(self):
        """PARSE precedes PERMIT (04 §1): a schema-invalid call is the
        `invalid_arguments` correction even when its values would also trip
        a SQL or path rule; the same values in a well-formed call are the
        `forbidden_argument` violation. The population rule alone outranks
        the schema (a documented anti-evasion rule, SoT T3 / matrix §4)."""
        query = FakeTool("dev_query", input_schema={
            "type": "object", "properties": {"sql": {"type": "string"}},
            "required": ["sql"], "additionalProperties": False,
        })
        result, _ = self.run_session(
            [[tool_use("dev_query", {"sql": "ATTACH 'x.duckdb'", "extra": 1}, "a")], [tool_use(SUBMIT, {"patch": "p"}, "b")]],
            [query, submit_tool()],
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual((result.correction_count, result.security_events), (1, ()))
        self.assertEqual(query.calls, [])
        with self.assertRaises(S.SessionPolicyViolation) as ctx:
            self.run_session([[tool_use("dev_query", {"sql": "ATTACH 'x.duckdb'"}, "a")]], [query, submit_tool()])
        self.assertEqual(ctx.exception.fault.detail, "forbidden_sql")
        with self.assertRaises(S.SessionPolicyViolation) as ctx:
            self.run_session([[tool_use("dev_query", {"sql": "select 1", "population": "x"}, "a")]], [query, submit_tool()])
        self.assertEqual(ctx.exception.fault.detail, "population_argument")

    def test_non_canonical_locator_index_is_forbidden_argument_at_permit_without_a_worker(self):
        """Phase 3 re-check verdict (locator canonicalization): a locator-typed
        argument (`field`, `locator`) whose list-index segment is an alias of
        the canonical unsigned spelling — negative, zero-padded, signed,
        whitespace, a digit-group underscore — is the
        `ForbiddenArgument(non_canonical_index)` at PERMIT: a violation with
        a security event, judged BEFORE the schema like the population rule
        (`SCHEMA_OUTRANKING_ARGUMENT_RULES`), so a strict wire pattern never
        turns the probe into a free correction, and the tool never runs. The
        canonical spelling reaches the tool; a non-locator path argument is
        not held to the rule."""
        from elt_taskgen.review.tools import validators as V

        read = FakeTool("read_field", input_schema=json.loads(json.dumps(dict(V.proposer_tool("read_field").input_schema))))
        for alias in ("populations.-1.conditions.2", "populations.01.conditions.2", "populations.+1.conditions",
                      "populations. 1.conditions.2", "tables.1_0.columns"):
            with self.subTest(alias=alias):
                with self.assertRaises(S.SessionPolicyViolation) as ctx:
                    self.run_session([[tool_use("read_field", {"field": alias}, "a")]], [read, submit_tool()])
                fault = ctx.exception.fault
                self.assertIsInstance(fault, S.ForbiddenArgument)
                self.assertEqual((fault.tool, fault.code, fault.detail), ("read_field", "forbidden_argument", "non_canonical_index"))
                partial = ctx.exception.session_result
                self.assertIs(partial.terminal, S.TerminalState.POLICY_VIOLATION)
                self.assertEqual((partial.correction_count, partial.tool_call_count), (0, 0))
                self.assertEqual([e["code"] for e in partial.security_events], ["forbidden_argument"])
                self.assertEqual(read.calls, [])
                self.assert_identities(partial)
        # Outranking the schema: a call that is ALSO schema-invalid (an extra
        # property, a pattern the wire refuses) is the violation, not the
        # `invalid_arguments` correction a plain malformed call earns.
        with self.assertRaises(S.SessionPolicyViolation) as ctx:
            self.run_session([[tool_use("read_field", {"field": "populations.-1.conditions.2", "extra": 1}, "a")]], [read, submit_tool()])
        self.assertEqual(ctx.exception.fault.detail, "non_canonical_index")
        self.assertEqual(read.calls, [])
        result, _ = self.run_session(
            [[tool_use("read_field", {"field": "Tables.0", "extra": 1}, "a")], [tool_use(SUBMIT, {"patch": "p"}, "b")]],
            [read, submit_tool()],
        )
        self.assertEqual((result.terminal, result.correction_count, read.calls), (S.TerminalState.SUBMITTED, 1, []))
        # The canonical spelling reaches the tool.
        result, _ = self.run_session(
            [[tool_use("read_field", {"field": "populations.1.conditions.2"}, "a")], [tool_use(SUBMIT, {"patch": "p"}, "b")]],
            [read, submit_tool()],
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(read.calls, [{"field": "populations.1.conditions.2"}])
        self.assertEqual(result.security_events, ())
        # The rule is scoped to locator arguments: a path argument holding
        # `01` is judged by the path rules alone.
        self.assertEqual(S._forbidden_argument({"path": "parts/01.parquet"}), "")
        self.assertEqual(S._forbidden_argument({"locator": "populations.01.conditions"}), "non_canonical_index")
        self.assertEqual(S._forbidden_argument({"field_path": "tables.-1"}), "non_canonical_index")
        self.assertEqual(S.locator_argument_problem("tables.0.columns.1.type"), "")
        self.assertIn("non_canonical_index", S.SCHEMA_OUTRANKING_ARGUMENT_RULES)
        self.assertIn("population_argument", S.SCHEMA_OUTRANKING_ARGUMENT_RULES)

    def test_an_outranking_rule_is_not_shadowed_by_another_argument_problem(self):
        """finding p4-2-1: `_forbidden_argument` answers the FIRST problem it
        finds in the model's own JSON key order, so ANY earlier path- or
        SQL-typed argument with a non-outranking problem shadowed the
        anti-evasion rules.

        `{"path": "/abs", "field": "populations.-1.conditions.2"}` returned
        `absolute_path`, the strict schema then refused the call as
        `invalid_arguments`, and the aliasing-index probe became a FREE
        correction with no violation and no security event — taking the
        absolute-path argument down with it, so `security_events` and the
        metrology policy-violation counter under-reported by construction.
        The outranking pass now reads every argument first, whatever else the
        call carries, and `_submit` applies it to the submitted payload too.
        """
        from elt_taskgen.review.tools import validators as V

        read = FakeTool(
            "read_field",
            input_schema=json.loads(
                json.dumps(dict(V.proposer_tool("read_field").input_schema))
            ),
        )
        alias = "populations.-1.conditions.2"
        # The SHADOWED shapes: an earlier problem no longer hides the rule.
        for args, detail in (
            ({"path": "/Users/x/answer_key.duckdb", "field": alias}, "non_canonical_index"),
            ({"artifact": "runs/x", "locator": alias}, "non_canonical_index"),
            ({"sql": "SET a=1", "e": [{"population": "GOLD"}]}, "population_argument"),
        ):
            with self.subTest(args=sorted(args)):
                self.assertEqual(S._outranking_argument_problem(args), detail)
                with self.assertRaises(S.SessionPolicyViolation) as ctx:
                    self.run_session(
                        [[tool_use("read_field", args, "a")]], [read, submit_tool()]
                    )
                fault = ctx.exception.fault
                self.assertIsInstance(fault, S.ForbiddenArgument)
                self.assertEqual(fault.detail, detail)
                partial = ctx.exception.session_result
                self.assertIs(partial.terminal, S.TerminalState.POLICY_VIOLATION)
                self.assertEqual(
                    [e["code"] for e in partial.security_events], ["forbidden_argument"]
                )
                self.assertEqual(partial.correction_count, 0)
                self.assertEqual(read.calls, [])
        # A clean call is untouched, and a plain non-outranking problem is
        # still judged after the schema exactly as before.
        self.assertEqual(S._outranking_argument_problem({"path": "parts/01.parquet"}), "")
        self.assertEqual(
            S._outranking_argument_problem({"sql": "SET a=1", "path": "/abs"}), ""
        )
        # SUBMIT: the outranking pass runs BEFORE the payload schema, so the
        # probe cannot be downgraded there either.
        with self.assertRaises(S.SessionPolicyViolation) as ctx:
            self.run_session(
                [[tool_use(SUBMIT, {"patch": "p", "field": alias}, "s")]],
                [read, submit_tool()],
            )
        self.assertEqual(ctx.exception.fault.detail, "non_canonical_index")
        self.assertIs(
            ctx.exception.session_result.terminal, S.TerminalState.POLICY_VIOLATION
        )

    def test_a_policy_fault_from_a_harness_run_validator_is_classified(self):
        """finding p4-2-6: a `PolicyFault` raised by a HARNESS-run validator
        (the `auto_validators` loop after a write, `_submit`'s declared
        validator loop) or by a tool's `permit` hook escaped
        `run_bounded_session` BARE — not wrapped in `SessionPolicyViolation`,
        with no `session_result` attached.

        Under a metrology trial `providers._run_session_core` could then not
        turn it into the seat outcome C7/SoT T4 require and the run exited 2;
        in the production review path it reached the engine as an
        unclassified `RuntimeError` to be keyword-routed on its text. The
        runner now classifies every escaping `PolicyFault` by its own
        vocabulary and always attaches the partial result.
        """

        def refuse(ctx, args):
            raise S.ForbiddenArgument(tool="check_load_plan", detail="denied_tree")

        validator = FakeTool("check_load_plan", impl=refuse)
        writer = FakeTool("replace_load_plan", surface_write=True,
                          auto_validators=("check_load_plan",))
        with self.assertRaises(S.SessionPolicyViolation) as ctx:
            self.run_session(
                [[tool_use("replace_load_plan", {}, "w")]],
                [writer, validator, submit_tool()],
            )
        fault = ctx.exception.fault
        self.assertIsInstance(fault, S.ForbiddenArgument)
        partial = ctx.exception.session_result
        self.assertIsNotNone(partial)
        self.assertIs(partial.terminal, S.TerminalState.POLICY_VIOLATION)
        self.assertEqual(
            [e["code"] for e in partial.security_events], ["forbidden_argument"]
        )
        # An `OracleCapExceeded` from the same site is a LIMIT, never a
        # violation (SoT T4): it stops the session, it does not halt it.
        def cap(ctx, args):
            raise S.OracleCapExceeded(tool="check_load_plan")

        capped = FakeTool("check_load_plan", impl=cap)
        result, _ = self.run_session(
            [[tool_use("replace_load_plan", {}, "w")]],
            [FakeTool("replace_load_plan", surface_write=True,
                      auto_validators=("check_load_plan",)), capped, submit_tool()],
        )
        self.assertIs(result.terminal, S.TerminalState.LIMIT_ORACLE)
        self.assertEqual(result.security_events, ())

    def test_worker_typed_protocol_fault_counts_toward_the_three_fault_streak(self):
        """A `ToolProtocolFault` a tool raises AFTER PERMIT (a bad reason code,
        a second artifact) is answered as one fixed code and counts toward
        R5 exactly like a dispatcher correction: three in a row are
        PROTOCOL_EXHAUSTED, and a clean step in between resets the streak."""

        def bad(ctx, args):
            raise S.ToolProtocolFault("invalid_arguments", tool="apply_edit_trial", detail="artifact")

        tool = FakeTool("apply_edit_trial", impl=bad)
        script = [[tool_use("apply_edit_trial", {}, f"e{i}")] for i in range(3)]
        with self.assertRaises(S.SessionProtocolError) as ctx:
            self.run_session(script, [tool, submit_tool()])
        self.assertEqual((ctx.exception.faults, ctx.exception.last_code), (3, "invalid_arguments"))
        partial = ctx.exception.session_result
        self.assertIs(partial.terminal, S.TerminalState.PROTOCOL_EXHAUSTED)
        self.assertEqual(partial.model_call_count, 3)
        self.assertEqual([t.outcome_code for t in partial.turns if t.kind == "tool"], ["invalid_arguments"] * 3)
        self.assert_identities(partial)
        # A dispatcher correction and a worker fault share one streak; a
        # clean step resets it.
        ok = FakeTool("check_scope")
        script = [
            [tool_use("apply_edit_trial", {}, "e0")],
            [tool_use("no_such_tool", {}, "u0")],
            [tool_use("check_scope", {}, "c0")],
            [tool_use("apply_edit_trial", {}, "e1")],
            [tool_use("apply_edit_trial", {}, "e2")],
            [tool_use(SUBMIT, {"patch": "p"}, "s")],
        ]
        result, _ = self.run_session(script, [FakeTool("apply_edit_trial", impl=bad), ok, submit_tool()], max_turns=8)
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assert_identities(result)

    def test_max_tool_calls_zero_is_a_hard_zero_and_max_writes_binds(self):
        """`max_tool_calls: 0` caps at ZERO: the wire is narrowed to the
        terminal tools from the first turn and a model-initiated call is
        LIMIT_TOOL_CALLS, never executed. `max_writes` declared in the block
        refuses the write past it at PERMIT (`max_writes`, no worker)."""
        check = FakeTool("check_scope")
        result, provider = self.run_session([[tool_use("check_scope", {}, "a")]], [check, submit_tool()], max_tool_calls=0)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_TOOL_CALLS)
        self.assertEqual(check.calls, [])
        self.assertEqual(provider.calls[0]["wire_tools"], [SUBMIT, "abort"])
        self.assertTrue(provider.calls[0]["narrowed"])
        self.assertEqual((result.turns[-1].category, result.turns[-1].outcome_code), ("limit_stop", "limit_tool_calls"))
        self.assert_identities(result)
        # ...while submitting on that turn is SUBMITTED.
        result, _ = self.run_session([[tool_use(SUBMIT, {"patch": "p"}, "s")]], [FakeTool("check_scope"), submit_tool()], max_tool_calls=0)
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        # max_writes.
        written: list[str] = []
        write = FakeTool("apply_edit_trial", input_schema=_text_schema(), surface_write=True,
                         impl=lambda ctx, args: written.append(args["text"]) or diag())
        worker = S.InProcessValidatorWorker(surface_fingerprint=lambda ctx: sha256_hex("".join(written)))
        script = [[tool_use("apply_edit_trial", {"text": "a"}, "w0")], [tool_use("apply_edit_trial", {"text": "b"}, "w1")],
                  [tool_use(SUBMIT, {"patch": "p"}, "s")]]
        result, _ = self.run_session(script, [write, submit_tool()], worker=worker, max_writes=1)
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(written, ["a"])
        self.assertEqual([t.outcome_code for t in result.turns if t.kind == "refused"], ["max_writes"])
        self.assertEqual(result.state_epoch, 1)
        self.assert_identities(result)

    def test_tool_cap_exhausted_still_gets_a_submit_or_abort_turn(self):
        """Spending exactly `max_tool_calls` is not a limit stop: the next
        turn is offered with the wire narrowed to submit and abort (the
        last-turn forcing, 04 §1), and only a model-initiated call on it is
        LIMIT_TOOL_CALLS."""
        read = FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True))
        script = [[tool_use("read_view", {}, "r0")], [tool_use("read_view", {}, "r1")], [tool_use(SUBMIT, {"patch": "p"}, "s")]]
        result, provider = self.run_session(script, [read, submit_tool()], max_turns=5, max_tool_calls=2)
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual([c["narrowed"] for c in provider.calls], [False, False, True])
        self.assertEqual(provider.calls[2]["wire_tools"], [SUBMIT, "abort"])
        self.assertEqual(result.tool_call_count, 2)
        self.assert_identities(result)
        # Abort on that turn is ABSTAINED; another read is the cap binding.
        result, _ = self.run_session(
            [[tool_use("read_view", {}, "r0")], [tool_use("read_view", {}, "r1")], [tool_use("abort", {"reason_code": "cannot_repair"}, "x")]],
            [FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True)), submit_tool()], max_turns=5, max_tool_calls=2,
        )
        self.assertIs(result.terminal, S.TerminalState.ABSTAINED)
        read = FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True))
        result, _ = self.run_session([[tool_use("read_view", {}, f"r{i}")] for i in range(3)], [read, submit_tool()], max_turns=5, max_tool_calls=2)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_TOOL_CALLS)
        self.assertEqual((len(read.calls), result.tool_call_count, result.limit_stop_count), (2, 2, 1))
        self.assertEqual(result.blocked_on, "session_limit:tool_calls")
        self.assert_identities(result)

    def test_format_error_disposition_reads_the_session_wide_default(self):
        """`session.format_error_disposition` at the document's top level
        (the documented key) is the default a role block may override; it
        rides beside the block un-hashed, so declaring it re-keys nothing."""
        limits = S.SessionLimits.from_block({"max_turns": 3}, session_defaults={"format_error_disposition": "stage_fail"})
        self.assertEqual(limits.format_error_disposition, "stage_fail")
        self.assertEqual(limits.as_manifest(), {"max_turns": 3})
        self.assertEqual(S.SessionLimits.from_block({"max_turns": 3}).as_manifest(), limits.as_manifest())
        self.assertEqual(S.SessionLimits.from_block({"max_turns": 3}).format_error_disposition, "halt")
        self.assertEqual(
            S.SessionLimits.from_block({"format_error_disposition": "halt"}, session_defaults={"format_error_disposition": "stage_fail"}).format_error_disposition,
            "halt",
        )
        self.assertEqual(limits.with_session_defaults({}).format_error_disposition, "halt")
        with self.assertRaises(ValueError):
            S.SessionLimits.from_block({}, session_defaults={"format_error_disposition": "retry"})
        # The shipped document declares `halt` at the top level and the
        # policy reads it from there.
        policy = P.session_policy_for("repair_proposer")
        self.assertEqual(dict(policy.limits.session_defaults), {"format_error_disposition": "halt"})
        self.assertEqual(policy.limits.format_error_disposition, "halt")
        # Under `stage_fail` the third protocol fault is RETURNED, not raised.
        policy = S.SessionPolicy(role=ROLE, tools=(submit_tool(),), submit_tool=SUBMIT, limits=limits, mode="model_driven",
                                 wire_tools=make_policy([submit_tool()]).wire_tools)
        result, _ = self.run_session([[text_block()]] * 3, [submit_tool()], policy=policy)
        self.assertIs(result.terminal, S.TerminalState.PROTOCOL_EXHAUSTED)
        self.assertEqual(result.fault.exception_type, "SessionProtocolError")


# ---------------------------------------------------------------------------
# One test per TerminalState (SoT T4)
# ---------------------------------------------------------------------------

class TerminalStateCoverageTest(SessionCase):
    def test_terminal_state_vocabulary_matches_sot(self):
        self.assertEqual(
            [s.name for s in S.TerminalState],
            ["SUBMITTED", "ABSTAINED", "LIMIT_TURNS", "LIMIT_TOOL_CALLS", "LIMIT_TOKENS", "LIMIT_USD",
             "LIMIT_WALL", "LIMIT_WORKING", "LIMIT_ORACLE", "STUCK", "PROTOCOL_EXHAUSTED", "OUTPUT_TRUNCATED",
             "POLICY_VIOLATION", "LEAK_TRIPWIRE", "HARNESS_FAULT", "PROVIDER_FAULT"],
        )
        for state in S.TerminalState:
            expected = "stuck_in_a_loop" if state is S.TerminalState.STUCK else state.name.lower()
            self.assertEqual(state.value, expected, state)
            self.assertIs(S.TerminalState.from_name(state.name), state)
            self.assertIs(S.TerminalState.from_name(state.value), state)
            self.assertEqual(sum([state.is_scored, state.is_limit_stop, state.is_halt]), 1, state)
        # Every Phase 0 fault class maps onto its terminal.
        cases = {
            S.ProviderFault("x"): "PROVIDER_FAULT",
            S.ToolHarnessFault("t"): "HARNESS_FAULT",
            S.ToolDeadlineExceeded("t", deadline_s=1): "HARNESS_FAULT",
            S.SandboxFault("x"): "HARNESS_FAULT",
            S.TaskDefectFault("x"): "HARNESS_FAULT",
            PJ.DiagnosticTripwire("canary", "private_scalar"): "LEAK_TRIPWIRE",
            S.ToolProtocolFault("no_tool_call"): "PROTOCOL_EXHAUSTED",
            S.ToolNotPermitted("dev_query"): "POLICY_VIOLATION",
            S.WriteOutsideSurface(): "POLICY_VIOLATION",
            S.ForbiddenArgument(): "POLICY_VIOLATION",
            S.OracleCapExceeded(): "LIMIT_ORACLE",
            S.SessionProtocolError(ROLE): "PROTOCOL_EXHAUSTED",
            S.SessionPolicyViolation(S.ToolNotPermitted("dev_query")): "POLICY_VIOLATION",
            S.OutputTruncated(ROLE, turn_index=0): "OUTPUT_TRUNCATED",
            P.RoleCapExceeded("cap"): "LIMIT_USD",
            P.BudgetExceededError("b", scope="task"): "PROVIDER_FAULT",
            P.TranscriptMissingError("miss"): "PROVIDER_FAULT",
            P.MissingCredentialsError("none"): "PROVIDER_FAULT",
            ProviderProtocolError("bad"): "PROVIDER_FAULT",
        }
        for exc, terminal in cases.items():
            self.assertEqual(S.terminal_for_exception(exc).name, terminal, type(exc).__name__)
            self.assertEqual(S.FaultRecord.from_exception(exc).terminal, terminal)

    def test_terminal_submitted(self):
        result, _ = self.run_session([[tool_use(SUBMIT, {"patch": "p"})]], [submit_tool()])
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.final, {"patch": "p"})
        self.assertEqual((result.model_call_count, result.terminal_count), (1, 1))

    def test_terminal_abstained(self):
        result, _ = self.run_session([[tool_use("abort", {"reason_code": "out_of_scope"})]], [submit_tool()])
        self.assertIs(result.terminal, S.TerminalState.ABSTAINED)
        self.assertEqual(result.final["reason_code"], "out_of_scope")

    def test_terminal_limit_turns(self):
        read = FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True))
        result, provider = self.run_session([[tool_use("read_view")]] * 3, [read, submit_tool()], max_turns=2)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_TURNS)
        self.assertEqual(len(provider.calls), 2)

    def test_terminal_limit_tool_calls(self):
        read = FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True))
        result, _ = self.run_session([[tool_use("read_view")]] * 4, [read, submit_tool()], max_turns=10, max_tool_calls=3)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_TOOL_CALLS)
        self.assertEqual(len(read.calls), 3)

    def test_terminal_limit_tokens(self):
        read = FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True))
        result, _ = self.run_session([[tool_use("read_view")]] * 4, [read, submit_tool()], max_turns=10, max_output_tokens=80)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_TOKENS)
        self.assertEqual(result.model_call_count, 2)

    def test_terminal_limit_usd(self):
        result, _ = self.run_session([P.RoleCapExceeded("cap")], [submit_tool()])
        self.assertIs(result.terminal, S.TerminalState.LIMIT_USD)
        self.assertEqual(result.model_call_count, 0)
        self.assertEqual((result.limit, result.limit_scope), ("usd", "role"))

    def test_terminal_limit_wall(self):
        clock = FakeClock()
        read = FakeTool("read_view", cost=RG.ToolCost(idempotent_read=True))

        def slow(_turn_index):
            clock.advance(301.0)
            return [tool_use("read_view")]

        result, provider = self.run_session([slow, slow], [read, submit_tool()], wall_clock_s=300, clock=clock)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_WALL)
        self.assertEqual(len(provider.calls), 1)
        self.assertGreaterEqual(result.wall_ms, 301_000)
        self.assertFalse(result.auto_submitted)

    @unittest.skipUnless(
        hasattr(S.signal, "SIGALRM") and hasattr(S.signal, "setitimer"),
        "requires a POSIX interval timer",
    )
    def test_terminal_limit_wall_interrupts_a_blocking_model_transport(self):
        def blocked(_turn_index):
            time.sleep(1.0)
            return [tool_use("read_view")]

        started = time.monotonic()
        result, provider = self.run_session(
            [blocked],
            [FakeTool("read_view"), submit_tool()],
            wall_clock_s=0.05,
        )

        self.assertIs(result.terminal, S.TerminalState.LIMIT_WALL)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.model_call_count, 0)
        self.assertEqual(result.blocked_on, "session_limit:wall")

    def test_tool_attempt_is_clamped_to_remaining_session_wall(self):
        clock = FakeClock()
        seen_deadlines: list[float] = []

        class DeadlineWorker:
            def run(self, tool, ctx, args, *, deadline_s):
                seen_deadlines.append(deadline_s)
                clock.advance(deadline_s)
                raise S.ToolDeadlineExceeded(tool.name, deadline_s=deadline_s)

        def near_expiry(_turn_index):
            clock.advance(9.0)
            return [tool_use("read_view")]

        read = FakeTool(
            "read_view",
            cost=RG.ToolCost(idempotent_read=True, wall_s=10.0),
        )
        result, _ = self.run_session(
            [near_expiry],
            [read, submit_tool()],
            worker=DeadlineWorker(),
            clock=clock,
            wall_clock_s=10.0,
        )

        self.assertIs(result.terminal, S.TerminalState.LIMIT_WALL)
        self.assertEqual(len(seen_deadlines), 1)
        self.assertAlmostEqual(seen_deadlines[0], 1.0)
        self.assertEqual(result.tool_call_count, 1)
        self.assertEqual(result.turns[-1].outcome_code, "session_wall_deadline")
        self.assertEqual(result.blocked_on, "session_limit:wall")
        self.assertTrue(result.verify_chain())

    def test_terminal_limit_working_is_disabled_for_council_roles(self):
        clock = FakeClock()

        def slow_tool(ctx, args):
            clock.advance(20.0)
            return diag()

        tool = FakeTool("check_scope", impl=slow_tool)
        # A council role ignores max_tool_wall_s: the session runs to submit.
        result, _ = self.run_session(
            [[tool_use("check_scope", {}, "a")], [tool_use("check_scope", {}, "b")], [tool_use(SUBMIT, {"patch": "p"}, "c")]],
            [tool, submit_tool()], max_tool_wall_s=10, clock=clock,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertGreaterEqual(result.tool_wall_ms, 40_000)
        self.assertIn(ROLE, S.COUNCIL_ROLES_WITHOUT_WORKING_LIMIT)
        # The L2 policy (not a council role) is stopped as LIMIT_WORKING.
        policy_tool = FakeTool("check_scope", permitted_roles=frozenset({"policy"}), impl=slow_tool)
        policy_submit = FakeTool(SUBMIT, permitted_roles=frozenset({"policy"}), input_schema=submit_tool().input_schema)
        policy = make_policy([policy_tool, policy_submit], role="policy", max_tool_wall_s=10)
        ctx = RG.ToolContext(root=self.root, task_id=TASK.task_id, role="policy", task=TASK)
        provider = ScriptedProvider([[tool_use("check_scope", {}, "a")], [tool_use("check_scope", {}, "b")]])
        result = S.run_bounded_session(
            "policy", "v", policy.tools, policy, policy.limits, provider=provider, ctx=ctx,
            worker=S.InProcessValidatorWorker(), clock=clock,
        )
        self.assertIs(result.terminal, S.TerminalState.LIMIT_WORKING)
        self.assertEqual(len(policy_tool.calls), 1)
        self.assertEqual(result.limit, "working")
        # The shipped council blocks declare no working limit at all.
        doc = yaml.safe_load(P.default_agents_config_path().read_text(encoding="utf-8"))
        for role, spec in doc["roles"].items():
            self.assertNotIn("max_tool_wall_s", spec.get("session") or {}, role)

    def test_tool_wall_ms_charges_only_the_answering_attempt(self):
        """Finding 2-4: `tool_wall_ms` (the scored LIMIT_WORKING clock) is
        the MEASURED duration of the worker attempt that answered or refused
        — never a dead attempt, a worker respawn, a transient retry's first
        attempt, a stalled deadline or the D1 sanitizer — so an OOM kill near
        the ceiling cannot turn a continuing session into a scored
        LIMIT_WORKING stop. A FakeClock and a policy-role session (the
        council roles ignore the limit) pin every branch of `_dispatch`."""

        class TimedWorker(S.InProcessValidatorWorker):
            """Each attempt advances the fake clock by `plan[i][0]` seconds
            and then raises `plan[i][1]` (a fault instance, a fault class
            needing `deadline_s`, or None to answer); `rebuild()` and
            `fresh_scratch()` advance it by `spawn_s` / `scratch_s`."""

            def __init__(self, clock, plan, *, spawn_s=0.0, scratch_s=0.0):
                super().__init__()
                self.clock, self.plan = clock, list(plan)
                self.spawn_s, self.scratch_s = spawn_s, scratch_s
                self.runs = self.rebuilt = self.scratched = 0

            def run(self, tool, ctx, args, *, deadline_s):
                self.runs += 1
                seconds, exc = self.plan.pop(0)
                self.clock.advance(seconds)
                if exc is not None:
                    if isinstance(exc, type):
                        raise exc(str(getattr(tool, "name", "t")), deadline_s=deadline_s)
                    raise exc
                return super().run(tool, ctx, args, deadline_s=deadline_s)

            def rebuild(self):
                self.rebuilt += 1
                self.clock.advance(self.spawn_s)
                return self

            def fresh_scratch(self):
                self.scratched += 1
                self.clock.advance(self.scratch_s)

        def policy_tool(name, impl=None):
            return FakeTool(name, permitted_roles=frozenset({"policy"}), impl=impl)

        def policy_submit():
            return FakeTool(SUBMIT, permitted_roles=frozenset({"policy"}), input_schema=submit_tool().input_schema)

        # The policy role's submit schema rejects {"patch": "p"}; the session
        # ends on the abort tool (ABSTAINED) — proof the limit did not bind.
        abort = tool_use("abort", {"reason_code": "infeasible"}, "c")

        def run_policy(script, tools, *, worker, clock, max_tool_wall_s):
            policy = make_policy(tools, role="policy", max_tool_wall_s=max_tool_wall_s, max_turns=8, max_tool_calls=8)
            ctx = RG.ToolContext(root=self.root, task_id=TASK.task_id, role="policy", task=TASK)
            provider = ScriptedProvider(script)
            try:
                result = S.run_bounded_session(
                    "policy", "v", policy.tools, policy, policy.limits,
                    provider=provider, ctx=ctx, worker=worker, clock=clock,
                )
                return result, None
            except S.SessionFault as exc:
                return exc.session_result, exc

        def tool_elapsed(result):
            return [t.elapsed_tool_ms for t in result.turns if t.kind != "model"]

        oom = lambda: S.SandboxFault("oom", code="oom_killed")  # noqa: E731

        # (E) The finding's own trigger at its numbers: the scored clock is at
        # 535 s of a 540 s ceiling; a dev_query's worker is OOM-killed after
        # 4 s, the respawn takes 2 s, the retry answers in 3 s. Old accounting
        # summed 544 s -> a scored LIMIT_WORKING; the correct charge is 538 s.
        with self.subTest(branch="sandbox re-dispatch"):
            clock = FakeClock()
            first = policy_tool("check_scope", impl=lambda ctx, args: (clock.advance(535.0), diag())[1])
            worker = TimedWorker(clock, [(0.0, None), (4.0, oom()), (3.0, None)], spawn_s=2.0)
            result, exc = run_policy(
                [[tool_use("check_scope", {}, "a")], [tool_use("dev_query", {}, "b")], [abort]],
                [first, policy_tool("dev_query"), policy_submit()], worker=worker, clock=clock, max_tool_wall_s=540,
            )
            self.assertIsNone(exc)
            self.assertEqual((result.resume_count, worker.runs, worker.rebuilt), (1, 3, 1))
            self.assertIs(result.terminal, S.TerminalState.ABSTAINED)
            self.assertEqual(result.tool_wall_ms, 538_000)
            self.assertEqual(tool_elapsed(result), [535_000, 3_000])
            self.assertGreaterEqual(535_000 + 4_000 + 2_000 + 3_000, 540_000)  # the old sum would have bound
            self.assert_identities(result)

        # (B) A transient harness retry: the failed attempt (8 s) and the
        # fresh scratch (1 s) are excluded; the 3 s answer is charged.
        with self.subTest(branch="transient harness retry"):
            clock = FakeClock()
            worker = TimedWorker(
                clock, [(8.0, S.ToolHarnessFault("dev_query", code="runtime_unavailable")), (3.0, None)], scratch_s=1.0,
            )
            result, exc = run_policy(
                [[tool_use("dev_query", {}, "b")], [abort]],
                [policy_tool("dev_query"), policy_submit()], worker=worker, clock=clock, max_tool_wall_s=10,
            )
            self.assertIsNone(exc)
            self.assertEqual((result.resume_count, worker.runs, worker.scratched), (1, 2, 1))
            self.assertIs(result.terminal, S.TerminalState.ABSTAINED)
            self.assertEqual(result.tool_wall_ms, 3_000)
            self.assertEqual(tool_elapsed(result), [3_000])

        # (C) A stalled tool: the worker burns the deadline and raises
        # ToolDeadlineExceeded -> HARNESS_FAULT, nothing charged, the record
        # still carries the measured time as evidence.
        with self.subTest(branch="stalled deadline"):
            clock = FakeClock()
            worker = TimedWorker(clock, [(10.0, S.ToolDeadlineExceeded)])
            result, exc = run_policy(
                [[tool_use("dev_query", {}, "b")], [abort]],
                [policy_tool("dev_query"), policy_submit()], worker=worker, clock=clock, max_tool_wall_s=10,
            )
            self.assertIsInstance(exc, S.ToolDeadlineExceeded)
            self.assertIs(result.terminal, S.TerminalState.HARNESS_FAULT)
            self.assertEqual(result.tool_wall_ms, 0)
            self.assertEqual(tool_elapsed(result), [10_000])

        # (C2) Two deaths halt as HARNESS_FAULT with nothing charged.
        with self.subTest(branch="second sandbox fault"):
            clock = FakeClock()
            worker = TimedWorker(clock, [(4.0, oom()), (4.0, oom())], spawn_s=2.0)
            result, exc = run_policy(
                [[tool_use("dev_query", {}, "b")], [abort]],
                [policy_tool("dev_query"), policy_submit()], worker=worker, clock=clock, max_tool_wall_s=10,
            )
            self.assertIsInstance(exc, S.SandboxFault)
            self.assertIs(result.terminal, S.TerminalState.HARNESS_FAULT)
            self.assertEqual((result.resume_count, worker.runs, worker.rebuilt), (1, 2, 1))
            self.assertEqual(result.tool_wall_ms, 0)

        # (D) The D1 sanitizer is slow (9 s on a 3 s tool): excluded.
        with self.subTest(branch="sanitizer time"):
            clock = FakeClock()
            worker = TimedWorker(clock, [(3.0, None)])
            real_avf = PJ.assert_value_free

            def slow_avf(*a, **kw):
                clock.advance(9.0)
                return real_avf(*a, **kw)

            with mock.patch.object(PJ, "assert_value_free", slow_avf):
                result, exc = run_policy(
                    [[tool_use("dev_query", {}, "b")], [abort]],
                    [policy_tool("dev_query"), policy_submit()], worker=worker, clock=clock, max_tool_wall_s=10,
                )
            self.assertIsNone(exc)
            self.assertIs(result.terminal, S.TerminalState.ABSTAINED)
            self.assertEqual(result.tool_wall_ms, 3_000)
            self.assertEqual(tool_elapsed(result), [3_000])

        # (F) Candidate-attributable time still binds: an answering attempt
        # that itself takes 10 s is LIMIT_WORKING at max_tool_wall_s=10 —
        # after a dead 4 s attempt and a 2 s respawn that are NOT part of it.
        with self.subTest(branch="a long answering attempt binds"):
            clock = FakeClock()
            worker = TimedWorker(clock, [(4.0, oom()), (10.0, None)], spawn_s=2.0)
            result, exc = run_policy(
                [[tool_use("dev_query", {}, "b")], [tool_use("dev_query", {}, "b2")], [abort]],
                [policy_tool("dev_query"), policy_submit()], worker=worker, clock=clock, max_tool_wall_s=10,
            )
            self.assertIsNone(exc)
            self.assertIs(result.terminal, S.TerminalState.LIMIT_WORKING)
            self.assertEqual(result.limit, "working")
            self.assertEqual(result.tool_wall_ms, 10_000)

    def test_terminal_limit_oracle(self):
        certify = FakeTool("certify", cost=RG.ToolCost(oracle_bits=5))
        result, _ = self.run_session([[tool_use("certify")]], [certify, submit_tool()], max_oracle_bits=4)
        self.assertIs(result.terminal, S.TerminalState.LIMIT_ORACLE)
        self.assertEqual(certify.calls, [])

    def test_terminal_stuck(self):
        check = FakeTool("check_scope")
        script = [[tool_use("check_scope", {}, f"t{i}")] for i in range(6)]
        result, provider = self.run_session(script, [check, submit_tool()], max_turns=10)
        self.assertIs(result.terminal, S.TerminalState.STUCK)
        self.assertEqual(result.terminal.value, "stuck_in_a_loop")
        self.assertEqual(result.limit, "stuck_in_a_loop")
        self.assertEqual(result.blocked_on, "session_limit:stuck_in_a_loop")
        self.assertEqual(result.nudge_count, 1)
        self.assertEqual(len(check.calls), 2)  # two executed, third nudged, fourth halted
        self.assertEqual(len(provider.calls), 4)
        self.assertEqual([e.verdict for e in result.detector_events], ["nudge", "halt"])
        self.assert_identities(result)

    def test_terminal_protocol_exhausted(self):
        with self.assertRaises(S.SessionProtocolError) as ctx:
            self.run_session([[text_block()]] * 3, [submit_tool()])
        self.assertIs(ctx.exception.session_result.terminal, S.TerminalState.PROTOCOL_EXHAUSTED)

    def test_terminal_output_truncated(self):
        with self.assertRaises(S.OutputTruncated) as ctx:
            self.run_session([ScriptedTurn(content=[text_block()], stop_reason="max_tokens")], [submit_tool()])
        self.assertIs(ctx.exception.session_result.terminal, S.TerminalState.OUTPUT_TRUNCATED)

    def test_terminal_policy_violation(self):
        def escape(ctx, args):
            raise S.WriteOutsideSurface(tool="apply_edit_trial", detail="answer_key/")

        edit = FakeTool("apply_edit_trial", impl=escape)
        with self.assertRaises(S.SessionPolicyViolation) as ctx:
            self.run_session([[tool_use("apply_edit_trial")]], [edit, submit_tool()])
        partial = ctx.exception.session_result
        self.assertIs(partial.terminal, S.TerminalState.POLICY_VIOLATION)
        self.assertEqual(partial.fault.code, "write_outside_surface")
        self.assertEqual([e["code"] for e in partial.security_events], ["write_outside_surface"])
        self.assertEqual(partial.tool_call_count, 1)
        self.assert_identities(partial)
        # A model-supplied path the tool's own path check refuses is the
        # same class (ToolPathDenied -> ForbiddenArgument), never a harness fault.
        def climb(ctx, args):
            return ctx.resolve("../answer_key/gold.csv")

        edit = FakeTool("apply_edit_trial", impl=climb)
        with self.assertRaises(S.SessionPolicyViolation) as ctx:
            self.run_session([[tool_use("apply_edit_trial")]], [edit, submit_tool()])
        self.assertEqual(ctx.exception.code, "forbidden_argument")

    def test_terminal_leak_tripwire(self):
        def leaky(ctx, args):
            # A gate name that is a value trips the projector before transport.
            return PJ.Diagnostic(source=PJ.DiagnosticSource.GATE, ok=True, code="ok", subject="primary")

        tool = FakeTool("check_scope", impl=leaky)
        with self.assertRaises(PJ.DiagnosticTripwire) as ctx:
            self.run_session([[tool_use("check_scope")]], [tool, submit_tool()])
        exc = ctx.exception
        self.assertEqual(exc.terminal, "LEAK_TRIPWIRE")
        self.assertEqual(engine_mod._infra_marker_for(exc), "DiagnosticTripwire")
        partial = exc.session_result
        self.assertIs(partial.terminal, S.TerminalState.LEAK_TRIPWIRE)
        self.assertEqual(partial.action_trace[-1].outcome_code, "sanitizer_tripwire")
        self.assertEqual(partial.fault.boundary, "sanitizer")
        self.assert_identities(partial)
        # The gatekeeper (D1) alone, over bytes the worker forged, halts too.
        class ForgingWorker(S.InProcessValidatorWorker):
            def run(self, tool, ctx, args, *, deadline_s):
                forged = canonical_json({
                    "kind": "dev_rows", "diagnostics_version": PJ.DIAGNOSTICS_VERSION,
                    "columns": ["customer_id"], "rows": [["/Users/nobody/answer_key/gold.csv"]],
                })
                return S.WorkerResult(observation=PJ.DevRows(columns=("customer_id",), rows=(("x",),)), payload=forged)

        with self.assertRaises(PJ.DiagnosticTripwire):
            self.run_session([[tool_use("check_scope")]], [FakeTool("check_scope"), submit_tool()], worker=ForgingWorker())

    def test_terminal_harness_fault(self):
        def crash(ctx, args):
            raise RuntimeError("Binder Error: column 4711 not found in /Users/nobody/answer_key/gold.csv")

        tool = FakeTool("check_scope", impl=crash)
        with self.assertRaises(S.ToolHarnessFault) as ctx:
            self.run_session([[tool_use("check_scope")]], [tool, submit_tool()])
        exc = ctx.exception
        self.assertEqual(exc.terminal, "HARNESS_FAULT")
        self.assertEqual(exc.cause_type, "RuntimeError")
        for token in ("4711", "Binder", "answer_key"):
            self.assertNotIn(token, str(exc))
        partial = exc.session_result
        self.assertIs(partial.terminal, S.TerminalState.HARNESS_FAULT)
        self.assertEqual(partial.fault.exception_type, "ToolHarnessFault")
        self.assertEqual(partial.fault.code, "harness_exception")
        self.assertNotIn("4711", json.dumps(partial.as_dict()))
        self.assert_identities(partial)
        # A harness-side deadline is the same terminal.
        def stall(ctx, args):
            time.sleep(2.0)
            return diag()

        slow = FakeTool("check_scope", impl=stall, cost=RG.ToolCost(wall_s=0.2))
        with self.assertRaises(S.ToolDeadlineExceeded) as ctx:
            self.run_session([[tool_use("check_scope")]], [slow, submit_tool()])
        self.assertIs(ctx.exception.session_result.terminal, S.TerminalState.HARNESS_FAULT)

    def test_terminal_provider_fault(self):
        # A replay miss and missing credentials halt on the FIRST occurrence
        # (nothing a re-issue can change); a transport fault is re-issued
        # once (04 §6 retry policy) and halts on the second.
        for exc, calls in ((P.TranscriptMissingError("no transcript"), 1), (P.MissingCredentialsError("no key"), 1),
                           (OSError("connection reset"), 2)):
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(Exception) as ctx:
                    self.run_session([exc, exc], [submit_tool()])
                raised = ctx.exception
                self.assertIs(raised.session_result.terminal, S.TerminalState.PROVIDER_FAULT)
                self.assertNotEqual(engine_mod._infra_marker_for(raised), "")
                self.assertEqual(raised.session_result.resume_count, calls - 1)
        # An untyped transport error is wrapped as ProviderFault (code
        # transport), chained onto the original — the SECOND one, after the
        # single resume on the same prefix.
        with self.assertRaises(S.ProviderFault) as ctx:
            self.run_session([OSError("connection reset"), OSError("still reset")], [submit_tool()])
        self.assertEqual(ctx.exception.code, "transport")
        self.assertIsInstance(ctx.exception.__cause__, OSError)
        self.assertEqual(str(ctx.exception.__cause__), "still reset")

    def test_provider_fault_resumes_once_from_the_recorded_prefix_then_halts(self):
        """04 §6 / SoT T6 ProviderFault: the first transport fault on turn k
        re-issues `_turn` ONCE on the SAME prefix — the recorded turns 0..k-1
        replay from the store, no tool is re-run, no counter moves — and the
        session goes on; a second transport fault halts as PROVIDER_FAULT.
        A truncation (`OutputTruncated`), a replay miss and a budget
        refusal are never resumed."""
        check = FakeTool("check_scope")
        # turn 0 executes; turn 1 faults once, resumes, then submits.
        script = [
            [tool_use("check_scope", {}, "a")],
            S.ProviderFault("HTTP 529 after 4 retries", code="transport"),
            [tool_use(SUBMIT, {"patch": "p1"}, "b")],
        ]
        result, provider = self.run_session(script, [check, submit_tool()])
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.resume_count, 1)
        event = dict(result.resume_events[0])
        self.assertEqual(event, {"boundary": "provider", "exception_type": "ProviderFault", "code": "transport",
                                 "turn_index": 1, "tool": ""})
        # The re-issued turn carried the SAME prefix as the faulted one, and
        # the counters see one model turn for it, not two.
        self.assertEqual([c["turn_index"] for c in provider.calls], [0, 1, 1])
        self.assertEqual(provider.calls[1]["messages"], provider.calls[2]["messages"])
        self.assertEqual(result.model_call_count, 2)
        self.assertEqual(len(check.calls), 1)
        self.assert_identities(result)
        # The second transport fault in one session halts.
        script = [
            [tool_use("check_scope", {}, "a")],
            S.ProviderFault("HTTP 529", code="transport"),
            [tool_use("check_scope", {}, "b")],
            S.ProviderFault("HTTP 529 again", code="transport"),
            [tool_use(SUBMIT, {"patch": "p1"}, "c")],
        ]
        with self.assertRaises(S.ProviderFault) as ctx:
            self.run_session(script, [FakeTool("check_scope"), submit_tool()])
        partial = ctx.exception.session_result
        self.assertIs(partial.terminal, S.TerminalState.PROVIDER_FAULT)
        self.assertEqual(partial.resume_count, 1)
        self.assertEqual(partial.model_call_count, 2)
        self.assert_identities(partial)
        # Never resumed: truncation, a replay miss, a task budget refusal.
        for exc in (
            S.OutputTruncated(ROLE, turn_index=0),
            P.TranscriptMissingError("no transcript"),
            P.BudgetExceededError("task budget", scope="task"),
        ):
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(Exception) as ctx:
                    self.run_session([exc, [tool_use(SUBMIT, {"patch": "p"}, "z")]], [submit_tool()])
                self.assertEqual(ctx.exception.session_result.resume_count, 0)

    def test_sandbox_fault_rebuilds_the_worker_once_then_halts(self):
        """04 §6 SandboxFault: the first one re-dispatches the same call
        ONCE on a fresh worker (the recorded prefix is the controller's);
        no turn is recorded for the dead attempt; the second halts as
        HARNESS_FAULT with the partial result attached."""

        class DyingWorker(S.InProcessValidatorWorker):
            def __init__(self, deaths: int):
                super().__init__()
                self.deaths = deaths
                self.rebuilt = 0
                self.runs = 0

            def run(self, tool, ctx, args, *, deadline_s):
                self.runs += 1
                if self.deaths > 0:
                    self.deaths -= 1
                    raise S.SandboxFault("worker died", code="worker_died")
                return super().run(tool, ctx, args, deadline_s=deadline_s)

            def rebuild(self):
                self.rebuilt += 1
                return self

        check = FakeTool("check_scope")
        worker = DyingWorker(deaths=1)
        result, _ = self.run_session(
            [[tool_use("check_scope", {}, "a")], [tool_use(SUBMIT, {"patch": "p"}, "b")]],
            [check, submit_tool()], worker=worker,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual((worker.runs, worker.rebuilt, len(check.calls)), (2, 1, 1))
        self.assertEqual(result.resume_count, 1)
        self.assertEqual(dict(result.resume_events[0]),
                         {"boundary": "sandbox", "exception_type": "SandboxFault", "code": "worker_died",
                          "turn_index": 0, "tool": "check_scope"})
        # ONE tool-side turn for the call: the attempt that answered.
        self.assertEqual([t.kind for t in result.turns], ["model", "tool", "model"])
        self.assertEqual(result.tool_call_count, 1)
        self.assert_identities(result)
        # Two deaths: the second halts as HARNESS_FAULT (exit 2, no round).
        worker = DyingWorker(deaths=2)
        with self.assertRaises(S.SandboxFault) as ctx:
            self.run_session(
                [[tool_use("check_scope", {}, "a")], [tool_use(SUBMIT, {"patch": "p"}, "b")]],
                [FakeTool("check_scope"), submit_tool()], worker=worker,
            )
        partial = ctx.exception.session_result
        self.assertIs(partial.terminal, S.TerminalState.HARNESS_FAULT)
        self.assertEqual(partial.fault.exception_type, "SandboxFault")
        self.assertEqual((partial.resume_count, worker.runs, worker.rebuilt), (1, 2, 1))
        self.assertEqual(engine_mod._infra_marker_for(ctx.exception), "SandboxFault")
        self.assert_identities(partial)

    def test_transient_tool_harness_fault_retries_once_on_fresh_scratch(self):
        """04 §6 ToolHarnessFault: a transient `DATABASE_NOT_FRESH` /
        `RUNTIME_UNAVAILABLE` retries the tool ONCE on fresh scratch; any
        other harness code halts on the first, and a second transient one
        halts too."""
        attempts: list[str] = []
        scratch: list[int] = []

        def flaky(code):
            def impl(ctx, args):
                attempts.append(code)
                if len(attempts) == 1:
                    raise S.ToolHarnessFault("dev_query", code=code)
                return diag()
            return impl

        class Scratch(S.InProcessValidatorWorker):
            def fresh_scratch(self):
                scratch.append(1)

        for code in ("DATABASE_NOT_FRESH", "runtime_unavailable"):
            with self.subTest(code=code):
                attempts.clear(); scratch.clear()
                tool = FakeTool("dev_query", impl=flaky(code))
                result, _ = self.run_session(
                    [[tool_use("dev_query", {}, "a")], [tool_use(SUBMIT, {"patch": "p"}, "b")]],
                    [tool, submit_tool()], worker=Scratch(),
                )
                self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
                self.assertEqual((len(attempts), scratch, result.resume_count), (2, [1], 1))
                self.assertEqual(dict(result.resume_events[0])["boundary"], "tool")
                self.assertEqual(dict(result.resume_events[0])["code"], code)
                self.assertEqual(result.tool_call_count, 1)
                self.assert_identities(result)
        # A non-transient harness code halts on the first occurrence.
        attempts.clear()
        with self.assertRaises(S.ToolHarnessFault) as ctx:
            self.run_session([[tool_use("dev_query", {}, "a")]], [FakeTool("dev_query", impl=flaky("harness_exception")), submit_tool()])
        self.assertEqual((len(attempts), ctx.exception.session_result.resume_count), (1, 0))
        # A second transient fault halts.
        def always(ctx, args):
            raise S.ToolHarnessFault("dev_query", code="DATABASE_NOT_FRESH")

        with self.assertRaises(S.ToolHarnessFault) as ctx:
            self.run_session([[tool_use("dev_query", {}, "a")]], [FakeTool("dev_query", impl=always), submit_tool()])
        partial = ctx.exception.session_result
        self.assertIs(partial.terminal, S.TerminalState.HARNESS_FAULT)
        self.assertEqual(partial.resume_count, 1)
        self.assert_identities(partial)


# Session hard-cap and wire-level parity tests using the real runner and fake
# transport. One-shot transcript fields are pinned below.
ONE_SHOT_ENTRY_FIELDS = (
    "role", "prompt_sha256", "system_sha256", "provider", "model", "served_model", "response",
    "response_sha256", "task_id", "task_content_hash", "attempt_count", "correction_count",
    "finding_count", "usage", "raw_attempts",
)
#: The evidence-row fields a zero-tool session and the one-shot exchange must
#: agree on (everything but the schema they were recorded under, the
#: measured wall and the served-vs-live accounting).
SHARED_ROW_FIELDS = (
    "task_id", "task_content_hash", "role", "prompt_sha256", "response_sha256", "attempt_count",
    "correction_count", "model_call_count", "tool_call_count", "refused_count", "nudge_count",
    "validator_run_count", "correction_kinds", "terminal", "stale_tool_result_count",
    "finding_count", "zero_findings", "provider", "model", "usage",
)


class WireParityTest(unittest.TestCase):
    def setUp(self):
        P.clear_behavior_caches()
        self.addCleanup(P.clear_behavior_caches)

    def test_session_zero_tool_turns_is_byte_identical_to_complete(self):
        """THE PARITY GATE (roadmap §4 DoD 1; 04 §7): a session with an EMPTY
        registry — no model-initiated tool, the role's own wire tools and
        tool choice — run through `run_session` -> the real runner -> `_turn`
        produces, on BOTH backends and for BOTH shapes (a schema role's forced
        submit tool, a prose role's text reply): the same request bytes on
        the wire as `complete()`, the same normalized text as its `final`,
        an entry under the same key that agrees with the one-shot entry on
        every one-shot field (only the route block's `entry_schema` states 3
        and the `turn` block is added), an evidence row that agrees on every
        shared field, and each path serves the other's entry with zero HTTP.
        `complete()` itself is untouched (brief rule 4, R1)."""
        # The useful author and implementer roles are bounded by default.  This
        # parity test exercises their explicit one-shot rollback, where their
        # registry is empty and the old request remains byte-identical.
        for default_role in ("semantic_author", "independent_implementer"):
            self.assertTrue(P.role_is_agentic(default_role), default_role)
            self.assertTrue(P.session_policy_for(default_role).tools, default_role)
        rollback = json.loads(json.dumps(P._agents_doc()))
        for rollback_role in ("semantic_author", "independent_implementer"):
            rollback["roles"][rollback_role]["session"]["enabled"] = False
        patcher = mock.patch.object(P, "_agents_doc", lambda: rollback)
        patcher.start()
        self.addCleanup(patcher.stop)
        P.clear_behavior_caches()

        chat_routing = make_routing(oss_base="http://oss:8000/v1", oss_key="k", oss_model="m")
        chat_schema_routing = P.RoleRouting(
            roles={**chat_routing.roles,
                   "ambiguity_critic": P.RoleRoute("ambiguity_critic", "openai_compat", "m", 2048, None)},
            provider_config=chat_routing.provider_config,
        )
        cases = (
            ("anthropic schema", "ambiguity_critic", make_routing(),
             lambda: anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5")),
            ("anthropic prose", "semantic_author", make_routing(),
             lambda: anthropic_text_response("Each row is one customer with the completed-order count.")),
            ("openai_compat prose", "independent_implementer", chat_routing,
             lambda: openai_text_response("select customer_id from customers")),
            ("openai_compat schema", "ambiguity_critic", chat_schema_routing,
             lambda: openai_tool_response([VALID_FINDING], model="m")),
        )
        for label, role, routing, body in cases:
            with self.subTest(case=label):
                policy = P.session_policy_for(role)
                self.assertEqual(policy.tools, ())
                self.assertFalse(P.role_is_agentic(role))
                self.assertEqual(policy.wire_tools, tuple(P.wire_tools_for(role)))
                # (A) complete() first; the session is served from its entry.
                with tempfile.TemporaryDirectory() as tmp:
                    transport = FakeTransport([body()])
                    provider = _bound(_provider(tmp, transport, routing=routing))
                    text = provider.complete(role, "VIEW")
                    one_shot_call = json.dumps(transport.calls[0][2])
                    one_shot_row = dict(provider.exchange_evidence[-1])
                    key = provider.transcript_key_for(role, "VIEW")
                    entry_path = Path(tmp) / "transcripts" / role / f"{key}.json"
                    one_shot_entry = json.loads(entry_path.read_text(encoding="utf-8"))
                    self.assertEqual(one_shot_entry["route"]["entry_schema"], 2)
                    result = provider.run_session(role, "VIEW", policy, _session_ctx(tmp, role))
                    self.assertEqual(len(transport.calls), 1, "served from the one-shot entry: zero HTTP")
                    self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
                    self.assertEqual(result.final, text)
                    self.assertEqual((result.model_call_count, result.tool_call_count, result.terminal_count), (1, 0, 1))
                    self.assertEqual(result.live_model_call_count, 0)
                    self.assertEqual(result.turns[0].memo_key, key)
                    self.assertTrue(result.turns[0].replayed)
                    self.assertTrue(result.verify_chain())
                    served_row = dict(provider.exchange_evidence[-1])
                    self.assertEqual(len(provider.exchange_evidence), 2)
                    for name in SHARED_ROW_FIELDS:
                        self.assertEqual(served_row[name], one_shot_row[name], name)
                    self.assertEqual((served_row["entry_schema"], one_shot_row["entry_schema"]), (3, 2))
                    self.assertEqual((served_row["replayed"], served_row["usd"], served_row["live_model_call_count"]), (True, 0.0, 0))
                    self.assertEqual(json.loads(entry_path.read_text(encoding="utf-8")), one_shot_entry, "a stored entry is never rewritten")
                # (B) the session first on a fresh store; complete() is served from ITS entry.
                with tempfile.TemporaryDirectory() as tmp:
                    transport = FakeTransport([body()])
                    provider = _bound(_provider(tmp, transport, routing=routing))
                    result = provider.run_session(role, "VIEW", policy, _session_ctx(tmp, role))
                    self.assertEqual(len(transport.calls), 1)
                    self.assertEqual(json.dumps(transport.calls[0][2]), one_shot_call, "byte-identical request on the wire")
                    self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
                    self.assertEqual(result.final, text)
                    self.assertFalse(result.turns[0].replayed)
                    self.assertEqual(result.live_model_call_count, 1)
                    session_row = dict(provider.exchange_evidence[-1])
                    entry_path = Path(tmp) / "transcripts" / role / f"{key}.json"
                    self.assertTrue(entry_path.is_file(), "recorded under the one-shot key")
                    session_entry = json.loads(entry_path.read_text(encoding="utf-8"))
                    for name in ONE_SHOT_ENTRY_FIELDS:
                        self.assertEqual(session_entry[name], one_shot_entry[name], name)
                    self.assertEqual(session_entry.get("zero_findings"), one_shot_entry.get("zero_findings"))
                    one_shot_route, session_route = dict(one_shot_entry["route"]), dict(session_entry["route"])
                    self.assertEqual((one_shot_route.pop("entry_schema"), session_route.pop("entry_schema")), (2, 3))
                    self.assertEqual(session_route, one_shot_route)
                    self.assertEqual(set(session_entry) - set(one_shot_entry), {"turn"})
                    self.assertEqual(session_entry["turn"]["turn_index"], 0)
                    # Turn 0's VIEW is never persisted (a one-shot entry records
                    # `prompt_sha256` only): its digest stands in for it.
                    self.assertEqual(
                        session_entry["turn"]["user_messages"],
                        [{"role": "user", "content_sha256": sha256_hex(canonical_json("VIEW")), "content_omitted": "initial_view"}],
                    )
                    self.assertNotIn("VIEW", canonical_json(session_entry["turn"]["user_messages"]))
                    served = provider.complete(role, "VIEW")
                    self.assertEqual(len(transport.calls), 1, "served from the session entry: zero HTTP")
                    self.assertEqual(served, text)
                    self.assertEqual(served, result.final)
                    replayed_row = dict(provider.exchange_evidence[-1])
                    for name in SHARED_ROW_FIELDS:
                        self.assertEqual(session_row[name], one_shot_row[name], name)
                        self.assertEqual(replayed_row[name], one_shot_row[name], name)
                    self.assertAlmostEqual(session_row["usd"], one_shot_row["usd"])
                    self.assertEqual(session_row["trajectory_sha256"], result.session_sha256)
                    self.assertEqual(session_row["terminal"], "SUBMITTED")
                    self.assertEqual(P.exchange_row_problems(session_row), [])
                    self.assertEqual(P.exchange_row_problems(one_shot_row), [])
        # The one-shot path keeps its three attempts on a truncation
        # (SoT T4), and the parity holds on the request builders too.
        self.assertEqual(P.SCHEMA_RETRIES, 2)

    def test_session_row_manifest_branch_accepts_multi_turn_counts(self):
        """SoT T8 validator rule. A REAL multi-turn session through the wire
        (a protocol correction, a tool call, the submit) produces one row
        whose `correction_count <= attempt_count - 1` and whose SoT T5
        identity holds from the row alone (`terminal_count`,
        `limit_stop_count` ride on it): `exchange_row_problems` accepts it
        and refuses a tampered copy; a one-shot row keeps today's
        `correction_count == attempt_count - 1` rule. The review manifest
        (`cli._validated_review_manifest`) validates the four critic seats'
        one-shot rows exactly as today and a Phase 1 session row (author,
        proposer) rides beside them unharmed; the session BRANCH for a critic
        seat's row is Phase 4's (SoT T8: the seat flip), so today's critic
        branch still applies the one-shot rule to it."""
        with tempfile.TemporaryDirectory() as tmp:
            check = _RunningTool("check_draft")
            with _AgenticRoles(semantic_author=(check, "submit_prose", "abort")):
                policy = _session_policy("semantic_author")
                transport = FakeTransport([
                    anthropic_text_response("Let me think first."),          # no tool call: a correction
                    tool_use_body("check_draft", {"text": "draft"}, id="toolu_a"),
                    tool_use_body("submit_prose", {"text": "final"}, id="toolu_b"),
                ])
                provider = _bound(_provider(tmp, transport))
                result = provider.run_session("semantic_author", "VIEW", policy, _session_ctx(tmp, "semantic_author"))
            self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
            self.assertEqual((result.model_call_count, result.correction_count, result.tool_call_count), (3, 1, 1))
            self.assertEqual(len(provider.exchange_evidence), 1)
            row = provider.exchange_evidence[0]
            self.assertEqual((row["attempt_count"], row["correction_count"]), (3, 1))
            self.assertEqual((row["terminal_count"], row["limit_stop_count"]), (1, 0))
            self.assertEqual(row["correction_kinds"], {"schema": 1, "compile": 0})
            self.assertEqual(row["entry_schema"], 3)
            self.assertLessEqual(row["correction_count"], row["attempt_count"] - 1)
            self.assertEqual(
                row["model_call_count"],
                row["tool_call_count"] + row["refused_count"] + row["nudge_count"]
                + row["correction_count"] + row["terminal_count"] + row["limit_stop_count"],
            )
            self.assertEqual(P.exchange_row_problems(row), [])
            # Tampered session rows are refused by the rule they break.
            for tamper, needle in (
                ({"correction_count": 3}, "exceeds attempts-1"),
                ({"tool_call_count": 5}, "identity broken"),
                ({"model_call_count": 2}, "does not equal attempt_count"),
                ({"nudge_count": 2, "tool_call_count": 0, "correction_count": 0}, "more than one nudge"),
            ):
                with self.subTest(tamper=tamper):
                    problems = P.exchange_row_problems({**row, **tamper})
                    self.assertTrue(any(needle in p for p in problems), problems)
            # A one-shot row keeps today's exact rule.
            critic_transport = FakeTransport([anthropic_tool_response([VALID_FINDING])])
            critic = _bound(_provider(tmp, critic_transport, subdir="critic"))
            critic.complete("ambiguity_critic", "VIEW")
            one_shot = critic.exchange_evidence[0]
            self.assertEqual((one_shot["attempt_count"], one_shot["correction_count"], one_shot["entry_schema"]), (1, 0, 2))
            self.assertEqual(P.exchange_row_problems(one_shot), [])
            self.assertIn("does not equal attempts-1", P.exchange_row_problems({**one_shot, "correction_count": 1})[0])
            # The review manifest: four one-shot critic rows validate as today
            # and the session row rides beside them without a problem.
            zero = P.normalized_text_for("ambiguity_critic", {"findings": []})
            for critic_role in ("ambiguity_critic", "population_adversary", "shortcut_attacker", "feasibility_reviewer"):
                route_binding = provider._route_block(
                    provider.routing.for_role(critic_role),
                    agents_config=provider.agents_document,
                )
                provider._append_exchange_evidence(
                    critic_role, sha256_hex(critic_role), zero,
                    {"raw_attempts": [{}], "usage": {}, "provider": "anthropic", "model": "claude-opus-5",
                     "route": route_binding},
                    replayed=True,
                )
            manifest, problems = cli._validated_review_manifest(provider, TASK, [], required=True)
            self.assertEqual(problems, [])
            self.assertEqual([m["role"] for m in manifest],
                             ["ambiguity_critic", "feasibility_reviewer", "population_adversary", "shortcut_attacker"])
            self.assertNotIn("semantic_author", [m["role"] for m in manifest])
            # Critic session rows use exchange_row_problems/T5 identity in review
            # manifests, not the one-shot attempts-minus-one rule.
            critic_behavior = P.role_behavior_manifest("ambiguity_critic")
            as_critic = {
                **row,
                "role": "ambiguity_critic",
                "finding_count": 0,
                "zero_findings": True,
                "behavior_sha256": P.role_behavior_sha256("ambiguity_critic"),
                "tools_sha256": sha256_hex(canonical_json(critic_behavior["tools"])),
                "policy_sha256": critic_behavior["policy_sha256"],
            }
            critic.exchange_evidence[:] = [dict(as_critic)]
            _, problems = cli._validated_review_manifest(critic, TASK, [], required=True)
            self.assertFalse(any("correction_count does not equal attempts-1" in p for p in problems), problems)
            self.assertFalse(any("ambiguity_critic" in p for p in problems), problems)
            self.assertEqual(P.exchange_row_problems(as_critic), [])


class AgentsConfigTest(unittest.TestCase):
    def test_agents_yaml_session_defaults_never_exceed_hard_caps(self):
        doc = yaml.safe_load(P.default_agents_config_path().read_text(encoding="utf-8"))
        author = doc["roles"]["semantic_author"]["session"]
        # Author defaults match the hard caps after one revision proved
        # insufficient; defaults must never exceed those caps.
        self.assertEqual(
            author,
            {"enabled": True, "max_revisions": 4, "max_turns": 5, "max_tool_calls": 5,
             "harness_validators": ["check_prose"], "max_compile_corrections": 4,
             "max_usd": 2.75, "wall_clock_s": 900,
             "hard_caps": {"revisions": 4, "turns": 5, "tool_calls": 5, "compile_corrections": 4,
                           "usd": 2.75, "wall_clock_s": 1800}},
        )
        # R0.2 for the AUT seat: the hashed `loop_limits` IS the block the
        # runner enforces — the declared keys equal the SoT derivation
        # (`validators.author_limits`), and the caps bound every key the
        # runner reads (`turns` = revisions + 1, `tool_calls` its validator runs).
        from elt_taskgen.review.tools import validators as V

        self.assertEqual(P.role_loop_limits("semantic_author"), V.author_limits(author).as_manifest())
        # The shipped bounded block enters the behaviour manifest in full.
        self.assertEqual(P.role_behavior_manifest("semantic_author")["loop_limits"], author)
        self.assertEqual(P.role_manifest_limits("semantic_author"), author)
        self.assertEqual(author["hard_caps"]["turns"], author["hard_caps"]["revisions"] + 1)
        self.assertEqual(author["hard_caps"]["tool_calls"], author["hard_caps"]["turns"])
        self.assertEqual(author["max_turns"], author["max_revisions"] + 1)
        self.assertEqual(author["max_compile_corrections"], author["max_revisions"])
        proposer = doc["roles"]["repair_proposer"]["session"]
        self.assertEqual(
            proposer,
            {"enabled": True, "max_turns": 12, "max_tool_calls": 16, "max_certify": 2, "max_oracle_bits": 4,
             "max_usd": 4.00, "wall_clock_s": 900, "certify": {"attack_enabled": False, "deadline_s": 300},
             "hard_caps": {"turns": 12, "tool_calls": 20, "usd": 4.00, "wall_clock_s": 2700}},
        )
        # Phase 3 (roadmap Table 7, config row): the POPULATION route joins
        # the bounded proposer's routes.
        self.assertEqual(doc["repair"]["routes_bounded"], ["specification", "reference", "population"])
        self.assertEqual(doc["repair"]["nested_ceiling_usd"], {"specification": 0.56, "population": 0.12, "reference": 0.05})
        # 15.00 / 3 since 2026-09-11 (run L): three sessions at 4.00 plus
        # certification; lefty02w's session stopped at 2.00 after six edits.
        self.assertEqual(doc["repair"]["max_usd_per_failure"], 15.00)
        self.assertEqual(doc["repair"]["max_attempts"], 3)
        self.assertEqual(doc["session"], {"format_error_disposition": "halt"})
        enabled_roles = {
            "semantic_author", "population_adversary", "shortcut_attacker",
            "independent_implementer", "independent_loader", "repair_proposer",
        }
        one_shot_roles = {"ambiguity_critic", "feasibility_reviewer"}
        # Useful validator/tool-using roles ship bounded; critics with no
        # useful loop remain one-shot.  Every declared limit stays under cap.
        for role, spec in doc["roles"].items():
            block = spec.get("session")
            if block is not None:
                self.assertEqual(block["enabled"], role in enabled_roles, role)
                limits = S.SessionLimits.from_block(block)  # the loader accepts every shipped block
                self.assertEqual(limits.as_manifest(), P.role_loop_limits(role), role)
        self.assertEqual(
            {role for role, spec in doc["roles"].items() if spec.get("session", {}).get("enabled")},
            enabled_roles,
        )
        self.assertEqual(
            {role for role, spec in doc["roles"].items()
             if spec.get("session") is not None and not spec["session"]["enabled"]},
            one_shot_roles,
        )
        limits = S.SessionLimits.from_block(author)
        # The shipped author block after the 2026-09-10 rerun: four
        # revisions (the hard cap), five turns, five validator runs.
        self.assertEqual((limits.max_turns, limits.max_tool_calls, limits.max_revisions), (5, 5, 4))
        self.assertEqual((limits.max_usd, limits.session_wall_seconds), (2.75, 900.0))
        limits = S.SessionLimits.from_block(proposer)
        self.assertEqual((limits.max_turns, limits.max_tool_calls, limits.max_certify, limits.max_oracle_bits), (12, 16, 2, 4))
        self.assertEqual((limits.max_usd, limits.session_wall_seconds), (4.0, 900.0))
        self.assertEqual(dict(limits.hard_caps), {"turns": 12, "tool_calls": 20, "usd": 4.00, "wall_clock_s": 2700})
        # The loader REFUSES a default above its cap, and an unknown cap name.
        for bad in (
            {"max_turns": 9, "hard_caps": {"turns": 8}},
            {"max_tool_calls": 17, "hard_caps": {"tool_calls": 16}},
            {"max_usd": 1.01, "hard_caps": {"usd": 1.00}},
            {"wall_clock_s": 2701, "hard_caps": {"wall_clock_s": 2700}},
            {"max_revisions": 3, "hard_caps": {"revisions": 2}},
            {"max_turns": 2, "hard_caps": {"turnz": 8}},
            {"max_turns": 2, "hard_caps": "8"},
            # A revision block's turns are its draft plus its revisions.
            {"max_revisions": 1, "max_turns": 3},
            {"max_revisions": 2, "max_turns": 9, "hard_caps": {"revisions": 2}},
            # The third-fault disposition is a closed vocabulary.
            {"format_error_disposition": "retry"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    S.SessionLimits.from_block(bad)
        # A default AT its cap is fine; the block is hashed verbatim.
        at_cap = S.SessionLimits.from_block({"max_turns": 8, "hard_caps": {"turns": 8}})
        self.assertEqual(at_cap.as_manifest(), {"hard_caps": {"turns": 8}, "max_turns": 8})
        # Enabled roles carry their enforced blocks.  Explicitly disabling
        # them remains the compact, byte-compatible rollback manifest.
        self.assertEqual(P.role_behavior_manifest("semantic_author")["loop_limits"], author)
        self.assertEqual(P.role_behavior_manifest("repair_proposer")["loop_limits"], proposer)
        self.assertEqual(P.role_loop_limits("repair_proposer"), S.SessionLimits.from_block(proposer).as_manifest())
        rollback = json.loads(json.dumps(doc))
        rollback["roles"]["semantic_author"]["session"]["enabled"] = False
        rollback["roles"]["repair_proposer"]["session"]["enabled"] = False
        with mock.patch.object(P, "_agents_doc", lambda: rollback):
            P.clear_behavior_caches()
            self.assertEqual(P.role_manifest_limits("semantic_author"), {"enabled": False})
            self.assertEqual(P.role_manifest_limits("repair_proposer"), {"enabled": False})
        P.clear_behavior_caches()


# ---------------------------------------------------------------------------
# Phase 1 review remediation (docs/plans/bounded_agents_phase1.md §2)
# ---------------------------------------------------------------------------

class ReviewRemediationTest(SessionCase):
    def test_capped_tool_result_binds_the_full_observation_digest(self):
        """Finding 2-1: a tool_result cut at the cap carries the sha256 of
        the FULL observation in its marker, so two observations that differ
        only past the cap render different model-bound bytes and the next
        turn's memo key moves with them — a replay whose tool output diverged
        beyond the cap is a miss, never a silently served turn."""

        def run_with(last_cell: str):
            rows = tuple(("customer_id", "c" * 400) for _ in range(199)) + (("customer_id", "c" * 399 + last_cell),)

            def big_rows(ctx, args):
                return PJ.DevRows(columns=("customer_id", "customer_name"), rows=rows, truncated=False)

            wide = FakeTool("dev_query", impl=big_rows, cost=RG.ToolCost(idempotent_read=True))
            result, provider = self.run_session(
                [[tool_use("dev_query", {}, "a")], [tool_use(SUBMIT, {"patch": "p"}, "c")]],
                [wide, submit_tool()],
            )
            self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
            delivered = provider.calls[1]["messages"][2]["content"][0]["content"]
            rows_turn = next(t for t in result.turns if t.kind == "tool")
            model_turns = [t for t in result.turns if t.kind == "model"]
            return delivered, rows_turn.output_sha256, model_turns[1].memo_key

        first, second = run_with("a"), run_with("b")
        self.assertNotEqual(first[1], second[1], "the full observations differ")
        self.assertEqual(first[0][:8000], second[0][:8000], "identical up to the cap")
        self.assertNotEqual(first[0], second[0], "the delivered bytes still differ")
        self.assertNotEqual(first[2], second[2], "so the next memo key differs")
        for delivered, digest, _ in (first, second):
            self.assertLessEqual(len(delivered.encode("utf-8")), 16 * 1024)
            self.assertTrue(delivered.endswith("(truncated)"))
            self.assertIn(f"(full observation sha256 {digest})", delivered)
        text, truncated = S.cap_tool_output("diagnostic", "x" * (8 * 1024 + 1), digest="d" * 64)
        self.assertTrue(truncated)
        self.assertLessEqual(len(text.encode("utf-8")), 8 * 1024)
        self.assertTrue(text.endswith(f"(full observation sha256 {'d' * 64})\n(truncated)"))
        self.assertEqual(S.cap_tool_output("diagnostic", "short", digest="d" * 64), ("short", False))

    def test_policy_sha256_moves_with_runner_behaviour_fields(self):
        """Finding 2-3: the runner-behaviour facts of a tool (`surface_write`,
        `validator`, `harness_only`, `terminal`, `auto_validators`,
        `dev_population_only`, `trust_domain`, `version`,
        `sanitizer_version`, `cost.no_cost_codes`) enter `policy_sha256`, so
        a semantics change re-keys the store; the wire digest (what the
        payload sends) is untouched by them."""
        base = FakeTool("check_scope")
        baseline = make_policy([base])
        variants = {
            "surface_write": FakeTool("check_scope", surface_write=True),
            "validator": FakeTool("check_scope", validator=True),
            "auto_validators": FakeTool("check_scope", auto_validators=("check_prose",)),
            "no_cost_codes": FakeTool("check_scope", cost=RG.ToolCost(no_cost_codes=frozenset({"scope_refused"}))),
        }
        for name, value in (
            ("harness_only", True), ("terminal", True), ("version", "2"), ("sanitizer_version", "2"),
            ("dev_population_only", True), ("trust_domain", "sandbox"),
        ):
            tool = FakeTool("check_scope")
            setattr(tool, name, value)
            variants[name] = tool
        digests = {name: make_policy([tool]).sha256() for name, tool in variants.items()}
        for name, digest in digests.items():
            self.assertNotEqual(digest, baseline.sha256(), name)
            self.assertEqual(make_policy([variants[name]]).tools_sha256(), baseline.tools_sha256(), name)
        self.assertEqual(len(set(digests.values())), len(digests))
        manifest = S._tool_manifest(base)
        self.assertLessEqual(set(S.TOOL_MANIFEST_BEHAVIOUR_FIELDS), set(manifest))
        self.assertEqual(manifest["cost"]["no_cost_codes"], [])
        self.assertEqual(S._tool_manifest(variants["no_cost_codes"])["cost"]["no_cost_codes"], ["scope_refused"])

    def test_trajectory_digest_is_independent_of_admission_stamp_and_cache_usage(self):
        """Findings 2-5 / 2-6: two sessions with identical bodies, tools and
        policy carry the same `trajectory_sha256` whatever the admission
        stamp's machine path and digest and whatever the provider's
        prompt-cache split; both stay on the turn records as provenance."""

        def run(admission: dict, cache_read: int):
            first = ScriptedTurn(
                content=[tool_use("check_scope", {}, "a")],
                usage={"input_tokens": 100, "output_tokens": 40, "cache_read_input_tokens": cache_read},
            )
            first.admission = admission
            first.route = {
                "provider": "anthropic", "model": "claude-opus-5", "max_tokens": 8, "effort": "high",
                "behavior_sha256": "b", "tools_sha256": "t", "policy_sha256": "p",
                "diagnostics_version": "1" if cache_read else "2", "entry_schema": 3,
            }
            result, _ = self.run_session([first, ScriptedTurn(content=[tool_use(SUBMIT, {"patch": "p"}, "b")])],
                                         [FakeTool("check_scope"), submit_tool()])
            self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
            self.assertTrue(result.verify_chain())
            return result

        alice = run({"admission_record_path": "/Users/alice/council/state/council.live_admitted",
                     "admission_evidence_sha256": "a" * 64}, 0)
        ci = run({"admission_record_path": "/home/ci/council/state/council.live_admitted",
                  "admission_evidence_sha256": "b" * 64}, 900)
        self.assertEqual(alice.session_sha256, ci.session_sha256)
        self.assertEqual(alice.chain_hashes, ci.chain_hashes)
        self.assertNotEqual(dict(alice.turns[0].admission), dict(ci.turns[0].admission))
        self.assertNotEqual(dict(alice.turns[0].usage), dict(ci.turns[0].usage))
        self.assertNotEqual(dict(alice.turns[0].route), dict(ci.turns[0].route))
        self.assertEqual(alice.turns[0].as_dict()["admission"]["admission_record_path"],
                         "/Users/alice/council/state/council.live_admitted")
        self.assertEqual(alice.turns[0].chain_fields()["route"],
                         {"provider": "anthropic", "model": "claude-opus-5", "max_tokens": 8, "effort": "high",
                          "behavior_sha256": "b", "tools_sha256": "t", "policy_sha256": "p"})
        self.assertNotIn("admission", alice.turns[0].chain_fields())
        self.assertNotIn("usage", alice.turns[0].chain_fields())
        # A different STABLE identity still moves the digest.
        other = run({"admission_record_path": "/home/ci/x", "admission_evidence_sha256": "b" * 64}, 900)
        self.assertEqual(other.session_sha256, ci.session_sha256)

    def test_free_text_private_path_is_a_correction_not_a_violation(self):
        """Finding 3-2: the path rules apply to path-typed arguments and the
        SQL rules to SQL-typed ones; ordinary prose in an edit's `new` or a
        submit's text that happens to contain `private/regulated` or
        `docs/runs/2024` is a CORRECTION (`private_path_in_text`, no
        security event, the tool never runs on it), never a terminal
        POLICY_VIOLATION — the session goes on and submits."""
        edit = FakeTool("apply_edit_trial", input_schema={
            "type": "object", "properties": {"new": {"type": "string"}},
            "required": ["new"], "additionalProperties": False,
        }, surface_write=True)
        script = [
            [tool_use("apply_edit_trial", {"new": "Rows flagged private/regulated are excluded (see docs/runs/2024)."}, "a")],
            [tool_use("apply_edit_trial", {"new": "Rows flagged as regulated are excluded; the .git/hooks dir is ignored."}, "b")],
            [tool_use(SUBMIT, {"patch": "Customers whose data is private/confidential are dropped."}, "c")],
            [tool_use(SUBMIT, {"patch": "Customers whose data is regulated are dropped."}, "d")],
        ]
        result, _ = self.run_session(script, [edit, submit_tool()])
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.final, {"patch": "Customers whose data is regulated are dropped."})
        self.assertEqual(result.security_events, ())
        self.assertEqual(result.correction_count, 2)
        self.assertEqual(edit.calls, [{"new": "Rows flagged as regulated are excluded; the .git/hooks dir is ignored."}])
        model_turns = [t for t in result.turns if t.kind == "model"]
        self.assertEqual([t.outcome_code for t in model_turns],
                         [S.FREE_TEXT_PRIVATE_PATH_CODE, "", S.FREE_TEXT_PRIVATE_PATH_CODE, "submitted"])
        self.assertEqual([t.category for t in model_turns], ["correction", "tool_call", "correction", "terminal"])
        self.assert_identities(result)
        # The unit rules: prose is never a violation ...
        for args in (
            {"new": "Records marked private/confidential are excluded."},
            {"text": "see docs/runs/2024 for details"},
            {"rationale": "the .git/hooks dir"},
            {"new": "/etc/passwd is not a table"},
        ):
            self.assertEqual(S._forbidden_argument(args), "", args)
        self.assertEqual(S._free_text_problem({"rationale": "the .git/hooks dir"}), "")
        self.assertEqual(S._free_text_problem({"new": "see /Users/alice/answer_key/gold.csv"}), S.FREE_TEXT_PRIVATE_PATH_CODE)
        self.assertEqual(S._free_text_problem({"new": "loaded from answer_key/gold.csv"}), S.FREE_TEXT_PRIVATE_PATH_CODE)
        # ... while a path-typed or SQL-typed argument keeps its terminal rules.
        for args, detail in (
            ({"artifact": "answer_key/reference/solution.sql"}, "denied_tree"),
            ({"locator": "../x"}, "path_climb"),
            ({"field": "/etc/passwd"}, "absolute_path"),
            ({"path": "runs/x/live_credential.json"}, "denied_tree"),
            ({"file": "x_credential.json"}, "denied_file"),
            ({"sql": "select 'answer_key/gold.csv'"}, "denied_tree"),
            ({"sql": "ATTACH 'x.duckdb'"}, "forbidden_sql"),
            ({"new": "x", "population": "primary"}, "population_argument"),
        ):
            self.assertEqual(S._forbidden_argument(args), detail, args)
            self.assertEqual(S._free_text_problem(args), "", args)

    def test_forbidden_sql_ignores_literals_and_comments(self):
        """Finding 3-3: a forbidden keyword inside a string LITERAL or a
        comment is not executable SQL and must not become a terminal
        violation; a genuine statement keyword still is."""
        for clean_sql in (
            "SELECT * FROM customers WHERE customer_name = 'set'",
            "SELECT * FROM customers -- load the rows now",
            "SELECT * FROM customers /* copy this */ WHERE x = 1",
            "SELECT 'ATTACH me' AS note FROM customers",
        ):
            self.assertEqual(S._forbidden_argument({"sql": clean_sql}), "", clean_sql)
        # A genuine forbidden statement (keyword outside any literal/comment)
        # is still terminal, and a private path in a literal is still denied.
        for sql, detail in (
            ("SET threads = 1", "forbidden_sql"),
            ("LOAD spatial", "forbidden_sql"),
            ("select * from read_csv('x.csv')", "forbidden_sql"),
            ("select 'answer_key/gold.csv'", "denied_tree"),
        ):
            self.assertEqual(S._forbidden_argument({"sql": sql}), detail, sql)

    def test_path_argument_rejects_nul_byte_and_backslash(self):
        """Finding 3-4: a NUL byte or backslash in a path-typed argument is a
        refusal at PERMIT, never a value that reaches `Path(...).resolve()`."""
        self.assertEqual(S._path_argument_problem("a\x00b"), "invalid_path_char")
        self.assertEqual(S._path_argument_problem("a\\b"), "invalid_path_char")
        self.assertEqual(S._forbidden_argument({"path": "a\x00b"}), "invalid_path_char")


# Correction-channel coverage: task-aware validators, diagnostic turns,
# retry and compile caps, and recorded correction kinds.

class CorrectionChannelTest(SessionCase):
    def run_with_hooks(self, script, tools, hooks, **policy_kw):
        provider = ScriptedProvider(script)
        policy = make_policy(tools, **policy_kw)
        result = S.run_bounded_session(
            ROLE, "the view", tools, policy, policy.limits,
            provider=provider, ctx=self.ctx, worker=S.InProcessValidatorWorker(),
            payload_validators=hooks,
        )
        return result, provider

    def test_semantic_corrections_share_schema_retries_budget(self):
        """SoT T1.1 "schema + compile corrections share the 2": a red hook
        result is a compile correction carrying the Diagnostic itself (never
        CORRECTION_TEXT plus a problem) only while BOTH `max_compile_
        corrections` and the shared `SCHEMA_RETRIES` total allow; past
        either the payload is accepted as submitted. The hook sees the
        task (`validate_payload_for` never does), every run is a recorded
        validator turn, and the kinds split into the evidence row."""
        self.assertEqual(S.SCHEMA_RETRIES, P.SCHEMA_RETRIES)
        self.assertEqual(S.SCHEMA_RETRIES, S.PROTOCOL_FAULT_LIMIT - 1)
        seen: list = []

        def compile_check(task, payload):
            seen.append((task.task_id, dict(payload)))
            return None if payload.get("patch") == "green" else diag(ok=False, code="failed")

        # One schema correction (no tool call) + one compile correction spend
        # the shared two: the third turn's red payload is accepted as is even
        # though `max_compile_corrections: 2` alone would allow another.
        script = [
            [text_block("no call at all")],
            [tool_use(SUBMIT, {"patch": "red"}, "a")],
            [tool_use(SUBMIT, {"patch": "red"}, "b")],
        ]
        result, provider = self.run_with_hooks(script, [submit_tool()], [compile_check], max_turns=6, max_compile_corrections=2)
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.final, {"patch": "red"})
        self.assertEqual(result.correction_count, S.SCHEMA_RETRIES)
        self.assertEqual(dict(result.correction_kinds), {"schema": 1, "compile": 1})
        self.assertEqual(result.validator_run_count, 2)
        self.assertEqual(result.tool_call_count, 0)
        self.assertEqual([task_id for task_id, _ in seen], [TASK.task_id, TASK.task_id])
        self.assertEqual([p for _, p in seen], [{"patch": "red"}, {"patch": "red"}])
        # The correction turn carries the rendered Diagnostic, as an error result.
        correction = provider.calls[2]["messages"][-1]["content"][0]
        self.assertEqual(correction["type"], "tool_result")
        self.assertTrue(correction["is_error"])
        self.assertEqual(correction["content"], diag(ok=False, code="failed").render())
        self.assertNotIn("That call was refused", correction["content"])
        self.assertNotIn(P.CORRECTION_TEXT[:20], correction["content"])
        validator_turns = [t for t in result.turns if t.kind == "validator"]
        self.assertEqual([t.tool_name for t in validator_turns], ["compile_check", "compile_check"])
        self.assertEqual([t.outcome_code for t in validator_turns], ["failed", "failed"])
        self.assertTrue(all(t.fresh and t.output_sha256 for t in validator_turns))
        corrections = [t for t in result.turns if t.kind == "model" and t.category == "correction"]
        self.assertEqual([t.outcome_code for t in corrections], ["no_tool_call", "validator_red"])
        self.assert_identities(result)
        # `max_compile_corrections` binds first when it is the tighter bound.
        seen.clear()
        result, _ = self.run_with_hooks(
            [[tool_use(SUBMIT, {"patch": "red"}, "a")], [tool_use(SUBMIT, {"patch": "red"}, "b")]],
            [submit_tool()], [compile_check], max_turns=6, max_compile_corrections=1,
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(dict(result.correction_kinds), {"schema": 0, "compile": 1})
        self.assertEqual(result.validator_run_count, 2)
        # A green run is recorded and corrects nothing.
        result, _ = self.run_with_hooks([[tool_use(SUBMIT, {"patch": "green"}, "a")]], [submit_tool()], [compile_check])
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(dict(result.correction_kinds), {"schema": 0, "compile": 0})
        self.assertEqual(result.validator_run_count, 1)
        self.assertEqual([t.outcome_code for t in result.turns if t.kind == "validator"], ["ok"])
        self.assert_identities(result)
        # The hook and a declared harness validator share the same budget.
        registry_check = FakeTool("check_prose", input_schema=submit_tool().input_schema, validator=True,
                                  impl=lambda ctx, args: diag(ok=False, code="failed"))
        registry_check.harness_only = True
        result, provider = self.run_with_hooks(
            [[text_block("no call")], [tool_use(SUBMIT, {"patch": "red"}, "a")], [tool_use(SUBMIT, {"patch": "red"}, "b")]],
            [registry_check, submit_tool()], [compile_check], max_turns=6, max_compile_corrections=2,
            harness_validators=["check_prose"],
        )
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(dict(result.correction_kinds), {"schema": 1, "compile": 1})
        self.assertEqual(result.validator_run_count, 4)  # hook + registry validator, twice
        self.assertEqual(len(registry_check.calls), 2)
        self.assert_identities(result)
        # No hooks: the parity path is untouched (a submit is a submit).
        result, _ = self.run_with_hooks([[tool_use(SUBMIT, {"patch": "red"}, "a")]], [submit_tool()], ())
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.validator_run_count, 0)

    def test_payload_validator_hook_faults_are_harness_faults(self):
        """C7: a hook that raises, returns a non-projection, or returns a
        Diagnostic the gatekeeper refuses is a HARNESS fault (exit 2, reward
        None, the partial result attached) — never a correction the model
        could learn from and never a task rejection."""
        cases = [
            (lambda task, payload: "not a diagnostic", S.ToolHarnessFault, "non_projection_result", "HARNESS_FAULT"),
            (lambda task, payload: (_ for _ in ()).throw(RuntimeError("boom")), S.ToolHarnessFault, "harness_exception", "HARNESS_FAULT"),
            (lambda task, payload: diag(ok=False, code="failed", names=("hidden_secret_table",)),
             PJ.DiagnosticTripwire, "", "LEAK_TRIPWIRE"),
        ]
        for hook, exc_type, code, terminal in cases:
            with self.subTest(terminal=terminal):
                with self.assertRaises(exc_type) as caught:
                    self.run_with_hooks([[tool_use(SUBMIT, {"patch": "red"}, "a")]], [submit_tool()], [hook])
                exc = caught.exception
                if code:
                    self.assertEqual(exc.code, code)
                partial = exc.session_result
                self.assertEqual(partial.terminal.name, terminal)
                self.assertIsNone(partial.final)
                self.assertEqual(partial.validator_run_count, 1)
                # The validator turn that faulted, then the model turn it was
                # validating closed as a terminal carrying the fault's code:
                # the SoT T5 identities hold on the partial result.
                self.assertEqual([t.kind for t in partial.turns][-2:], ["validator", "model"])
                closing = partial.turns[-1]
                self.assertEqual((closing.category, closing.tool_name), ("terminal", SUBMIT))
                self.assertEqual(closing.outcome_code, code or "sanitizer_tripwire")
                self.assertEqual(partial.terminal_count, 1)
                self.assertTrue(partial.verify_chain())
                self.assertNotEqual(engine_mod._infra_marker_for(exc), "")
        with self.assertRaises(TypeError):
            self.run_with_hooks([[tool_use(SUBMIT, {"patch": "red"}, "a")]], [submit_tool()], ["not callable"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

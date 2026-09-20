"""The headless harness backends (review/headless.py) with a scripted
driver: turn synthesis, answer delivery, wire narrowing, accounting, the
session close, one-shot prose, and the provider-kind plumbing."""
from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

from elt_taskgen.review import headless as H
from elt_taskgen.review import providers as P


class ScriptedDriver(H.HarnessDriver):
    """A driver whose 'model' is a script: on start and on every follow-up
    it calls the listed tools in order (each after the previous answer
    arrives) and then ends the turn with a text."""

    kind = "scripted"
    instances: list["ScriptedDriver"] = []

    def __init__(self, bridge, config):
        super().__init__(bridge, config)
        self.script = list(config.get("script") or [])
        self.started = None
        self.sent: list[str] = []
        self.interrupted = 0
        self.closed = 0
        self.answers: list[tuple[str, str, bool]] = []
        ScriptedDriver.instances.append(self)

    def _play(self, calls, end):
        def run():
            for name, args in calls:
                call = self.bridge.call(name, args)
                call.wait(5)
                self.answers.append((name, call.text, call.is_error))
                if call.is_error and call.text == H.SESSION_ENDED_TEXT:
                    return
            self.bridge.add_usage(P.Usage(input_tokens=100, output_tokens=20))
            self.bridge.turn_ended(end)

        threading.Thread(target=run, daemon=True).start()

    def start(self, spec):
        self.started = spec
        calls, end = self.script.pop(0)
        self._play(calls, end)

    def send(self, text):
        self.sent.append(text)
        calls, end = self.script.pop(0) if self.script else ([], H.TurnEnd(text="done"))
        self._play(calls, end)

    def interrupt(self):
        self.interrupted += 1

    def close(self, timeout=15.0):
        self.closed += 1


def _backend(script):
    ScriptedDriver.instances.clear()
    return H.HeadlessBackend(
        {"script": script}, driver_factory=lambda bridge, cfg: ScriptedDriver(bridge, cfg)
    )


WIRE = (
    {"name": "dev_query", "description": "q", "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "submit", "description": "s", "input_schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}},
    {"name": "abort", "description": "a", "input_schema": {"type": "object", "properties": {"reason_code": {"type": "string"}}, "required": ["reason_code"]}},
)
TERMINAL = tuple(t for t in WIRE if t["name"] in ("submit", "abort"))


def _step(backend, messages, tools=WIRE):
    return backend.step(
        role_name="repair_proposer", model="m", messages=messages, max_tokens=10,
        effort=None, tools=tools, tool_choice=None,
    )


class TurnSynthesisTests(unittest.TestCase):
    def test_each_tool_call_is_one_turn_and_the_answer_reaches_the_harness(self):
        backend = _backend([
            ([("dev_query", {"sql": "select 1"}), ("submit", {"answer": "1"})], H.TurnEnd(text="fin")),
        ])
        turn = _step(backend, [{"role": "user", "content": "view"}])
        self.assertEqual(turn.stop_reason, "tool_use")
        block = turn.tool_call()
        self.assertEqual((block["name"], block["input"]), ("dev_query", {"sql": "select 1"}))
        self.assertEqual(turn.raw["event"], "tool_call")
        # The runner answers the call; the next turn is the harness's next call.
        answered = P._with_tool_results(
            [{"role": "user", "content": "view"}, {"role": "assistant", "content": list(turn.content)}][:1]
            + [{"role": "assistant", "content": list(turn.content)}],
            list(turn.content),
            [(block["id"], "rows: 1", False)],
        )
        turn2 = _step(backend, answered)
        self.assertEqual(turn2.tool_call()["name"], "submit")
        driver = ScriptedDriver.instances[0]
        self.assertEqual(driver.answers[0], ("dev_query", "rows: 1", False))
        # A terminal stop never asks for another turn: close ends the harness
        # and answers the pending call with the session-ended refusal.
        backend.close_session("repair_proposer")
        self.assertEqual(driver.interrupted, 1)
        self.assertEqual(driver.closed, 1)
        self.assertEqual(driver.answers[1][1], H.SESSION_ENDED_TEXT)

    def test_a_turn_that_ends_without_a_call_is_a_text_turn_and_a_correction_reprompts(self):
        backend = _backend([
            ([], H.TurnEnd(text="I think it is done.")),
            ([("submit", {"answer": "x"})], H.TurnEnd(text="fin")),
        ])
        turn = _step(backend, [{"role": "user", "content": "view"}])
        self.assertEqual(turn.stop_reason, "end_turn")
        self.assertEqual(turn.text_content, "I think it is done.")
        self.assertIsNotNone(turn.protocol_fault())  # the runner's no_tool_call correction
        turn2 = _step(backend, [
            {"role": "user", "content": "view"},
            {"role": "assistant", "content": list(turn.content)},
            {"role": "user", "content": "correction: call a tool"},
        ])
        self.assertEqual(turn2.tool_call()["name"], "submit")
        self.assertEqual(ScriptedDriver.instances[0].sent, ["correction: call a tool"])

    def test_usage_deltas_sum_to_the_harness_total(self):
        backend = _backend([([("submit", {"answer": "x"})], H.TurnEnd(text="fin"))])
        turn = _step(backend, [{"role": "user", "content": "view"}])
        self.assertEqual(turn.usage.input_tokens, 0)  # no usage reported before the first call
        answered = P._with_tool_results(
            [{"role": "user", "content": "view"}, {"role": "assistant", "content": list(turn.content)}][:1]
            + [{"role": "assistant", "content": list(turn.content)}],
            list(turn.content), [(turn.tool_call()["id"], "accepted", False)],
        )
        turn2 = _step(backend, answered)
        self.assertEqual(turn2.stop_reason, "end_turn")
        self.assertEqual((turn2.usage.input_tokens, turn2.usage.output_tokens), (100, 20))
        self.assertEqual(turn2.raw["harness_steps"], 0)

    def test_budget_end_is_the_role_cap_with_the_usage_attached(self):
        backend = _backend([([], H.TurnEnd(text="", reason="budget_exceeded"))])
        with self.assertRaises(P.RoleCapExceeded) as caught:
            _step(backend, [{"role": "user", "content": "view"}])
        self.assertEqual(caught.exception.scope, "role")
        self.assertEqual(P.backend_attempts(caught.exception)[0].usage.input_tokens, 100)

    def test_a_harness_error_is_a_provider_fault(self):
        backend = _backend([([], H.TurnEnd(text="", reason="error", is_error=True, detail="401"))])
        with self.assertRaises(P.ProviderFault) as caught:
            _step(backend, [{"role": "user", "content": "view"}])
        self.assertEqual(caught.exception.code, "transport")

    def test_a_served_prefix_cannot_be_continued_live(self):
        backend = _backend([])
        with self.assertRaises(H.HeadlessHarnessError) as caught:
            _step(backend, [{"role": "user", "content": "a"}, {"role": "assistant", "content": []}, {"role": "user", "content": "b"}])
        self.assertEqual(caught.exception.code, "headless_prefix")


class WireNarrowingTests(unittest.TestCase):
    def test_the_bridge_refuses_calls_outside_a_narrowed_wire_then_lets_one_through(self):
        bridge = H.HarnessBridge(["dev_query", "submit", "abort"])
        bridge.set_wire(["submit", "abort"])
        refused = [bridge.call("dev_query", {"sql": "x"}) for _ in range(H.MAX_WIRE_REFUSALS)]
        self.assertTrue(all(c.answered and c.is_error for c in refused))
        self.assertIn("submit", refused[0].text)
        self.assertTrue(bridge.events.empty())
        passed = bridge.call("dev_query", {"sql": "x"})
        self.assertFalse(passed.answered)
        self.assertEqual(bridge.events.get_nowait()[0], "tool_call")
        bridge.set_wire(None)
        again = bridge.call("dev_query", {"sql": "x"})
        self.assertFalse(again.answered)

    def test_step_narrows_the_bridge_to_the_turn_wire(self):
        backend = _backend([
            ([("dev_query", {"sql": "select 1"}), ("submit", {"answer": "x"})], H.TurnEnd(text="fin")),
        ])
        turn = _step(backend, [{"role": "user", "content": "view"}])
        bridge = ScriptedDriver.instances[0].bridge
        self.assertIsNone(bridge.permitted)  # the full wire: nothing to refuse
        answered = [
            {"role": "user", "content": "view"},
            {"role": "assistant", "content": list(turn.content)},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": turn.tool_call()["id"], "content": "rows", "is_error": False}]},
        ]
        _step(backend, answered, tools=TERMINAL)
        self.assertEqual(bridge.permitted, frozenset({"submit", "abort"}))

    def test_a_session_opened_on_the_terminal_wire_registers_only_those_tools(self):
        backend = _backend([([("submit", {"answer": "x"})], H.TurnEnd(text="fin"))])
        _step(backend, [{"role": "user", "content": "view"}], tools=TERMINAL)
        driver = ScriptedDriver.instances[0]
        self.assertEqual([t["name"] for t in driver.started.tools], ["submit", "abort"])
        self.assertIsNone(driver.bridge.permitted)


class RetryPolicyTests(unittest.TestCase):
    def test_transient_classification_and_backoff(self):
        self.assertTrue(H._codex_error_is_transient("stream error: 529 Overloaded"))
        self.assertTrue(H._codex_error_is_transient("getaddrinfo ENOTFOUND api.openai.com"))
        self.assertFalse(H._codex_error_is_transient("invalid_request_error: max_tokens too large"))
        self.assertFalse(H._codex_error_is_transient("unauthorized"))
        self.assertIn("server_error", H._CLAUDE_TRANSIENT_ERRORS)
        self.assertNotIn("invalid_request", H._CLAUDE_TRANSIENT_ERRORS)
        self.assertNotIn("authentication_failed", H._CLAUDE_TRANSIENT_ERRORS)
        self.assertEqual([H._retry_backoff_seconds(i) for i in range(3)], [2.0, 4.0, 8.0])
        self.assertEqual(H.HARNESS_RETRIES, P._HTTP_RETRIES)


class OneShotTests(unittest.TestCase):
    def test_prose_complete_returns_the_final_text_and_closes(self):
        backend = _backend([([], H.TurnEnd(text="  the prose  ", harness_cost_usd=0.01))])
        result = backend.complete(role_name="semantic_author", model="m", prompt="p", max_tokens=1, effort=None)
        self.assertEqual(result.text, "the prose")
        self.assertEqual(result.usage.input_tokens, 100)
        self.assertEqual(ScriptedDriver.instances[0].closed, 1)
        self.assertEqual(backend.harness_log("semantic_author"), [])

    def test_schema_complete_validates_the_forced_tool_payload(self):
        good = {"findings": []}
        backend = _backend([([("report_findings", {"nope": 1}), ("report_findings", good)], H.TurnEnd(text="fin"))])
        result = backend.complete(role_name="ambiguity_critic", model="m", prompt="p", max_tokens=1, effort=None)
        self.assertEqual(result.text, P.normalized_text_for("ambiguity_critic", good))
        driver = ScriptedDriver.instances[0]
        self.assertEqual([t["name"] for t in driver.started.tools], [P.tool_name_for("ambiguity_critic")])
        first_answer = driver.answers[0]
        self.assertTrue(first_answer[2])
        self.assertIn("REJECTED", first_answer[1])
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(driver.closed, 1)

    def test_schema_complete_exhausts_after_the_shared_retries(self):
        bad = [("report_findings", {"nope": i}) for i in range(1 + P.SCHEMA_RETRIES + 1)]
        backend = _backend([(bad, H.TurnEnd(text="fin"))])
        from elt_taskgen.review.council import ProviderProtocolError

        with self.assertRaises(ProviderProtocolError) as caught:
            backend.complete(role_name="ambiguity_critic", model="m", prompt="p", max_tokens=1, effort=None)
        self.assertEqual(len(P.backend_attempts(caught.exception)), 1 + P.SCHEMA_RETRIES)


class ProviderKindTests(unittest.TestCase):
    def test_the_kinds_are_registered_backends(self):
        self.assertIs(P.PROVIDER_BACKENDS["claude_headless"], H.ClaudeHeadlessBackend)
        self.assertIs(P.PROVIDER_BACKENDS["codex_headless"], H.CodexHeadlessBackend)

    def test_pricing_family_and_credentials(self):
        cfg = {"pricing": {"gpt-5.5": {"usd_per_mtok_input": 5.0, "usd_per_mtok_output": 30.0}}}
        self.assertEqual(P.rate_card_for("codex_headless", "gpt-5.5", cfg).output, 30.0)
        with self.assertRaisesRegex(RuntimeError, "codex_headless"):
            P.rate_card_for("codex_headless", "gpt-9", cfg)
        self.assertEqual(P.rate_card_for("claude_headless", "claude-sonnet-5", {}), P.ANTHROPIC_PRICING_USD_PER_MTOK["claude-sonnet-5"])
        self.assertEqual(P.model_family("claude_headless", "claude-sonnet-5"), "anthropic")
        self.assertEqual(P.model_family("codex_headless", "gpt-5.5", {}), "openai")
        self.assertEqual(H.credential_problems_for("claude_headless", {}), ["ANTHROPIC_API_KEY is not set (claude_headless provider)"])
        self.assertEqual(H.credential_problems_for("claude_headless", {"api_key": "k"}), [])
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/nonexistent-codex-home"}):  # noqa: E501
            self.assertEqual(len(H.credential_problems_for("codex_headless", {})), 1)
        self.assertEqual(H.credential_problems_for("codex_headless", {"api_key": "k"}), [])

    def test_the_mode_switch_moves_every_model_calling_role(self):
        from elt_taskgen import cli
        from elt_taskgen.review import metrology as M

        claude_roles = ("semantic_author", "repair_proposer")
        # One-shot seats never call a tool and answer one forced tool call the
        # API sends and a coding-agent harness cannot; they are pinned to the
        # API in both modes (measured 2026-09-14: the population adversary
        # through Claude Code raised 5 false alarms on 10 clean tasks and hit
        # the impossible canary twice; the API seat raised none).
        one_shot_roles = ("ambiguity_critic", "population_adversary", "shortcut_attacker",
                          "feasibility_reviewer")
        witness_roles = ("independent_implementer", "independent_loader")
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("ELT_TASKGEN_AGENT_HARNESS", "ELT_TASKGEN_CLAUDE_PROVIDER", "ELT_TASKGEN_OSS_PROVIDER"):
                os.environ.pop(name, None)
            self.assertEqual(cli.current_agent_harness(), "api")
            self.assertEqual(cli.apply_agent_harness(None), "api")
            default = P.load_role_routing()
            self.assertEqual(cli.apply_agent_harness("headless"), "headless")
            self.assertEqual(os.environ["ELT_TASKGEN_AGENT_HARNESS"], "headless")
            switched = P.load_role_routing()
            self.assertEqual(cli.current_agent_harness(), "headless")
        for role in claude_roles:
            self.assertEqual(default.for_role(role).provider, "anthropic", role)
            self.assertEqual(switched.for_role(role).provider, "claude_headless", role)
            self.assertEqual(switched.for_role(role).model, default.for_role(role).model, role)
        for role in one_shot_roles:
            self.assertEqual(default.for_role(role).provider, "anthropic", role)
            self.assertEqual(switched.for_role(role).provider, "anthropic", role)
        for role in witness_roles:
            self.assertEqual(default.for_role(role).provider, "openai_compat", role)
            self.assertEqual(switched.for_role(role).provider, "codex_headless", role)
            self.assertEqual(switched.for_role(role).model, switched.provider_config["codex_headless"]["model"], role)
            self.assertLessEqual(switched.for_role(role).max_tokens, int(switched.provider_config["codex_headless"]["max_tokens"]))
        self.assertEqual(default.for_role("independent_implementer").max_tokens, 65536)
        self.assertEqual(switched.for_role("independent_implementer").max_tokens, 16384)
        # The council seats did not move, so one admission covers both modes.
        self.assertEqual(M.council_routing_fingerprint(default), M.council_routing_fingerprint(switched))
        # The calibration solver roster is a measurement instrument and stays put.
        from elt_taskgen.corpus import calibration as calibration_mod

        with mock.patch.dict(os.environ, {"ELT_TASKGEN_AGENT_HARNESS": "headless"}):
            self.assertTrue(all(t.provider in ("anthropic", "openai_compat") for t in calibration_mod.load_calibration_roster()))

    def test_the_mode_is_run_identity_and_selects_the_admission_record(self):
        from elt_taskgen import cli
        from elt_taskgen.pipeline_readiness import GenerationRunSpec, StageName, _configured_inputs

        api = GenerationRunSpec(candidate_count=1, profile="draft")
        headless = GenerationRunSpec(candidate_count=1, profile="draft", agent_harness="headless")
        self.assertEqual(api.agent_harness, "api")
        self.assertNotEqual(api.fingerprint(), headless.fingerprint())
        self.assertEqual(_configured_inputs(headless, StageName.REVIEW)["agent_harness"], "headless")
        with self.assertRaises(Exception):
            GenerationRunSpec(candidate_count=1, profile="draft", agent_harness="remote")
        with mock.patch.dict(os.environ, {"ELT_TASKGEN_ADMISSION": ""}):
            os.environ.pop("ELT_TASKGEN_ADMISSION", None)
            cli.apply_agent_harness("api")
            api_record = cli._default_admission_reference()
            cli.apply_agent_harness("headless")
            headless_record = cli._default_admission_reference()
        self.assertTrue(api_record is None or api_record.name == "council.live_admitted")
        # Both modes consult the same record: the critics are pinned to the API.
        self.assertEqual(api_record, headless_record)
        self.assertTrue(headless_record is None or "council_headless" not in str(headless_record))

    def test_codex_close_and_interrupt_never_block_on_an_unanswered_sdk_request(self):
        """headless-20 (2026-09-15): the app-server stopped answering after
        the runner refused its late tool calls; `close()` called the SDK's
        `interrupt()` on the worker's main thread and the pipeline hung for an
        hour. Every main-thread SDK request is now bounded."""
        import threading
        import time

        driver = H.CodexDriver.__new__(H.CodexDriver)
        driver._handle_lock = threading.Lock()
        driver._thread = None
        driver._http_server = None
        driver._http_thread = None
        driver._commands = __import__("queue").Queue()
        never = threading.Event()
        closed = threading.Event()

        class Handle:
            def interrupt(self):
                never.wait()  # the reply that never comes

        class Codex:
            def close(self):
                closed.set()

        driver._handle = Handle()
        driver._codex = Codex()
        driver.SDK_REQUEST_TIMEOUT_S = 0.2
        started = time.monotonic()
        driver.close(timeout=0.2)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertTrue(closed.is_set())
        self.assertIsNone(driver._codex)
        never.set()

    def test_end_session_closes_the_harness(self):
        backend = _backend([([("submit", {"answer": "x"})], H.TurnEnd(text="fin"))])
        _step(backend, [{"role": "user", "content": "view"}])
        driver = ScriptedDriver.instances[0]
        provider = mock.Mock()
        provider._sessions = {}
        provider._backends = {"claude_headless": backend}
        account = mock.Mock(role="repair_proposer", provider="claude_headless")
        P.RoutedProvider.end_session(provider, account)
        self.assertEqual(driver.closed, 1)


if __name__ == "__main__":
    unittest.main()

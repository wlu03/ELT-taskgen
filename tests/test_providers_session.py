"""Phase 1.R: the provider wire layer for bounded sessions (review/providers.py).

What is pinned here, all with NO network and NO new double type (the
`FakeTransport` of tests/test_providers.py scripts a session as a FIFO of raw
API bodies, `[tool_use turn, ..., submit turn]`):

  * `AnthropicBackend.step` / `OpenAICompatBackend.step`: one HTTP exchange
    over a message prefix with N wire tools and `tool_choice` any / auto /
    forced, every `tool_use` block returned, more than one refused as
    `ToolProtocolFault("multiple_tool_use")` that the caller answers on EVERY
    id (never executes);
  * `_with_tool_results`: the generic `tool_result` turn on both wires;
  * `RoutedProvider._turn`: memo under `transcript_key_v3` (role, policy,
    tools and the exact message prefix), replay-only serves the exact prefix
    then raises `TranscriptMissingError`, `rate_card_for` fail-closed,
    `reserve` before transport, one call, record-before-return as an
    `entry_schema` 3 entry, `charge` per attempt after the record;
  * `RoutedProvider.run_session`: one `exchange_evidence` row per session;
  * `run_session` -> the REAL `review.session.run_bounded_session` ->
    `_turn` -> `FakeTransport`, end to end on BOTH backends (item 1.R-c):
    one entry_schema-3 entry per model turn, the trajectory chain, the
    detector, replay with zero transport pops giving the same
    `SessionResult`, a transport fault resumed from the recorded prefix;
  * the zero-tool parity: turn 0 of a zero-tool session keys, records and
    sends exactly what `complete()` does;
  * `BatchQueue` refuses any role with a non-empty tool registry;
  * `RoleCapExceeded` (scope role) is catchable by the runner while the task
    and total scopes stay the base `BudgetExceededError`.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest import mock

from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.models import CouncilRole, Severity, canonical_json, sha256_hex
from elt_taskgen.review import providers as P
from elt_taskgen.review import session as S
from elt_taskgen.review.tools import projection as PJ
from elt_taskgen.review.tools import registry as R
from tests.test_providers import (
    VALID_FINDING,
    FakeTransport,
    _FaultingTransport,
    anthropic_text_response,
    anthropic_tool_response,
    make_routing,
    openai_text_response,
)


# ---------------------------------------------------------------------------
# Scripted raw API bodies (the FIFO a FakeTransport pops per turn)
# ---------------------------------------------------------------------------

def tool_use_body(name, input, *, id="toolu_01", input_tokens=1000, output_tokens=100,
                  model="claude-opus-5", stop_reason="tool_use", text=None):
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    content.append({"type": "tool_use", "id": id, "name": name, "input": input})
    return {
        "model": model,
        "stop_reason": stop_reason,
        "content": content,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def parallel_tool_use_body(calls, *, input_tokens=1000, output_tokens=100, model="claude-opus-5"):
    """Two or more `tool_use` blocks in ONE assistant turn."""
    return {
        "model": model,
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "id": id, "name": name, "input": input}
            for id, name, input in calls
        ],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def chat_tool_call_body(name, arguments, *, call_id="call_01", prompt_tokens=800,
                        completion_tokens=150, model="test-oss-model", finish="tool_calls"):
    return {
        "model": model,
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments) if isinstance(arguments, dict) else arguments,
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


# ---------------------------------------------------------------------------
# A stub model-facing tool: registered per test to make a role AGENTIC
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _StubTool:
    name: str
    description: str = "A stub model-facing tool for the provider wire tests."
    input_schema: Mapping[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
    )
    cost: R.ToolCost = field(default_factory=R.ToolCost)
    permitted_roles: frozenset[str] = frozenset(
        {"semantic_author", "independent_implementer", "repair_proposer"}
    )

    def run(self, ctx, args):
        raise AssertionError(
            f"tool {self.name!r} was EXECUTED at the provider layer (it never dispatches)"
        )


@dataclass
class _RunningTool:
    """A model-facing tool that RUNS: it returns a real projection (a
    `Diagnostic` of the GATE source) that the real gatekeeper vets on the
    demo task, so a runner-driven session dispatches in-process against the
    real projection layer (04 §7: no new doubling mechanism). `code`/`ok`
    are the observation it reports; changing them is "a mutated projection
    version": the tool_result bytes in the prefix change and every later
    turn is re-keyed."""

    name: str
    code: str = "ok"
    ok: bool = True
    description: str = "A running stub tool for the provider wire tests."
    input_schema: Mapping[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
    )
    cost: R.ToolCost = field(default_factory=R.ToolCost)
    permitted_roles: frozenset[str] = frozenset(
        {"semantic_author", "independent_implementer", "repair_proposer"}
    )
    calls: list = field(default_factory=list)

    def run(self, ctx, args):
        self.calls.append(dict(args))
        return PJ.Diagnostic(source=PJ.DiagnosticSource.GATE, ok=self.ok, code=self.code)


AUTHOR = "semantic_author"
IMPLEMENTER = "independent_implementer"
AUTHOR_TOOLS = ("replace_prose", "submit_prose", "abort")
TASK = demo_task()


class _AgenticRoles:
    """Register stub tools for roles for the duration of a test (the registry
    is EMPTY for every role at baseline), dropping the behaviour caches on
    the way in and out so every digest is recomputed. A name becomes a
    never-dispatching `_StubTool`; a tool OBJECT (a `_RunningTool`) is
    registered as given."""

    def __init__(self, **roles):
        self.roles = {
            role: tuple(_StubTool(t) if isinstance(t, str) else t for t in tools)
            for role, tools in roles.items()
        }
        self._patch = mock.patch.dict(R._ROLE_TOOLS, self.roles)

    def __enter__(self):
        self._patch.__enter__()
        P.clear_behavior_caches()
        return self

    def __exit__(self, *exc):
        self._patch.__exit__(*exc)
        P.clear_behavior_caches()
        return False


def _provider(tmp, transport, *, meter=None, replay_only=False, routing=None, subdir="transcripts"):
    return P.RoutedProvider(
        routing or make_routing(),
        P.TranscriptStore(Path(tmp) / subdir),
        meter or P.CostMeter(budget_per_task_usd=100.0),
        task_id="task-1",
        replay_only=replay_only,
        transports={"anthropic": transport, "openai_compat": transport},
    )


def _entry_path(tmp, role, key, subdir="transcripts"):
    return Path(tmp) / subdir / role / f"{key}.json"


def _view(text="VIEW"):
    return [{"role": "user", "content": text}]


def _session_policy(role, *, submit="submit_prose", max_turns=6, max_tool_calls=8, max_usd=5.0):
    """The policy a runner-driven session for `role` runs under: the role's
    REGISTERED tools (the `_AgenticRoles` patch), `submit` as the submit
    tool, an ENABLED limits block, the same wire tools `wire_tools_for`
    sends."""
    declared = P.session_policy_for(role)
    return P.SessionPolicy(
        role=role, tools=declared.tools, submit_tool=submit,
        limits=P.SessionLimits.from_block(
            {"enabled": True, "max_turns": max_turns, "max_tool_calls": max_tool_calls, "max_usd": max_usd}
        ),
        mode="model_driven", wire_tools=declared.wire_tools,
    )


def _session_ctx(tmp, role):
    """A `ToolContext` on the demo task (the gatekeeper scans against it)."""
    root = Path(tmp) / "work"
    root.mkdir(exist_ok=True)
    return R.ToolContext(root=root, task_id=TASK.task_id, role=role, task=TASK)


def _bound(provider):
    provider.begin_task_evidence(TASK.task_id, TASK.content_hash())
    return provider


def _tool_results_in(payload):
    """(id, content) of every tool result a transport payload carries, on
    either wire: `tool_result` blocks of the Messages API, `role: "tool"`
    messages of chat completions."""
    out = []
    for message in payload["messages"]:
        if message.get("role") == "tool":
            out.append((message["tool_call_id"], message["content"]))
        elif message.get("role") == "user" and isinstance(message.get("content"), list):
            for block in message["content"]:
                if block.get("type") == "tool_result":
                    out.append((block["tool_use_id"], block["content"]))
    return out


def _evidence_of(result):
    """A `SessionResult` as evidence: everything but the accounting fields
    (`usd`, the walls, the served-vs-live counts and flags, the timings),
    which a replay legitimately differs in."""
    data = result.as_dict()
    for key in ("usd", "wall_ms", "tool_wall_ms", "live_model_call_count"):
        data.pop(key)
    data["turns"] = [
        {k: v for k, v in turn.items() if k not in ("usd", "replayed", "elapsed_model_ms", "elapsed_tool_ms")}
        for turn in data["turns"]
    ]
    return data


class _SessionCase(unittest.TestCase):
    def setUp(self):
        P.clear_behavior_caches()
        self.addCleanup(P.clear_behavior_caches)


# ---------------------------------------------------------------------------
# Backend steps and the generic tool_result turn
# ---------------------------------------------------------------------------

class BackendStepTest(_SessionCase):
    def test_step_sends_n_tools_with_any_and_returns_every_tool_use(self):
        """One HTTP exchange: the N wire tools, `tool_choice: any`, the whole
        prefix; EVERY `tool_use` block comes back, `truncated` folds the two
        wire spellings, and a step never retries on its own."""
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}):
            policy = P.session_policy_for(AUTHOR)
            self.assertEqual(policy.allowlist, tuple(sorted(AUTHOR_TOOLS)))
            self.assertEqual(P.session_tool_choice(policy), "any")
            tools = [dict(t) for t in policy.wire_tools]
            transport = FakeTransport([
                parallel_tool_use_body([
                    ("toolu_a", "replace_prose", {"text": "one"}),
                    ("toolu_b", "submit_prose", {"text": "two"}),
                ]),
                tool_use_body("submit_prose", {"text": "x"}, stop_reason="max_tokens"),
            ])
            backend = P.AnthropicBackend("sk-test", transport=transport)
            turn = backend.step(
                role_name=AUTHOR, model="claude-opus-5", messages=_view(),
                max_tokens=1024, effort="high", tools=tools, tool_choice="any",
            )
            payload = transport.calls[0][2]
            self.assertEqual(payload["messages"], _view())
            self.assertEqual([t["name"] for t in payload["tools"]], sorted(AUTHOR_TOOLS))
            self.assertTrue(all(t["strict"] for t in payload["tools"]))
            self.assertEqual(payload["tool_choice"], {"type": "any"})
            self.assertEqual(payload["output_config"], {"effort": "high"})
            self.assertEqual(payload["system"], P._system_prompt(AUTHOR, schema_mode=False))
            self.assertNotIn("temperature", payload)
            self.assertEqual(turn.tool_use_ids, ("toolu_a", "toolu_b"))
            self.assertEqual(len(turn.tool_uses), 2)
            self.assertEqual(len(turn.attempts), 1)
            self.assertEqual(turn.usage.input_tokens, 1000)
            self.assertEqual(turn.served_model, "claude-opus-5")
            self.assertFalse(turn.truncated)
            self.assertEqual(turn.stop_reason, "tool_use")
            # The second body is a truncation: reported, never retried.
            truncated = backend.step(
                role_name=AUTHOR, model="claude-opus-5", messages=_view(),
                max_tokens=1024, effort=None, tools=tools, tool_choice="auto",
            )
            self.assertTrue(truncated.truncated)
            self.assertEqual(transport.calls[1][2]["tool_choice"], {"type": "auto"})
            self.assertEqual(len(transport.calls), 2)
            # A forced tool name and the one-shot shapes are accepted too.
            self.assertEqual(P._anthropic_tool_choice("submit_prose"), {"type": "tool", "name": "submit_prose"})
            self.assertEqual(P._anthropic_tool_choice({"type": "tool", "name": "x"}), {"type": "tool", "name": "x"})
            self.assertIsNone(P._anthropic_tool_choice(None))
            self.assertEqual(P._chat_tool_choice("any"), "required")
            self.assertEqual(P._chat_tool_choice("auto"), "auto")
            self.assertEqual(
                P._chat_tool_choice({"type": "tool", "name": "x"}),
                {"type": "function", "function": {"name": "x"}},
            )

    def test_chat_step_answers_the_same_wire_and_parses_tool_calls_back(self):
        """The chat-completions step sends the SAME tools as function objects,
        `tool_choice: required` for `any`, renders `tool_result` blocks as
        `role: "tool"` messages (every id) and parses `tool_calls` back into
        canonical `tool_use` blocks; `finish_reason: length` is a truncation."""
        with _AgenticRoles(**{IMPLEMENTER: ("dev_query",)}):
            policy = P.session_policy_for(IMPLEMENTER)
            tools = [dict(t) for t in policy.wire_tools]
            transport = FakeTransport([
                chat_tool_call_body("dev_query", {"text": "select 1"}),
                chat_tool_call_body("dev_query", "{not json", call_id="call_02", finish="length"),
            ])
            backend = P.OpenAICompatBackend("http://oss:8000/v1", "k", transport=transport)
            turn = backend.step(
                role_name=IMPLEMENTER, model="m", messages=_view(), max_tokens=64,
                effort=None, tools=tools, tool_choice="any",
            )
            payload = transport.calls[0][2]
            self.assertEqual(payload["tool_choice"], "required")
            self.assertEqual(payload["tools"][0]["function"]["name"], "dev_query")
            self.assertEqual(payload["tools"][0]["function"]["parameters"], tools[0]["input_schema"])
            self.assertEqual(payload["messages"], [{"role": "user", "content": "VIEW"}])
            self.assertEqual(
                turn.content,
                ({"type": "tool_use", "id": "call_01", "name": "dev_query", "input": {"text": "select 1"}},),
            )
            self.assertIsNone(turn.protocol_fault())
            self.assertEqual(turn.tool_call()["input"], {"text": "select 1"})
            self.assertEqual(turn.usage.input_tokens, 800)
            # Feed a result back: the canonical list renders as tool messages.
            messages = P._with_tool_results(
                _view(), turn.content, [P.ToolResultBlock("call_01", "[dev_query] ok ok=true")],
            )
            second = backend.step(
                role_name=IMPLEMENTER, model="m", messages=messages, max_tokens=64,
                effort=None, tools=tools, tool_choice="submit_sql_by_mart",
            )
            sent = transport.calls[1][2]["messages"]
            self.assertEqual(sent[0], {"role": "user", "content": "VIEW"})
            self.assertEqual(sent[1]["role"], "assistant")
            self.assertEqual(sent[1]["tool_calls"][0]["id"], "call_01")
            self.assertEqual(
                json.loads(sent[1]["tool_calls"][0]["function"]["arguments"]), {"text": "select 1"}
            )
            self.assertEqual(
                sent[2], {"role": "tool", "tool_call_id": "call_01", "content": "[dev_query] ok ok=true"}
            )
            self.assertEqual(
                transport.calls[1][2]["tool_choice"],
                {"type": "function", "function": {"name": "submit_sql_by_mart"}},
            )
            self.assertTrue(second.truncated)
            self.assertEqual(second.stop_reason, "length")
            # Undecodable arguments are an `invalid_arguments` protocol fault.
            self.assertEqual(second.protocol_fault().code, "invalid_arguments")

    def test_with_tool_results_answers_every_id_on_both_wires(self):
        """`_with_tool_results` is the generic form of `_with_correction`:
        one assistant turn, one user turn opening with a `tool_result` per
        id (`is_error` only when set, text after), a bare text otherwise;
        the chat rendering answers every id with a `role: "tool"` message."""
        assistant = [
            {"type": "text", "text": "thinking"},
            {"type": "tool_use", "id": "a", "name": "t1", "input": {"x": 1}},
            {"type": "tool_use", "id": "b", "name": "t2", "input": {"y": 2}},
        ]
        out = P._with_tool_results(
            _view(), assistant,
            [P.ToolResultBlock("a", "[t1] ok ok=true"), ("b", "refused", True)],
            text="continue",
        )
        self.assertEqual(out[0], {"role": "user", "content": "VIEW"})
        self.assertEqual(out[1], {"role": "assistant", "content": assistant})
        self.assertEqual(
            out[2],
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "[t1] ok ok=true"},
                    {"type": "tool_result", "tool_use_id": "b", "is_error": True, "content": "refused"},
                    {"type": "text", "text": "continue"},
                ],
            },
        )
        self.assertEqual(len(_view()), 1, "the input list is never mutated")
        bare = P._with_tool_results(_view(), None, [], text="just text")
        self.assertEqual(bare, [{"role": "user", "content": "VIEW"}, {"role": "user", "content": "just text"}])
        chat = P._chat_messages(out, "SYSTEM")
        self.assertEqual(chat[0], {"role": "system", "content": "SYSTEM"})
        self.assertEqual(chat[1], {"role": "user", "content": "VIEW"})
        self.assertEqual(chat[2]["role"], "assistant")
        self.assertEqual(chat[2]["content"], "thinking")
        self.assertEqual([c["id"] for c in chat[2]["tool_calls"]], ["a", "b"])
        self.assertEqual(chat[3], {"role": "tool", "tool_call_id": "a", "content": "[t1] ok ok=true"})
        self.assertEqual(chat[4], {"role": "tool", "tool_call_id": "b", "content": "refused"})
        self.assertEqual(chat[5], {"role": "user", "content": "continue"})
        # The one-shot correction is the special case, byte for byte.
        payload = {"messages": _view()}
        rejected = {"content": [{"type": "tool_use", "id": "toolu_01", "name": "report_findings", "input": {}}]}
        correction = P.CORRECTION_TEXT.format(problem="bad")
        self.assertEqual(
            P._with_correction(payload, rejected, "bad")["messages"],
            [
                {"role": "user", "content": "VIEW"},
                {"role": "assistant", "content": rejected["content"]},
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_01", "is_error": True, "content": correction},
                        {"type": "text", "text": correction},
                    ],
                },
            ],
        )


# ---------------------------------------------------------------------------
# The zero-tool parity: turn 0 of a zero-tool session IS the one-shot exchange
# ---------------------------------------------------------------------------

class ZeroToolParityTest(_SessionCase):
    def test_transcript_key_for_parity_zero_tool_single_turn(self):
        """`transcript_key_v3` of ONE user turn under a role's declared policy
        with an empty tool list equals today's `transcript_key`, for a schema
        role, a prose role and a chat-routed role, module and provider alike."""
        # The author, witness and proposer now ship enabled.  Zero-tool
        # parity remains their explicit rollback contract, exercised under
        # an explicitly disabled profile rather than stale default state.
        doc = _doc(
            semantic_author={"enabled": False},
            independent_implementer={"enabled": False},
            repair_proposer={"enabled": False},
        )
        with _agents(doc):
            provider = _provider(tempfile.mkdtemp(), FakeTransport([]))
            for role in ("ambiguity_critic", AUTHOR, IMPLEMENTER, "audit_triage", "repair_proposer"):
                with self.subTest(role=role):
                    policy = P.session_policy_for(role)
                    self.assertEqual(policy.tools, ())
                    self.assertFalse(P.role_is_agentic(role))
                    key = P.transcript_key_v3(role, policy, _view("VIEW"))
                    self.assertEqual(key, P.transcript_key(role, "VIEW"))
                    if role in provider.routing.roles:
                        self.assertEqual(key, provider.transcript_key_for(role, "VIEW"))
                    self.assertEqual(
                        P.session_tool_choice(policy),
                        P.tool_choice_for(role, schema_mode=P.uses_findings_schema(role)),
                    )

    def test_harness_validated_critic_policy_keeps_the_forced_tool_choice(self):
        """SoT T1.1: an ENABLED critic seat's declared policy carries its
        harness-only validators (the runner runs them) and nothing on the
        wire beyond the forced `report_findings`, so the default tool choice
        stays the forced tool — never `any` — and the wire `tools[]` is the
        one-shot list byte for byte; disabled, the policy has no tool."""
        from elt_taskgen.review.tools import critic_validators as CV

        for role, validator in (("population_adversary", "compile_proposal"), ("shortcut_attacker", "compile_probe")):
            with self.subTest(role=role):
                with _agents(_doc(**{role: {"enabled": False}})):
                    disabled = P.session_policy_for(role)
                    self.assertEqual(disabled.tools, ())
                    wire = P.wire_tools_for(role)
                with _agents(_doc(**{role: {"enabled": True}})):
                    policy = P.session_policy_for(role)
                    self.assertEqual([t.name for t in policy.tools], [validator])
                    self.assertTrue(all(t.harness_only for t in policy.tools))
                    self.assertEqual(P.session_tool_choice(policy), P.tool_choice_for(role))
                    self.assertEqual(P.session_tool_choice(policy), {"type": "tool", "name": P.FINDINGS_TOOL_NAME})
                    self.assertEqual([dict(t) for t in policy.wire_tools], wire)
                    self.assertEqual(P.wire_tools_for(role), wire)
                    self.assertEqual(policy.mode, "harness_validated")
                    self.assertEqual(policy.limits.active_harness_validators, (validator,))
                    self.assertEqual([t.name for t in CV.critic_policy(role).tools], [validator])
        # A model-facing tool is what makes the choice `any` — `auto` for the
        # repair proposer, whose turns may think first (2026-09-11: under a
        # forced choice it read fields and aborted on every blocked task), and
        # `any` again on a turn whose wire carries only submit and abort.
        with _AgenticRoles(repair_proposer=("read_view",)):
            policy = P.session_policy_for("repair_proposer")
            self.assertEqual(P.session_tool_choice(policy), "auto")
            self.assertEqual(P.session_tool_choice(policy, terminal_only=True), "any")
        # The shipped proposer policy: its last permitted turn carries only
        # submit and abort, and that wire is what forces `any` back.
        shipped = P.session_policy_for("repair_proposer")
        last = shipped.wire_tools_for_turn(shipped.limits.max_turns - 1)
        self.assertTrue(P._wire_is_terminal_only(shipped, last))
        self.assertFalse(P._wire_is_terminal_only(shipped, shipped.wire_tools))
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}):
            self.assertEqual(P.session_tool_choice(P.session_policy_for(AUTHOR)), "any")

    def test_turn_zero_of_a_zero_tool_session_sends_and_records_as_complete(self):
        """Turn 0 of a zero-tool session builds the SAME request bytes as the
        one-shot path on both wires, records under the SAME key, and each path
        serves the other's entry with zero HTTP and the same text."""
        for role, schema_mode in (("ambiguity_critic", True), (AUTHOR, False)):
            with self.subTest(role=role):
                doc = _doc(**({role: {"enabled": False}} if role == AUTHOR else {}))
                with _agents(doc):
                    one_shot = P.AnthropicBackend._payload(
                        model="claude-opus-5", prompt="VIEW", max_tokens=64, effort="high",
                        role_name=role, schema_mode=schema_mode,
                    )
                    policy = P.session_policy_for(role)
                    session = P.AnthropicBackend._session_payload(
                        model="claude-opus-5", messages=_view(), max_tokens=64, effort="high",
                        role_name=role, tools=[dict(t) for t in policy.wire_tools],
                        tool_choice=P.session_tool_choice(policy),
                    )
                    self.assertEqual(json.dumps(session), json.dumps(one_shot))
                    cached = P.AnthropicBackend._payload(
                        model="claude-opus-5", prompt="VIEW", max_tokens=64, effort="high",
                        role_name=role, schema_mode=schema_mode, prompt_cache=True,
                    )
                    cached_session = P.AnthropicBackend._session_payload(
                        model="claude-opus-5", messages=_view(), max_tokens=64, effort="high",
                        role_name=role, tools=[dict(t) for t in policy.wire_tools],
                        tool_choice=P.session_tool_choice(policy), prompt_cache=True,
                    )
                    self.assertEqual(json.dumps(cached_session), json.dumps(cached))
        # complete() then _turn on the same store: served, zero HTTP.
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport([anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5")])
            provider = _provider(tmp, transport)
            text = provider.complete(CouncilRole.AMBIGUITY_CRITIC, "VIEW")
            policy = P.session_policy_for("ambiguity_critic")
            turn = provider._turn("ambiguity_critic", _view(), policy, 0)
            self.assertEqual(len(transport.calls), 1)
            self.assertTrue(turn.replayed)
            self.assertEqual(turn.key, P.transcript_key("ambiguity_critic", "VIEW"))
            self.assertEqual(turn.text, text)
            self.assertEqual(turn.tool_call()["name"], P.FINDINGS_TOOL_NAME)
            self.assertEqual(turn.tool_call()["input"], {"findings": [VALID_FINDING]})
        # _turn then complete() on a fresh store: the same text, zero HTTP.
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport([anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5")])
            provider = _provider(tmp, transport)
            policy = P.session_policy_for("ambiguity_critic")
            turn = provider._turn("ambiguity_critic", _view(), policy, 0)
            self.assertFalse(turn.replayed)
            self.assertEqual(transport.calls[0][2]["tool_choice"], {"type": "tool", "name": P.FINDINGS_TOOL_NAME})
            served = provider.complete(CouncilRole.AMBIGUITY_CRITIC, "VIEW")
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(served, turn.text)
            self.assertEqual(served, P.normalized_text_for("ambiguity_critic", {"findings": [VALID_FINDING]}))
        # The chat wire, prose role: the request bytes agree too.
        with _agents(_doc(independent_implementer={"enabled": False})), tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport([openai_text_response("built"), openai_text_response("built")])
            routing = make_routing(oss_base="http://oss:8000/v1", oss_key="k", oss_model="m")
            provider = _provider(tmp, transport, routing=routing)
            provider.complete(IMPLEMENTER, "VIEW")
            provider.refresh = True
            turn = provider._turn(IMPLEMENTER, _view(), P.session_policy_for(IMPLEMENTER), 0)
            self.assertEqual(json.dumps(transport.calls[0][2]), json.dumps(transport.calls[1][2]))
            self.assertEqual(turn.text, "built")
            self.assertEqual(turn.text_content, "built")


# ---------------------------------------------------------------------------
# RoutedProvider._turn: memo, replay, reserve, record, charge
# ---------------------------------------------------------------------------

class TurnMemoTest(_SessionCase):
    def test_turn_memo_key_covers_tools_policy_and_message_prefix(self):
        """A recorded turn is served (zero HTTP) only for the exact role,
        tool manifest, policy and message prefix that produced it; one more
        message, another policy or another wire tool is a new key and a new
        call. The key is `transcript_key_v3`, and the entry is filed under it."""
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}), tempfile.TemporaryDirectory() as tmp:
            policy = P.session_policy_for(AUTHOR)
            transport = FakeTransport([
                tool_use_body("replace_prose", {"text": "draft 1"}, id="toolu_a"),
                tool_use_body("submit_prose", {"text": "draft 1"}, id="toolu_b"),
                tool_use_body("replace_prose", {"text": "other policy"}, id="toolu_c"),
                tool_use_body("replace_prose", {"text": "other tools"}, id="toolu_d"),
            ])
            provider = _provider(tmp, transport)
            with provider.session(AUTHOR, policy) as account:
                turn0 = provider._turn(AUTHOR, _view(), policy, 0)
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(turn0.key, P.transcript_key_v3(AUTHOR, policy, _view()))
                self.assertEqual(turn0.key, P.transcript_key(AUTHOR, "VIEW"))
                self.assertTrue(_entry_path(tmp, AUTHOR, turn0.key).is_file())
                self.assertEqual(turn0.tool_call()["name"], "replace_prose")
                # Same prefix again: served from the store, byte-equal content.
                again = provider._turn(AUTHOR, _view(), policy, 0)
                self.assertEqual(len(transport.calls), 1)
                self.assertTrue(again.replayed)
                self.assertEqual(again.content, turn0.content)
                self.assertEqual(again.key, turn0.key)
                # One more message (the tool result): a new key, a new call.
                prefix1 = P._with_tool_results(
                    _view(), turn0.content, [P.ToolResultBlock("toolu_a", "[prose] ok ok=true")]
                )
                turn1 = provider._turn(AUTHOR, prefix1, policy, 1)
                self.assertEqual(len(transport.calls), 2)
                self.assertNotEqual(turn1.key, turn0.key)
                # Turn 1 is the final permitted turn in the shipped author
                # policy, so its wire is narrowed to submit/abort and the
                # per-turn helper (not the full-wire v3 shortcut) is the key.
                self.assertEqual(turn1.key, P.session_turn_key(AUTHOR, policy, prefix1, 1))
                self.assertEqual(transport.calls[1][2]["messages"], prefix1)
                self.assertEqual(account.turn_keys, [turn0.key, turn0.key, turn1.key])
                self.assertEqual(account.model_call_count, 3)
                self.assertEqual(account.live_model_call_count, 2)
            # Another policy (the nudge text) over the same prefix: a new key.
            other_policy = P.SessionPolicy(
                role=AUTHOR, tools=policy.tools, submit_tool=policy.submit_tool,
                limits=policy.limits, mode=policy.mode, wire_tools=policy.wire_tools,
                nudge_text="edited nudge",
            )
            self.assertNotEqual(other_policy.sha256(), policy.sha256())
            turn_other = provider._turn(AUTHOR, _view(), other_policy, 0)
            self.assertEqual(len(transport.calls), 3)
            self.assertNotEqual(turn_other.key, turn0.key)
            self.assertEqual(turn_other.key, P.transcript_key_v3(AUTHOR, other_policy, _view()))
            # It is served back under ITS policy, and never under the other.
            self.assertTrue(provider._turn(AUTHOR, _view(), other_policy, 0).replayed)
            self.assertEqual(len(transport.calls), 3)
            # Another wire manifest: a new key.
            extra = P.SessionPolicy(
                role=AUTHOR, tools=policy.tools, submit_tool=policy.submit_tool,
                limits=policy.limits, mode=policy.mode,
                wire_tools=policy.wire_tools + (
                    {"name": "dev_query", "description": "", "strict": True,
                     "input_schema": {"type": "object", "properties": {}}},
                ),
            )
            self.assertNotEqual(extra.tools_sha256(), policy.tools_sha256())
            turn_extra = provider._turn(AUTHOR, _view(), extra, 0)
            self.assertEqual(len(transport.calls), 4)
            self.assertNotEqual(turn_extra.key, turn0.key)
            self.assertEqual([t["name"] for t in transport.calls[3][2]["tools"]][-1], "dev_query")
            # The salt is not policy: same key, served.
            salted = P.SessionPolicy(
                role=AUTHOR, tools=policy.tools, submit_tool=policy.submit_tool,
                limits=policy.limits, mode=policy.mode, wire_tools=policy.wire_tools,
                session_salt=7,
            )
            self.assertTrue(provider._turn(AUTHOR, _view(), salted, 0).replayed)
            self.assertEqual(len(transport.calls), 4)

    def test_session_turn_entry_is_schema_3_with_turn_block(self):
        """A session turn's entry is the one-shot entry plus a `turn` block:
        the assistant content with every `tool_use` id, the user messages
        since the previous turn, stop reason, tool choice; the route block
        states `entry_schema` 3; readers keep serving it and never rewrite it."""
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}), tempfile.TemporaryDirectory() as tmp:
            policy = P.session_policy_for(AUTHOR)
            response = tool_use_body("replace_prose", {"text": "d"}, id="toolu_a", text="Let me revise.")
            response["usage"]["cache_read_input_tokens"] = 600
            provider = _provider(tmp, FakeTransport([response]))
            provider.begin_task_evidence("task-1", "a" * 64)
            with provider.session(AUTHOR, policy):
                turn = provider._turn(AUTHOR, _view(), policy, 0)
            path = _entry_path(tmp, AUTHOR, turn.key)
            entry = json.loads(path.read_text(encoding="utf-8"))
            one_shot_fields = {
                "role", "prompt_sha256", "system_sha256", "provider", "model", "served_model",
                "response", "response_sha256", "task_id", "task_content_hash", "attempt_count",
                "correction_count", "finding_count", "usage", "elapsed_ms", "raw_attempts", "route",
            }
            self.assertEqual(set(entry), one_shot_fields | {"turn"} | set(provider._record_stamp()))
            self.assertEqual(P.transcript_entry_schema(entry), P.SESSION_TRANSCRIPT_ENTRY_SCHEMA)
            self.assertEqual(entry["route"]["entry_schema"], 3)
            self.assertEqual(entry["route"]["policy_sha256"], policy.sha256())
            self.assertEqual(entry["route"]["tools_sha256"], policy.tools_sha256())
            self.assertEqual(entry["usage"]["cache_read_input_tokens"], 600)
            self.assertEqual(entry["served_model"], "claude-opus-5")
            self.assertEqual((entry["task_id"], entry["task_content_hash"]), ("task-1", "a" * 64))
            self.assertIsNone(entry["finding_count"])
            self.assertEqual(entry["response"], canonical_json(response["content"]))
            self.assertEqual(entry["response_sha256"], sha256_hex(entry["response"]))
            block = entry["turn"]
            self.assertEqual(
                set(block),
                {"turn_index", "memo_key", "messages_sha256", "user_messages", "content",
                 "content_sha256", "tool_use_ids", "tool_uses", "stop_reason", "truncated",
                 "tool_choice", "tools_sha256", "observations_sha256", "session_salt"},
            )
            self.assertEqual(block["turn_index"], 0)
            self.assertEqual(block["memo_key"], turn.key)
            self.assertEqual(block["content"], response["content"])
            self.assertEqual(block["content_sha256"], sha256_hex(canonical_json(response["content"])))
            self.assertEqual(block["tool_use_ids"], ["toolu_a"])
            self.assertEqual(block["tool_uses"], [{"id": "toolu_a", "name": "replace_prose"}])
            # Turn 0's VIEW is never persisted: the digest of the message
            # stands in for it (a one-shot entry records `prompt_sha256` only).
            self.assertEqual(
                block["user_messages"],
                [{"role": "user", "content_sha256": sha256_hex(canonical_json("VIEW")), "content_omitted": "initial_view"}],
            )
            self.assertEqual(block["messages_sha256"], sha256_hex(canonical_json(_view())))
            self.assertEqual(block["stop_reason"], "tool_use")
            self.assertFalse(block["truncated"])
            self.assertEqual(block["tool_choice"], {"type": "any"})
            self.assertEqual(block["tools_sha256"], policy.tools_sha256())
            self.assertEqual(block["observations_sha256"], [], "turn 0 is fed by no observation")
            route = provider.routing.for_role(AUTHOR)
            self.assertIsNone(P.transcript_route_mismatch(entry, route, policy=policy))
            before = path.read_bytes()
            served = provider._turn(AUTHOR, _view(), policy, 0)
            self.assertTrue(served.replayed)
            self.assertEqual(served.content, turn.content)
            self.assertEqual(served.text_content, "Let me revise.")
            self.assertEqual(path.read_bytes(), before, "a stored entry is never rewritten")


class ReplayAndResumeTest(_SessionCase):
    def _record_two_turns(self, tmp, transport):
        policy = P.session_policy_for(AUTHOR)
        provider = _provider(tmp, transport)
        with provider.session(AUTHOR, policy):
            turn0 = provider._turn(AUTHOR, _view(), policy, 0)
            prefix1 = P._with_tool_results(
                _view(), turn0.content, [P.ToolResultBlock("toolu_a", "[prose] ok ok=true")]
            )
            turn1 = provider._turn(AUTHOR, prefix1, policy, 1)
        return policy, turn0, prefix1, turn1

    def test_replay_only_serves_exact_prefix_then_raises_transcript_missing(self):
        """Under `--replay-only` the recorded turns are served for their exact
        prefixes with zero HTTP; the first re-keyed turn (a diverged tool
        result, or a turn never recorded) raises `TranscriptMissingError`
        — which is also the SoT T6 `ProviderFault` — with nothing spent and
        nothing recorded (replay is diagnostic only, C3)."""
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}), tempfile.TemporaryDirectory() as tmp:
            live = FakeTransport([
                tool_use_body("replace_prose", {"text": "draft"}, id="toolu_a"),
                tool_use_body("submit_prose", {"text": "draft"}, id="toolu_b"),
            ])
            policy, turn0, prefix1, turn1 = self._record_two_turns(tmp, live)
            replay_transport = FakeTransport([])
            replay = _provider(tmp, replay_transport, replay_only=True)
            with replay.session(AUTHOR, policy) as account:
                served0 = replay._turn(AUTHOR, _view(), policy, 0)
                served1 = replay._turn(AUTHOR, prefix1, policy, 1)
                self.assertTrue(served0.replayed and served1.replayed)
                self.assertEqual((served0.content, served1.content), (turn0.content, turn1.content))
                self.assertEqual(served1.tool_call()["name"], "submit_prose")
                self.assertEqual(replay_transport.calls, [])
                self.assertEqual(account.live_model_call_count, 0)
                self.assertTrue(account.replayed)
                # A diverged prefix at turn 1: the first re-keyed turn raises.
                diverged = P._with_tool_results(
                    _view(), turn0.content, [P.ToolResultBlock("toolu_a", "[prose] ok ok=false")]
                )
                with self.assertRaises(P.TranscriptMissingError) as ctx:
                    replay._turn(AUTHOR, diverged, policy, 1)
                self.assertIsInstance(ctx.exception, S.ProviderFault)
                self.assertIsInstance(ctx.exception, P.SessionTranscriptMissingError)
                self.assertEqual(ctx.exception.terminal, "PROVIDER_FAULT")
                self.assertEqual(ctx.exception.code, "replay_miss")
                self.assertIn("replay-only", str(ctx.exception))
                # A turn that was never recorded raises the same way.
                prefix2 = P._with_tool_results(
                    prefix1, turn1.content, [P.ToolResultBlock("toolu_b", "[prose] ok ok=true")]
                )
                with self.assertRaises(P.TranscriptMissingError):
                    replay._turn(AUTHOR, prefix2, policy, 2)
            self.assertEqual(replay_transport.calls, [])
            self.assertEqual(replay.meter.total_usd, 0.0)
            self.assertEqual(
                sorted(p.name for p in (Path(tmp) / "transcripts" / AUTHOR).iterdir()),
                sorted(f"{k}.json" for k in (turn0.key, turn1.key)),
            )
            # The class is in the engine's infrastructure set under BOTH names.
            from elt_taskgen import engine as engine_mod

            self.assertIn("TranscriptMissingError", engine_mod._INFRA_EXCEPTION_NAMES)
            self.assertIn("ProviderFault", engine_mod._INFRA_EXCEPTION_NAMES)
        # Live execution must replay to the same SessionResult at zero cost.
        # Changed tool-result bytes re-key the affected turn and stop there.
        with tempfile.TemporaryDirectory() as tmp:
            check = _RunningTool("check_draft")
            with _AgenticRoles(**{AUTHOR: (check, "submit_prose", "abort")}):
                policy = _session_policy(AUTHOR)
                live = FakeTransport([
                    tool_use_body("check_draft", {"text": "one"}, id="toolu_a"),
                    tool_use_body("check_draft", {"text": "two"}, id="toolu_b"),
                    tool_use_body("submit_prose", {"text": "final"}, id="toolu_c"),
                ])
                provider = _bound(_provider(tmp, live))
                ctx = _session_ctx(tmp, AUTHOR)
                recorded = provider.run_session(AUTHOR, "VIEW", policy, ctx)
                self.assertEqual(len(live.calls), 3)
                self.assertIs(recorded.terminal, S.TerminalState.SUBMITTED)
                self.assertEqual(recorded.live_model_call_count, 3)
                replay_transport = FakeTransport([])
                replay = _bound(_provider(tmp, replay_transport, replay_only=True))
                served = replay.run_session(AUTHOR, "VIEW", policy, ctx)
                self.assertEqual(replay_transport.calls, [], "zero transport pops")
                self.assertEqual(_evidence_of(served), _evidence_of(recorded))
                self.assertEqual(served.session_sha256, recorded.session_sha256)
                self.assertEqual(served.chain_hashes, recorded.chain_hashes)
                self.assertEqual(served.final, {"text": "final"})
                self.assertEqual((served.live_model_call_count, served.usd), (0, 0.0))
                self.assertTrue(all(t.replayed for t in served.turns if t.kind == "model"))
                self.assertTrue(served.verify_chain())
                self.assertEqual(check.calls, [{"text": "one"}, {"text": "two"}] * 2, "tools re-run on replay")
                self.assertEqual(replay.meter.total_usd, 0.0)
                row_live, row_served = provider.exchange_evidence[0], replay.exchange_evidence[0]
                self.assertEqual(row_served["trajectory_sha256"], row_live["trajectory_sha256"])
                self.assertEqual(row_served["response_sha256"], row_live["response_sha256"])
                self.assertTrue(row_served["replayed"] and not row_live["replayed"])
                self.assertEqual((row_served["live_model_call_count"], row_served["usd"]), (0, 0.0))
            mutated = _RunningTool("check_draft", code="failed", ok=False)
            with _AgenticRoles(**{AUTHOR: (mutated, "submit_prose", "abort")}):
                policy_m = _session_policy(AUTHOR)
                # The manifest is unchanged (same name, schema, cost): the same
                # keys — only the OBSERVATION differs.
                self.assertEqual((policy_m.sha256(), policy_m.tools_sha256()), (policy.sha256(), policy.tools_sha256()))
                replay2_transport = FakeTransport([])
                replay2 = _bound(_provider(tmp, replay2_transport, replay_only=True))
                with self.assertRaises(P.TranscriptMissingError) as ctx_exc:
                    replay2.run_session(AUTHOR, "VIEW", policy_m, ctx)
                exc = ctx_exc.exception
            # Catch replay divergence one turn earlier at the tool observation
            # digest as replay_mismatch/TranscriptMissingError.
                self.assertIsInstance(exc, P.SessionReplayMismatchError)
                self.assertIsInstance(exc, S.ToolHarnessFault)
                self.assertEqual((exc.code, exc.tool), (P.REPLAY_MISMATCH_CODE, "check_draft"))
                partial = exc.session_result
                self.assertIs(partial.terminal, S.TerminalState.HARNESS_FAULT)
                self.assertEqual(partial.fault.exception_type, "SessionReplayMismatchError")
                self.assertEqual((partial.fault.code, partial.fault.tool), ("replay_mismatch", "check_draft"))
                self.assertEqual(partial.fault.turn_index, 0, "the observation of model turn 0 diverged")
                self.assertEqual(partial.stale_tool_result_count, 1)
                self.assertEqual((partial.model_call_count, partial.tool_call_count), (1, 1))
                self.assertTrue(partial.turns[0].replayed)
                self.assertEqual(partial.turns[1].outcome_code, "failed")
                self.assertNotEqual(partial.turns[1].output_sha256, recorded.turns[1].output_sha256)
                self.assertEqual(mutated.calls, [{"text": "one"}])
                self.assertEqual(replay2_transport.calls, [], "nothing is invented under replay-only")
                self.assertEqual(replay2.exchange_evidence, [], "a faulted trajectory is never evidence")
                self.assertEqual(replay2.meter.total_usd, 0.0)
                self.assertIsNone(replay2.active_session(AUTHOR))
                # The recording's turn 1 folds the FULL observation digest into
                # its key and records it: without the session record the
                # diverged observation would still re-key turn 1 (a miss).
                model_keys = [t.memo_key for t in recorded.turns if t.kind == "model"]
                entry1 = json.loads(_entry_path(tmp, AUTHOR, model_keys[1]).read_text(encoding="utf-8"))
                self.assertEqual(entry1["turn"]["observations_sha256"], [recorded.turns[1].output_sha256])
                record = provider.store.lookup_session(AUTHOR, provider.exchange_evidence[0]["prompt_sha256"])
                self.assertEqual(record["session_sha256"], recorded.session_sha256)
                self.assertEqual(record["turn_keys"], model_keys)
                self.assertEqual(
                    record["observations_sha256"],
                    [t.output_sha256 for t in recorded.turns if t.kind == "tool"],
                )

    def test_transport_fault_at_turn_k_resumes_from_recorded_prefix(self):
        """A transport fault on turn k is a `ProviderFault` (its cause kept);
        turns 0..k-1 are on disk. A resumed session over the same prefixes
        replays them with zero HTTP and only turn k goes to the transport."""
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}), tempfile.TemporaryDirectory() as tmp:
            fault = RuntimeError("provider HTTP 529 from https://api: overloaded")
            faulting = _FaultingTransport(
                [
                    tool_use_body("replace_prose", {"text": "draft"}, id="toolu_a"),
                    tool_use_body("replace_prose", {"text": "draft 2"}, id="toolu_b"),
                ],
                fault,
            )
            policy = P.session_policy_for(AUTHOR)
            provider = _provider(tmp, faulting)
            with provider.session(AUTHOR, policy) as account:
                turn0 = provider._turn(AUTHOR, _view(), policy, 0)
                prefix1 = P._with_tool_results(
                    _view(), turn0.content, [P.ToolResultBlock("toolu_a", "[prose] ok ok=false")]
                )
                turn1 = provider._turn(AUTHOR, prefix1, policy, 1)
                prefix2 = P._with_tool_results(
                    prefix1, turn1.content, [P.ToolResultBlock("toolu_b", "[prose] ok ok=false")]
                )
                with self.assertRaises(S.ProviderFault) as ctx:
                    provider._turn(AUTHOR, prefix2, policy, 2)
                self.assertIs(ctx.exception.__cause__, fault)
                self.assertEqual(ctx.exception.code, "transport")
                self.assertEqual(ctx.exception.terminal, "PROVIDER_FAULT")
                self.assertNotIsInstance(ctx.exception, S.PolicyFault)
                self.assertEqual(len(faulting.calls), 3)
                self.assertEqual(account.model_call_count, 2)
            # Turn 2 is the last permitted turn under the declared limits:
            # its narrowed wire enters its key (`session_turn_key`).
            key2 = P.session_turn_key(AUTHOR, policy, prefix2, 2)
            self.assertTrue(_entry_path(tmp, AUTHOR, turn0.key).is_file())
            self.assertTrue(_entry_path(tmp, AUTHOR, turn1.key).is_file())
            self.assertFalse(_entry_path(tmp, AUTHOR, key2).exists())
            spent_before = provider.meter.total_usd
            # Resume: a fresh provider on the same store.
            resumed_transport = FakeTransport([tool_use_body("submit_prose", {"text": "final"}, id="toolu_c")])
            resumed = _provider(tmp, resumed_transport)
            with resumed.session(AUTHOR, policy) as account:
                r0 = resumed._turn(AUTHOR, _view(), policy, 0)
                r1 = resumed._turn(AUTHOR, prefix1, policy, 1)
                self.assertTrue(r0.replayed and r1.replayed)
                self.assertEqual((r0.content, r1.content), (turn0.content, turn1.content))
                self.assertEqual(resumed_transport.calls, [])
                r2 = resumed._turn(AUTHOR, prefix2, policy, 2)
                self.assertFalse(r2.replayed)
                self.assertEqual(len(resumed_transport.calls), 1)
                self.assertEqual(resumed_transport.calls[0][2]["messages"], prefix2)
                self.assertEqual(r2.tool_call()["name"], "submit_prose")
                self.assertEqual(account.model_call_count, 3)
                self.assertEqual(account.live_model_call_count, 1)
            self.assertTrue(_entry_path(tmp, AUTHOR, key2).is_file())
            self.assertGreater(resumed.meter.total_usd, 0.0)
            self.assertEqual(provider.meter.total_usd, spent_before)
        # END TO END through the real runner: the fault on turn k halts the
        # session as PROVIDER_FAULT (the partial result on the exception, no
        # evidence row, the ledger closed, turns 0..k-1 on disk); a resumed
        # `run_session` on the same store serves those turns with zero HTTP,
        # sends only turn k, and its chain CONTINUES the recorded prefix.
        with tempfile.TemporaryDirectory() as tmp:
            check = _RunningTool("check_draft")
            with _AgenticRoles(**{AUTHOR: (check, "submit_prose", "abort")}):
                policy = _session_policy(AUTHOR)
                fault = RuntimeError("provider HTTP 529 from https://api: overloaded")
                faulting = _FaultingTransport(
                    [
                        tool_use_body("check_draft", {"text": "one"}, id="toolu_a"),
                        tool_use_body("check_draft", {"text": "two"}, id="toolu_b"),
                    ],
                    fault,
                )
                provider = _bound(_provider(tmp, faulting))
                ctx = _session_ctx(tmp, AUTHOR)
                with self.assertRaises(S.ProviderFault) as ctx_exc:
                    provider.run_session(AUTHOR, "VIEW", policy, ctx)
                exc = ctx_exc.exception
                self.assertIs(exc.__cause__, fault)
                self.assertEqual(exc.code, "transport")
                partial = exc.session_result
                self.assertIs(partial.terminal, S.TerminalState.PROVIDER_FAULT)
                self.assertEqual(partial.fault.turn_index, 2)
                self.assertEqual((partial.model_call_count, partial.tool_call_count), (2, 2))
                # The runner's retry policy (04 §6) re-issued turn k ONCE on
                # the same recorded prefix before halting: two transport
                # attempts for turn k, no extra model turn recorded.
                self.assertEqual(len(faulting.calls), 4)
                self.assertEqual(partial.resume_count, 1)
                self.assertEqual(partial.resume_events[0]["boundary"], "provider")
                self.assertEqual(provider.exchange_evidence, [], "a halt appends no row")
                self.assertIsNone(provider.active_session(AUTHOR))
                keys = [t.memo_key for t in partial.turns if t.kind == "model"]
                self.assertTrue(all(_entry_path(tmp, AUTHOR, k).is_file() for k in keys))
                self.assertEqual(len(list((Path(tmp) / "transcripts" / AUTHOR).glob("*.json"))), 2)
                self.assertIsNone(provider.store.lookup_session(AUTHOR, keys[0]), "a halt files no session record")
                resumed_transport = FakeTransport([tool_use_body("submit_prose", {"text": "final"}, id="toolu_c")])
                resumed = _bound(_provider(tmp, resumed_transport))
                result = resumed.run_session(AUTHOR, "VIEW", policy, ctx)
                self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
                self.assertEqual(len(resumed_transport.calls), 1, "only turn k goes to the transport")
                self.assertEqual((result.model_call_count, result.live_model_call_count), (3, 1))
                self.assertEqual([t.replayed for t in result.turns if t.kind == "model"], [True, True, False])
                self.assertEqual(result.chain_hashes[: len(partial.chain_hashes)], partial.chain_hashes,
                                 "the resumed trajectory continues the recorded prefix's chain")
                self.assertTrue(result.verify_chain())
                self.assertEqual(check.calls, [{"text": "one"}, {"text": "two"}] * 2)
                row = resumed.exchange_evidence[0]
                self.assertEqual((row["model_call_count"], row["live_model_call_count"]), (3, 1))
                self.assertFalse(row["replayed"])
                self.assertEqual(row["trajectory_sha256"], result.session_sha256)
                self.assertEqual(len(list((Path(tmp) / "transcripts" / AUTHOR).glob("*.json"))), 3)
                self.assertEqual(
                    resumed.store.lookup_session(AUTHOR, keys[0])["session_sha256"], result.session_sha256
                )


class BudgetTest(_SessionCase):
    def test_budget_reserve_refuses_before_spend_and_charge_after_record(self):
        """`reserve` runs BEFORE transport: a refusal spends nothing, records
        nothing (the task scope is the base `BudgetExceededError` exactly).
        `charge` runs AFTER the record: a session cap crossed by the paid
        turn raises `RoleCapExceeded` with the entry on disk, and the next
        turn of the same session is refused at reserve before any call."""
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}), tempfile.TemporaryDirectory() as tmp:
            declared = P.session_policy_for(AUTHOR)
            # (a) The task budget cannot absorb the prompt floor: refused.
            tiny = P.CostMeter(budget_per_task_usd=0.000001)
            transport = FakeTransport([tool_use_body("replace_prose", {"text": "d"})])
            provider = _provider(tmp, transport, meter=tiny, subdir="a")
            with provider.session(AUTHOR, declared):
                with self.assertRaises(P.BudgetExceededError) as ctx:
                    provider._turn(AUTHOR, _view("x" * 4000), declared, 0)
            self.assertIs(type(ctx.exception), P.BudgetExceededError)
            self.assertEqual(ctx.exception.scope, "task")
            self.assertNotIsInstance(ctx.exception, P.RoleCapExceeded)
            self.assertIn("refused before the call, nothing spent", str(ctx.exception))
            self.assertEqual(transport.calls, [])
            self.assertEqual(tiny.total_usd, 0.0)
            self.assertFalse((Path(tmp) / "a" / AUTHOR).exists())
            self.assertEqual(provider.exchange_evidence, [])
            # (b) The session's own cap: the turn is made (the floor is tiny),
            # RECORDED, then charge crosses the cap -> RoleCapExceeded.
            capped = P.SessionPolicy(
                role=AUTHOR, tools=declared.tools, submit_tool="submit_prose",
                limits=P.SessionLimits.from_block({"enabled": True, "max_usd": 0.005}),
                mode="model_driven", wire_tools=declared.wire_tools,
            )
            opus = P.rate_card_for("anthropic", "claude-opus-5", {})
            per_turn = opus.usd_for(P.Usage(input_tokens=1000, output_tokens=100))
            self.assertGreater(per_turn, 0.005)
            transport = FakeTransport([
                tool_use_body("replace_prose", {"text": "d"}, id="toolu_a"),
                tool_use_body("submit_prose", {"text": "d"}, id="toolu_b"),
            ])
            meter = P.CostMeter(budget_per_task_usd=100.0)
            provider = _provider(tmp, transport, meter=meter, subdir="b")
            with provider.session(AUTHOR, capped) as account:
                self.assertEqual(account.trajectory.max_usd, 0.005)
                with self.assertRaises(P.RoleCapExceeded) as ctx:
                    provider._turn(AUTHOR, _view(), capped, 0)
                self.assertEqual(ctx.exception.scope, "role")
                self.assertIsInstance(ctx.exception, P.BudgetExceededError)
                self.assertIn("IS recorded", str(ctx.exception))
                self.assertEqual(len(transport.calls), 1)
                key0 = P.transcript_key_v3(AUTHOR, capped, _view())
                self.assertTrue(_entry_path(tmp, AUTHOR, key0, subdir="b").is_file())
                self.assertAlmostEqual(meter.total_usd, per_turn)
                self.assertAlmostEqual(account.usd, per_turn)
                self.assertEqual(account.model_call_count, 1)
                # The runner catches the cap as LIMIT_USD and stops; a further
                # turn on the same session is refused BEFORE transport.
                entry = json.loads(_entry_path(tmp, AUTHOR, key0, subdir="b").read_text())
                prefix1 = P._with_tool_results(
                    _view(), entry["turn"]["content"], [P.ToolResultBlock("toolu_a", "[prose] ok ok=true")]
                )
                with self.assertRaises(P.RoleCapExceeded) as ctx:
                    provider._turn(AUTHOR, prefix1, capped, 1)
                self.assertIn("refused before the call, nothing spent", str(ctx.exception))
                self.assertEqual(len(transport.calls), 1)
                self.assertFalse(
                    _entry_path(tmp, AUTHOR, P.transcript_key_v3(AUTHOR, capped, prefix1), subdir="b").exists()
                )
                self.assertAlmostEqual(meter.total_usd, per_turn)
            # No evidence row was appended by `_turn` itself: the row is the
            # session's (one per session, appended by run_session).
            self.assertEqual(provider.exchange_evidence, [])

    def test_role_cap_exceeded_is_catchable_by_the_session_runner(self):
        """Carry-over item 3: the session's own cap surfaces from `_turn` as
        `RoleCapExceeded` (exported, scope role, a `BudgetExceededError`
        subclass whose name is not infrastructure) so the runner maps it to
        `LIMIT_USD`; the task and total scopes stay the base class, which the
        runner maps to `PROVIDER_FAULT`."""
        from elt_taskgen import engine as engine_mod

        self.assertIn("RoleCapExceeded", P.__all__)
        self.assertTrue(issubclass(P.RoleCapExceeded, P.BudgetExceededError))
        self.assertNotIn("RoleCapExceeded", engine_mod._INFRA_EXCEPTION_NAMES)
        self.assertIn("BudgetExceededError", engine_mod._INFRA_EXCEPTION_NAMES)
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}), tempfile.TemporaryDirectory() as tmp:
            declared = P.session_policy_for(AUTHOR)
            capped = P.SessionPolicy(
                role=AUTHOR, tools=declared.tools, submit_tool="submit_prose",
                limits=P.SessionLimits.from_block({"enabled": True, "max_usd": 0.001}),
                mode="model_driven", wire_tools=declared.wire_tools,
            )
            provider = _provider(tmp, FakeTransport([tool_use_body("replace_prose", {"text": "d"})]))
            caught = None
            with provider.session(AUTHOR, capped):
                try:
                    provider._turn(AUTHOR, _view(), capped, 0)
                except P.RoleCapExceeded as exc:
                    caught = exc
            self.assertIsNotNone(caught)
            self.assertEqual(caught.scope, "role")
            # The total-scope breach from the same path is the base class.
            total = P.CostMeter(budget_per_task_usd=100.0, budget_total_usd=0.000001)
            provider = _provider(tmp, FakeTransport([]), meter=total, subdir="t")
            with provider.session(AUTHOR, declared):
                with self.assertRaises(P.BudgetExceededError) as ctx:
                    provider._turn(AUTHOR, _view("x" * 4000), declared, 0)
            self.assertIs(type(ctx.exception), P.BudgetExceededError)
            self.assertEqual(ctx.exception.scope, "total")


class ParallelToolUseTest(_SessionCase):
    def test_session_parallel_tool_use_answers_every_id(self):
        """Two `tool_use` blocks in one turn: the turn is returned and recorded
        with BOTH ids, `tool_call()` refuses with the one fixed code
        `multiple_tool_use`, the correction answers EVERY id as `is_error`
        on both wires, and no tool is executed."""
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}), tempfile.TemporaryDirectory() as tmp:
            policy = P.session_policy_for(AUTHOR)
            transport = FakeTransport([
                parallel_tool_use_body([
                    ("toolu_a", "replace_prose", {"text": "one"}),
                    ("toolu_b", "submit_prose", {"text": "two"}),
                ]),
                tool_use_body("submit_prose", {"text": "two"}, id="toolu_c"),
            ])
            provider = _provider(tmp, transport)
            with provider.session(AUTHOR, policy):
                turn = provider._turn(AUTHOR, _view(), policy, 0)
                self.assertEqual(turn.tool_use_ids, ("toolu_a", "toolu_b"))
                fault = turn.protocol_fault()
                self.assertIsInstance(fault, S.ToolProtocolFault)
                self.assertEqual(fault.code, "multiple_tool_use")
                self.assertIn(fault.code, S.PROTOCOL_FAULT_CODES)
                self.assertFalse(fault.ends_session)
                with self.assertRaises(S.ToolProtocolFault) as ctx:
                    turn.tool_call()
                self.assertEqual(ctx.exception.code, "multiple_tool_use")
                entry = json.loads(_entry_path(tmp, AUTHOR, turn.key).read_text())
                self.assertEqual(entry["turn"]["tool_use_ids"], ["toolu_a", "toolu_b"])
                self.assertEqual(entry["response"], canonical_json(list(turn.content)))
                # The correction: one fixed code, every id answered, no execution.
                results = P.protocol_correction_results(turn, fault)
                self.assertEqual([r.tool_use_id for r in results], ["toolu_a", "toolu_b"])
                self.assertTrue(all(r.is_error for r in results))
                self.assertEqual(len({r.content for r in results}), 1)
                self.assertIn("multiple_tool_use", results[0].content)
                self.assertEqual(results[0].content, S.REFUSAL_TEXT.format(code="multiple_tool_use"))
                prefix1 = P._with_tool_results(_view(), turn.content, results)
                answered = [b for b in prefix1[-1]["content"] if b["type"] == "tool_result"]
                self.assertEqual([b["tool_use_id"] for b in answered], ["toolu_a", "toolu_b"])
                self.assertTrue(all(b["is_error"] for b in answered))
                chat = P._chat_messages(prefix1, None)
                self.assertEqual([m["tool_call_id"] for m in chat if m["role"] == "tool"], ["toolu_a", "toolu_b"])
                # The next turn carries the correction; the refused calls never ran
                # (the stub tools raise on run, and nothing raised).
                turn1 = provider._turn(AUTHOR, prefix1, policy, 1)
                sent = transport.calls[1][2]["messages"]
                self.assertEqual(sent[-1]["content"], prefix1[-1]["content"])
                self.assertIsNone(turn1.protocol_fault())
                self.assertEqual(turn1.tool_call()["name"], "submit_prose")
            # A turn with no tool call at all is the `no_tool_call` code with
            # no id to answer.
            bare = P.BackendTurn(content=({"type": "text", "text": "hi"},), stop_reason="end_turn", model="m")
            self.assertEqual(bare.protocol_fault().code, "no_tool_call")
            self.assertEqual(P.protocol_correction_results(bare, bare.protocol_fault()), [])


# ---------------------------------------------------------------------------
# run_session: one evidence row per session, INIT reserve
# ---------------------------------------------------------------------------

class RunSessionTest(_SessionCase):
    def _runner(self, provider_turns, submit="submit_prose"):
        """A stand-in for review.session.run_bounded_session that drives
        `provider._turn` through a scripted loop and returns a result-shaped
        object, isolating the PROVIDER half of `run_session` (the ledger,
        the INIT reserve, the single row). The real runner is exercised end
        to end by `WireLoopTest` and the replay / resume gates above."""

        def run_bounded_session(role, initial_view, tools, policy, limits, *, provider, ctx, **kw):
            messages = _view(initial_view)
            turns = []
            for index in range(provider_turns):
                turn = provider._turn(role, messages, policy, index)
                turns.append(turn)
                call = turn.tool_call()
                if call["name"] == (policy.submit_tool or submit):
                    break
                messages = P._with_tool_results(
                    messages, turn.content, [P.ToolResultBlock(call["id"], "[prose] ok ok=true")]
                )
            return SimpleNamespace(
                terminal=SimpleNamespace(name="SUBMITTED", value="submitted"),
                final=turns[-1].tool_call()["input"]["text"],
                model_call_count=len(turns),
                tool_call_count=len(turns) - 1,
                refused_count=0,
                nudge_count=0,
                correction_count=0,
                validator_run_count=0,
                stale_tool_result_count=0,
                session_sha256="c" * 64,
                turns=turns,
            )

        return run_bounded_session

    def test_run_session_appends_one_exchange_evidence_row_per_session(self):
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}), tempfile.TemporaryDirectory() as tmp:
            policy = P.session_policy_for(AUTHOR)
            transport = FakeTransport([
                tool_use_body("replace_prose", {"text": "draft"}, id="toolu_a", output_tokens=50),
                tool_use_body("submit_prose", {"text": "final"}, id="toolu_b", output_tokens=70),
            ])
            provider = _provider(tmp, transport)
            provider.begin_task_evidence("task-1", "a" * 64)
            ctx = R.ToolContext(root=Path(tmp) / "work", task_id="task-1", role=AUTHOR)
            with mock.patch.object(S, "run_bounded_session", self._runner(4), create=True):
                result = provider.run_session(AUTHOR, "VIEW", policy, ctx)
            self.assertEqual(result.final, "final")
            self.assertEqual(len(transport.calls), 2)
            self.assertIsNone(provider.active_session(AUTHOR), "the ledger is closed")
            self.assertEqual(len(provider.exchange_evidence), 1)
            row = provider.exchange_evidence[0]
            opus = P.rate_card_for("anthropic", "claude-opus-5", {})
            expected_usd = opus.usd_for(P.Usage(input_tokens=2000, output_tokens=120))
            self.assertEqual(row["role"], AUTHOR)
            self.assertEqual(row["task_id"], "task-1")
            self.assertEqual(row["task_content_hash"], "a" * 64)
            self.assertEqual(row["prompt_sha256"], P.transcript_key(AUTHOR, "VIEW"))
            self.assertEqual(row["response_sha256"], sha256_hex("final"))
            self.assertEqual((row["model_call_count"], row["attempt_count"]), (2, 2))
            self.assertEqual(row["live_model_call_count"], 2)
            self.assertEqual(row["tool_call_count"], 1)
            self.assertEqual((row["refused_count"], row["nudge_count"], row["correction_count"]), (0, 0, 0))
            self.assertEqual(row["validator_run_count"], 0)
            self.assertEqual(row["correction_kinds"], {"schema": 0, "compile": 0})
            self.assertEqual(row["terminal"], "SUBMITTED")
            self.assertEqual(row["stale_tool_result_count"], 0)
            self.assertFalse(row["replayed"])
            self.assertEqual(row["trajectory_sha256"], "c" * 64)
            self.assertEqual(row["entry_schema"], 3)
            self.assertEqual((row["provider"], row["model"]), ("anthropic", "claude-opus-5"))
            self.assertEqual(row["behavior_sha256"], P.role_behavior_sha256(AUTHOR))
            self.assertEqual(row["tools_sha256"], policy.tools_sha256())
            self.assertEqual(row["policy_sha256"], policy.sha256())
            self.assertEqual(row["diagnostics_version"], P.DIAGNOSTICS_VERSION)
            self.assertEqual(row["usage"], {"input": 2000, "output": 120, "cache_read": 0, "cache_write": 0})
            self.assertAlmostEqual(row["usd"], expected_usd)
            self.assertAlmostEqual(provider.meter.total_usd, expected_usd)
            self.assertIsInstance(row["wall_ms"], int)
            self.assertIsNone(row["finding_count"])
            # The identity the metrology summary checks holds for the row.
            self.assertEqual(
                row["model_call_count"],
                row["tool_call_count"] + row["refused_count"] + row["nudge_count"]
                + row["correction_count"] + 1,
            )
            # Replayed on the same store: the same single row, zero HTTP,
            # zero USD, replayed true.
            replay = _provider(tmp, FakeTransport([]), replay_only=True)
            replay.begin_task_evidence("task-1", "a" * 64)
            with mock.patch.object(S, "run_bounded_session", self._runner(4), create=True):
                replay.run_session(AUTHOR, "VIEW", policy, ctx)
            self.assertEqual(len(replay.exchange_evidence), 1)
            self.assertTrue(replay.exchange_evidence[0]["replayed"])
            self.assertEqual(replay.exchange_evidence[0]["live_model_call_count"], 0)
            self.assertEqual(replay.exchange_evidence[0]["usd"], 0.0)
            self.assertEqual(replay.exchange_evidence[0]["model_call_count"], 2)
            self.assertEqual(replay.meter.total_usd, 0.0)

    def test_run_session_init_reserve_refuses_before_any_call_and_appends_no_row(self):
        """INIT: the task budget must absorb the session's cap before a single
        call (04 §1); a refusal is the base `BudgetExceededError` (task scope,
        infrastructure) with nothing spent, nothing recorded, no row, and the
        ledger closed. A runner raise appends no row either."""
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}), tempfile.TemporaryDirectory() as tmp:
            declared = P.session_policy_for(AUTHOR)
            policy = P.SessionPolicy(
                role=AUTHOR, tools=declared.tools, submit_tool="submit_prose",
                limits=P.SessionLimits.from_block({"enabled": True, "max_usd": 1.00}),
                mode="model_driven", wire_tools=declared.wire_tools,
            )
            transport = FakeTransport([tool_use_body("submit_prose", {"text": "x"})])
            provider = _provider(tmp, transport, meter=P.CostMeter(budget_per_task_usd=0.50))
            ctx = R.ToolContext(root=Path(tmp) / "work", task_id="task-1", role=AUTHOR)
            with mock.patch.object(S, "run_bounded_session", self._runner(4), create=True):
                with self.assertRaises(P.BudgetExceededError) as ctx_exc:
                    provider.run_session(AUTHOR, "VIEW", policy, ctx)
            self.assertIs(type(ctx_exc.exception), P.BudgetExceededError)
            self.assertEqual(ctx_exc.exception.scope, "task")
            self.assertEqual(transport.calls, [])
            self.assertEqual(provider.exchange_evidence, [])
            self.assertIsNone(provider.active_session(AUTHOR))
            self.assertEqual(provider.meter.total_usd, 0.0)
            # A nested ceiling is reserved on top through `reserve_usd`.
            with mock.patch.object(S, "run_bounded_session", self._runner(4), create=True):
                provider.meter.budget_per_task_usd = 1.20
                with self.assertRaises(P.BudgetExceededError):
                    provider.run_session(AUTHOR, "VIEW", policy, ctx, reserve_usd=1.00 + 0.56)
                self.assertEqual(transport.calls, [])
                result = provider.run_session(AUTHOR, "VIEW", policy, ctx, reserve_usd=1.00)
            self.assertEqual(result.final, "x")
            self.assertEqual(len(provider.exchange_evidence), 1)
            # A ctx built for another role is refused before anything.
            other = R.ToolContext(root=Path(tmp) / "work", task_id="task-1", role=IMPLEMENTER)
            with self.assertRaises(ValueError):
                provider.run_session(AUTHOR, "VIEW", policy, other)
            # A raising runner leaves no row and a closed ledger.
            def raising(*args, **kwargs):
                raise S.ToolHarnessFault("replace_prose")

            with mock.patch.object(S, "run_bounded_session", raising, create=True):
                with self.assertRaises(S.ToolHarnessFault):
                    provider.run_session(AUTHOR, "VIEW", policy, ctx)
            self.assertEqual(len(provider.exchange_evidence), 1)
            self.assertIsNone(provider.active_session(AUTHOR))


# ---------------------------------------------------------------------------
# run_session -> the REAL runner -> _turn -> FakeTransport, on both backends
# ---------------------------------------------------------------------------

class WireLoopTest(_SessionCase):
    def test_scripted_tool_loop_records_one_transcript_entry_per_turn(self):
        """At the wire, on BOTH backends: `run_session` drives the real
        `run_bounded_session`, whose every model turn is ONE `_turn` -> ONE
        transport call -> ONE entry_schema-3 entry keyed on the exact prefix
        (the runner's model records carry those keys in order); the tool
        dispatches in-process against the real projections, its rendered
        Diagnostic answers the `tool_use` id on the next call; the detector,
        the chain and the single SoT T8 evidence row are all wired."""
        chat_routing = make_routing(oss_base="http://oss:8000/v1", oss_key="k", oss_model="m")
        cases = (
            ("anthropic", AUTHOR, make_routing(), [
                tool_use_body("check_draft", {"text": "one"}, id="toolu_a"),
                tool_use_body("check_draft", {"text": "two"}, id="toolu_b"),
                tool_use_body("submit_prose", {"text": "final"}, id="toolu_c"),
            ], ("toolu_a", "toolu_b")),
            ("openai_compat", IMPLEMENTER, chat_routing, [
                chat_tool_call_body("check_draft", {"text": "one"}, call_id="call_a", model="m"),
                chat_tool_call_body("check_draft", {"text": "two"}, call_id="call_b", model="m"),
                chat_tool_call_body("submit_prose", {"text": "final"}, call_id="call_c", model="m"),
            ], ("call_a", "call_b")),
        )
        for backend, role, routing, bodies, ids in cases:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as tmp:
                check = _RunningTool("check_draft")
                with _AgenticRoles(**{role: (check, "submit_prose", "abort")}):
                    policy = _session_policy(role)
                    self.assertTrue(P.role_is_agentic(role))
                    transport = FakeTransport(list(bodies))
                    provider = _bound(_provider(tmp, transport, routing=routing))
                    ctx = _session_ctx(tmp, role)
                    result = provider.run_session(role, "VIEW", policy, ctx)
                    self._assert_loop(result, provider, transport, check, routing, role, policy, tmp, ids)

    def _assert_loop(self, result, provider, transport, check, routing, role, policy, tmp, ids):
        route = routing.for_role(role)
        self.assertIs(result.terminal, S.TerminalState.SUBMITTED)
        self.assertEqual(result.final, {"text": "final"})
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(check.calls, [{"text": "one"}, {"text": "two"}])
        self.assertEqual((result.model_call_count, result.tool_call_count, result.terminal_count), (3, 2, 1))
        self.assertEqual(result.live_model_call_count, 3)
        self.assertTrue(result.verify_chain())
        self.assertEqual(result.session_sha256, result.chain_hashes[-1])
        model_turns = [t for t in result.turns if t.kind == "model"]
        self.assertEqual([t.category for t in model_turns], ["tool_call", "tool_call", "terminal"])
        keys = [t.memo_key for t in model_turns]
        self.assertEqual(len(set(keys)), 3)
        self.assertEqual(keys[0], P.transcript_key_v3(role, policy, _view()))
        store_dir = Path(tmp) / "transcripts" / role
        self.assertEqual(sorted(p.name for p in store_dir.glob("*.json")), sorted(f"{k}.json" for k in keys))
        self.assertEqual([p.name for p in store_dir.iterdir() if p.is_dir()], [P.SESSION_RECORDS_SUBDIR])
        for index, (key, turn) in enumerate(zip(keys, model_turns)):
            entry = json.loads((store_dir / f"{key}.json").read_text(encoding="utf-8"))
            self.assertEqual(P.transcript_entry_schema(entry), P.SESSION_TRANSCRIPT_ENTRY_SCHEMA)
            self.assertEqual(entry["turn"]["turn_index"], index)
            self.assertEqual(entry["turn"]["memo_key"], key)
            self.assertEqual(entry["turn"]["tools_sha256"], policy.tools_sha256())
            self.assertEqual(entry["route"]["policy_sha256"], policy.sha256())
            self.assertEqual((entry["provider"], entry["model"]), (route.provider, route.model))
            self.assertEqual((entry["task_id"], entry["task_content_hash"]), (TASK.task_id, TASK.content_hash()))
            self.assertEqual(sha256_hex(entry["response"]), turn.response_sha256)
            self.assertEqual(dict(turn.route), entry["route"])
            self.assertFalse(turn.replayed)
            self.assertIsNone(P.transcript_route_mismatch(entry, route, policy=policy))
        # Each call carries the whole prefix; the tool's projection answers
        # the previous tool_use id on the next call.
        lengths = [len(c[2]["messages"]) for c in transport.calls]
        self.assertEqual(lengths, sorted(lengths))
        self.assertEqual(len(set(lengths)), 3)
        for turn_index, tool_use_id in enumerate(ids, start=1):
            results = _tool_results_in(transport.calls[turn_index][2])
            self.assertEqual(results[-1][0], tool_use_id)
            self.assertIn("[gate] ok", results[-1][1])
        tool_turns = [t for t in result.turns if t.kind == "tool"]
        self.assertEqual([t.outcome_code for t in tool_turns], ["ok", "ok"])
        expected_sha = PJ.Diagnostic(source=PJ.DiagnosticSource.GATE, ok=True, code="ok").sha256
        self.assertEqual([t.output_sha256 for t in tool_turns], [expected_sha] * 2)
        # Every later turn's key folds the FULL digest of the observation
        # that fed it (0-1), and the entry records it.
        for key, fed_by in zip(keys[1:], tool_turns):
            entry = json.loads((store_dir / f"{key}.json").read_text(encoding="utf-8"))
            self.assertEqual(entry["turn"]["observations_sha256"], [fed_by.output_sha256])
        # ONE session record per completed session (0-1): what a replay is
        # verified against — the chain digest, the turn keys, the full
        # observation digests; filed beside the entries, never among them.
        record = provider.store.lookup_session(role, keys[0])
        self.assertEqual((record["role"], record["session_key"], record["entry_schema"]), (role, keys[0], 3))
        self.assertEqual(record["session_sha256"], result.session_sha256)
        self.assertEqual(record["chain_hashes"], list(result.chain_hashes))
        self.assertEqual(record["turn_keys"], keys)
        self.assertEqual(record["terminal"], "SUBMITTED")
        self.assertEqual(record["observations_sha256"], [expected_sha] * 2)
        self.assertEqual((record["policy_sha256"], record["tools_sha256"]), (policy.sha256(), policy.tools_sha256()))
        self.assertEqual((record["task_id"], record["task_content_hash"]), (TASK.task_id, TASK.content_hash()))
        self.assertEqual((record["model_call_count"], record["stale_tool_result_count"]), (3, 0))
        self.assertTrue((store_dir / P.SESSION_RECORDS_SUBDIR / f"{keys[0]}.json").is_file())
        self.assertFalse(P.transcripts_present(store_dir / P.SESSION_RECORDS_SUBDIR))
        # The detector saw both calls (different args: no repeat).
        self.assertEqual(result.detector_events, ())
        # ONE evidence row per session (SoT T8), the identity checkable from it.
        self.assertEqual(len(provider.exchange_evidence), 1)
        row = provider.exchange_evidence[0]
        self.assertEqual(row["role"], role)
        self.assertEqual(
            (row["model_call_count"], row["tool_call_count"], row["terminal_count"], row["limit_stop_count"]),
            (3, 2, 1, 0),
        )
        self.assertEqual((row["refused_count"], row["nudge_count"], row["correction_count"]), (0, 0, 0))
        self.assertEqual(row["terminal"], "SUBMITTED")
        self.assertEqual(row["trajectory_sha256"], result.session_sha256)
        self.assertEqual(row["entry_schema"], 3)
        self.assertEqual(row["prompt_sha256"], keys[0])
        self.assertEqual(row["response_sha256"], sha256_hex(canonical_json({"text": "final"})))
        self.assertEqual((row["provider"], row["model"]), (route.provider, route.model))
        self.assertGreater(row["usd"], 0.0)
        self.assertEqual(P.exchange_row_problems(row), [])
        self.assertIsNone(provider.active_session(role))


# ---------------------------------------------------------------------------
# Agentic roles are serial: never batched
# ---------------------------------------------------------------------------

class BatchRefusalTest(_SessionCase):
    def test_agentic_roles_are_not_batchable(self):
        """`BatchQueue.batchable` is False for any group holding a role with a
        non-empty registry, `groups()` filters such a request out, and a
        one-shot role stays batchable.  The shipped author is agentic too."""
        with tempfile.TemporaryDirectory() as tmp:
            provider = _provider(tmp, FakeTransport([]))
            queue = P.BatchQueue(provider, batch_transport=lambda *a: {})
            critic = P.BatchRequest("c-1", "ambiguity_critic", "p", "0" * 64)
            self.assertTrue(queue.batchable("anthropic", 2, [critic, critic]))
            with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}):
                self.assertTrue(P.role_is_agentic(AUTHOR))
                self.assertFalse(P.role_is_agentic("ambiguity_critic"))
                author = P.BatchRequest("a-1", AUTHOR, "p", "1" * 64)
                self.assertFalse(queue.batchable("anthropic", 2, [author, author]))
                self.assertFalse(queue.batchable("anthropic", 3, [critic, author, critic]))
                self.assertTrue(queue.batchable("anthropic", 2, [critic, critic]))
                self.assertIn("bounded sessions", queue._why_not_batched("anthropic", 2, [author]))
                # Filtered defensively from the groups even if it got in.
                queue._pending[author.custom_id] = author
                queue._pending[critic.custom_id] = critic
                self.assertEqual(
                    {name: [r.custom_id for r in reqs] for name, reqs in queue.groups().items()},
                    {"anthropic": ["c-1"]},
                )
            self.assertTrue(P.role_is_agentic(AUTHOR))

    def test_batch_queue_refuses_agentic_role(self):
        """`add` refuses an agentic role up front and `_run_serial` refuses
        it before any call: a session is never a queued `complete()`."""
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}), tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport([anthropic_text_response("prose")])
            provider = _provider(tmp, transport)
            queue = P.BatchQueue(provider, batch_transport=None)
            with self.assertRaises(ValueError) as ctx:
                queue.add(AUTHOR, "VIEW")
            self.assertIn("run_session", str(ctx.exception))
            self.assertEqual(len(queue), 0)
            request = P.BatchRequest("a-1", AUTHOR, "VIEW", P.transcript_key(AUTHOR, "VIEW"))
            responses: dict[str, str] = {}
            with self.assertRaises(ValueError):
                queue._run_serial([request], responses, [])
            self.assertEqual(responses, {})
            self.assertEqual(transport.calls, [])
            # A one-shot role on the same queue runs serially as today.
            queue.add(CouncilRole.AMBIGUITY_CRITIC, "p")
            transport.responses.clear()
            transport.responses.append(anthropic_tool_response([VALID_FINDING]))
            result = queue.run()
            self.assertEqual(len(result.serial), 1)
            self.assertEqual(len(transport.calls), 1)


# ---------------------------------------------------------------------------
# The key moves with the manifest and the policy (roadmap red-team row)
# ---------------------------------------------------------------------------

class TranscriptKeyMovesTest(_SessionCase):
    def test_transcript_key_moves_when_tool_manifest_or_policy_changes(self):
        """The one-shot `transcript_key`, the behaviour digest and the wire
        tools digest all move when a role's tool manifest gains a tool or
        its policy (limits, nudge text) changes; the session salt moves
        nothing; restored, the key is exactly what it was."""
        before = P.transcript_key(AUTHOR, "VIEW")
        behaviour_before = P.role_behavior_sha256(AUTHOR)
        tools_before = P.role_tools_sha256(AUTHOR)
        moved: dict[str, str] = {}
        with _AgenticRoles(**{AUTHOR: ("replace_prose",)}):
            moved["manifest"] = P.transcript_key(AUTHOR, "VIEW")
            self.assertNotEqual(P.role_behavior_sha256(AUTHOR), behaviour_before)
            self.assertNotEqual(P.role_tools_sha256(AUTHOR), tools_before)
            self.assertEqual(
                [t["name"] for t in P.role_behavior_manifest(AUTHOR)["tools"]], ["replace_prose"]
            )
            one_tool = P.transcript_key(AUTHOR, "VIEW")
            with _AgenticRoles(**{AUTHOR: ("replace_prose", "submit_prose")}):
                moved["second tool"] = P.transcript_key(AUTHOR, "VIEW")
            self.assertNotEqual(moved["second tool"], one_tool)
        doc = json.loads(json.dumps(P._agents_doc()))
        # A limit edit that stays loadable: `max_turns` is bound to
        # `max_revisions + 1` (and its hard cap) by the loader, so the wall
        # is the limit this test moves.  The shipped block is enabled; an
        # explicit disabled rollback ignores edits inside that inactive
        # block, while the same edit when enabled moves digest and key.
        disabled_base = json.loads(json.dumps(doc))
        disabled_base["roles"][AUTHOR]["session"]["enabled"] = False
        with mock.patch.object(P, "_agents_doc", lambda: disabled_base):
            P.clear_behavior_caches()
            disabled_before = P.transcript_key(AUTHOR, "VIEW")
            disabled_behaviour_before = P.role_behavior_sha256(AUTHOR)
        disabled = json.loads(json.dumps(disabled_base))
        disabled["roles"][AUTHOR]["session"] = {
            **dict(disabled["roles"][AUTHOR].get("session") or {}), "wall_clock_s": 301,
        }
        with mock.patch.object(P, "_agents_doc", lambda: disabled):
            P.clear_behavior_caches()
            self.assertEqual(
                P.transcript_key(AUTHOR, "VIEW"), disabled_before,
                "an edit within a disabled runner block is inert",
            )
            self.assertEqual(P.role_behavior_sha256(AUTHOR), disabled_behaviour_before)
            self.assertEqual(P.role_behavior_manifest(AUTHOR)["loop_limits"], {"enabled": False})
        doc["roles"][AUTHOR]["session"] = {
            **dict(doc["roles"][AUTHOR].get("session") or {}), "enabled": True, "wall_clock_s": 301,
        }
        with mock.patch.object(P, "_agents_doc", lambda: doc):
            P.clear_behavior_caches()
            moved["policy limits"] = P.transcript_key(AUTHOR, "VIEW")
            self.assertNotEqual(P.role_behavior_sha256(AUTHOR), behaviour_before)
            self.assertEqual(P.role_behavior_manifest(AUTHOR)["loop_limits"]["wall_clock_s"], 301)
        P.clear_behavior_caches()
        policy = P.session_policy_for(AUTHOR)
        for label, edit in (
            ("nudge text", {"nudge_text": "edited nudge"}),
            ("refusal text", {"refusal_text": "edited refusal (code: {code})"}),
            ("stuck thresholds", {"stuck_thresholds": {**policy.stuck_thresholds, "identical_pairs_halt": 5}}),
            ("submit tool", {"submit_tool": "submit_prose"}),
        ):
            edited = P.SessionPolicy(
                role=AUTHOR, tools=policy.tools, limits=policy.limits, mode=policy.mode,
                wire_tools=policy.wire_tools,
                **{"submit_tool": policy.submit_tool, "nudge_text": policy.nudge_text,
                   "refusal_text": policy.refusal_text, "stuck_thresholds": policy.stuck_thresholds,
                   **edit},
            )
            self.assertNotEqual(edited.sha256(), policy.sha256(), label)
            moved[label] = P.transcript_key_v3(AUTHOR, edited, _view())
        for label, key in moved.items():
            self.assertNotEqual(key, before, label)
        self.assertEqual(len(set(moved.values())), len(moved), "each change keys apart")
        self.assertEqual(P.transcript_key(AUTHOR, "VIEW"), before)
        policy = P.session_policy_for(AUTHOR)
        salted = P.SessionPolicy(
            role=AUTHOR, tools=policy.tools, submit_tool=policy.submit_tool,
            limits=policy.limits, mode=policy.mode, wire_tools=policy.wire_tools, session_salt=3,
        )
        self.assertEqual(P.transcript_key_v3(AUTHOR, salted, _view()), before)


# ---------------------------------------------------------------------------
# Phase 1 review remediation (docs/plans/bounded_agents_phase1.md §2):
# the view is never persisted; served turn blocks verify; the per-turn wire
# ---------------------------------------------------------------------------

class SessionEntryIntegrityTest(_SessionCase):
    def test_session_entries_never_persist_the_initial_view(self):
        """Finding 0-1: a REFERENCE-route proposer view carries the reference
        SQL verbatim (D4-private at rest). A one-shot entry records
        `prompt_sha256` only; a session entry keeps that discipline — turn 0
        records the view's DIGEST (`content_sha256`, `content_omitted`),
        never its bytes, while a later turn's tail (tool results the
        gatekeeper passed) is recorded verbatim — so no transcript store,
        the version-controlled `council/transcripts/` included, receives
        reference SQL."""
        from elt_taskgen.models import RepairRoute
        from elt_taskgen.review import repair_proposer as rp

        role = "repair_proposer"
        view = rp.session_view(TASK, RepairRoute.REFERENCE, "reference red", index=1, max_sessions=2)
        private_sql = [sql for sql in TASK.reference.sql_by_mart.values()]
        self.assertTrue(private_sql)
        self.assertTrue(all(sql in view for sql in private_sql), "the REFERENCE view carries the reference SQL")
        routing = make_routing()
        routing.roles[role] = P.RoleRoute(role, "anthropic", "claude-opus-5", 8192, "high")
        with tempfile.TemporaryDirectory() as tmp:
            policy = P.session_policy_for(role)
            bodies = [
                tool_use_body("abort", {"reason_code": "cannot_repair"}, id="toolu_a", text="no tool needed"),
                tool_use_body("abort", {"reason_code": "cannot_repair"}, id="toolu_b"),
            ]
            provider = _bound(_provider(tmp, FakeTransport(bodies), routing=routing))
            with provider.session(role, policy):
                turn0 = provider._turn(role, _view(view), policy, 0)
                prefix1 = P._with_tool_results(
                    _view(view), turn0.content, [P.ToolResultBlock("toolu_a", "[rejection] scope_ok ok=true")]
                )
                turn1 = provider._turn(role, prefix1, policy, 1)
            for key, turn_index in ((turn0.key, 0), (turn1.key, 1)):
                entry = json.loads(_entry_path(tmp, role, key).read_text(encoding="utf-8"))
                serialized = canonical_json(entry)
                for sql in private_sql:
                    self.assertNotIn(sql, serialized, f"turn {turn_index} persisted reference SQL")
                self.assertNotIn(view, serialized)
                self.assertNotIn("prompt", entry, "no prompt text on a session entry either")
            entry0 = json.loads(_entry_path(tmp, role, turn0.key).read_text(encoding="utf-8"))
            self.assertEqual(
                entry0["turn"]["user_messages"],
                [{"role": "user", "content_sha256": sha256_hex(canonical_json(view)), "content_omitted": "initial_view"}],
            )
            self.assertEqual(entry0["turn"]["messages_sha256"], sha256_hex(canonical_json(_view(view))))
            entry1 = json.loads(_entry_path(tmp, role, turn1.key).read_text(encoding="utf-8"))
            self.assertEqual(
                entry1["turn"]["user_messages"],
                [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_a",
                                               "content": "[rejection] scope_ok ok=true"}]}],
            )
            # Both entries still serve (the digest, not the bytes, is the binding).
            replay = _bound(_provider(tmp, FakeTransport([]), routing=routing, replay_only=True))
            with replay.session(role, policy):
                self.assertTrue(replay._turn(role, _view(view), policy, 0).replayed)
                self.assertTrue(replay._turn(role, prefix1, policy, 1).replayed)

    def test_served_turn_content_is_verified_against_its_digest(self):
        """Finding 2-0: the SERVED bytes of a session entry are `turn.content`,
        bound by `turn.content_sha256`; an entry whose content was rewritten
        (response and response_sha256 untouched), whose `memo_key` or
        `messages_sha256` claims another turn, or whose digest is missing is
        refused as `TranscriptMissingError` in live mode, under
        `--replay-only` and on the content-hash-tolerant F2 path; the
        pristine entry serves."""
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}), tempfile.TemporaryDirectory() as tmp:
            policy = P.session_policy_for(AUTHOR)
            provider = _bound(_provider(tmp, FakeTransport([tool_use_body("submit_prose", {"text": "d"}, id="toolu_a")])))
            with provider.session(AUTHOR, policy):
                turn = provider._turn(AUTHOR, _view(), policy, 0)
            path = _entry_path(tmp, AUTHOR, turn.key)
            pristine = json.loads(path.read_text(encoding="utf-8"))

            def rewrite(mutate):
                entry = json.loads(json.dumps(pristine))
                mutate(entry)
                path.write_text(canonical_json(entry), encoding="utf-8")

            def tamper(entry):
                entry["turn"]["content"][0]["input"]["text"] = "TAMPERED: rm -rf gold"

            cases = (
                (tamper, "content_sha256 does not match"),
                (lambda e: e["turn"].__setitem__("memo_key", "0" * 64), "memo_key"),
                (lambda e: e["turn"].__setitem__("messages_sha256", "0" * 64), "messages_sha256"),
                (lambda e: e["turn"].pop("content_sha256"), "content_sha256 missing"),
            )
            for mutate, needle in cases:
                for replay_only in (False, True):
                    with self.subTest(needle=needle, replay_only=replay_only):
                        rewrite(mutate)
                        served = _bound(_provider(tmp, FakeTransport([]), replay_only=replay_only))
                        with served.session(AUTHOR, policy):
                            with self.assertRaisesRegex(P.TranscriptMissingError, needle):
                                served._turn(AUTHOR, _view(), policy, 0)
            # The F2 path (same task, same key, another content hash, live mode).
            rewrite(tamper)
            moved = _provider(tmp, FakeTransport([]))
            moved.begin_task_evidence(TASK.task_id, "f" * 64)
            with moved.session(AUTHOR, policy):
                with self.assertRaisesRegex(P.TranscriptMissingError, "content_sha256"):
                    moved._turn(AUTHOR, _view(), policy, 0)
            path.write_text(canonical_json(pristine), encoding="utf-8")
            with moved.session(AUTHOR, policy):
                self.assertTrue(moved._turn(AUTHOR, _view(), policy, 0).replayed)
            served = _bound(_provider(tmp, FakeTransport([]), replay_only=True))
            with served.session(AUTHOR, policy):
                again = served._turn(AUTHOR, _view(), policy, 0)
            self.assertTrue(again.replayed)
            self.assertEqual(again.content, turn.content)

    def test_served_raw_content_is_verified_on_every_source(self):
        """Finding 0-0 (residual): the served bytes are bound on EVERY source
        `_turn_from_entry` reads, not only `turn.content`. A schema-3 entry
        whose turn block lost its content (or the whole block) while its last
        raw attempt was rewritten, and a genuine one-shot `complete()` entry
        whose raw content was rewritten and is served into turn 0 of a
        zero-tool session, are refused as `TranscriptMissingError` in live
        mode, under `--replay-only` and on the F2 path: the stored blocks
        must reduce to the verified `response`, which alone never serves."""
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}), tempfile.TemporaryDirectory() as tmp:
            policy = P.session_policy_for(AUTHOR)
            provider = _bound(_provider(tmp, FakeTransport([tool_use_body("submit_prose", {"text": "d"}, id="toolu_a")])))
            with provider.session(AUTHOR, policy):
                turn = provider._turn(AUTHOR, _view(), policy, 0)
            path = _entry_path(tmp, AUTHOR, turn.key)
            pristine = json.loads(path.read_text(encoding="utf-8"))

            def tamper_raw(entry):
                entry["raw_attempts"][-1]["content"][0]["input"]["text"] = "TAMPERED: rm -rf gold"

            def drop_content(entry):
                entry["turn"].pop("content")
                entry["turn"].pop("content_sha256")
                tamper_raw(entry)

            def drop_block(entry):
                entry.pop("turn")
                tamper_raw(entry)

            def drop_keys(entry):
                entry["turn"].pop("memo_key")
                entry["turn"].pop("messages_sha256")

            cases = (
                (drop_content, "turn.content missing"),
                (drop_block, "verified response"),
                (drop_keys, "turn.memo_key missing"),
            )
            for mutate, needle in cases:
                entry = json.loads(json.dumps(pristine))
                mutate(entry)
                path.write_text(canonical_json(entry), encoding="utf-8")
                for replay_only in (False, True):
                    with self.subTest(needle=needle, replay_only=replay_only):
                        served = _bound(_provider(tmp, FakeTransport([]), replay_only=replay_only))
                        with served.session(AUTHOR, policy):
                            with self.assertRaisesRegex(P.TranscriptMissingError, needle):
                                served._turn(AUTHOR, _view(), policy, 0)
                with self.subTest(needle=needle, path="F2"):
                    moved = _provider(tmp, FakeTransport([]))
                    moved.begin_task_evidence(TASK.task_id, "f" * 64)
                    with moved.session(AUTHOR, policy):
                        with self.assertRaisesRegex(P.TranscriptMissingError, needle):
                            moved._turn(AUTHOR, _view(), policy, 0)
            path.write_text(canonical_json(pristine), encoding="utf-8")
            served = _bound(_provider(tmp, FakeTransport([]), replay_only=True))
            with served.session(AUTHOR, policy):
                self.assertEqual(served._turn(AUTHOR, _view(), policy, 0).content, turn.content)
        # A genuine one-shot entry (no turn block) served into turn 0 of a
        # zero-tool session: its raw content is verified against `response`.
        with tempfile.TemporaryDirectory() as tmp:
            provider = _bound(_provider(tmp, FakeTransport([anthropic_text_response("PROSE")])))
            text = provider.complete(AUTHOR, "VIEW")
            self.assertEqual(text, "PROSE")
            policy = P.session_policy_for(AUTHOR)
            with provider.session(AUTHOR, policy):
                served = provider._turn(AUTHOR, _view(), policy, 0)
            self.assertTrue(served.replayed)
            self.assertEqual(served.text, "PROSE")
            path = _entry_path(tmp, AUTHOR, served.key)
            entry = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("turn", entry, "a one-shot entry carries no turn block")
            entry["raw_attempts"][-1]["content"][0]["text"] = "TAMPERED: rm -rf gold"
            path.write_text(canonical_json(entry), encoding="utf-8")
            for replay_only in (False, True):
                with self.subTest(source="one_shot_raw", replay_only=replay_only):
                    refusing = _bound(_provider(tmp, FakeTransport([]), replay_only=replay_only))
                    with refusing.session(AUTHOR, policy):
                        with self.assertRaisesRegex(P.TranscriptMissingError, "verified response"):
                            refusing._turn(AUTHOR, _view(), policy, 0)

    def test_served_turn_must_match_the_per_turn_wire(self):
        """Finding 0-2: the per-turn wire ENTERS the memo key — a turn
        recorded under a narrowed `tools[]` or a forced `tool_choice` keys
        apart from the same prefix under the full wire (`turn_wire_binding`),
        so a wide request never serves a narrow recording and vice versa.
        The entry still records both, and a stored entry whose recorded
        wire is not its key's (a tampered or corrupt store) is refused
        under `--replay-only` and remade, re-recorded, in live mode."""
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}), tempfile.TemporaryDirectory() as tmp:
            policy = P.session_policy_for(AUTHOR)
            submit_only = [dict(t) for t in policy.wire_tools if t["name"] == "submit_prose"]
            forced = {"type": "tool", "name": "submit_prose"}
            provider = _bound(_provider(tmp, FakeTransport([tool_use_body("submit_prose", {"text": "d"}, id="toolu_a")])))
            with provider.session(AUTHOR, policy):
                narrow = provider._turn(AUTHOR, _view(), policy, 0, tools=submit_only, tool_choice=forced)
            wide_key = P.transcript_key_v3(AUTHOR, policy, _view())
            self.assertNotEqual(narrow.key, wide_key, "the narrowed wire enters the key")
            self.assertEqual(
                narrow.key, P.session_turn_key(AUTHOR, policy, _view(), 0, tools=submit_only, tool_choice=forced)
            )
            self.assertEqual(
                narrow.key,
                P.transcript_key_v3(
                    AUTHOR, policy, _view(),
                    wire_tools_sha256=sha256_hex(canonical_json(submit_only)), tool_choice=forced,
                ),
            )
            path = _entry_path(tmp, AUTHOR, narrow.key)
            entry = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(entry["turn"]["tools_sha256"], sha256_hex(canonical_json(submit_only)))
            self.assertNotEqual(entry["turn"]["tools_sha256"], policy.tools_sha256())
            self.assertEqual(entry["turn"]["tool_choice"], forced)
            replay = _bound(_provider(tmp, FakeTransport([]), replay_only=True))
            with replay.session(AUTHOR, policy):
                # The full wire, or the narrowed wire under another choice:
                # other keys — a MISS, never the narrow recording.
                with self.assertRaises(P.SessionTranscriptMissingError):
                    replay._turn(AUTHOR, _view(), policy, 0)
                with self.assertRaises(P.SessionTranscriptMissingError):
                    replay._turn(AUTHOR, _view(), policy, 0, tools=submit_only, tool_choice={"type": "any"})
                served = replay._turn(AUTHOR, _view(), policy, 0, tools=submit_only, tool_choice=forced)
            self.assertTrue(served.replayed)
            self.assertEqual(served.content, narrow.content)
            # A stored entry whose RECORDED wire is not its key's: refused
            # under replay-only; in live mode a miss remade under this
            # request's wire and re-recorded under the same key.
            for field, value, marker, id_ in (
                ("tools_sha256", policy.tools_sha256(), "per-turn tools", "toolu_b"),
                ("tool_choice", {"type": "any"}, "tool_choice", "toolu_c"),
            ):
                tampered = json.loads(path.read_text(encoding="utf-8"))
                tampered["turn"][field] = value
                path.write_text(canonical_json(tampered), encoding="utf-8")
                refusing = _bound(_provider(tmp, FakeTransport([]), replay_only=True))
                with refusing.session(AUTHOR, policy):
                    with self.assertRaisesRegex(P.TranscriptRouteMismatchError, marker):
                        refusing._turn(AUTHOR, _view(), policy, 0, tools=submit_only, tool_choice=forced)
                live = _bound(_provider(tmp, FakeTransport([tool_use_body("submit_prose", {"text": "e"}, id=id_)])))
                with live.session(AUTHOR, policy):
                    remade = live._turn(AUTHOR, _view(), policy, 0, tools=submit_only, tool_choice=forced)
                self.assertFalse(remade.replayed)
                self.assertEqual(remade.key, narrow.key)
                rerecorded = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(rerecorded["turn"]["tools_sha256"], sha256_hex(canonical_json(submit_only)))
                self.assertEqual(rerecorded["turn"]["tool_choice"], forced)
                self.assertEqual(rerecorded["turn"]["tool_uses"], [{"id": id_, "name": "submit_prose"}])
            # The full-wire request records under ITS OWN key, the narrow
            # entry untouched beside it.
            live = _bound(_provider(tmp, FakeTransport([tool_use_body("replace_prose", {"text": "f"}, id="toolu_d")])))
            with live.session(AUTHOR, policy):
                wide = live._turn(AUTHOR, _view(), policy, 0)
            self.assertFalse(wide.replayed)
            self.assertEqual(wide.key, wide_key)
            self.assertNotEqual(wide.key, narrow.key)
            self.assertTrue(path.is_file())
            wide_entry = json.loads(_entry_path(tmp, AUTHOR, wide.key).read_text(encoding="utf-8"))
            self.assertEqual(wide_entry["turn"]["tools_sha256"], policy.tools_sha256())
            self.assertEqual(wide_entry["turn"]["tool_choice"], {"type": "any"})
            self.assertEqual(wide_entry["turn"]["tool_uses"], [{"id": "toolu_d", "name": "replace_prose"}])


class TurnKeyBindingTest(_SessionCase):
    def test_transcript_key_v3_folds_the_per_turn_wire_choice_and_observations(self):
        """Finding 0-2 / 0-1: `transcript_key_v3` folds THIS turn's wire
        (`wire_tools_sha256`, `tool_choice`) when it deviates from the
        policy's full wire and default choice, and the full digests of the
        observations that fed the turn; the policy's own defaults and no
        observation add nothing, so turn 0 of a zero-tool one-shot role IS
        `transcript_key(role, prompt)` (the parity gate)."""
        with _AgenticRoles(**{AUTHOR: AUTHOR_TOOLS}):
            policy = P.session_policy_for(AUTHOR)
            base = P.transcript_key_v3(AUTHOR, policy, _view())
            self.assertEqual(P.turn_wire_binding(policy), "")
            self.assertEqual(
                P.turn_wire_binding(
                    policy, wire_tools_sha256=policy.tools_sha256(), tool_choice=P.session_tool_choice(policy)
                ),
                "",
            )
            self.assertEqual(
                P.transcript_key_v3(AUTHOR, policy, _view(), wire_tools_sha256=policy.tools_sha256(), tool_choice="any"),
                base,
            )
            self.assertEqual(P.transcript_key_v3(AUTHOR, policy, _view(), observations=[]), base)
            self.assertEqual(P.session_turn_key(AUTHOR, policy, _view(), 0), base)
            self.assertEqual(P.transcript_key(AUTHOR, "VIEW"), base)
            last = policy.limits.max_turns - 1
            narrowed = [dict(t) for t in policy.wire_tools_for_turn(last)]
            # The declared policy of a PROSE role names no submit tool, so
            # its terminal wire is `abort` alone: narrower than the full wire.
            self.assertEqual([t["name"] for t in narrowed], ["abort"])
            self.assertLess({t["name"] for t in narrowed}, {t["name"] for t in policy.wire_tools})
            keys = {
                "narrowed wire (last permitted turn)": P.session_turn_key(AUTHOR, policy, _view(), last),
                "forced choice": P.transcript_key_v3(AUTHOR, policy, _view(), tool_choice="submit_prose"),
                "narrowed + forced": P.transcript_key_v3(
                    AUTHOR, policy, _view(),
                    wire_tools_sha256=sha256_hex(canonical_json(narrowed)),
                    tool_choice={"type": "tool", "name": "submit_prose"},
                ),
                "one observation": P.transcript_key_v3(AUTHOR, policy, _view(), observations=["a" * 64]),
                "another observation": P.transcript_key_v3(AUTHOR, policy, _view(), observations=["b" * 64]),
                "two observations": P.transcript_key_v3(AUTHOR, policy, _view(), observations=["a" * 64, "b" * 64]),
                "observations in the other order": P.transcript_key_v3(
                    AUTHOR, policy, _view(), observations=["b" * 64, "a" * 64]
                ),
            }
            for label, key in keys.items():
                self.assertNotEqual(key, base, label)
            self.assertEqual(len(set(keys.values())), len(keys), "each binding keys apart")
            # A choice by NAME and as the canonical object are one binding.
            self.assertEqual(
                keys["forced choice"],
                P.transcript_key_v3(AUTHOR, policy, _view(), tool_choice={"type": "tool", "name": "submit_prose"}),
            )
            self.assertEqual(
                keys["narrowed wire (last permitted turn)"],
                P.transcript_key_v3(
                    AUTHOR, policy, _view(), wire_tools_sha256=sha256_hex(canonical_json(narrowed)), tool_choice="any"
                ),
            )
        # A zero-tool one-shot role: turn 0 under its declared policy, the
        # full wire and the default (forced report_findings) choice, keys
        # exactly as `complete()` does.
        for role in ("ambiguity_critic", IMPLEMENTER):
            declared = P.session_policy_for(role)
            self.assertEqual(P.session_turn_key(role, declared, _view("p"), 0), P.transcript_key(role, "p"))


@dataclass
class _RowsTool:
    """A model-facing tool returning DEVELOPMENT rows LARGER than the
    `dev_rows` tool_result cap, whose LAST cell (`last`, past the cap) is
    the only thing two versions differ in: the delivered (capped) text is
    byte-identical up to the digest tail, the full observation is not."""

    name: str
    last: str = "same"
    description: str = "A rows stub tool for the replay integrity tests."
    input_schema: Mapping[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
    )
    cost: R.ToolCost = field(default_factory=R.ToolCost)
    permitted_roles: frozenset[str] = frozenset({"semantic_author", "repair_proposer"})
    calls: list = field(default_factory=list)

    def observation(self) -> PJ.DevRows:
        rows = tuple((f"row{i}", "x" * 110) for i in range(150)) + (("row150", self.last),)
        return PJ.DevRows(columns=("customer_id", "customer_name"), rows=rows)

    def run(self, ctx, args):
        self.calls.append(dict(args))
        return self.observation()


class ReplayIntegrityTest(_SessionCase):
    """Finding 0-1: a `--replay-only` session is verified against the SESSION
    RECORD the live run filed (`TranscriptStore.record_session`): every
    executed observation's FULL digest tool by tool, and the chain digest at
    the end; a divergence is `SessionReplayMismatchError` (the
    `ToolHarnessFault` of code `replay_mismatch`, also a
    `TranscriptMissingError`), counted in `stale_tool_result_count`, and never
    a `replayed=True` evidence row; a missing record is refused before a
    single turn is served."""

    def _live(self, tmp, rows):
        policy = _session_policy(AUTHOR)
        live = FakeTransport([
            tool_use_body("dev_rows_probe", {"text": "q"}, id="toolu_a"),
            tool_use_body("submit_prose", {"text": "final"}, id="toolu_b"),
        ])
        provider = _bound(_provider(tmp, live))
        ctx = _session_ctx(tmp, AUTHOR)
        recorded = provider.run_session(AUTHOR, "VIEW", policy, ctx)
        self.assertIs(recorded.terminal, S.TerminalState.SUBMITTED)
        return policy, live, provider, ctx, recorded

    def test_replay_only_refuses_an_observation_diverged_past_the_tool_result_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = _RowsTool("dev_rows_probe")
            with _AgenticRoles(**{AUTHOR: (rows, "submit_prose", "abort")}):
                policy, live, provider, ctx, recorded = self._live(tmp, rows)
                tool_turn = next(t for t in recorded.turns if t.kind == "tool")
                self.assertEqual(tool_turn.output_sha256, rows.observation().sha256)
                # The tool_result the model saw was CUT at the cap; the full
                # digest rides in its tail, the key of turn 1 folds it.
                delivered = _tool_results_in(live.calls[1][2])[-1][1]
                self.assertIn(S._TRUNCATION_MARKER, delivered)
                self.assertIn(tool_turn.output_sha256, delivered)
                self.assertLess(len(delivered.encode("utf-8")), len(rows.observation().render().encode("utf-8")))
                model_keys = [t.memo_key for t in recorded.turns if t.kind == "model"]
                entry1 = json.loads(_entry_path(tmp, AUTHOR, model_keys[1]).read_text(encoding="utf-8"))
                self.assertEqual(entry1["turn"]["observations_sha256"], [tool_turn.output_sha256])
                session_key = provider.exchange_evidence[0]["prompt_sha256"]
                record = provider.store.lookup_session(AUTHOR, session_key)
                self.assertEqual(record["observations_sha256"], [tool_turn.output_sha256])
                self.assertEqual(record["session_sha256"], recorded.session_sha256)
                self.assertRegex(record["trajectory_sha256"], r"^[0-9a-f]{64}$")
                trajectory = provider.store.lookup_trajectory(
                    AUTHOR, record["trajectory_sha256"]
                )
                self.assertIsNotNone(trajectory)
                self.assertEqual(trajectory["session_sha256"], recorded.session_sha256)
                self.assertEqual(trajectory["prompt_sha256"], session_key)
                self.assertEqual(
                    [m["turn_key"] for m in trajectory["model_turns"]], model_keys
                )
                self.assertEqual(len(trajectory["model_turns"]), 2)
                self.assertTrue(all(m["raw_response"] for m in trajectory["model_turns"]))
                from elt_taskgen.review import trajectory as trajectory_mod

                self.assertEqual(
                    trajectory_mod.verify_trajectory_record(trajectory),
                    record["trajectory_sha256"],
                )
                # The SAME environment replays cleanly: zero transport, the
                # recorded digest, no stale observation, a replayed row.
                clean_transport = FakeTransport([])
                clean = _bound(_provider(tmp, clean_transport, replay_only=True))
                served = clean.run_session(AUTHOR, "VIEW", policy, ctx)
                self.assertEqual(clean_transport.calls, [])
                self.assertEqual((served.session_sha256, served.stale_tool_result_count), (recorded.session_sha256, 0))
                self.assertTrue(clean.exchange_evidence[0]["replayed"])
                self.assertEqual(clean.exchange_evidence[0]["stale_tool_result_count"], 0)
            # The same tool diverged in the LAST cell only — past the cap.
            diverged = _RowsTool("dev_rows_probe", last="different")
            with _AgenticRoles(**{AUTHOR: (diverged, "submit_prose", "abort")}):
                policy_d = _session_policy(AUTHOR)
                self.assertEqual((policy_d.sha256(), policy_d.tools_sha256()), (policy.sha256(), policy.tools_sha256()))
                self.assertNotEqual(diverged.observation().sha256, rows.observation().sha256)
                cap = S.TOOL_OUTPUT_CAP_BYTES["dev_rows"]
                self.assertEqual(
                    rows.observation().render()[: cap - 256], diverged.observation().render()[: cap - 256],
                    "the delivered prefix is byte-identical: only the digest tail differs",
                )
                replay_transport = FakeTransport([])
                replay = _bound(_provider(tmp, replay_transport, replay_only=True))
                with self.assertRaises(P.SessionReplayMismatchError) as ctx_exc:
                    replay.run_session(AUTHOR, "VIEW", policy_d, ctx)
                exc = ctx_exc.exception
                self.assertIsInstance(exc, S.ToolHarnessFault)
                self.assertIsInstance(exc, P.TranscriptMissingError)
                self.assertEqual((exc.code, exc.tool, exc.boundary, exc.terminal), ("replay_mismatch", "dev_rows_probe", "tool", "HARNESS_FAULT"))
                partial = exc.session_result
                self.assertIs(partial.terminal, S.TerminalState.HARNESS_FAULT)
                self.assertEqual(partial.stale_tool_result_count, 1)
                self.assertEqual((partial.model_call_count, partial.tool_call_count), (1, 1))
                self.assertTrue(partial.turns[0].replayed)
                self.assertEqual(partial.turns[1].output_sha256, diverged.observation().sha256)
                self.assertEqual(partial.fault.code, "replay_mismatch")
                self.assertEqual(replay_transport.calls, [])
                self.assertEqual(replay.exchange_evidence, [], "no replayed=True row for a diverged replay")
                self.assertEqual(replay.meter.total_usd, 0.0)
                self.assertIsNone(replay.active_session(AUTHOR))
                self.assertEqual(diverged.calls, [{"text": "q"}])
                # The store is untouched: the recording's entries and record stand.
                self.assertEqual(provider.store.lookup_session(AUTHOR, session_key), record)
                self.assertTrue(all(_entry_path(tmp, AUTHOR, k).is_file() for k in model_keys))
                # The engine classifies the halt as infrastructure under BOTH names.
                from elt_taskgen import engine as engine_mod

                self.assertIn("ToolHarnessFault", engine_mod._INFRA_EXCEPTION_NAMES)
                self.assertIn("TranscriptMissingError", engine_mod._INFRA_EXCEPTION_NAMES)

    def test_replay_only_refuses_a_session_without_a_session_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = _RowsTool("dev_rows_probe")
            with _AgenticRoles(**{AUTHOR: (rows, "submit_prose", "abort")}):
                policy, _live, provider, ctx, recorded = self._live(tmp, rows)
                session_key = provider.exchange_evidence[0]["prompt_sha256"]
                record_path = Path(tmp) / "transcripts" / AUTHOR / P.SESSION_RECORDS_SUBDIR / f"{session_key}.json"
                self.assertTrue(record_path.is_file())
                record_path.unlink()
                calls_before = list(rows.calls)
                replay_transport = FakeTransport([])
                replay = _bound(_provider(tmp, replay_transport, replay_only=True))
                with self.assertRaises(P.SessionTranscriptMissingError) as ctx_exc:
                    replay.run_session(AUTHOR, "VIEW", policy, ctx)
                self.assertIn("session record", str(ctx_exc.exception))
                self.assertEqual(ctx_exc.exception.code, "replay_miss")
                self.assertEqual(rows.calls, calls_before, "refused before any tool ran")
                self.assertEqual(replay_transport.calls, [])
                self.assertEqual(replay.exchange_evidence, [])
                self.assertIsNone(replay.active_session(AUTHOR))
                # A record whose stated key is not its filename is a corrupt store.
                record_path.write_text(canonical_json({"session_key": "0" * 64}), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "corrupt store"):
                    replay.store.lookup_session(AUTHOR, session_key)

    def test_replay_only_refuses_a_replay_whose_chain_digest_is_not_the_recorded_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = _RowsTool("dev_rows_probe")
            with _AgenticRoles(**{AUTHOR: (rows, "submit_prose", "abort")}):
                policy, _live, provider, ctx, recorded = self._live(tmp, rows)
                session_key = provider.exchange_evidence[0]["prompt_sha256"]
                record_path = Path(tmp) / "transcripts" / AUTHOR / P.SESSION_RECORDS_SUBDIR / f"{session_key}.json"
                record = json.loads(record_path.read_text(encoding="utf-8"))
                record["session_sha256"] = "0" * 64
                record_path.write_text(canonical_json(record), encoding="utf-8")
                replay_transport = FakeTransport([])
                replay = _bound(_provider(tmp, replay_transport, replay_only=True))
                with self.assertRaises(P.SessionReplayMismatchError) as ctx_exc:
                    replay.run_session(AUTHOR, "VIEW", policy, ctx)
                exc = ctx_exc.exception
                self.assertEqual((exc.code, exc.tool), ("replay_mismatch", "session"))
                self.assertIn("session_sha256", str(exc))
                completed = exc.session_result
                self.assertIs(completed.terminal, S.TerminalState.SUBMITTED, "the session itself completed")
                self.assertEqual(completed.session_sha256, recorded.session_sha256)
                self.assertEqual(replay_transport.calls, [])
                self.assertEqual(replay.exchange_evidence, [], "no replayed=True row when the digest is not the recorded one")
                self.assertIsNone(replay.active_session(AUTHOR))
                self.assertEqual(record_path.read_text(encoding="utf-8"), canonical_json(record), "replay-only records nothing")

    def test_replay_only_verifies_the_linked_full_trajectory_before_serving_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = _RowsTool("dev_rows_probe")
            with _AgenticRoles(**{AUTHOR: (rows, "submit_prose", "abort")}):
                policy, _live, provider, ctx, _recorded = self._live(tmp, rows)
                session_key = provider.exchange_evidence[0]["prompt_sha256"]
                session_record = provider.store.lookup_session(AUTHOR, session_key)
                digest = session_record["trajectory_sha256"]
                path = Path(tmp) / "transcripts" / P.TRAJECTORIES_SUBDIR / AUTHOR / f"{digest}.json"
                trajectory = json.loads(path.read_text(encoding="utf-8"))
                trajectory["turns"][0]["response_sha256"] = "0" * 64
                path.write_text(canonical_json(trajectory), encoding="utf-8")

                replay_transport = FakeTransport([])
                replay = _bound(_provider(tmp, replay_transport, replay_only=True))
                with self.assertRaisesRegex(
                    P.SessionReplayMismatchError, "linked trajectory.*does not verify"
                ):
                    replay.run_session(AUTHOR, "VIEW", policy, ctx)
                self.assertEqual(replay_transport.calls, [])
                self.assertEqual(replay.exchange_evidence, [])
                self.assertIsNone(replay.active_session(AUTHOR))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ---------------------------------------------------------------------------
# Phase 4: the provider trial seam and the critic-session dispatch
# (roadmap Table 8 `review/providers.py` row; Seam and Chain test groups)
# ---------------------------------------------------------------------------

import contextlib as _contextlib
import hashlib as _hashlib
import inspect as _inspect
import shutil as _shutil

from elt_taskgen.review import council as _council
from elt_taskgen.review import metrology as _metrology
from elt_taskgen.review import trajectory as _trajectory
from elt_taskgen.review.tools import critic_validators as _cv

POP = "population_adversary"
SHC = "shortcut_attacker"
AMB = "ambiguity_critic"


def _doc(**session_updates) -> dict:
    """The loaded agents document with `roles.<role>.session` keys updated."""
    doc = json.loads(json.dumps(P._agents_doc()))
    for role, keys in session_updates.items():
        doc["roles"][role].setdefault("session", {}).update(keys)
    return doc


@_contextlib.contextmanager
def _agents(doc: dict):
    with mock.patch.object(P, "_agents_doc", lambda: doc):
        P.clear_behavior_caches()
        try:
            yield
        finally:
            P.clear_behavior_caches()


def _expected(passes=("development", "stress")) -> dict:
    from elt_taskgen.models import PopulationName

    return {p.value: (p.value in passes) for p in PopulationName}


def _proposal(kind="inner_join", params=None, *, passes=("development", "stress")) -> dict:
    from elt_taskgen.models import PopulationName

    expected = _expected(passes)
    return {
        "kind": kind,
        "params": json.dumps(params or {}),
        "expected_pass_by_stage": {
            "extract_load": {p.value: True for p in PopulationName},
            "transform": expected,
        },
        "rationale": "development and stress cannot distinguish INNER from LEFT",
    }


def _critic_finding(*, attack="inner_join", proposed=None, severity="major") -> dict:
    return {
        "severity": severity,
        "summary": "an INNER join is indistinguishable on the stated populations",
        "detail": "every development customer has a completed order",
        "route_hint": None,
        "suggested_attack": attack, "disposition": "active",
        "proposed_case": proposed,
    }


def _trial_ctx(tmp, *, nonce="0123456789abcdef0123456789abcdef", index=None, task=TASK):
    workspace = Path(tmp) / f"m-{nonce[:8]}-x"
    workspace.mkdir(exist_ok=True)
    fields = dict(trial_nonce=nonce, workspace=workspace, limits_by_role={}, tool_policy_by_role={},
                  roles=(POP,), task_id=task.task_id, task_content_hash=task.content_hash(), task=task)
    if index is not None:
        fields["trial_index"] = index
    return SimpleNamespace(**fields)


def _trial_provider(tmp, transport, *, routing=None, replay_only=False, subdir="ws"):
    """A RoutedProvider recording into `<tmp>/<subdir>/transcripts` under the
    metrology task id (no `begin_task_evidence`: the trial binds the task)."""
    ws = Path(tmp) / subdir
    return P.RoutedProvider(
        routing or make_routing(), P.TranscriptStore(ws / "transcripts"),
        P.CostMeter(budget_per_task_usd=100.0), task_id="council-metrology",
        replay_only=replay_only, transports={"anthropic": transport, "openai_compat": transport},
    ), ws


class Phase4SeamTest(_SessionCase):
    def test_routed_provider_complete_dispatches_agentic_roles_only_with_trial_context(self):
        """`RoutedProvider.complete` runs a bounded session for a critic seat
        ONLY when the seat is in `AGENTIC_ROLES`, its `session:` block says
        `enabled: true` in the provider's agents document AND a trial or task
        context is active; every other call — the seat outside a context, a
        seat outside `AGENTIC_ROLES` under a context, or a seat under an
        explicit `enabled: false` rollback — is the one-shot path, byte-identical:
        the same request bytes, the same entry, no session row."""
        self.assertEqual(P.AGENTIC_ROLES, (POP, SHC))
        self.assertTrue(P.agentic_role_enabled(POP))
        self.assertTrue(P.agentic_role_enabled(SHC))
        self.assertFalse(P.agentic_role_enabled(AMB))
        disabled = _doc(population_adversary={"enabled": False}, shortcut_attacker={"enabled": False})
        with _agents(disabled):
            one_shot_payload = P.AnthropicBackend._payload(
                model="claude-sonnet-5", prompt="VIEW", max_tokens=1024, effort="medium",
                role_name=POP, schema_mode=True,
            )
            # The explicit rollback under a trial context: one-shot, byte-identical.
            with tempfile.TemporaryDirectory() as tmp:
                transport = FakeTransport([anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5")])
                provider, ws = _trial_provider(tmp, transport)
                provider.begin_trial(_trial_ctx(tmp))
                try:
                    text = provider.complete(POP, "VIEW")
                finally:
                    provider.end_trial()
                self.assertEqual(json.dumps(transport.calls[0][2]), json.dumps(one_shot_payload))
                self.assertEqual(text, P.normalized_text_for(POP, {"findings": [VALID_FINDING]}))
                (row,) = provider.exchange_evidence
                self.assertEqual((row["entry_schema"], row["attempt_count"], row["tool_call_count"]), (2, 1, 0))
        # A one-shot row inside a trial carries trial_nonce but no session
        # record; its wire bytes remain otherwise identical.
                self.assertEqual(row["trial_nonce"], "0123456789abcdef0123456789abcdef")
                self.assertNotIn("trial_index", row)
                self.assertFalse((ws / "transcripts" / "trajectories").exists())
                self.assertFalse((ws / "tool_raw").exists())
        enabled = _doc(population_adversary={"enabled": True}, shortcut_attacker={"enabled": False})
        with _agents(enabled):
            self.assertTrue(P.agentic_role_enabled(POP))
            self.assertFalse(P.agentic_role_enabled(SHC))
            self.assertFalse(P.agentic_role_enabled(AMB))
            # ENABLED but NO context: still the one-shot path (the same
            # bytes as the rollback config's, the prompt aside — the derived
            # prompt is the block's, see test_providers.py).
            with tempfile.TemporaryDirectory() as tmp:
                transport = FakeTransport([anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5")])
                provider, ws = _trial_provider(tmp, transport)
                text = provider.complete(POP, "VIEW")
                sent = transport.calls[0][2]
                self.assertEqual(sent["messages"], [{"role": "user", "content": "VIEW"}])
                self.assertNotIn("cache_control", json.dumps(sent))
                self.assertEqual(text, P.normalized_text_for(POP, {"findings": [VALID_FINDING]}))
                (row,) = provider.exchange_evidence
                self.assertEqual(row["entry_schema"], 2)
                self.assertFalse((ws / "transcripts" / "trajectories").exists())
            # ENABLED under a TRIAL context: a bounded session — one model
            # turn on the forced report_findings, the harness's
            # compile_proposal run fresh, the SoT T8 session row, the
            # content-addressed trajectory record, and the final normalized
            # findings text `run_council` parses as a one-shot answer.
            with tempfile.TemporaryDirectory() as tmp:
                transport = FakeTransport([anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5")])
                provider, ws = _trial_provider(tmp, transport)
                ctx = _trial_ctx(tmp, index=4)
                provider.begin_trial(ctx)
                self.assertIs(provider.active_trial, ctx)
                self.assertIsNone(provider.trial_executor.cache)
                try:
                    text = provider.complete(POP, "VIEW")
                finally:
                    provider.end_trial()
                self.assertIsNone(provider.active_trial)
                self.assertEqual(text, P.normalized_text_for(POP, {"findings": [VALID_FINDING]}))
                self.assertEqual(len(transport.calls), 1)
                sent = transport.calls[0][2]
                self.assertEqual(sent["tool_choice"], {"type": "tool", "name": P.FINDINGS_TOOL_NAME})
                self.assertEqual([t["name"] for t in sent["tools"]], [P.FINDINGS_TOOL_NAME])
                (row,) = provider.exchange_evidence
                self.assertEqual(row["entry_schema"], 3)
                self.assertEqual(row["terminal"], "SUBMITTED")
                self.assertEqual((row["model_call_count"], row["attempt_count"], row["correction_count"]), (1, 1, 0))
                self.assertEqual((row["tool_call_count"], row["refused_count"], row["nudge_count"]), (0, 0, 0))
                self.assertEqual(row["validator_run_count"], 1)
                self.assertEqual((row["terminal_count"], row["limit_stop_count"]), (1, 0))
                self.assertEqual((row["live_model_call_count"], row["stale_tool_result_count"], row["replayed"]), (1, 0, False))
                self.assertEqual((row["trial_nonce"], row["trial_index"]), (ctx.trial_nonce, 4))
                self.assertEqual(row["task_content_hash"], TASK.content_hash())
                self.assertEqual(row["finding_count"], 1)
                self.assertEqual(P.exchange_row_problems(row), [])
                # The same row the metrology summary reads (count invariants).
                normalized = _metrology._normalize_trajectory_row(0, row, allowed_roles=frozenset({POP}))
                self.assertEqual((normalized["validator_run_count"], normalized["limit_stopped"], normalized["policy_violation"]), (1, 0, 0))
                records = list((ws / "transcripts" / "trajectories" / POP).glob("*.json"))
                self.assertEqual(len(records), 1)
                record = json.loads(records[0].read_text())
                self.assertEqual(records[0].stem, record["trajectory_sha256"])
                self.assertEqual(record["trajectory_sha256"], row["trajectory_sha256"])
                self.assertEqual(record["session_sha256"], row["session_sha256"])
                self.assertEqual(_trajectory.verify_trajectory_record(record), row["trajectory_sha256"])
                self.assertEqual(record["trial_nonce"], ctx.trial_nonce)
                self.assertEqual([m["live"] for m in record["model_turns"]], [True])
                self.assertEqual([(v["name"], v["fresh"], v["code"]) for v in record["validator_turns"]],
                                 [("compile_proposal", True, "compiles")])
                # Through the byte-identical `run_council`: one `complete` per
                # seat, the parsed findings exactly as the one-shot answer.
                transport.responses.append(anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5"))
                provider.begin_trial(_trial_ctx(tmp, nonce="fedcba9876543210fedcba9876543210"))
                try:
                    findings = _council.run_council(TASK, provider, roles=(_council.CouncilRole.POPULATION_ADVERSARY,))
                finally:
                    provider.end_trial()
                self.assertEqual([f.summary for f in findings], [VALID_FINDING["summary"]])
                self.assertEqual(len(provider.exchange_evidence), 2)
            # ENABLED under a TASK context (the review stage's
            # `begin_task_evidence(..., task=)`): the same session.
            with tempfile.TemporaryDirectory() as tmp:
                transport = FakeTransport([anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5")])
                provider, ws = _trial_provider(tmp, transport)
                provider.begin_task_evidence(TASK.task_id, TASK.content_hash(), task=TASK)
                text = provider.complete(POP, "VIEW")
                self.assertEqual(text, P.normalized_text_for(POP, {"findings": [VALID_FINDING]}))
                (row,) = provider.exchange_evidence
                self.assertEqual((row["entry_schema"], row["validator_run_count"]), (3, 1))
                self.assertNotIn("trial_nonce", row)
                self.assertEqual(row["trajectory_sha256"], row["session_sha256"] if "session_sha256" in row else row["trajectory_sha256"])
                self.assertTrue(list((ws / "transcripts" / "trajectories" / POP).glob("*.json")))
                # A seat outside AGENTIC_ROLES under the same context: one-shot.
                transport.responses.append(anthropic_tool_response([], model="claude-sonnet-5"))
                provider.complete(AMB, "VIEW")
                self.assertEqual(provider.exchange_evidence[-1]["entry_schema"], 2)
                # The identity-only `begin_task_evidence` (every existing
                # caller and double) opens no context: one-shot.
                provider.begin_task_evidence(TASK.task_id, TASK.content_hash())
                transport.responses.append(anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5"))
                provider.complete(POP, "VIEW 2")
                self.assertEqual(provider.exchange_evidence[-1]["entry_schema"], 2)

    def test_one_shot_seat_row_under_a_trial_states_the_trial_it_was_made_in(self):
        """SoT T8 (`trial_nonce?`, `trial_index?`; metrology redesign §14):
        under a metrology trial EVERY evidence row states the trial it was
        made in — a ONE-SHOT seat's (the ambiguity critic never dispatches a
        session) as much as a session's, live or replayed — so a row is
        attributable to its trial without counting rows. Outside a trial the
        one-shot row is byte-identical to Phase 0's (no trial key), and
        neither key enters the trajectory manifest digest, which is why the
        manifest is the same at any worker count (OQ-20)."""
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport([anthropic_tool_response([], model="claude-sonnet-5")])
            provider, ws = _trial_provider(tmp, transport)
            ctx = _trial_ctx(tmp, nonce="fedcba9876543210fedcba9876543210", index=4)
            provider.begin_trial(ctx)
            try:
                provider.complete(AMB, "VIEW")
            finally:
                provider.end_trial()
            (row,) = provider.exchange_evidence
            self.assertEqual((row["entry_schema"], row["attempt_count"], row["validator_run_count"]), (2, 1, 0))
            self.assertEqual((row["trial_nonce"], row["trial_index"]), (ctx.trial_nonce, 4))
            self.assertEqual(row["task_content_hash"], TASK.content_hash())
            self.assertEqual(P.exchange_row_problems(row), [])
            self.assertFalse((ws / "transcripts" / "trajectories").exists())
            # Replayed from the same store under ANOTHER trial: the same
            # one-shot row, stamped with that trial.
            replay, _ = _trial_provider(tmp, FakeTransport([]), replay_only=True)
            other = _trial_ctx(tmp, nonce="0011223344556677889900aabbccddee", index=9)
            replay.begin_trial(other)
            try:
                replay.complete(AMB, "VIEW")
            finally:
                replay.end_trial()
            (replayed,) = replay.exchange_evidence
            self.assertTrue(replayed["replayed"])
            self.assertEqual((replayed["trial_nonce"], replayed["trial_index"]), (other.trial_nonce, 9))
            # Outside a trial: the Phase 0 row, no trial key at all.
            transport.responses.append(anthropic_tool_response([], model="claude-sonnet-5"))
            provider.complete(AMB, "VIEW 2")
            outside = provider.exchange_evidence[-1]
            self.assertNotIn("trial_nonce", outside)
            self.assertNotIn("trial_index", outside)
            self.assertEqual(set(row) - set(outside), {"trial_nonce", "trial_index"})
            # The manifest digest ignores both keys (OQ-20).
            stripped = {k: v for k, v in row.items() if k not in ("trial_nonce", "trial_index")}

            def digest(rows):
                return _metrology.summarize_fresh_live_trajectories(
                    rows, expected_by_role={AMB: 1}
                ).trajectory_manifest_sha256

            self.assertEqual(digest([row]), digest([stripped]))

    def test_the_council_entry_points_match_their_reviewed_source(self):
        """Pin the reviewed council entry points by their source digest.

        `_parse_findings` was re-pinned for R02 after a read of the current
        source: its only change passes the provider's `disposition` from an
        item the closed schema has already validated. It still independently
        enforces that schema at its consumer boundary. `run_council` and
        `screen_findings` were re-pinned after a read of the current source,
        which holds every property this test names and adds none of its own:

          * `run_council` computes `leak_findings` BEFORE the first
            `provider.complete`, and a FATAL leak returns without any provider
            call at all, so a leaking `solver_prompt` never reaches four
            external endpoints. That short circuit is a strengthening; nothing
            in the loop was relaxed to get it.
          * `screen_findings` keeps the documented order of decisions —
            CODE-origin findings bypass screening untouched, a FATAL provider
            finding is voided only by an explicit nullifying signal, and
            duplicate detection runs over a finding_id ordering, so it stays
            deterministic. It supplies mart cardinality to the screen through
            `multi_mart`.
          * neither function dispatches a session; `findings_from_session`
            remains ADDED beside them, which the assertions below still hold.

        A different digest here means one of those two functions was edited
        again: read it, satisfy yourself that the properties above survive,
        and re-pin deliberately. Never update the literal to quiet the test.
        """
        pinned = {
            "run_council": "38290ea1e60c37157c7a26c7010855ad49853f66952ccbba0b2cb10c7d4cc506",
            "_parse_findings": "dd946f5ea31fc3b9180f37e8a0e34031718ed5b07f4701a57a165ac5bdeb1d8c",
            "screen_findings": "cd518323c958a41e3d7998f2595e49988b74e9543bbfa5345322d41cccea5c8b",
        }
        for name, digest in pinned.items():
            with self.subTest(function=name):
                source = _inspect.getsource(getattr(_council, name))
                self.assertEqual(_hashlib.sha256(source.encode("utf-8")).hexdigest(), digest)
        self.assertIn(
            "validate_payload_for(role.value, wire_data)",
            _inspect.getsource(_council._parse_findings),
        )
        source = _inspect.getsource(_council.run_council)
        self.assertIn("provider.complete(role, view)", source)
        self.assertIn("if any(f.severity is Severity.FATAL for f in findings):", source)
        self.assertNotIn("run_session", source)
        self.assertNotIn("findings_from_session", source)
        self.assertTrue(callable(_council.findings_from_session))
        # The leak screen runs before the transport, not after it.
        self.assertLess(
            source.index("leak_findings"),
            source.index("provider.complete"),
            "a leaking solver_prompt would reach the providers before the screen",
        )

    def test_policy_violation_under_trial_context_is_a_seat_outcome_not_exit_2(self):
        """SoT T4 POLICY_VIOLATION, metrology column: under a trial context a
        `SessionPolicyViolation` (here the seat naming another seat's
        registered validator, `tool_not_permitted`) is caught inside
        `complete` and answered as an EMPTY findings list with the row's
        terminal POLICY_VIOLATION — a scored seat outcome the policy-violation
        ratio counts, never a raise the CLI would turn into exit 2. The same
        violation under a TASK context still halts (no round, the task not
        rejected); every harness fault propagates under both."""
        violation = tool_use_body("compile_probe", {"finding_index": 0}, model="claude-sonnet-5")
        with _agents(_doc(population_adversary={"enabled": True})):
            with tempfile.TemporaryDirectory() as tmp:
                provider, ws = _trial_provider(tmp, FakeTransport([violation]))
                ctx = _trial_ctx(tmp, index=2)
                provider.begin_trial(ctx)
                try:
                    text = provider.complete(POP, "VIEW")
                finally:
                    provider.end_trial()
                self.assertEqual(text, P.normalized_text_for(POP, {"findings": []}))
                (row,) = provider.exchange_evidence
                self.assertEqual(row["terminal"], "POLICY_VIOLATION")
                self.assertEqual((row["model_call_count"], row["terminal_count"], row["limit_stop_count"]), (1, 1, 0))
                self.assertEqual((row["finding_count"], row["zero_findings"]), (0, True))
                self.assertEqual(P.exchange_row_problems(row), [])
                normalized = _metrology._normalize_trajectory_row(0, row, allowed_roles=frozenset({POP}))
                self.assertEqual(normalized["policy_violation"], 1)
                self.assertIn("POLICY_VIOLATION", _metrology.SCORED_TERMINALS)
                (record_path,) = list((ws / "transcripts" / "trajectories" / POP).glob("*.json"))
                record = json.loads(record_path.read_text())
                self.assertEqual((record["terminal"], record["stop_reason"]), ("POLICY_VIOLATION", "policy_violation"))
                self.assertEqual(record["fault"]["exception_type"], "SessionPolicyViolation")
                self.assertEqual(_trajectory.verify_trajectory_record(record), row["trajectory_sha256"])
                # Through run_council: no findings, no raise.
                provider2, _ = _trial_provider(tmp, FakeTransport([violation]))
                provider2.begin_trial(_trial_ctx(tmp, nonce="fedcba9876543210fedcba9876543210"))
                try:
                    findings = _council.run_council(TASK, provider2, roles=(_council.CouncilRole.POPULATION_ADVERSARY,))
                finally:
                    provider2.end_trial()
                self.assertEqual(findings, [])
                # Under a TASK context the violation HALTS (SoT T4 council column).
                provider3, _ = _trial_provider(tmp, FakeTransport([violation]), subdir="task")
                provider3.begin_task_evidence(TASK.task_id, TASK.content_hash(), task=TASK)
                with self.assertRaises(S.SessionPolicyViolation):
                    provider3.complete(POP, "VIEW")
                self.assertEqual(provider3.exchange_evidence, [])
                # A harness fault under a trial context propagates (C7).
                provider4, _ = _trial_provider(
                    tmp, FakeTransport([anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5")]), subdir="fault"
                )
                provider4.begin_trial(_trial_ctx(tmp, nonce="1111111111111111"))
                crashing = mock.patch.object(
                    P.TrialToolExecutor, "run", side_effect=S.ToolHarnessFault("compile_proposal", cause_type="RuntimeError")
                )
                try:
                    with crashing, self.assertRaises(S.ToolHarnessFault):
                        provider4.complete(POP, "VIEW")
                finally:
                    provider4.end_trial()
                self.assertEqual(provider4.exchange_evidence, [])

    def test_raw_tool_output_never_in_transcript_store(self):
        """The raw validator output tier (`<ws>/tool_raw/<trajectory_sha256>/
        <turn_index>.bin`) sits OUTSIDE the transcript store: no file under
        `<ws>/transcripts` — the turn entries, the session record, the
        content-addressed trajectory record — carries the raw bytes; the
        record names their digest only. The record is content-addressed by
        the trial-bound `trajectory_sha256`, verifies, and is append-only."""
        with _agents(_doc(population_adversary={"enabled": True})):
            with tempfile.TemporaryDirectory() as tmp:
                payload = [_critic_finding(proposed=_proposal())]
                transport = FakeTransport([anthropic_tool_response(payload, model="claude-sonnet-5")])
                provider, ws = _trial_provider(tmp, transport)
                provider.begin_trial(_trial_ctx(tmp))
                try:
                    provider.complete(POP, "VIEW")
                    (run,) = provider.trial_executor.runs
                finally:
                    provider.end_trial()
                self.assertTrue(run.raw_output)
                self.assertEqual(run.raw_output_sha256, sha256_hex(run.raw_output.decode("utf-8")))
                (row,) = provider.exchange_evidence
                digest = row["trajectory_sha256"]
                record_path = ws / "transcripts" / "trajectories" / POP / f"{digest}.json"
                record = json.loads(record_path.read_text())
                (validator_turn,) = record["validator_turns"]
                # The validator turn precedes the terminal model turn in the
                # chain (the runner records the model turn once PARSE decided).
                turn_index = int(validator_turn["index"])
                raw_files = sorted((ws / "tool_raw").rglob("*.bin"))
                self.assertEqual([str(p.relative_to(ws)) for p in raw_files], [f"tool_raw/{digest}/{turn_index}.bin"])
                self.assertEqual(raw_files[0].read_bytes(), run.raw_output)
                self.assertEqual(P.tool_raw_root(ws / "transcripts"), ws / "tool_raw")
                self.assertNotIn(ws / "transcripts", (ws / "tool_raw").parents)
                store_files = sorted(p for p in (ws / "transcripts").rglob("*") if p.is_file())
                self.assertTrue(store_files)
                raw_text = run.raw_output.decode("utf-8")
                for path in store_files:
                    with self.subTest(file=str(path.relative_to(ws))):
                        self.assertNotIn(raw_text, path.read_text(encoding="utf-8"))
                        self.assertTrue(path.suffix == ".json")
                self.assertTrue(record_path.is_file())
                self.assertEqual(validator_turn["raw_output_sha256"], run.raw_output_sha256)
                self.assertEqual(validator_turn["observation_sha256"], run.observation_sha256)
                self.assertEqual([t["kind"] for t in record["turns"]][turn_index], "validator")
                self.assertEqual(_trajectory.verify_trajectory_record(record), digest)
                # The `<role>/*.json` walkers of the store see entries only.
                self.assertEqual(
                    sorted(p.name for p in (ws / "transcripts" / POP).glob("*.json")),
                    sorted(p.name for p in (ws / "transcripts" / POP).iterdir() if p.suffix == ".json"),
                )
                self.assertTrue(P.transcripts_present(ws / "transcripts"))
                # Append-only: the same session again (served from the store,
                # the same digests) leaves the record byte-identical.
                before = record_path.read_bytes()
                provider.begin_trial(_trial_ctx(tmp))
                try:
                    provider.complete(POP, "VIEW")
                finally:
                    provider.end_trial()
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(record_path.read_bytes(), before)
                self.assertEqual(provider.exchange_evidence[-1]["trajectory_sha256"], digest)

    def test_retry_recovers_trajectory_published_before_raw_and_session_records(self):
        """A crash after the immutable trajectory commit is recoverable.

        The model turn was already memoized and the trajectory is already
        visible, but neither the raw validator output nor the mutable session
        index was committed.  Re-running the same trial must validate and keep
        the first trajectory, recompute/publish its raw binding, then publish
        the session index without spending another model call.
        """
        with _agents(_doc(population_adversary={"enabled": True})):
            with tempfile.TemporaryDirectory() as tmp:
                payload = [_critic_finding(proposed=_proposal())]
                transport = FakeTransport([
                    anthropic_tool_response(payload, model="claude-sonnet-5")
                ])
                provider, ws = _trial_provider(tmp, transport)
                ctx = _trial_ctx(tmp)

                provider.begin_trial(ctx)
                try:
                    with (
                        mock.patch.object(
                            P,
                            "_publish_raw_output",
                            side_effect=OSError("simulated crash before raw commit"),
                        ),
                        self.assertRaises(OSError),
                    ):
                        provider.complete(POP, "VIEW")
                finally:
                    provider.end_trial()

                (record_path,) = list(
                    (ws / "transcripts" / "trajectories" / POP).glob("*.json")
                )
                before = record_path.read_bytes()
                record = json.loads(before)
                digest = record["trajectory_sha256"]
                self.assertFalse(list((ws / "tool_raw").rglob("*.bin")))
                self.assertFalse(
                    list((ws / "transcripts" / POP / "sessions").glob("*.json"))
                )

                provider.begin_trial(ctx)
                try:
                    provider.complete(POP, "VIEW")
                finally:
                    provider.end_trial()

                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(record_path.read_bytes(), before)
                recovered = provider.exchange_evidence[-1]
                self.assertEqual(recovered["trajectory_sha256"], digest)
                (validator_turn,) = record["validator_turns"]
                raw_path = (
                    ws
                    / "tool_raw"
                    / digest
                    / f"{int(validator_turn['index'])}.bin"
                )
                self.assertEqual(
                    _hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                    validator_turn["raw_output_sha256"],
                )
                session = provider.store.lookup_session(
                    POP, recovered["prompt_sha256"]
                )
                self.assertIsNotNone(session)
                self.assertEqual(session["trajectory_sha256"], digest)

    def test_trial_executor_is_fresh_per_trial_and_closed_at_end_trial(self):
        """`begin_trial` builds ONE `TrialToolExecutor` with `cache=None`;
        `end_trial` closes it (a run through it is then a harness fault) and
        the next trial gets a fresh one with an empty log; every outcome the
        executor answers is `fresh=True` (the `ToolExecutor` protocol)."""
        with _agents(_doc(population_adversary={"enabled": True})):
            with tempfile.TemporaryDirectory() as tmp:
                transport = FakeTransport([
                    anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5"),
                    anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5"),
                ])
                provider, ws = _trial_provider(tmp, transport)
                with self.assertRaises(S.ToolHarnessFault):
                    provider.begin_trial(SimpleNamespace(trial_nonce="", workspace=None, task=TASK))
                provider.begin_trial(_trial_ctx(tmp))
                first = provider.trial_executor
                self.assertIsNone(first.cache)
                self.assertEqual(first.runs, [])
                with self.assertRaises(S.ToolHarnessFault):
                    provider.begin_trial(_trial_ctx(tmp))
                provider.complete(POP, "VIEW")
                self.assertEqual(len(first.runs), 1)
                session = _cv.CriticSession(task=TASK, role=POP)
                session.install_payload({"findings": [VALID_FINDING]})
                outcome = first.execute(POP, "compile_proposal", {"findings": [VALID_FINDING]}, session.context(Path(tmp)))
                self.assertIsInstance(outcome, S.ToolOutcome)
                self.assertTrue(outcome.fresh)
                self.assertEqual(outcome.code, "compiles")
                self.assertEqual(len(first.runs), 2)
                provider.end_trial()
                provider.end_trial()  # idempotent
                self.assertTrue(first.closed)
                with self.assertRaises(S.ToolHarnessFault) as ctx:
                    first.execute(POP, "compile_proposal", {"findings": []}, session.context(Path(tmp)))
                self.assertEqual(ctx.exception.code, "executor_closed")
                provider.begin_trial(_trial_ctx(tmp, nonce="fedcba9876543210fedcba9876543210"))
                second = provider.trial_executor
                self.assertIsNot(second, first)
                self.assertEqual(second.runs, [])
                self.assertIsNone(second.cache)
                provider.end_trial()
                self.assertTrue(second.closed)

    def test_critic_session_turns_carry_cache_control_only_when_enabled(self):
        """OQ-14 (bundled with the re-earn): the two `cache_control`
        breakpoints reach a critic payload only on its SESSION turns while
        its block is enabled; the one-shot payload of the shipped config, and
        of the enabled seat outside a context, carries none."""
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                doc = _doc(population_adversary={"enabled": enabled})
                with _agents(doc), tempfile.TemporaryDirectory() as tmp:
                    transport = FakeTransport([anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5")])
                    provider, _ = _trial_provider(tmp, transport)
                    provider.begin_trial(_trial_ctx(tmp))
                    try:
                        provider.complete(POP, "VIEW")
                    finally:
                        provider.end_trial()
                    sent = transport.calls[0][2]
                    if enabled:
                        self.assertEqual(sent["system"][0]["cache_control"], {"type": "ephemeral"})
                        self.assertEqual(sent["messages"][0]["content"][0]["cache_control"], {"type": "ephemeral"})
                    else:
                        self.assertNotIn("cache_control", json.dumps(sent))
                        self.assertIsInstance(sent["system"], str)

    def test_limit_stopped_critic_session_answers_no_findings(self):
        """SoT T4 metrology column: a `LIMIT_*` stop with no validator-green
        draft is a seat outcome scored as NO findings — `complete` answers
        the empty findings text, the row's terminal is the limit, and the
        metrology row reader counts it limit-stopped, never faulted."""
        stray = tool_use_body("inspect_public_schema", {"table": "orders"}, model="claude-sonnet-5")
        with _agents(_doc(population_adversary={"enabled": True})):
            with tempfile.TemporaryDirectory() as tmp:
                provider, _ = _trial_provider(tmp, FakeTransport([stray, stray, stray]))
                provider.begin_trial(_trial_ctx(tmp))
                try:
                    text = provider.complete(POP, "VIEW")
                finally:
                    provider.end_trial()
                self.assertEqual(text, P.normalized_text_for(POP, {"findings": []}))
                (row,) = provider.exchange_evidence
                self.assertEqual(row["terminal"], "LIMIT_TURNS")
                self.assertEqual((row["model_call_count"], row["correction_count"], row["limit_stop_count"]), (3, 2, 1))
                self.assertEqual(P.exchange_row_problems(row), [])
                normalized = _metrology._normalize_trajectory_row(0, row, allowed_roles=frozenset({POP}))
                self.assertEqual((normalized["limit_stopped"], normalized["policy_violation"]), (1, 0))

    def test_limit_stopped_task_critic_is_incomplete_protocol_not_clean(self):
        """Only a metrology trial may score a missing final as no finding."""
        stray = tool_use_body(
            "inspect_public_schema",
            {"table": "orders"},
            model="claude-sonnet-5",
        )
        with _agents(_doc(population_adversary={"enabled": True})):
            with tempfile.TemporaryDirectory() as tmp:
                transport = FakeTransport([stray, stray, stray])
                provider = _provider(tmp, transport)
                provider.begin_task_evidence(
                    TASK.task_id,
                    TASK.content_hash(),
                    task=TASK,
                )
                with self.assertRaises(_council.ProviderProtocolError) as caught:
                    provider.complete(POP, "VIEW")
                self.assertIn(
                    "ended without a schema-valid submission",
                    str(caught.exception),
                )
                self.assertEqual(len(transport.calls), 3)
                self.assertEqual(provider.exchange_evidence, [])

    def test_harness_invalid_proposal_is_voided_never_a_run_abort(self):
        """A red executable handoff never becomes a silent non-finding — and
        never a run abort (batch-repair D4, 2026-09-09).

        After the bounded compile correction is exhausted, `run_council`
        PROCEEDS: the still-red proposal is VOIDed by the harness
        (`critic_validators.void_uncompilable_proposals`: severity INFO, no
        attack, no case, the words kept), the seat's evidence row counts the
        red submission, and no `ProviderProtocolError` leaves the seat
        (raising here once ended a 152-exchange metrology run over ONE
        finding).  A subsequent independent green payload still travels
        byte-identically.
        """

        red = [_critic_finding(proposed=_proposal(params={"bogus": 1}))]
        with _agents(_doc(population_adversary={"enabled": True})):
            with tempfile.TemporaryDirectory() as tmp:
                transport = FakeTransport([
                    anthropic_tool_response(red, model="claude-sonnet-5"),
                    anthropic_tool_response(red, model="claude-sonnet-5"),
                ])
                provider, ws = _trial_provider(tmp, transport)
                provider.begin_trial(_trial_ctx(tmp))
                try:
                    findings = _council.run_council(
                        TASK,
                        provider,
                        roles=(_council.CouncilRole.POPULATION_ADVERSARY,),
                    )
                finally:
                    provider.end_trial()
                self.assertEqual(len(transport.calls), 2)
                correction = transport.calls[1][2]["messages"][-1]["content"][0]
                self.assertEqual(correction["type"], "tool_result")
                self.assertTrue(correction["content"].startswith("[compile] uncompilable"))
                # The voided finding reads as no finding downstream: INFO, no
                # executable content, its words verbatim.
                (voided,) = findings
                self.assertIs(voided.severity, Severity.INFO)
                self.assertIsNone(voided.proposed_case)
                self.assertIsNone(voided.suggested_attack)
                self.assertEqual(voided.summary, red[0]["summary"])
                (row,) = provider.exchange_evidence
                self.assertEqual(row["submitted_with_red_validators"], 1)
                self.assertEqual(row["correction_kinds"], {"schema": 0, "compile": 1})
                self.assertEqual(row["finding_count"], 1)
                self.assertEqual(P.exchange_row_problems(row), [])
                # A green payload: the returned text IS the runner's final.
                green = [_critic_finding(proposed=_proposal())]
                transport.responses.append(anthropic_tool_response(green, model="claude-sonnet-5"))
                provider.begin_trial(_trial_ctx(tmp, nonce="fedcba9876543210fedcba9876543210"))
                try:
                    text = provider.complete(POP, "VIEW 2")
                finally:
                    provider.end_trial()
                self.assertEqual(text, P.normalized_text_for(POP, {"findings": green}))
                self.assertEqual(provider.exchange_evidence[-1]["submitted_with_red_validators"], 0)
                self.assertEqual(len(_council._parse_findings(_council.CouncilRole.POPULATION_ADVERSARY, text, "x")), 1)
                self.assertIsNotNone(_council._parse_findings(_council.CouncilRole.POPULATION_ADVERSARY, text, "x")[0].proposed_case)


class RepairPassSeamTest(_SessionCase):
    """The Phase 4/5 repair pass: findings p4-0-3 / p4-2-4 and p4-2-2."""

    def test_trajectory_and_raw_tier_hold_only_this_seats_runs(self):
        """findings p4-0-3 / p4-2-4: one `TrialToolExecutor` is shared by
        EVERY seat of a trial, and the trajectory record and the `tool_raw/`
        tier read `executor.runs` whole.

        The unmatched-run loop then wrote the FIRST seat's unsanitized
        validator bytes into the SECOND seat's `tool_raw/<trajectory_sha256>/`
        as `unmatched-<i>.bin`, so the raw audit tier misattributed one seat's
        bytes to another seat's trajectory, monotonically in seat order (which
        also made the tier non-reproducible across a re-ordered council). Both
        builders now see only the runs of the session being recorded.
        """
        both = _doc(
            population_adversary={"enabled": True}, shortcut_attacker={"enabled": True}
        )
        with _agents(both):
            self.assertTrue(P.agentic_role_enabled(POP))
            self.assertTrue(P.agentic_role_enabled(SHC))
            with tempfile.TemporaryDirectory() as tmp:
                transport = FakeTransport(
                    [
                        anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5"),
                        anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5"),
                    ]
                )
                provider, ws = _trial_provider(tmp, transport)
                ctx = _trial_ctx(tmp, index=0)
                provider.begin_trial(ctx)
                try:
                    executor = provider.trial_executor
                    provider.complete(POP, "VIEW")
                    provider.complete(SHC, "VIEW")
                    # ONE executor, both seats' runs in one log.
                    self.assertGreaterEqual(len(executor.runs), 2)
                    self.assertEqual(
                        sorted({run.role for run in executor.runs}), sorted([POP, SHC])
                    )
                finally:
                    provider.end_trial()
                rows = {row["role"]: row for row in provider.exchange_evidence}
                self.assertEqual(sorted(rows), sorted([POP, SHC]))
                for role, validator in ((POP, "compile_proposal"), (SHC, "compile_probe")):
                    with self.subTest(role=role):
                        digest = rows[role]["trajectory_sha256"]
                        raw_dir = ws / "tool_raw" / digest
                        self.assertTrue(raw_dir.is_dir())
                        names = sorted(path.name for path in raw_dir.iterdir())
                        # No `unmatched-*.bin`: nothing but this seat's runs
                        # ever reaches this seat's address.
                        self.assertTrue(
                            all(not name.startswith("unmatched-") for name in names),
                            names,
                        )
                        (record_path,) = list(
                            (ws / "transcripts" / "trajectories" / role).glob("*.json")
                        )
                        record = json.loads(record_path.read_text())
                        self.assertEqual(
                            [turn["name"] for turn in record["validator_turns"]],
                            [validator],
                        )
                        self.assertEqual(len(names), len(record["validator_turns"]))

    def test_private_probe_in_a_tool_argument_is_refused_and_counted(self):
        """finding p4-2-2: `private_probe_tokens` had NO production caller.

        The blocking zero-tolerance bar `private_probe_count <= 0` read the 0
        default on every real run: `RoutedProvider._append_session_evidence`
        wrote no such key, and `TrialToolExecutor` never inspected an
        argument, so only the test double produced the counter. The executor
        now refuses an identifier-typed argument naming a private surface (or
        another trial's nonce), counts it, and the count reaches the SoT T8
        evidence row the admission bar re-derives from.
        """
        import elt_taskgen.review.metrology as M

        self.assertEqual(
            M.private_probe_tokens(
                {"field": "populations.0.literal_rows", "path": "private/answer_key.duckdb"}
            ),
            2,
        )
        with _agents(_doc(population_adversary={"enabled": True})):
            with tempfile.TemporaryDirectory() as tmp:
                transport = FakeTransport(
                    [anthropic_tool_response([VALID_FINDING], model="claude-sonnet-5")]
                )
                provider, ws = _trial_provider(tmp, transport)
                ctx = _trial_ctx(tmp, index=1)
                provider.begin_trial(ctx)
                try:
                    executor = provider.trial_executor
                    self.assertEqual(executor.private_probe_count, 0)
                    # The `ToolExecutor.execute` half answers the refusal as an
                    # OUTCOME, so a metrology loop scores it and continues.
                    outcome = executor.execute(
                        POP,
                        "compile_proposal",
                        {"finding_index": 0, "path": "answer_key/gold.json"},
                        _cv.CriticSession(task=TASK, role=POP).context(ctx.workspace),
                    )
                    self.assertEqual(outcome.code, S.PRIVATE_PROBE_CODE)
                    self.assertFalse(outcome.ok)
                    self.assertEqual(executor.private_probe_count, 1)
                finally:
                    provider.end_trial()

                # ...and on the PRODUCTION path: a submitted payload naming a
                # private surface is refused by the executor when the declared
                # harness validator runs on it, and the count reaches the row.
                probing = {
                    **VALID_FINDING,
                    "detail": VALID_FINDING["detail"]
                    + " (see populations.0.literal_rows)",
                }
                transport2 = FakeTransport(
                    [anthropic_tool_response([probing], model="claude-sonnet-5")]
                )
                provider2, ws2 = _trial_provider(tmp, transport2, subdir="probe")
                provider2.begin_trial(_trial_ctx(tmp, nonce="2222222222222222", index=2))
                try:
                    text = provider2.complete(POP, "VIEW")
                finally:
                    provider2.end_trial()
                self.assertEqual(text, P.normalized_text_for(POP, {"findings": []}))
                (row,) = provider2.exchange_evidence
                self.assertEqual(row["terminal"], "POLICY_VIOLATION")
                self.assertEqual(row["private_probe_count"], 1)
                self.assertIn("duplicate_tool_calls", row)
                summary = _metrology._trajectory_summary([row])
                self.assertEqual(summary.private_probe_count, 1)

    def test_a_foreign_trial_nonce_in_a_tool_argument_is_a_private_probe(self):
        """The second half of the same rule (metrology redesign §8.2): an
        argument naming ANOTHER trial's nonce is a probe of that trial."""
        foreign = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        executor = P.TrialToolExecutor(
            trial_nonce="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", foreign_nonces=(foreign,)
        )
        with self.assertRaises(S.ForbiddenArgument) as raised:
            executor.run(
                _cv.COMPILE_PROPOSAL_TOOL,
                _cv.CriticSession(task=TASK, role=POP).context(Path(".")),
                {"note": f"see {foreign}"},
                deadline_s=1.0,
            )
        self.assertEqual(raised.exception.detail, S.PRIVATE_PROBE_CODE)
        self.assertEqual(executor.private_probe_count, 1)

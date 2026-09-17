"""Tests for review/providers.py — the real multi-agent provider layer.

WHY THIS EXISTS
The provider layer must be buildable and testable with NO API keys. Tests
double the HTTP TRANSPORT with recorded canned API responses (Messages API /
chat-completions wire shapes) — this is transport-level test doubling, not a
revived mock provider: every line of provider logic (schema enforcement,
retries, recording, memoization, budgets, routing) runs for real.

Fail-closed contracts under test:
  * schema violations retry twice then raise ProviderProtocolError;
  * replay-only mode raises TranscriptMissingError on a missing transcript;
  * memoization: the second identical call performs ZERO HTTP;
  * budget breach raises BudgetExceededError (the paid call stays recorded);
  * agents.yaml routing resolves per role with env interpolation;
  * missing key + no transcript = clean MissingCredentialsError with
    instructions (never a silent skip).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from elt_taskgen.models import CouncilRole, sha256_hex
from elt_taskgen.review import providers as P
from elt_taskgen.review import session as S
from elt_taskgen.review.council import ProviderProtocolError


# ---------------------------------------------------------------------------
# Canned wire-shape responses (what the real APIs return over HTTP)
# ---------------------------------------------------------------------------

VALID_FINDING = {
    "severity": "major",
    "summary": "hard-coding the development outputs must score zero elsewhere",
    "detail": "compile a constants mutant and require reward loss",
    "route_hint": None,
    "suggested_attack": "constants",
    "proposed_case": {
        "kind": "constants",
        "params": "{}",
        "expected_pass_by_stage": {
            "extract_load": {
                name: True
                for name in (
                    "development", "primary", "resampled",
                    "counterfactual", "stress",
                )
            },
            "transform": {
                name: False
                for name in (
                    "development", "primary", "resampled",
                    "counterfactual", "stress",
                )
            },
        },
        "rationale": "a constant-output shortcut should lose transform reward",
    },
}


def anthropic_tool_response(findings, *, input_tokens=1000, output_tokens=200,
                            model="claude-sonnet-5"):
    return {
        "model": model,
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": P.FINDINGS_TOOL_NAME,
                "input": {"findings": findings},
            }
        ],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def anthropic_bad_tool_response():
    """Schema violation: severity outside the enum."""
    return anthropic_tool_response(
        [{"severity": "catastrophic", "summary": "s", "detail": "d",
          "route_hint": None, "suggested_attack": None}]
    )


def anthropic_text_response(text, *, input_tokens=500, output_tokens=300,
                            model="claude-opus-5"):
    return {
        "model": model,
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def openai_tool_response(findings, *, prompt_tokens=800, completion_tokens=150,
                         model="test-oss-model", call_id="call_01"):
    return {
        "model": model,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    # Real endpoints always carry an id, and the schema-retry
                    # payload MUST answer it with a role:"tool" message.
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": P.FINDINGS_TOOL_NAME,
                                "arguments": json.dumps({"findings": findings}),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def openai_text_response(text, *, prompt_tokens=800, completion_tokens=150):
    return {
        "model": "test-oss-model",
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


class HttpTransportRetryTest(unittest.TestCase):
    def test_connection_reset_is_retried_with_bounded_backoff(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        with mock.patch.object(
            P.urllib.request,
            "urlopen",
            side_effect=[ConnectionResetError("peer reset"), response],
        ) as opened, mock.patch.object(P.time, "sleep") as sleep:
            result = P._http_post_json("https://api.example/v1", {}, {"x": 1})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(opened.call_count, 2)
        sleep.assert_called_once_with(P._HTTP_BACKOFF_BASE_SECONDS)

    @unittest.skipUnless(
        hasattr(P.signal, "SIGALRM") and hasattr(P.signal, "setitimer"),
        "requires a POSIX interval timer",
    )
    def test_one_shot_wall_interrupts_a_blocking_transport(self):
        started = time.monotonic()
        with self.assertRaises(P.ProviderFault) as ctx:
            P._call_with_wall_deadline(
                lambda: time.sleep(1.0),
                deadline_s=0.05,
                role_name="ambiguity_critic",
            )

        self.assertEqual(ctx.exception.code, "wall_deadline_exceeded")
        self.assertLess(time.monotonic() - started, 0.5)


class FakeTransport:
    """Transport double: canned HTTP responses, call recording, no network."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url, headers, payload):
        self.calls.append((url, dict(headers), json.loads(json.dumps(payload))))
        if not self.responses:
            raise AssertionError("unexpected HTTP call (transport exhausted)")
        return self.responses.pop(0)


def make_routing(
    *, api_key="sk-test", oss_base="", oss_key="", oss_model="",
    oss_rates=(0.70, 3.50),
):
    """Test routing. `oss_rates=None` leaves the openai_compat route UNPRICED,
    which rates_for now refuses (see OpenAICompatPricingFailsClosedTest)."""
    roles = {
        name: P.RoleRoute(name, "anthropic", model, 1024, effort)
        for name, model, effort in (
            ("semantic_author", "claude-opus-5", "high"),
            ("ambiguity_critic", "claude-sonnet-5", "medium"),
            ("population_adversary", "claude-sonnet-5", "medium"),
            ("shortcut_attacker", "claude-opus-5", "high"),
            ("feasibility_reviewer", "claude-haiku-4-5", None),
        )
    }
    roles["independent_implementer"] = P.RoleRoute(
        "independent_implementer", "openai_compat", oss_model or "test-oss-model",
        2048, None,
    )
    oss_config = {"base_url": oss_base, "api_key": oss_key, "model": oss_model}
    if oss_rates is not None:
        oss_config["usd_per_mtok_input"] = oss_rates[0]
        oss_config["usd_per_mtok_output"] = oss_rates[1]
    return P.RoleRouting(
        roles=roles,
        provider_config={
            "anthropic": {"api_key": api_key},
            "openai_compat": oss_config,
        },
        source="(test routing)",
    )


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------

class AnthropicBackendTest(unittest.TestCase):
    def test_missing_key_raises_missing_credentials(self):
        with self.assertRaises(P.MissingCredentialsError):
            P.AnthropicBackend("")

    def test_workspace_id_header_is_sent_only_when_configured(self):
        """An API key that is not scoped to a workspace must name one per
        request (`anthropic-workspace-id`) or the API answers HTTP 400; a
        scoped key must NOT send the header. It travels from
        `providers.anthropic.workspace_id`, is trimmed, and reaches both wire
        call sites."""
        for configured, expected in (("", None), ("  wrkspc_01abc  ", "wrkspc_01abc")):
            with self.subTest(configured=configured):
                transport = FakeTransport([anthropic_tool_response([VALID_FINDING])])
                backend = P.AnthropicBackend(
                    "sk-test", transport=transport, workspace_id=configured
                )
                backend.complete(
                    role_name="ambiguity_critic", model="claude-sonnet-5",
                    prompt="PROMPT", max_tokens=1024, effort="medium",
                )
                headers = transport.calls[0][1]
                self.assertEqual(headers["x-api-key"], "sk-test")
                self.assertEqual(headers.get("anthropic-workspace-id"), expected)

    def test_routed_provider_passes_the_configured_workspace_id(self):
        """`_backend` reads `providers.anthropic.workspace_id` out of the
        routing, so the operator sets it once in `config/agents.yaml`."""
        routing = P.load_role_routing()
        self.assertIn("workspace_id", routing.provider_config.get("anthropic", {}))
        backend = P.AnthropicBackend("sk-test", workspace_id="wrkspc_02xyz")
        self.assertEqual(
            backend._headers()["anthropic-workspace-id"], "wrkspc_02xyz"  # noqa: SLF001
        )
        self.assertNotIn(
            "anthropic-workspace-id",
            P.AnthropicBackend("sk-test")._headers(),  # noqa: SLF001
        )

    def test_critic_call_is_tool_forced_strict_and_unsampled(self):
        transport = FakeTransport([anthropic_tool_response([VALID_FINDING])])
        backend = P.AnthropicBackend("sk-test", transport=transport)
        result = backend.complete(
            role_name="ambiguity_critic", model="claude-sonnet-5",
            prompt="PROMPT", max_tokens=1024, effort="medium",
        )
        url, headers, payload = transport.calls[0]
        self.assertTrue(url.endswith("/v1/messages"))
        self.assertEqual(headers["x-api-key"], "sk-test")
        self.assertIn("anthropic-version", headers)
        # Tool-forced strict schema.
        self.assertEqual(payload["tool_choice"], {"type": "tool", "name": P.FINDINGS_TOOL_NAME})
        self.assertTrue(payload["tools"][0]["strict"])
        self.assertEqual(payload["tools"][0]["input_schema"], P.findings_tool_schema())
        # No sampling-parameter pinning (the models reject it with a 400).
        for banned in ("temperature", "top_p", "top_k"):
            self.assertNotIn(banned, payload)
        self.assertEqual(payload["output_config"], {"effort": "medium"})
        # Normalized text is council-parseable JSON.
        data = json.loads(result.text)
        self.assertEqual(data["role"], "ambiguity_critic")
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["suggested_attack"], "constants")

    def test_haiku_route_omits_effort_parameter(self):
        transport = FakeTransport([anthropic_tool_response([VALID_FINDING])])
        backend = P.AnthropicBackend("sk-test", transport=transport)
        backend.complete(
            role_name="feasibility_reviewer", model="claude-haiku-4-5",
            prompt="PROMPT", max_tokens=512, effort="high",
        )
        self.assertNotIn("output_config", transport.calls[0][2])

    def test_schema_retry_then_success(self):
        transport = FakeTransport(
            [anthropic_bad_tool_response(), anthropic_tool_response([VALID_FINDING])]
        )
        backend = P.AnthropicBackend("sk-test", transport=transport)
        result = backend.complete(
            role_name="shortcut_attacker", model="claude-opus-5",
            prompt="PROMPT", max_tokens=1024, effort="high",
        )
        self.assertEqual(len(transport.calls), 2)
        # Both attempts were paid for: usage is summed across attempts.
        self.assertEqual(result.input_tokens, 2000)
        self.assertEqual(result.output_tokens, 400)
        self.assertEqual(len(result.raw_attempts), 2)

    def test_schema_retries_bounded_then_raises_protocol_error(self):
        transport = FakeTransport([anthropic_bad_tool_response() for _ in range(5)])
        backend = P.AnthropicBackend("sk-test", transport=transport)
        with self.assertRaises(ProviderProtocolError):
            backend.complete(
                role_name="ambiguity_critic", model="claude-sonnet-5",
                prompt="PROMPT", max_tokens=1024, effort=None,
            )
        # Exactly 1 initial attempt + SCHEMA_RETRIES retries, never more.
        self.assertEqual(len(transport.calls), 1 + P.SCHEMA_RETRIES)

    def test_prose_role_is_plain_completion(self):
        transport = FakeTransport([anthropic_text_response("Solver-visible prose.")])
        backend = P.AnthropicBackend("sk-test", transport=transport)
        result = backend.complete(
            role_name="semantic_author", model="claude-opus-5",
            prompt="PROMPT", max_tokens=2048, effort="high",
        )
        payload = transport.calls[0][2]
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertEqual(result.text, "Solver-visible prose.")

    def test_empty_prose_retries_then_raises(self):
        transport = FakeTransport([anthropic_text_response("") for _ in range(3)])
        backend = P.AnthropicBackend("sk-test", transport=transport)
        with self.assertRaises(ProviderProtocolError):
            backend.complete(
                role_name="semantic_author", model="claude-opus-5",
                prompt="PROMPT", max_tokens=2048, effort=None,
            )
        self.assertEqual(len(transport.calls), 3)


class SchemaRetryWireProtocolTest(unittest.TestCase):
    """The retry payload must be a LEGAL follow-up on each wire.

    This was never tested and never worked live: the Anthropic retry appended
    a plain-text user turn after a tool_use assistant turn, so every schema
    retry 400ed ("`tool_use` ids were found without `tool_result` blocks
    immediately after") — the bounded-retry mechanism the module documents at
    length had never once run against the real API (docs/runs/synsql.md §3.4).
    The chat-completions path carried the identical bug in its own dialect.

    Both wires were re-measured live by POSTing the old and new
    payload shapes side by side: anthropic OLD -> HTTP 400, NEW -> 200;
    chat OLD -> HTTP 400 ("No tool output found for function call ...") on a
    strict upstream, NEW -> 200 (the configured kimi route tolerates both, so
    the chat defect was latent rather than firing). Both shapes are pinned
    here so no future edit can un-fix them by inspection alone.
    """

    def test_anthropic_retry_answers_every_tool_use_with_a_tool_result(self):
        rejected = anthropic_bad_tool_response()
        payload = P.AnthropicBackend._payload(
            model="claude-sonnet-5", prompt="P", max_tokens=64, effort=None,
            role_name="ambiguity_critic", schema_mode=True,
        )
        retry = P._with_correction(payload, rejected, "bad severity")
        messages = retry["messages"]
        self.assertEqual(messages[-2]["role"], "assistant")
        self.assertEqual(messages[-1]["role"], "user")
        blocks = messages[-1]["content"]
        self.assertIsInstance(blocks, list)
        # EVERY tool_use id in the assistant turn is answered, and the FIRST
        # block of the user turn is a tool_result (the API's rule).
        used = [
            b["id"] for b in rejected["content"] if b.get("type") == "tool_use"
        ]
        answered = [
            b["tool_use_id"] for b in blocks if b.get("type") == "tool_result"
        ]
        self.assertEqual(answered, used)
        self.assertEqual(blocks[0]["type"], "tool_result")
        self.assertIn("bad severity", blocks[0]["content"])

    def test_anthropic_prose_retry_stays_plain_text(self):
        """No tool_use in the rejected turn -> no tool_result to invent."""
        rejected = anthropic_text_response("")
        retry = P._with_correction({"messages": []}, rejected, "empty")
        self.assertEqual(retry["messages"][-1]["role"], "user")
        self.assertIsInstance(retry["messages"][-1]["content"], str)

    def test_chat_retry_answers_every_tool_call_with_a_tool_message(self):
        rejected = openai_tool_response(
            [{"severity": "catastrophic", "summary": "s", "detail": "d",
              "route_hint": None, "suggested_attack": None}],
            call_id="call_xyz",
        )
        retry = P._with_correction_chat(
            {"messages": [{"role": "user", "content": "P"}]},
            rejected,
            "bad severity",
        )
        roles = [m["role"] for m in retry["messages"]]
        self.assertEqual(roles, ["user", "assistant", "tool", "user"])
        tool_turn = retry["messages"][2]
        self.assertEqual(tool_turn["tool_call_id"], "call_xyz")
        self.assertIn("bad severity", tool_turn["content"])
        # The assistant turn is echoed back with wire-legal fields only, and
        # its null content is normalized (several endpoints reject null).
        assistant = retry["messages"][1]
        self.assertEqual(set(assistant) - {"role", "content", "tool_calls"}, set())
        self.assertEqual(assistant["content"], "")

    def test_chat_retry_drops_vendor_telemetry_from_the_echoed_turn(self):
        rejected = openai_tool_response([], call_id="call_1")
        message = rejected["choices"][0]["message"]
        message["reasoning"] = "internal"
        message["reasoning_details"] = [{"type": "reasoning.text", "text": "x"}]
        message["refusal"] = None
        retry = P._with_correction_chat({"messages": []}, rejected, "why")
        self.assertNotIn("reasoning", retry["messages"][0])
        self.assertNotIn("reasoning_details", retry["messages"][0])
        self.assertNotIn("refusal", retry["messages"][0])

    def test_chat_prose_retry_has_no_tool_message(self):
        retry = P._with_correction_chat(
            {"messages": []}, openai_text_response(""), "empty prose response"
        )
        self.assertEqual([m["role"] for m in retry["messages"]],
                         ["assistant", "user"])

    def test_chat_backend_retry_payload_is_legal_end_to_end(self):
        """Through the real complete() loop, not just the helper."""
        transport = FakeTransport([
            openai_tool_response(
                [{"severity": "catastrophic", "summary": "s", "detail": "d",
                  "route_hint": None, "suggested_attack": None}],
                call_id="call_a",
            ),
            openai_tool_response([VALID_FINDING]),
        ])
        backend = P.OpenAICompatBackend(
            "http://oss:8000/v1", "k", transport=transport
        )
        backend.complete(
            role_name="ambiguity_critic", model="m", prompt="P",
            max_tokens=64, effort=None,
        )
        second_payload = transport.calls[1][2]
        for index, msg in enumerate(second_payload["messages"]):
            if msg["role"] == "tool":
                previous = second_payload["messages"][index - 1]
                self.assertEqual(previous["role"], "assistant")
                self.assertIn(
                    msg["tool_call_id"],
                    [c["id"] for c in previous["tool_calls"]],
                )
        self.assertIn(
            "tool", [m["role"] for m in second_payload["messages"]]
        )


# ---------------------------------------------------------------------------
# OpenAI-compatible backend
# ---------------------------------------------------------------------------

class OpenAICompatBackendTest(unittest.TestCase):
    def test_missing_base_url_raises_missing_credentials(self):
        with self.assertRaises(P.MissingCredentialsError):
            P.OpenAICompatBackend("", "key")

    def test_critic_call_forces_function_and_parses_arguments(self):
        transport = FakeTransport([openai_tool_response([VALID_FINDING])])
        backend = P.OpenAICompatBackend(
            "http://localhost:8000/v1", "oss-key", transport=transport
        )
        result = backend.complete(
            role_name="ambiguity_critic", model="test-oss-model",
            prompt="PROMPT", max_tokens=1024, effort=None,
        )
        url, headers, payload = transport.calls[0]
        self.assertTrue(url.endswith("/v1/chat/completions"))
        self.assertEqual(headers["authorization"], "Bearer oss-key")
        self.assertEqual(
            payload["tool_choice"],
            {"type": "function", "function": {"name": P.FINDINGS_TOOL_NAME}},
        )
        self.assertEqual(
            payload["tools"][0]["function"]["parameters"], P.findings_tool_schema()
        )
        data = json.loads(result.text)
        self.assertEqual(data["role"], "ambiguity_critic")
        self.assertEqual(data["findings"][0]["severity"], "major")

    def test_schema_violation_retries_then_raises(self):
        bad = openai_tool_response([{"severity": "nope", "summary": "s"}])
        transport = FakeTransport([bad, dict(bad), dict(bad)])
        backend = P.OpenAICompatBackend("http://x/v1", "", transport=transport)
        with self.assertRaises(ProviderProtocolError):
            backend.complete(
                role_name="shortcut_attacker", model="test-oss-model",
                prompt="PROMPT", max_tokens=1024, effort=None,
            )
        self.assertEqual(len(transport.calls), 1 + P.SCHEMA_RETRIES)

    def test_prose_role_reads_message_content(self):
        transport = FakeTransport([openai_text_response("independent build")])
        backend = P.OpenAICompatBackend("http://x/v1", "", transport=transport)
        result = backend.complete(
            role_name="independent_implementer", model="test-oss-model",
            prompt="PROMPT", max_tokens=1024, effort=None,
        )
        self.assertEqual(result.text, "independent build")
        self.assertNotIn("tools", transport.calls[0][2])

    def test_missing_model_raises_missing_credentials(self):
        backend = P.OpenAICompatBackend("http://x/v1", "", transport=FakeTransport([]))
        with self.assertRaises(P.MissingCredentialsError):
            backend.complete(
                role_name="independent_implementer", model="",
                prompt="PROMPT", max_tokens=1024, effort=None,
            )


# ---------------------------------------------------------------------------
# Transcript store
# ---------------------------------------------------------------------------

class TranscriptStoreTest(unittest.TestCase):
    def test_record_then_lookup_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = P.TranscriptStore(Path(tmp))
            sha = sha256_hex("prompt")
            entry = {"role": "ambiguity_critic", "prompt_sha256": sha, "response": "R"}
            path = store.record("ambiguity_critic", sha, entry)
            self.assertTrue(path.is_file())
            self.assertEqual(path, Path(tmp) / "ambiguity_critic" / f"{sha}.json")
            self.assertEqual(store.lookup("ambiguity_critic", sha)["response"], "R")
            self.assertIsNone(store.lookup("ambiguity_critic", sha256_hex("other")))

    def test_workspace_recording_takes_precedence_over_fixtures(self):
        """What --record just wrote wins; a same-key committed fixture must
        never shadow the workspace recording forever."""
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = Path(tmp) / "fixtures"
            workspace = Path(tmp) / "ws"
            sha = sha256_hex("p")
            for root, response in ((fixtures, "FIXTURE"), (workspace, "WORKSPACE")):
                d = root / "role"
                d.mkdir(parents=True)
                (d / f"{sha}.json").write_text(
                    json.dumps({"prompt_sha256": sha, "response": response})
                )
            store = P.TranscriptStore(workspace, fixtures_dir=fixtures)
            self.assertEqual(store.lookup("role", sha)["response"], "WORKSPACE")
            self.assertEqual(store._search_dirs(), [workspace, fixtures])

    def test_fixture_served_only_on_workspace_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = Path(tmp) / "fixtures"
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            sha = sha256_hex("fixture-only")
            d = fixtures / "role"
            d.mkdir(parents=True)
            (d / f"{sha}.json").write_text(
                json.dumps({"prompt_sha256": sha, "response": "FIXTURE"})
            )
            store = P.TranscriptStore(workspace, fixtures_dir=fixtures)
            self.assertEqual(store.lookup("role", sha)["response"], "FIXTURE")
            # A workspace-only store never sees the fixture at all.
            self.assertIsNone(P.TranscriptStore(workspace).lookup("role", sha))

    def test_missing_prompt_sha_in_stored_entry_fails_closed(self):
        """The filename is the key, but the entry must SAY so: an entry with
        no prompt_sha256 is a planted/corrupt file, never a replay."""
        with tempfile.TemporaryDirectory() as tmp:
            sha = sha256_hex("p")
            d = Path(tmp) / "role"
            d.mkdir(parents=True)
            (d / f"{sha}.json").write_text(json.dumps({"response": "X"}))
            store = P.TranscriptStore(Path(tmp))
            with self.assertRaises(RuntimeError) as ctx:
                store.lookup("role", sha)
            self.assertIn("corrupt store", str(ctx.exception))

    def test_sha_mismatch_in_stored_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            sha = sha256_hex("p")
            d = Path(tmp) / "role"
            d.mkdir(parents=True)
            (d / f"{sha}.json").write_text(
                json.dumps({"prompt_sha256": "not-the-sha", "response": "R"})
            )
            store = P.TranscriptStore(Path(tmp))
            with self.assertRaises(RuntimeError):
                store.lookup("role", sha)

    def test_record_without_record_dir_fails_closed(self):
        store = P.TranscriptStore(None)
        with self.assertRaises(RuntimeError):
            store.record("role", sha256_hex("p"), {})

    def test_atomic_record_failure_preserves_the_previous_complete_json(self):
        """A death before the replace commit cannot truncate the served memo."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = P.TranscriptStore(root)
            sha = sha256_hex("atomic prompt")
            first = {"prompt_sha256": sha, "response": "first"}
            store.record("role", sha, first)

            with (
                mock.patch.object(P.os, "replace", side_effect=OSError("interrupted")),
                self.assertRaises(OSError),
            ):
                store.record(
                    "role", sha, {"prompt_sha256": sha, "response": "replacement"}
                )

            self.assertEqual(store.lookup("role", sha), first)
            self.assertEqual(list(root.rglob(".*.stage-*")), [])

    def test_atomic_session_failure_preserves_the_previous_complete_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = P.TranscriptStore(root)
            key = sha256_hex("bounded session")
            first = {"session_key": key, "session_sha256": "a" * 64}
            store.record_session("role", key, first)

            with (
                mock.patch.object(P.os, "replace", side_effect=OSError("interrupted")),
                self.assertRaises(OSError),
            ):
                store.record_session(
                    "role", key, {"session_key": key, "session_sha256": "b" * 64}
                )

            self.assertEqual(store.lookup_session("role", key), first)
            self.assertEqual(list(root.rglob(".*.stage-*")), [])

    def test_concurrent_atomic_recorders_leave_one_whole_entry(self):
        """Same-key workers may race, but readers see one complete JSON value."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = P.TranscriptStore(root)
            sha = sha256_hex("shared prompt")
            entries = [
                {
                    "prompt_sha256": sha,
                    "response": marker * 250_000,
                    "writer": marker,
                }
                for marker in ("a", "b", "c", "d")
            ]
            barrier = threading.Barrier(len(entries))

            def publish(entry):
                barrier.wait()
                store.record("role", sha, entry)

            with ThreadPoolExecutor(max_workers=len(entries)) as executor:
                list(executor.map(publish, entries))

            self.assertIn(store.lookup("role", sha), entries)
            self.assertEqual(list(root.rglob(".*.stage-*")), [])

    def test_concurrent_immutable_publish_has_one_complete_winner(self):
        """The hard-link commit is exclusive and never exposes staged bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "trajectories" / "role" / "digest.json"
            payloads = tuple(
                (marker * 250_000).encode("ascii")
                for marker in ("a", "b", "c", "d")
            )
            barrier = threading.Barrier(len(payloads))

            def publish(payload):
                barrier.wait()
                return P._publish_bytes_once(target, payload)

            with ThreadPoolExecutor(max_workers=len(payloads)) as executor:
                published = list(executor.map(publish, payloads))

            self.assertEqual(sum(published), 1)
            self.assertIn(target.read_bytes(), payloads)
            self.assertEqual(list(root.rglob(".*.stage-*")), [])

    def test_interrupted_immutable_publish_leaves_no_final_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "tool_raw" / "digest" / "0.bin"
            with (
                mock.patch.object(P.os, "link", side_effect=OSError("interrupted")),
                self.assertRaises(OSError),
            ):
                P._publish_bytes_once(target, b"complete payload")

            self.assertFalse(target.exists())
            self.assertEqual(list(root.rglob(".*.stage-*")), [])

    def test_raw_output_publication_refuses_a_conflicting_visible_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "tool_raw" / "digest" / "0.bin"
            payload = b"sealed validator output"
            digest = sha256_hex(payload.decode("ascii"))
            P._publish_raw_output(target, payload, digest)
            P._publish_raw_output(target, payload, digest)
            self.assertEqual(target.read_bytes(), payload)

            target.write_bytes(b"corrupt")
            with self.assertRaisesRegex(RuntimeError, "conflicts with its trajectory"):
                P._publish_raw_output(target, payload, digest)

    def test_transcripts_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(P.transcripts_present(root))
            self.assertFalse(P.transcripts_present(root / "missing"))
            d = root / "role"
            d.mkdir()
            (d / "abc.json").write_text("{}")
            self.assertTrue(P.transcripts_present(root))


# ---------------------------------------------------------------------------
# Cost meter + budgets
# ---------------------------------------------------------------------------

class CostMeterTest(unittest.TestCase):
    def test_usd_math_matches_published_rates(self):
        meter = P.CostMeter(budget_per_task_usd=1000.0)
        usd = meter.charge(
            task_id="t", role_name="semantic_author", model="claude-opus-5",
            input_tokens=1_000_000, output_tokens=1_000_000,
            rates=P.rates_for("anthropic", "claude-opus-5", {}),
        )
        self.assertAlmostEqual(usd, 5.00 + 25.00)
        self.assertAlmostEqual(meter.total_usd, 30.00)

    def test_unpriced_anthropic_model_fails_closed(self):
        with self.assertRaises(RuntimeError):
            P.rates_for("anthropic", "claude-imaginary-9", {})

    def test_openai_compat_reads_endpoint_wide_config(self):
        self.assertEqual(
            P.rates_for(
                "openai_compat", "m",
                {"usd_per_mtok_input": 0.5, "usd_per_mtok_output": 1.5},
            ),
            (0.5, 1.5),
        )

    def test_per_task_budget_breach_raises(self):
        meter = P.CostMeter(budget_per_task_usd=0.01)
        with self.assertRaises(P.BudgetExceededError):
            meter.charge(
                task_id="t", role_name="r", model="claude-opus-5",
                input_tokens=1_000_000, output_tokens=0,
                rates=(5.00, 25.00),
            )

    def test_total_budget_breach_raises_across_tasks(self):
        meter = P.CostMeter(budget_per_task_usd=100.0, budget_total_usd=8.0)
        meter.charge(task_id="a", role_name="r", model="m",
                     input_tokens=1_000_000, output_tokens=0, rates=(5.0, 0.0))
        with self.assertRaises(P.BudgetExceededError):
            meter.charge(task_id="b", role_name="r", model="m",
                         input_tokens=1_000_000, output_tokens=0, rates=(5.0, 0.0))

    def test_shared_meter_accounts_concurrent_workers_without_lost_updates(self):
        """A metrology pool shares one meter. Concurrent worker accounting is
        exact at the total, task, role and attempt levels."""
        workers = 8
        calls_per_worker = 250
        meter = P.CostMeter(budget_per_task_usd=10_000.0, budget_total_usd=10_000.0)
        start = threading.Barrier(workers)

        def spend(index: int) -> None:
            start.wait()
            for _ in range(calls_per_worker):
                meter.charge(
                    task_id=f"task-{index % 2}",
                    role_name=f"role-{index % 2}",
                    model="m",
                    usage=P.Usage(input_tokens=3, output_tokens=2),
                    rates=(0.0, 0.0),
                    provider_reported_usd=0.001,
                    elapsed_ms=1,
                )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(spend, range(workers)))

        attempts = workers * calls_per_worker
        self.assertEqual(meter.attempt_count, attempts)
        self.assertAlmostEqual(meter.total_usd, attempts * 0.001)
        for parity in range(2):
            expected = (workers // 2) * calls_per_worker
            self.assertAlmostEqual(meter.per_task_usd[f"task-{parity}"], expected * 0.001)
            row = meter.per_role[f"role-{parity}"]
            self.assertEqual(int(row["attempts"]), expected)
            self.assertEqual(int(row["input_tokens"]), expected * 3)
            self.assertEqual(int(row["output_tokens"]), expected * 2)
            self.assertEqual(int(row["elapsed_ms"]), expected)


class RoutedProviderPoolConcurrencyTest(unittest.TestCase):
    def test_trial_worker_binding_is_context_local(self):
        """Two overlapping trial drivers retain their own worker. The first
        trial completing after the second begins must not call worker 2, and
        ending either context must not clear the other's binding."""

        class Worker:
            def __init__(self, name: str):
                self.name = name
                self.exchange_evidence: list[dict] = []
                self.begun: list[str] = []
                self.completed: list[tuple[str, str]] = []
                self.ended = 0

            def begin_trial(self, ctx) -> None:
                self.begun.append(ctx.trial_nonce)

            def complete(self, role, prompt: str) -> str:
                self.completed.append((str(role), prompt))
                return self.name

            def end_trial(self) -> None:
                self.ended += 1

        first, second = Worker("worker-0"), Worker("worker-1")
        pool = P.RoutedProviderPool((first, second))
        first_begun = threading.Event()
        second_begun = threading.Event()
        first_done = threading.Event()

        def drive_first() -> tuple[str, str]:
            nonce = "first"
            pool.begin_trial(SimpleNamespace(trial_nonce=nonce))
            first_begun.set()
            second_begun.wait()
            try:
                return nonce, pool.complete("role", "prompt-first")
            finally:
                pool.end_trial()
                first_done.set()

        def drive_second() -> tuple[str, str]:
            nonce = "second"
            first_begun.wait()
            pool.begin_trial(SimpleNamespace(trial_nonce=nonce))
            second_begun.set()
            first_done.wait()
            try:
                return nonce, pool.complete("role", "prompt-second")
            finally:
                pool.end_trial()

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_first = executor.submit(drive_first)
            future_second = executor.submit(drive_second)
            answers = dict((future_first.result(), future_second.result()))

        self.assertEqual(answers, {"first": "worker-0", "second": "worker-1"})
        self.assertEqual(first.begun, ["first"])
        self.assertEqual(second.begun, ["second"])
        self.assertEqual(first.completed, [("role", "prompt-first")])
        self.assertEqual(second.completed, [("role", "prompt-second")])
        self.assertEqual((first.ended, second.ended), (1, 1))


class OpenAICompatPricingFailsClosedTest(unittest.TestCase):
    """An unpriced openai_compat model must RAISE, exactly like an unpriced
    anthropic model. It used to return (0.0, 0.0), which is how a paid
    OpenRouter route (10,430 output tokens) was metered as free — see
    docs/runs/demo.md §8.5."""

    def test_no_rates_at_all_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            P.rates_for("openai_compat", "moonshotai/kimi-k2.7-code", {})
        self.assertIn("no pricing entry for openai_compat", str(ctx.exception))
        self.assertIn("pricing", str(ctx.exception))

    def test_half_a_price_is_not_a_price(self):
        for half in ({"usd_per_mtok_input": 1.0}, {"usd_per_mtok_output": 1.0}):
            with self.assertRaises(RuntimeError):
                P.rates_for("openai_compat", "m", half)

    def test_explicit_zero_is_a_decision_and_is_honored(self):
        """A free self-hosted endpoint states 0.0/0.0 and gets it — the
        difference between 'free' and 'unpriced' is that someone typed it."""
        self.assertEqual(
            P.rates_for(
                "openai_compat", "m",
                {"usd_per_mtok_input": 0.0, "usd_per_mtok_output": 0.0},
            ),
            (0.0, 0.0),
        )

    def test_negative_or_nonfinite_configured_rate_is_not_a_price(self):
        for key in (
            "usd_per_mtok_input",
            "usd_per_mtok_output",
            "usd_per_mtok_cache_read",
            "usd_per_mtok_cache_write",
        ):
            for invalid in (False, True, -0.01, float("nan"), float("inf")):
                with self.subTest(key=key, invalid=invalid):
                    rates = {
                        "usd_per_mtok_input": 1.0,
                        "usd_per_mtok_output": 2.0,
                        key: invalid,
                    }
                    with self.assertRaisesRegex(
                        ValueError, "finite, non-negative"
                    ):
                        P.rate_card_for("openai_compat", "m", rates)

    def test_per_model_map_beats_endpoint_wide_rates(self):
        cfg = {
            "usd_per_mtok_input": 9.0,
            "usd_per_mtok_output": 9.0,
            "pricing": {
                "cheap": {"usd_per_mtok_input": 0.7, "usd_per_mtok_output": 3.5}
            },
        }
        self.assertEqual(P.rates_for("openai_compat", "cheap", cfg), (0.7, 3.5))
        self.assertEqual(P.rates_for("openai_compat", "other", cfg), (9.0, 9.0))

    def test_repo_config_prices_the_configured_oss_model(self):
        """config/agents.yaml must price whatever ELT_TASKGEN_OSS_MODEL names
        (verified against https://openrouter.ai/api/v1/models:
        kimi-k2.7-code prompt 7e-7 / completion 3.5e-6 USD per token)."""
        cfg = P.DEFAULT_ROUTING_DOC["providers"]["openai_compat"]
        self.assertEqual(
            P.rates_for("openai_compat", "moonshotai/kimi-k2.7-code", cfg),
            (0.70, 3.50),
        )
        routing = P.load_role_routing()
        file_cfg = routing.provider_config.get("openai_compat") or {}
        self.assertEqual(
            P.rates_for("openai_compat", "moonshotai/kimi-k2.7-code", file_cfg),
            (0.70, 3.50),
        )

    def test_unknown_provider_has_no_pricing_rule(self):
        with self.assertRaises(RuntimeError):
            P.rates_for("some_new_gateway", "m", {})


class ProviderReportedCostWinsTest(unittest.TestCase):
    """OpenRouter reports the amount it actually billed in `usage.cost`, and a
    gateway prices per upstream route (two identical kimi-k2.7-code calls
    billed 3.50e-5 and 3.55e-5 within a minute), so the reported figure beats
    the configured table. Configured rates remain MANDATORY: they price the
    endpoints that report nothing (the absent-report fallback is pinned by
    CostMeterTest::test_usd_math_matches_published_rates)."""

    def test_reported_cost_overrides_rates(self):
        meter = P.CostMeter(budget_per_task_usd=100.0)
        usd = meter.charge(
            task_id="t", role_name="independent_implementer", model="m",
            input_tokens=10, output_tokens=8, rates=(0.70, 3.50),
            reported_usd=3.55e-05,
        )
        self.assertAlmostEqual(usd, 3.55e-05)
        self.assertAlmostEqual(meter.total_usd, 3.55e-05)

    def test_nonsense_report_is_ignored_not_trusted(self):
        meter = P.CostMeter(budget_per_task_usd=100.0)
        for bogus in (-1.0, float("nan"), float("inf")):
            usd = meter.charge(
                task_id="t", role_name="r", model="m",
                input_tokens=1_000_000, output_tokens=0,
                rates=(1.0, 0.0), reported_usd=bogus,
            )
            self.assertAlmostEqual(usd, 1.0)

    def test_backend_sums_usage_cost_across_attempts(self):
        """Every attempt is billed, retries included."""
        first = openai_tool_response(
            [{"severity": "catastrophic", "summary": "s", "detail": "d",
              "route_hint": None, "suggested_attack": None}]
        )
        first["usage"]["cost"] = 0.001
        second = openai_tool_response([VALID_FINDING])
        second["usage"]["cost"] = 0.002
        transport = FakeTransport([first, second])
        backend = P.OpenAICompatBackend(
            "http://oss:8000/v1", "k", transport=transport
        )
        result = backend.complete(
            role_name="ambiguity_critic", model="m", prompt="p",
            max_tokens=64, effort=None,
        )
        self.assertAlmostEqual(result.reported_usd, 0.003)

    def test_no_cost_field_reports_none(self):
        transport = FakeTransport([openai_tool_response([VALID_FINDING])])
        backend = P.OpenAICompatBackend(
            "http://oss:8000/v1", "k", transport=transport
        )
        result = backend.complete(
            role_name="ambiguity_critic", model="m", prompt="p",
            max_tokens=64, effort=None,
        )
        self.assertIsNone(result.reported_usd)


# ---------------------------------------------------------------------------
# Routing config (agents.yaml)
# ---------------------------------------------------------------------------

class RoutingConfigTest(unittest.TestCase):
    def test_packaged_default_matches_agents_yaml_and_fingerprint(self):
        """The wheel fallback is the packaged YAML, never a copied literal."""
        from elt_taskgen.review import metrology as metrology_mod

        path = P.default_agents_config_path()
        self.assertTrue(path.is_file(), path)
        raw = path.read_bytes()
        on_disk = yaml.safe_load(raw.decode("utf-8"))
        self.assertIsInstance(on_disk, dict)
        # Kept only for old callers: this is a parsed view, not another source.
        self.assertEqual(P.DEFAULT_ROUTING_DOC, on_disk)

        from_source = P.load_role_routing(path)
        source_fingerprint = metrology_mod.council_routing_fingerprint(from_source)
        with tempfile.TemporaryDirectory() as tmp:
            packaged = Path(tmp) / "elt_taskgen" / "_resources" / "config" / "agents.yaml"
            packaged.parent.mkdir(parents=True)
            packaged.write_bytes(raw)
            with mock.patch.object(P, "resource_path", return_value=packaged):
                from_package = P.load_role_routing()
                package_fingerprint = metrology_mod.council_routing_fingerprint(from_package)

        self.assertEqual(from_package.agents_document, on_disk)
        self.assertEqual(from_package.roles, from_source.roles)
        self.assertEqual(from_package.provider_config, from_source.provider_config)
        self.assertEqual(package_fingerprint, source_fingerprint)
        for role in from_source.roles:
            with self.subTest(role=role):
                self.assertEqual(
                    P.role_behavior_sha256(
                        role, agents_config=from_package.agents_document
                    ),
                    P.role_behavior_sha256(
                        role, agents_config=from_source.agents_document
                    ),
                )

    def test_provider_uses_one_snapshot_and_later_construction_reloads(self):
        """A file replacement cannot mix A routing with B behavior."""
        first = json.loads(json.dumps(P._agents_doc()))
        second = json.loads(json.dumps(first))
        first["roles"]["ambiguity_critic"]["max_tokens"] = 1111
        first["roles"]["ambiguity_critic"]["session"]["max_wall_s"] = 301
        second["roles"]["ambiguity_critic"]["max_tokens"] = 2222
        second["roles"]["ambiguity_critic"]["session"]["max_wall_s"] = 302

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agents.yaml"
            path.write_text(yaml.safe_dump(first, sort_keys=False), encoding="utf-8")
            routing_a = P.load_role_routing(path)

            # Simulate an atomic config deployment in the crash/race window
            # between routing construction and provider construction.
            path.write_text(yaml.safe_dump(second, sort_keys=False), encoding="utf-8")
            provider_a = P.RoutedProvider(
                routing_a,
                P.TranscriptStore(Path(tmp) / "transcripts-a"),
                P.CostMeter(budget_per_task_usd=100.0),
            )
            self.assertEqual(routing_a.for_role("ambiguity_critic").max_tokens, 1111)
            self.assertEqual(
                P.role_loop_limits(
                    "ambiguity_critic", agents_config=provider_a.agents_document
                )["max_wall_s"],
                301,
            )

            # A separate construction keeps reload behavior and sees B in
            # both halves; it does not inherit A from a process-global cache.
            routing_b = P.load_role_routing(path)
            provider_b = P.RoutedProvider(
                routing_b,
                P.TranscriptStore(Path(tmp) / "transcripts-b"),
                P.CostMeter(budget_per_task_usd=100.0),
            )
            self.assertEqual(routing_b.for_role("ambiguity_critic").max_tokens, 2222)
            self.assertEqual(
                P.role_loop_limits(
                    "ambiguity_critic", agents_config=provider_b.agents_document
                )["max_wall_s"],
                302,
            )
            self.assertNotEqual(
                P.role_behavior_sha256(
                    "ambiguity_critic", agents_config=provider_a.agents_document
                ),
                P.role_behavior_sha256(
                    "ambiguity_critic", agents_config=provider_b.agents_document
                ),
            )

    def test_default_bounded_roles_and_caps_are_explicit(self):
        """The useful tool/validator loops ship on, while the two advisory
        readers with no useful loop remain one-shot. Every enabled runner is
        bounded by a role-local USD and wall-clock cap."""
        expected = {
            "semantic_author": True,
            "ambiguity_critic": False,
            "population_adversary": True,
            "shortcut_attacker": True,
            "feasibility_reviewer": False,
            "independent_implementer": True,
            "independent_loader": True,
            "repair_proposer": True,
        }
        for role, enabled in expected.items():
            with self.subTest(role=role):
                block = P.role_loop_limits(role)
                self.assertIs(block["enabled"], enabled)
                if enabled:
                    self.assertGreater(float(block["max_usd"]), 0.0)
                    wall = block.get("wall_clock_s", block.get("max_wall_s"))
                    self.assertIsNotNone(wall)
                    self.assertGreater(int(wall), 0)
    def test_repo_default_config_routes_all_roles(self):
        routing = P.load_role_routing()
        for role in (
            "semantic_author", "ambiguity_critic", "population_adversary",
            "shortcut_attacker", "feasibility_reviewer", "independent_implementer",
        ):
            self.assertIn(role, routing.roles)
        self.assertEqual(routing.for_role("semantic_author").model, "claude-opus-5")
        # The live harness-6 run at seed 757248678947434349 measured Haiku's
        # feasibility recall at 14/25 (Wilson LB 0.46 < 0.75), so that seat is
        # deliberately upgraded to the same high-effort Opus tier as the
        # other recall-sensitive critics. See config/agents.yaml.
        self.assertEqual(
            routing.for_role("ambiguity_critic").model, "claude-opus-5"
        )
        self.assertEqual(routing.for_role("ambiguity_critic").effort, "high")
        self.assertEqual(
            routing.for_role("population_adversary").model, "claude-opus-5"
        )
        self.assertEqual(
            routing.for_role("population_adversary").effort, "high"
        )
        self.assertEqual(
            routing.for_role("feasibility_reviewer").model, "claude-opus-5"
        )
        self.assertEqual(routing.for_role("feasibility_reviewer").effort, "high")
        # The cross-family independent implementer is MANDATORY openai_compat.
        self.assertEqual(
            routing.for_role("independent_implementer").provider, "openai_compat"
        )

    def test_env_interpolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "agents.yaml"
            cfg.write_text(
                "providers:\n"
                "  anthropic:\n"
                "    api_key: ${ELT_TEST_PROV_KEY}\n"
                "roles:\n"
                "  semantic_author:\n"
                "    provider: anthropic\n"
                "    model: claude-opus-5\n"
                "    max_tokens: 2048\n"
                "    effort: high\n"
            )
            with mock.patch.dict(os.environ, {"ELT_TEST_PROV_KEY": "sk-from-env"}):
                routing = P.load_role_routing(cfg)
            self.assertEqual(
                routing.provider_config["anthropic"]["api_key"], "sk-from-env"
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ELT_TEST_PROV_KEY", None)
                routing = P.load_role_routing(cfg)
            self.assertEqual(routing.provider_config["anthropic"]["api_key"], "")

    def test_explicit_missing_path_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            P.load_role_routing(Path("/nonexistent/agents.yaml"))

    def test_unknown_provider_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "agents.yaml"
            cfg.write_text(
                "roles:\n  semantic_author: {provider: sparkle, model: x, max_tokens: 1}\n"
            )
            with self.assertRaises(ValueError):
                P.load_role_routing(cfg)

    def test_unrouted_role_fails_closed(self):
        routing = make_routing()
        with self.assertRaises(RuntimeError):
            routing.for_role("some_new_role")

    def test_credential_problems(self):
        routing = make_routing(api_key="", oss_base="")
        problems = P.credential_problems(
            routing, ["semantic_author", "independent_implementer"]
        )
        self.assertTrue(any("ANTHROPIC_API_KEY" in p for p in problems))
        self.assertTrue(any("ELT_TASKGEN_OSS_BASE_URL" in p for p in problems))
        routing = make_routing(
            api_key="sk-x", oss_base="http://x", oss_key="or-x", oss_model="oss/m"
        )
        self.assertEqual(
            P.credential_problems(
                routing, ["semantic_author", "independent_implementer"]
            ),
            [],
        )

    def test_openai_compat_needs_all_three_credentials(self):
        """The guard must not fail OPEN on a set base_url with no key/model.

        It used to check base_url alone, so seeding cleared this check, paid
        for every anthropic role, and only then died on a 401 at the
        openai_compat call — the opposite of 'refuses to run without
        credentials'.
        """
        for missing, env_var in (
            ({"oss_key": "", "oss_model": "m"}, "ELT_TASKGEN_OSS_API_KEY"),
            ({"oss_key": "k", "oss_model": ""}, "ELT_TASKGEN_OSS_MODEL"),
            ({"oss_key": "   ", "oss_model": "m"}, "ELT_TASKGEN_OSS_API_KEY"),
        ):
            with self.subTest(env_var=env_var):
                routing = make_routing(api_key="sk-x", oss_base="http://x", **missing)
                problems = P.credential_problems(routing, ["independent_implementer"])
                self.assertTrue(
                    any(env_var in p for p in problems),
                    f"expected {env_var} to be reported, got {problems}",
                )


# ---------------------------------------------------------------------------
# RoutedProvider: memoization, replay fail-closed, budgets, missing keys
# ---------------------------------------------------------------------------

class RoutedProviderTest(unittest.TestCase):
    def _provider(self, tmp, *, responses=None, replay_only=False, refresh=False,
                  api_key="sk-test", fixtures=None, meter=None):
        transport = FakeTransport(responses or [])
        provider = P.RoutedProvider(
            make_routing(api_key=api_key),
            P.TranscriptStore(Path(tmp) / "transcripts", fixtures_dir=fixtures),
            meter or P.CostMeter(budget_per_task_usd=100.0),
            replay_only=replay_only,
            refresh=refresh,
            task_id="task-1",
            transports={"anthropic": transport, "openai_compat": transport},
        )
        return provider, transport

    def test_memoization_second_call_zero_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, transport = self._provider(
                tmp, responses=[anthropic_tool_response([VALID_FINDING])]
            )
            first = provider.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            self.assertEqual(len(transport.calls), 1)
            second = provider.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            self.assertEqual(len(transport.calls), 1)  # zero additional HTTP
            self.assertEqual(first, second)

    def test_live_call_is_recorded_before_returning(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = self._provider(
                tmp, responses=[anthropic_tool_response([VALID_FINDING])]
            )
            text = provider.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            sha = P.transcript_key("ambiguity_critic", "the prompt")
            path = Path(tmp) / "transcripts" / "ambiguity_critic" / f"{sha}.json"
            self.assertTrue(path.is_file())
            entry = json.loads(path.read_text())
            self.assertEqual(entry["response"], text)
            self.assertEqual(entry["prompt_sha256"], sha)
            self.assertEqual(entry["usage"]["input_tokens"], 1000)
            self.assertTrue(entry["raw_attempts"])

    def test_task_bound_transcript_records_response_attempts_and_zero_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = self._provider(
                tmp, responses=[anthropic_tool_response([VALID_FINDING])]
            )
            task_hash = "a" * 64
            provider.begin_task_evidence("task-1", task_hash)
            text = provider.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            sha = P.transcript_key("ambiguity_critic", "the prompt")
            entry = json.loads(
                (Path(tmp) / "transcripts" / "ambiguity_critic" / f"{sha}.json")
                .read_text()
            )
            self.assertEqual(entry["task_id"], "task-1")
            self.assertEqual(entry["task_content_hash"], task_hash)
            self.assertEqual(entry["response_sha256"], P.sha256_hex(text))
            self.assertEqual(entry["attempt_count"], 1)
            self.assertEqual(entry["correction_count"], 0)
            self.assertEqual(entry["finding_count"], 1)
            self.assertFalse(entry["zero_findings"])
            evidence = provider.exchange_evidence[0]
            self.assertEqual(evidence["prompt_sha256"], sha)
            self.assertEqual(evidence["response_sha256"], entry["response_sha256"])
            for field in ("behavior_sha256", "tools_sha256", "policy_sha256"):
                self.assertEqual(evidence[field], entry["route"][field])
            self.assertEqual(
                evidence["diagnostics_version"],
                entry["route"]["diagnostics_version"],
            )

    def test_task_bound_replay_refuses_a_different_content_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, _ = self._provider(
                tmp, responses=[anthropic_tool_response([VALID_FINDING])]
            )
            live.begin_task_evidence("task-1", "a" * 64)
            live.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")

            replay, transport = self._provider(tmp, replay_only=True)
            replay.begin_task_evidence("task-1", "b" * 64)
            with self.assertRaisesRegex(P.TranscriptMissingError, "current task identity"):
                replay.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            self.assertEqual(transport.calls, [])

    def test_live_memo_serves_same_task_entry_across_a_content_hash_move(self):
        """Certify addendum F2 (the 1.P.0 blocker): in LIVE mode an entry of
        the SAME task under the SAME key (an identical rendered view) whose
        response digest verifies is memo-served after the task's content hash
        moved — zero transport calls, an evidence row `replayed=True` bound
        to the CURRENT hash, nothing charged, the stored entry untouched —
        because what moved is private material the seat never saw. A
        different task and a response whose digest no longer verifies still
        refuse in live mode; replay-only keeps the exact binding (the test
        above)."""
        with tempfile.TemporaryDirectory() as tmp:
            live, transport = self._provider(
                tmp, responses=[anthropic_tool_response([VALID_FINDING])]
            )
            live.begin_task_evidence("task-1", "a" * 64)
            first = live.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            self.assertEqual(len(transport.calls), 1)
            sha = P.transcript_key("ambiguity_critic", "the prompt")
            path = Path(tmp) / "transcripts" / "ambiguity_critic" / f"{sha}.json"
            stored = path.read_bytes()
            spent = live.meter.per_task_usd.get("task-1", 0.0)

            live.begin_task_evidence("task-1", "b" * 64)
            served = live.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            self.assertEqual(served, first)
            self.assertEqual(len(transport.calls), 1)  # zero additional HTTP
            self.assertEqual(path.read_bytes(), stored)  # the store is not re-bound
            self.assertEqual(live.meter.per_task_usd.get("task-1", 0.0), spent)
            (row,) = live.exchange_evidence
            self.assertTrue(row["replayed"])
            self.assertEqual((row["task_id"], row["task_content_hash"]), ("task-1", "b" * 64))
            self.assertEqual(row["response_sha256"], P.sha256_hex(first))
            self.assertEqual(json.loads(path.read_text())["task_content_hash"], "a" * 64)

            # Another task under the same key: never served, in any mode.
            live.begin_task_evidence("task-2", "a" * 64)
            with self.assertRaisesRegex(P.TranscriptMissingError, "task_id 'task-1'"):
                live.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            self.assertEqual(len(transport.calls), 1)
            # A response whose digest no longer verifies: never served either.
            entry = json.loads(path.read_text())
            entry["response"] = entry["response"] + " "
            path.write_text(json.dumps(entry))
            live.begin_task_evidence("task-1", "b" * 64)
            with self.assertRaisesRegex(P.TranscriptMissingError, "response_sha256"):
                live.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            self.assertEqual(len(transport.calls), 1)

    def test_replay_only_missing_transcript_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, transport = self._provider(tmp, replay_only=True)
            with self.assertRaises(P.TranscriptMissingError) as ctx:
                provider.complete(CouncilRole.SHORTCUT_ATTACKER, "unseen prompt")
            self.assertEqual(transport.calls, [])  # never touched the network
            self.assertIn("record-transcripts", str(ctx.exception))

    def test_replay_only_serves_fixtures_without_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = Path(tmp) / "fixtures"
            # Exercise the legacy one-shot fixture path with a seat that is
            # still one-shot in the shipped profile.  The shortcut attacker
            # now has an enabled harness-validated session, whose route
            # identity intentionally rejects its old one-shot fixtures.
            sha = P.transcript_key("ambiguity_critic", "seen prompt")
            d = fixtures / "ambiguity_critic"
            d.mkdir(parents=True)
            # A fixture, like every real recording, names the route that
            # produced it; make_routing sends ambiguity_critic to sonnet.
            (d / f"{sha}.json").write_text(
                json.dumps({
                    "prompt_sha256": sha,
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "response": '{"findings": []}',
                })
            )
            provider, transport = self._provider(
                tmp, replay_only=True, api_key="", fixtures=fixtures
            )
            out = provider.complete(CouncilRole.AMBIGUITY_CRITIC, "seen prompt")
            self.assertEqual(out, '{"findings": []}')
            self.assertEqual(transport.calls, [])

    def test_missing_key_and_no_transcript_is_clean_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, transport = self._provider(tmp, api_key="")
            with self.assertRaises(P.MissingCredentialsError) as ctx:
                provider.complete(CouncilRole.AMBIGUITY_CRITIC, "prompt")
            self.assertEqual(transport.calls, [])
            message = str(ctx.exception)
            self.assertIn("ANTHROPIC_API_KEY", message)
            self.assertIn("no recorded transcript", message)

    def test_budget_breach_raises_but_transcript_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            meter = P.CostMeter(budget_per_task_usd=0.001)
            provider, _ = self._provider(
                tmp,
                responses=[anthropic_tool_response(
                    [VALID_FINDING], input_tokens=500_000, output_tokens=100_000
                )],
                meter=meter,
            )
            with self.assertRaises(P.BudgetExceededError):
                provider.complete(CouncilRole.AMBIGUITY_CRITIC, "prompt")
            sha = P.transcript_key("ambiguity_critic", "prompt")
            path = Path(tmp) / "transcripts" / "ambiguity_critic" / f"{sha}.json"
            self.assertTrue(path.is_file())  # the paid-for call was recorded

    def test_one_shot_transcript_entry_fields_are_stable(self):
        """Phase 0.D pins the one-shot exchange's recorded entry: exactly the
        pre-0.D fields plus `served_model`, `elapsed_ms` and the cache-token
        counters inside `usage`; the route block carries the 0.E digests. A
        refusal by the pre-flight reserve leaves NO transcript (nothing was
        spent), unlike a breach found after a recorded call."""
        baseline = {
            "role", "prompt_sha256", "system_sha256", "provider", "model", "response",
            "response_sha256", "task_id", "task_content_hash", "attempt_count",
            "correction_count", "finding_count", "usage", "raw_attempts", "route",
            "zero_findings",
        }
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = self._provider(
                tmp, responses=[anthropic_tool_response([VALID_FINDING])]
            )
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "prompt")
            sha = P.transcript_key("ambiguity_critic", "prompt")
            path = Path(tmp) / "transcripts" / "ambiguity_critic" / f"{sha}.json"
            entry = json.loads(path.read_text(encoding="utf-8"))
            stamp = set(provider._record_stamp())
            self.assertEqual(set(entry), baseline | {"served_model", "elapsed_ms"} | stamp)
            self.assertEqual(
                set(entry["usage"]),
                {"input_tokens", "output_tokens", "cache_read_input_tokens",
                 "cache_creation_input_tokens"},
            )
            self.assertEqual(
                set(entry["route"]),
                {"provider", "model", "max_tokens", "effort", "behavior_sha256",
                 "tools_sha256", "policy_sha256", "diagnostics_version", "entry_schema"},
            )
            self.assertEqual(entry["route"]["entry_schema"], 2)
            # The pre-0.D VALUES are what they were: one attempt, no
            # correction, the parsed finding count, the digests, the route,
            # the task binding; the exchange is metered ONCE at list price
            # (per-attempt charging of a single attempt is one computation,
            # bit-identical to the baseline single charge).
            self.assertEqual(entry["role"], "ambiguity_critic")
            self.assertEqual(entry["prompt_sha256"], sha)
            self.assertEqual(entry["system_sha256"], P.role_behavior_sha256("ambiguity_critic"))
            self.assertEqual((entry["provider"], entry["model"]), ("anthropic", "claude-sonnet-5"))
            self.assertEqual(entry["response_sha256"], sha256_hex(entry["response"]))
            self.assertEqual((entry["task_id"], entry["task_content_hash"]), ("task-1", ""))
            self.assertEqual((entry["attempt_count"], entry["correction_count"]), (1, 0))
            self.assertEqual((entry["finding_count"], entry["zero_findings"]), (1, False))
            self.assertEqual(len(entry["raw_attempts"]), 1)
            self.assertEqual(
                entry["usage"],
                {"input_tokens": 1000, "output_tokens": 200,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            )
            sonnet = P.rate_card_for("anthropic", "claude-sonnet-5", {})
            once = sonnet.usd_for(P.Usage(input_tokens=1000, output_tokens=200))
            self.assertEqual(provider.meter.total_usd, once)
            self.assertEqual(provider.meter.per_role["ambiguity_critic"]["attempts"], 1)
            self.assertEqual(len(provider.exchange_evidence), 1)
            self.assertEqual(provider.exchange_evidence[0]["usd"], once)
        with tempfile.TemporaryDirectory() as tmp:
            # Refused BEFORE transport: exit path 2 either way, but nothing
            # recorded — no transcript entry, no evidence row, nothing metered
            # — and the task scope is the base BudgetExceededError exactly
            # (infrastructure), never the role-scope subclass.
            meter = P.CostMeter(budget_per_task_usd=0.000001)
            provider, transport = self._provider(
                tmp, responses=[anthropic_tool_response([VALID_FINDING])], meter=meter
            )
            with self.assertRaises(P.BudgetExceededError) as ctx:
                provider.complete(CouncilRole.AMBIGUITY_CRITIC, "prompt")
            self.assertEqual(ctx.exception.scope, "task")
            self.assertIs(type(ctx.exception), P.BudgetExceededError)
            self.assertNotIsInstance(ctx.exception, P.RoleCapExceeded)
            self.assertIn("refused before the call, nothing spent", str(ctx.exception))
            self.assertEqual(transport.calls, [])
            self.assertFalse((Path(tmp) / "transcripts" / "ambiguity_critic").exists())
            self.assertEqual(meter.total_usd, 0.0)
            self.assertEqual(meter.per_role, {})
            self.assertEqual(provider.exchange_evidence, [])

    def test_refresh_forces_live_call_despite_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, transport = self._provider(
                tmp,
                responses=[
                    anthropic_tool_response([VALID_FINDING]),
                    anthropic_tool_response([VALID_FINDING]),
                ],
                refresh=True,
            )
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "prompt")
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "prompt")
            self.assertEqual(len(transport.calls), 2)

    # -- route binding at serve time (R3) ------------------------------------

    def _rerouted(self, tmp, *, model="claude-opus-5", effort="medium",
                  max_tokens=1024, replay_only=False, responses=None):
        """A second provider on the SAME store with ambiguity_critic re-routed."""
        routing = make_routing()
        roles = dict(routing.roles)
        roles["ambiguity_critic"] = P.RoleRoute(
            "ambiguity_critic", "anthropic", model, max_tokens, effort
        )
        rerouted = P.RoleRouting(
            roles=roles, provider_config=routing.provider_config, source="(rerouted)"
        )
        transport = FakeTransport(responses or [])
        provider = P.RoutedProvider(
            rerouted,
            P.TranscriptStore(Path(tmp) / "transcripts"),
            P.CostMeter(budget_per_task_usd=100.0),
            replay_only=replay_only,
            task_id="task-1",
            transports={"anthropic": transport},
        )
        return provider, transport

    def test_live_transcript_records_its_route_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = self._provider(
                tmp, responses=[anthropic_tool_response([VALID_FINDING])]
            )
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            sha = P.transcript_key("ambiguity_critic", "the prompt")
            entry = json.loads(
                (Path(tmp) / "transcripts" / "ambiguity_critic" / f"{sha}.json").read_text()
            )
            route = entry["route"]
            self.assertEqual(
                {k: route[k] for k in ("provider", "model", "max_tokens", "effort")},
                {"provider": "anthropic", "model": "claude-sonnet-5",
                 "max_tokens": 1024, "effort": "medium"},
            )
            # Phase 0.E (entry schema 2): the binding also names the behaviour,
            # wire-tool and policy digests the entry was recorded under, and
            # records (never hashes) the diagnostics version.
            self.assertEqual(route["behavior_sha256"], P.role_behavior_sha256("ambiguity_critic"))
            self.assertEqual(route["tools_sha256"], P.role_tools_sha256("ambiguity_critic"))
            self.assertEqual(
                route["policy_sha256"], P.session_policy_for("ambiguity_critic").sha256()
            )
            self.assertEqual(route["diagnostics_version"], P.DIAGNOSTICS_VERSION)
            self.assertEqual(route["entry_schema"], P.TRANSCRIPT_ENTRY_SCHEMA)
            self.assertEqual(P.transcript_entry_schema(entry), 2)
            self.assertNotIn("admission", entry)  # unstamped unless provided
            self.assertNotIn("recorded_by", entry)

    def test_transcript_from_other_model_is_a_miss_in_live_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, transport = self._provider(
                tmp, responses=[anthropic_tool_response([VALID_FINDING])]
            )
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            self.assertEqual(len(transport.calls), 1)
            other, other_transport = self._rerouted(
                tmp, responses=[anthropic_tool_response([VALID_FINDING])]
            )
            other.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            # The sonnet transcript is NOT served for the opus route: a live
            # call is made and the entry is rewritten under the new route.
            self.assertEqual(len(other_transport.calls), 1)
            sha = P.transcript_key("ambiguity_critic", "the prompt")
            entry = json.loads(
                (Path(tmp) / "transcripts" / "ambiguity_critic" / f"{sha}.json").read_text()
            )
            self.assertEqual(entry["model"], "claude-opus-5")
            self.assertEqual(entry["route"]["model"], "claude-opus-5")

    def test_transcript_from_other_model_fails_closed_in_replay_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = self._provider(
                tmp, responses=[anthropic_tool_response([VALID_FINDING])]
            )
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            other, other_transport = self._rerouted(tmp, replay_only=True)
            with self.assertRaises(P.TranscriptMissingError) as ctx:
                other.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            self.assertIsInstance(ctx.exception, P.TranscriptRouteMismatchError)
            message = str(ctx.exception)
            self.assertIn("claude-sonnet-5", message)
            self.assertIn("claude-opus-5", message)
            self.assertEqual(other_transport.calls, [])

    def test_unbound_route_model_replays_recorded_transcript(self):
        """route.model == '' is the un-interpolated ${ELT_TASKGEN_OSS_MODEL}:
        the documented 'missing credentials are allowed for replay' contract."""
        with tempfile.TemporaryDirectory() as tmp:
            routing = make_routing()  # implementer model 'test-oss-model'
            roles = dict(routing.roles)
            roles["independent_implementer"] = P.RoleRoute(
                "independent_implementer", "openai_compat", "", 2048, None
            )
            unbound = P.RoleRouting(
                roles=roles, provider_config=routing.provider_config, source="(t)"
            )
            store = P.TranscriptStore(Path(tmp) / "transcripts")
            sha = P.transcript_key("independent_implementer", "build")
            store.record("independent_implementer", sha, {
                "prompt_sha256": sha,
                "provider": "openai_compat",
                "model": "moonshotai/kimi-k2.7-code",
                "response": "the build",
            })
            provider = P.RoutedProvider(
                unbound, store, P.CostMeter(), replay_only=True
            )
            self.assertEqual(
                provider.complete("independent_implementer", "build"), "the build"
            )

    def test_entry_without_model_binding_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = self._provider(tmp, replay_only=True)
            sha = P.transcript_key("ambiguity_critic", "p")
            provider.store.record(
                "ambiguity_critic", sha, {"prompt_sha256": sha, "response": "R"}
            )
            with self.assertRaises(RuntimeError) as ctx:
                provider.complete(CouncilRole.AMBIGUITY_CRITIC, "p")
            self.assertIn("no provider/model binding", str(ctx.exception))

    def test_effort_change_misses_only_when_route_block_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            sha = P.transcript_key("ambiguity_critic", "p")
            store = P.TranscriptStore(Path(tmp) / "transcripts")
            # Legacy entry: provider/model only -> unbound on effort -> served.
            store.record("ambiguity_critic", sha, {
                "prompt_sha256": sha, "provider": "anthropic",
                "model": "claude-sonnet-5", "response": "legacy",
            })
            rerouted, _ = self._rerouted(
                tmp, model="claude-sonnet-5", effort="low", replay_only=True
            )
            self.assertEqual(rerouted.complete(CouncilRole.AMBIGUITY_CRITIC, "p"), "legacy")
            # Route-block entry at effort medium -> effort low is a mismatch.
            store.record("ambiguity_critic", sha, {
                "prompt_sha256": sha, "provider": "anthropic",
                "model": "claude-sonnet-5", "response": "bound",
                "route": {"provider": "anthropic", "model": "claude-sonnet-5",
                          "max_tokens": 1024, "effort": "medium"},
            })
            with self.assertRaises(P.TranscriptRouteMismatchError) as ctx:
                rerouted.complete(CouncilRole.AMBIGUITY_CRITIC, "p")
            self.assertIn("effort", str(ctx.exception))
            # And the matching route serves it with zero HTTP.
            same, transport = self._rerouted(
                tmp, model="claude-sonnet-5", effort="medium", replay_only=True
            )
            self.assertEqual(same.complete(CouncilRole.AMBIGUITY_CRITIC, "p"), "bound")
            self.assertEqual(transport.calls, [])

    def test_transcript_route_mismatch_helper(self):
        route = P.RoleRoute("r", "anthropic", "claude-opus-5", 1024, "high")
        self.assertIsNone(P.transcript_route_mismatch(
            {"provider": "anthropic", "model": "claude-opus-5"}, route
        ))
        self.assertIn("claude-sonnet-5", P.transcript_route_mismatch(
            {"provider": "anthropic", "model": "claude-sonnet-5"}, route
        ))
        self.assertIn("max_tokens", P.transcript_route_mismatch(
            {"route": {"provider": "anthropic", "model": "claude-opus-5",
                       "max_tokens": 2048, "effort": "high"}}, route
        ))
        with self.assertRaises(RuntimeError):
            P.transcript_route_mismatch({"response": "X"}, route)

    # -- admission provenance stamp (R1) --------------------------------------

    def test_live_transcript_carries_admission_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport([anthropic_tool_response([VALID_FINDING])])
            provider = P.RoutedProvider(
                make_routing(),
                P.TranscriptStore(Path(tmp) / "transcripts"),
                P.CostMeter(budget_per_task_usd=100.0),
                task_id="task-1",
                transports={"anthropic": transport},
                admission={"admission_evidence_sha256": "abc"},
                source="review",
            )
            self.assertEqual(
                provider.admission_provenance, {"admission_evidence_sha256": "abc"}
            )
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            sha = P.transcript_key("ambiguity_critic", "the prompt")
            entry = json.loads(
                (Path(tmp) / "transcripts" / "ambiguity_critic" / f"{sha}.json").read_text()
            )
            self.assertEqual(entry["admission"], {"admission_evidence_sha256": "abc"})
            self.assertEqual(entry["recorded_by"], "review")

    def test_admission_provenance_set_after_construction_is_stamped(self):
        """cli sets provider.admission_provenance once the gate passes."""
        with tempfile.TemporaryDirectory() as tmp:
            provider, _ = self._provider(
                tmp, responses=[anthropic_tool_response([VALID_FINDING])]
            )
            provider.admission_provenance = {"admission_mode": "admitted"}
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            sha = P.transcript_key("ambiguity_critic", "the prompt")
            entry = json.loads(
                (Path(tmp) / "transcripts" / "ambiguity_critic" / f"{sha}.json").read_text()
            )
            self.assertEqual(entry["admission"], {"admission_mode": "admitted"})

    def test_unstamped_legacy_transcript_still_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider, transport = self._provider(tmp, replay_only=True)
            sha = P.transcript_key("ambiguity_critic", "old prompt")
            provider.store.record("ambiguity_critic", sha, {
                "prompt_sha256": sha,
                "provider": "anthropic",
                "model": "claude-sonnet-5",
                "response": "legacy",
            })
            self.assertEqual(
                provider.complete(CouncilRole.AMBIGUITY_CRITIC, "old prompt"), "legacy"
            )
            self.assertEqual(transport.calls, [])

    def test_openai_compat_role_routes_to_chat_completions(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport([openai_text_response("built it")])
            provider = P.RoutedProvider(
                make_routing(oss_base="http://oss:8000/v1", oss_model="test-oss-model"),
                P.TranscriptStore(Path(tmp) / "transcripts"),
                P.CostMeter(budget_per_task_usd=100.0),
                task_id="task-1",
                transports={"openai_compat": transport},
            )
            out = provider.complete("independent_implementer", "build the marts")
            self.assertEqual(out, "built it")
            self.assertTrue(transport.calls[0][0].endswith("/chat/completions"))


# ---------------------------------------------------------------------------
# Model family resolution (R2: cross-family is about the MODEL, not the key)
# ---------------------------------------------------------------------------

class TestModelFamily(unittest.TestCase):
    def test_prefix_table(self):
        cases = {
            "claude-opus-5": "anthropic",
            "anthropic/claude-sonnet-4.5": "anthropic",
            "openrouter/anthropic/claude-3.5-sonnet:beta": "anthropic",
            "us.anthropic.claude-opus-5-v1:0": "anthropic",
            "moonshotai/kimi-k2.7-code:batch": "moonshot",
            "moonshotai/kimi-k2.7-code": "moonshot",
            "deepseek-chat": "deepseek",
            "openai/gpt-5": "openai",
            "gemini-2.5-pro": "google",
            "meta-llama/llama-4-maverick": "meta",
            "my-vllm-model": "unknown:my-vllm-model",
            "acme/served-model": "unknown:acme",
            "": "unknown:",
        }
        for model, family in cases.items():
            with self.subTest(model=model):
                self.assertEqual(P.model_family("openai_compat", model), family)

    def test_anthropic_provider_key_is_anthropic_family(self):
        self.assertEqual(P.model_family("anthropic", "anything"), "anthropic")

    def test_anthropic_host_wins(self):
        self.assertEqual(
            P.model_family(
                "openai_compat", "foo", {"base_url": "https://api.anthropic.com/v1"}
            ),
            "anthropic",
        )
        self.assertNotEqual(
            P.model_family(
                "openai_compat", "foo", {"base_url": "https://openrouter.ai/api/v1"}
            ),
            "anthropic",
        )
        self.assertEqual(P.endpoint_host({"base_url": "https://openrouter.ai/api/v1"}),
                         "openrouter.ai")
        self.assertEqual(P.endpoint_host({"base_url": ""}), "")
        self.assertEqual(P.endpoint_host(None), "")


# ---------------------------------------------------------------------------
# Role system prompts (review/prompts.py) — content and wire placement
# ---------------------------------------------------------------------------

class RoleSystemPromptTest(unittest.TestCase):
    """The five council roles carry written system prompts; the backends send
    them as the SYSTEM message while the user prompt stays EXACTLY the
    council view (information barriers live in council._view_for, prompts
    must never widen them)."""

    COUNCIL_ROLES = (
        "semantic_author",
        "ambiguity_critic",
        "population_adversary",
        "shortcut_attacker",
        "feasibility_reviewer",
    )

    def test_prompt_content_contracts(self):
        from elt_taskgen.review import prompts

        author = prompts.ROLE_SYSTEM["semantic_author"].lower()
        self.assertIn("no sql", author)          # forbids SQL outright
        self.assertIn("grain", author)
        self.assertIn("every rule", author)      # completeness demanded
        self.assertIn("tie-break", author)

        adversary = prompts.ROLE_SYSTEM["population_adversary"].lower()
        self.assertIn("name the population", adversary)
        self.assertIn("distinguishing rows", adversary)

        attacker = prompts.ROLE_SYSTEM["shortcut_attacker"].lower()
        self.assertIn("at least one", attacker)  # silence is a stage failure
        for kind in ("constants", "keys_only", "no_op", "skip_extraction"):
            self.assertIn(kind, attacker)

        feasibility = prompts.ROLE_SYSTEM["feasibility_reviewer"].lower()
        self.assertIn("sufficient", feasibility)

        # No critic prompt grants acceptance authority.
        for role in self.COUNCIL_ROLES[1:]:
            self.assertIn("no authority to accept", prompts.ROLE_SYSTEM[role].lower())

    def test_anthropic_backend_sends_role_system_prompt(self):
        from elt_taskgen.review import prompts

        transport = FakeTransport([anthropic_tool_response([VALID_FINDING])])
        backend = P.AnthropicBackend("sk-test", transport=transport)
        backend.complete(
            role_name="ambiguity_critic", model="claude-sonnet-5",
            prompt="THE VIEW", max_tokens=1024, effort=None,
        )
        payload = transport.calls[0][2]
        self.assertEqual(payload["system"], prompts.ROLE_SYSTEM["ambiguity_critic"])
        # The user message is EXACTLY the council view, nothing prepended.
        self.assertEqual(
            payload["messages"], [{"role": "user", "content": "THE VIEW"}]
        )

    def test_anthropic_prose_role_gets_author_system_prompt(self):
        from elt_taskgen.review import prompts

        transport = FakeTransport([anthropic_text_response("prose")])
        backend = P.AnthropicBackend("sk-test", transport=transport)
        backend.complete(
            role_name="semantic_author", model="claude-opus-5",
            prompt="THE VIEW", max_tokens=1024, effort=None,
        )
        payload = transport.calls[0][2]
        self.assertEqual(payload["system"], prompts.ROLE_SYSTEM["semantic_author"])
        self.assertNotIn("tools", payload)

    def test_anthropic_implementer_gets_no_system_prompt(self):
        transport = FakeTransport([anthropic_text_response("sql")])
        backend = P.AnthropicBackend("sk-test", transport=transport)
        backend.complete(
            role_name="independent_implementer", model="claude-opus-5",
            prompt="BUILD IT", max_tokens=1024, effort=None,
        )
        self.assertNotIn("system", transport.calls[0][2])

    def test_openai_backend_sends_role_system_message(self):
        from elt_taskgen.review import prompts

        transport = FakeTransport([openai_tool_response([VALID_FINDING])])
        backend = P.OpenAICompatBackend("http://oss:8000/v1", "", transport=transport)
        backend.complete(
            role_name="shortcut_attacker", model="test-oss-model",
            prompt="THE VIEW", max_tokens=1024, effort=None,
        )
        messages = transport.calls[0][2]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        # The executable critic's prompt is derived from its enabled session
        # block (the obsolete "no tools" sentence is removed).
        self.assertEqual(messages[0]["content"], prompts.role_system_prompt("shortcut_attacker"))
        self.assertEqual(messages[1], {"role": "user", "content": "THE VIEW"})

    def test_unknown_critic_role_falls_back_to_generic_instructions(self):
        generic_finding = {
            key: value for key, value in VALID_FINDING.items()
            if key != "proposed_case"
        }
        transport = FakeTransport([anthropic_tool_response([generic_finding])])
        backend = P.AnthropicBackend("sk-test", transport=transport)
        backend.complete(
            role_name="mystery_critic", model="claude-sonnet-5",
            prompt="THE VIEW", max_tokens=1024, effort=None,
        )
        system = transport.calls[0][2]["system"]
        self.assertIn("mystery_critic", system)
        self.assertIn("report_findings", system)


class DisabledRunnerBlockKeyStabilityTest(unittest.TestCase):
    """Review finding 1-0 (decision): a declared-but-DISABLED runner block
    (`roles.semantic_author.session`, `roles.repair_proposer.session`) does
    not enter the one-shot manifest — `role_manifest_limits` folds it to
    `{"enabled": false}` (mirroring `_harness_validators_for`) — so
    `session.enabled: false` is the byte-identical rollback config/agents.yaml
    promises: editing a disabled runner block's limits moves neither the
    behaviour digest nor the one-shot transcript key, while enabling it (or
    dropping the declaration) does. The reader `role_loop_limits` still
    returns the declared block verbatim (the runner, the CLI gate and the
    metrology row bound read it). The four critic seats are unchanged: their
    declared one-shot block is the SoT T1.1 identity ("production =
    metrology block", T7 row 229), hashed verbatim since 0.E. The stale author
    fixture is deliberately absent from the live replay store because its
    current system digest cannot be proved; this class pins the enabled and
    disabled key behavior without manufacturing replay evidence."""

    def setUp(self):
        P.clear_behavior_caches()
        self.addCleanup(P.clear_behavior_caches)

    @staticmethod
    def _patched(doc):
        return mock.patch.object(P, "_agents_doc", lambda: doc)

    def test_runner_roles_mirror_the_registry_declaration(self):
        from elt_taskgen.review.tools import registry as RG

        self.assertEqual(sorted(P.SESSION_RUNNER_ROLES), sorted(RG._DECLARED_ROLES))

    def test_disabled_runner_block_edits_do_not_move_the_one_shot_key_but_enabling_does(self):
        from elt_taskgen import demo_fixture
        from elt_taskgen.models import CouncilRole
        from elt_taskgen.review import council

        view = council.render_view(CouncilRole.SEMANTIC_AUTHOR, demo_fixture.demo_task())
        prompts = {"semantic_author": view, "repair_proposer": "a proposer prompt"}
        shipped_author_key = P.transcript_key("semantic_author", view)
        live = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "transcripts" / "semantic_author"
        self.assertEqual(sorted(p.stem for p in live.glob("*.json")), [])
        base = json.loads(json.dumps(P._agents_doc()))
        # The package now enables both runners by default.  This test covers
        # the documented rollback contract using an explicit disabled
        # profile; no response recorded under an obsolete author system prompt
        # is re-keyed into the enabled live store.
        for role in prompts:
            base["roles"][role]["session"]["enabled"] = False
        with self._patched(base):
            P.clear_behavior_caches()
            for role in prompts:
                declared = P.role_loop_limits(role)
                self.assertTrue(declared and not declared.get("enabled"), role)
                self.assertEqual(P.role_manifest_limits(role), {"enabled": False}, role)
                self.assertEqual(P.role_behavior_manifest(role)["loop_limits"], {"enabled": False}, role)
                self.assertEqual(P.session_policy_for(role).limits.as_manifest(), {"enabled": False}, role)
                self.assertEqual(P.role_behavior_manifest(role)["harness_validators"], [], role)
            keys = {role: P.transcript_key(role, prompt) for role, prompt in prompts.items()}
            digests = {role: P.role_behavior_sha256(role) for role in prompts}
            self.assertNotEqual(keys["semantic_author"], shipped_author_key)
        # (1) Editing the DISABLED block's limits: the reader sees the edit,
        # the manifest, the digest and the key do not move.
        edited = json.loads(json.dumps(base))
        edited["roles"]["semantic_author"]["session"].update({"max_usd": 0.75, "wall_clock_s": 299})
        edited["roles"]["repair_proposer"]["session"].update({"max_usd": 0.5, "max_turns": 4, "max_oracle_bits": 3})
        with self._patched(edited):
            P.clear_behavior_caches()
            for role, prompt in prompts.items():
                self.assertEqual(P.role_loop_limits(role)["max_usd"], edited["roles"][role]["session"]["max_usd"], role)
                self.assertEqual(P.role_manifest_limits(role), {"enabled": False}, role)
                self.assertEqual(P.transcript_key(role, prompt), keys[role], role)
                self.assertEqual(P.role_behavior_sha256(role), digests[role], role)
        # (2) Enabling the block: the declared limits enter the manifest
        # verbatim and the digest and the key move.
        enabled = json.loads(json.dumps(base))
        for role in prompts:
            enabled["roles"][role]["session"]["enabled"] = True
        with self._patched(enabled):
            P.clear_behavior_caches()
            for role, prompt in prompts.items():
                manifest = P.role_behavior_manifest(role)
                self.assertEqual(manifest["loop_limits"], P.role_loop_limits(role), role)
                self.assertTrue(manifest["loop_limits"]["enabled"], role)
                self.assertEqual(P.session_policy_for(role).limits.as_manifest(), P.role_loop_limits(role), role)
                self.assertNotEqual(P.transcript_key(role, prompt), keys[role], role)
                self.assertNotEqual(P.role_behavior_sha256(role), digests[role], role)
        # (3) Dropping the declaration: `{}` is not `{"enabled": false}`.
        dropped = json.loads(json.dumps(base))
        for role in prompts:
            del dropped["roles"][role]["session"]
        with self._patched(dropped):
            P.clear_behavior_caches()
            for role, prompt in prompts.items():
                self.assertEqual(P.role_loop_limits(role), {}, role)
                self.assertEqual(P.role_manifest_limits(role), {}, role)
                self.assertNotEqual(P.transcript_key(role, prompt), keys[role], role)
        with self._patched(base):
            P.clear_behavior_caches()
            for role, prompt in prompts.items():
                self.assertEqual(P.transcript_key(role, prompt), keys[role], role)
        # A disabled block is still VALIDATED at load: a default above its
        # hard cap is refused whether or not the seat is enabled.
        bad = json.loads(json.dumps(base))
        bad["roles"]["repair_proposer"]["session"]["max_turns"] = 99
        with self._patched(bad):
            P.clear_behavior_caches()
            with self.assertRaises(ValueError):
                P.role_manifest_limits("repair_proposer")
        P.clear_behavior_caches()

    def test_critic_seat_blocks_stay_hashed_verbatim_and_a_bare_role_is_unmoved(self):
        before = P.transcript_key("ambiguity_critic", "p")
        bare = P.transcript_key("audit_triage", "q")
        doc = json.loads(json.dumps(P._agents_doc()))
        doc["roles"]["ambiguity_critic"]["session"]["max_wall_s"] = 301
        with self._patched(doc):
            P.clear_behavior_caches()
            self.assertFalse(P.role_loop_limits("ambiguity_critic")["enabled"])
            self.assertEqual(P.role_manifest_limits("ambiguity_critic"), P.role_loop_limits("ambiguity_critic"))
            self.assertEqual(P.role_behavior_manifest("ambiguity_critic")["loop_limits"]["max_wall_s"], 301)
            self.assertNotEqual(P.transcript_key("ambiguity_critic", "p"), before)
            self.assertEqual(P.transcript_key("audit_triage", "q"), bare)
            self.assertEqual(P.role_manifest_limits("audit_triage"), {})
        P.clear_behavior_caches()


if __name__ == "__main__":
    unittest.main()


class StringifiedFindingsCoercionTest(unittest.TestCase):
    """Measured live (metrology, claude-sonnet-5, 4/4 attempts): the
    non-strict proposal roles can return the findings ARRAY JSON-encoded as a
    single string. The payload the model meant is losslessly recoverable, so
    the validator decodes it instead of burning three corrective retries
    reproducing the same stringification."""

    def _finding(self):
        return {
            "severity": "major",
            "summary": "s",
            "detail": "d",
            "route_hint": None,
            "suggested_attack": None,
            "proposed_case": None,
        }

    def test_stringified_findings_array_is_decoded_and_validates(self):
        import json as _json
        from elt_taskgen.review.providers import _validate_findings_payload

        payload = {"findings": _json.dumps([self._finding()])}
        self.assertIsNone(_validate_findings_payload(payload))
        # normalized IN PLACE: downstream text-building sees the real list
        self.assertIsInstance(payload["findings"], list)

    def test_stringified_non_list_json_is_still_rejected(self):
        from elt_taskgen.review.providers import _validate_findings_payload

        problem = _validate_findings_payload({"findings": '{"a": 1}'})
        self.assertIn("not a quoted string", problem)

    def test_decoded_findings_are_validated_on_the_merits(self):
        import json as _json
        from elt_taskgen.review.providers import _validate_findings_payload

        bad_finding = self._finding()
        bad_finding["severity"] = "not-a-severity"
        bad = {"findings": _json.dumps([bad_finding])}
        problem = _validate_findings_payload(bad)
        self.assertIsNotNone(problem)
        self.assertIn("invalid severity", problem)


class StringifiedFindingsHardeningTest(unittest.TestCase):
    """Stringified arrays may be decoded only when their content is complete."""

    def test_control_characters_inside_values_are_tolerated(self):
        from elt_taskgen.review.providers import _validate_findings_payload

        stringified = (
            '[{"severity":"major","summary":"line one\nline two",'
            '"detail":"d","route_hint":null,"suggested_attack":null,'
            '"proposed_case":null}]'
        )
        payload = {"findings": stringified}
        self.assertIsNone(_validate_findings_payload(payload))
        self.assertEqual(payload["findings"][0]["summary"], "line one\nline two")

    def test_truncated_trailing_element_is_a_protocol_failure(self):
        from elt_taskgen.review.providers import _validate_findings_payload

        truncated = (
            '[{"severity":"major","summary":"a","detail":"d",'
            '"route_hint":null,"suggested_attack":null,"proposed_case":null},'
            ' {"severity":"minor","summary":"b","detail":"d",'
            '"route_hint":null,"suggested_attack":null,"proposed_case":null},'
            ' {"severity": "major", "summ'
        )
        payload = {"findings": truncated}
        problem = _validate_findings_payload(payload)
        self.assertIsNotNone(problem)
        self.assertIn("could not be decoded", problem)
        self.assertIsInstance(payload["findings"], str)

    def test_undecodable_string_gets_a_teaching_correction(self):
        from elt_taskgen.review.providers import _validate_findings_payload

        problem = _validate_findings_payload({"findings": "][ not salvageable"})
        self.assertIsNotNone(problem)
        self.assertIn("JSON array", problem)
        self.assertIn("not a quoted string", problem)


class ProposedCaseTeachingCorrectionTest(unittest.TestCase):
    """Round 3 (metrology attempt 3): a proposed_case pydantic failure fed the
    retry only pydantic's count header ('1 validation error for
    ProposedAttackCase'), field and reason discarded — the model reproduced
    the same invalid proposal 3/3. The correction must carry field + reason."""

    def test_invalid_proposal_correction_names_field_and_reason(self):
        from elt_taskgen.review.providers import _validate_findings_payload

        payload = {"findings": [{
            "severity": "major", "summary": "s", "detail": "d",
            "route_hint": None, "suggested_attack": "no_dedup",
            "proposed_case": {"kind": "no_dedup"},  # missing required fields
        }]}
        problem = _validate_findings_payload(payload)
        self.assertIsNotNone(problem)
        self.assertIn("proposed_case is invalid", problem)
        # must NOT be just the pydantic count header
        self.assertNotRegex(problem, r"invalid: \d+ validation error")
        # must name at least one offending field
        self.assertRegex(problem, r"invalid: \S+: ")


# ---------------------------------------------------------------------------
# Money and instrumentation (roadmap Phase 0.D; Cost T8 items 1-7)
# ---------------------------------------------------------------------------

class _FaultingTransport:
    """Transport double: canned responses, then a transport fault."""

    def __init__(self, responses, fault):
        self.responses = list(responses)
        self.fault = fault
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url, headers, payload):
        self.calls.append((url, dict(headers), json.loads(json.dumps(payload))))
        if not self.responses:
            raise self.fault
        return self.responses.pop(0)


def _routed(tmp, transport, *, meter=None, routing=None):
    return P.RoutedProvider(
        routing or make_routing(),
        P.TranscriptStore(Path(tmp) / "transcripts"),
        meter or P.CostMeter(budget_per_task_usd=100.0),
        task_id="task-1",
        transports={"anthropic": transport, "openai_compat": transport},
    )


class FourRatePricingTest(unittest.TestCase):
    """The price table is four rates per model and Sonnet 5 is $2 / $10."""

    def test_sonnet_5_is_priced_at_two_and_ten(self):
        card = P.ANTHROPIC_PRICING_USD_PER_MTOK["claude-sonnet-5"]
        self.assertEqual((card.input, card.output), (2.00, 10.00))
        self.assertEqual(P.rates_for("anthropic", "claude-sonnet-5", {}), (2.00, 10.00))
        self.assertEqual(
            P.rate_card_for("anthropic", "claude-sonnet-5", {}),
            P.RateCard(2.00, 2.50, 0.20, 10.00),
        )
        # Opus and Haiku unchanged on the (input, output) pair.
        self.assertEqual(P.rates_for("anthropic", "claude-opus-5", {}), (5.00, 25.00))
        self.assertEqual(P.rates_for("anthropic", "claude-haiku-4-5", {}), (1.00, 5.00))

    def test_cost_meter_prices_cache_reads_at_one_tenth(self):
        """Cost T8 item 2: cache reads at 0.1x input, cache writes at 1.25x,
        on every Anthropic route, through `CostMeter.charge` with the four
        counters — and the per-role readings keep the cache counters apart."""
        for model, card in P.ANTHROPIC_PRICING_USD_PER_MTOK.items():
            with self.subTest(model=model):
                self.assertAlmostEqual(card.cache_read, 0.1 * card.input)
                self.assertAlmostEqual(card.cache_write, 1.25 * card.input)
        meter = P.CostMeter(budget_per_task_usd=1000.0)
        sonnet = P.rate_card_for("anthropic", "claude-sonnet-5", {})
        read_usd = meter.charge(
            task_id="t", role_name="ambiguity_critic", rates=sonnet,
            usage=P.Usage(cache_read_input_tokens=1_000_000),
        )
        self.assertAlmostEqual(read_usd, 0.20)
        self.assertAlmostEqual(read_usd, 0.1 * 2.00)
        write_usd = meter.charge(
            task_id="t", role_name="ambiguity_critic", rates=sonnet,
            usage=P.Usage(cache_creation_input_tokens=1_000_000),
        )
        self.assertAlmostEqual(write_usd, 2.50)
        mixed = meter.charge(
            task_id="t", role_name="ambiguity_critic", rates=sonnet,
            usage=P.Usage(
                input_tokens=1_000_000, output_tokens=1_000_000,
                cache_read_input_tokens=1_000_000,
                cache_creation_input_tokens=1_000_000,
            ),
        )
        self.assertAlmostEqual(mixed, 2.00 + 10.00 + 0.20 + 2.50)
        reading = meter.per_role["ambiguity_critic"]
        self.assertEqual(reading["cache_read_input_tokens"], 2_000_000)
        self.assertEqual(reading["cache_creation_input_tokens"], 2_000_000)
        self.assertEqual(reading["input_tokens"], 1_000_000)
        self.assertEqual(reading["attempts"], 3)

    def test_legacy_two_rate_charge_prices_uncached(self):
        """An (input, output) pair prices cache traffic AT THE INPUT RATE:
        a route nobody priced for caching may overstate, never understate."""
        meter = P.CostMeter(budget_per_task_usd=1000.0)
        usd = meter.charge(
            task_id="t", role_name="r", rates=(1.0, 4.0),
            usage=P.Usage(
                cache_read_input_tokens=1_000_000,
                cache_creation_input_tokens=1_000_000,
            ),
        )
        self.assertAlmostEqual(usd, 2.0)
        self.assertEqual(P.RateCard.coerce((1.0, 4.0)), P.RateCard(1.0, 1.0, 1.0, 4.0))

    def test_openai_compat_cache_rates_are_optional_and_default_to_input(self):
        cfg = {
            "pricing": {
                "m": {
                    "usd_per_mtok_input": 0.70,
                    "usd_per_mtok_output": 3.50,
                    "usd_per_mtok_cache_read": 0.175,
                }
            }
        }
        card = P.rate_card_for("openai_compat", "m", cfg)
        self.assertEqual(card, P.RateCard(0.70, 0.70, 0.175, 3.50))
        self.assertEqual(P.rates_for("openai_compat", "m", cfg), (0.70, 3.50))

    def test_default_budget_per_task_is_five_dollars(self):
        self.assertEqual(P.DEFAULT_BUDGET_PER_TASK_USD, 5.00)
        self.assertEqual(P.CostMeter().budget_per_task_usd, 5.00)

    def test_chat_usage_splits_cached_prompt_tokens(self):
        """`prompt_tokens` INCLUDES the cached share on the chat wire; the
        meter must not count it twice."""
        usage = P.Usage.from_chat({
            "usage": {
                "prompt_tokens": 1000, "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 600},
            }
        })
        self.assertEqual(usage.input_tokens, 400)
        self.assertEqual(usage.cache_read_input_tokens, 600)
        self.assertEqual(usage.output_tokens, 50)
        plain = P.Usage.from_chat({"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        self.assertEqual((plain.input_tokens, plain.cache_read_input_tokens), (10, 0))


class ReserveAndTrajectoryBudgetTest(unittest.TestCase):
    """Cost T8 item 3: a per-(role, trajectory) budget object with `reserve`
    before each turn, so a limit stops BEFORE the breaching turn."""

    def test_trajectory_budget_stops_before_breaching_turn(self):
        meter = P.CostMeter(budget_per_task_usd=100.0)
        opus = P.rate_card_for("anthropic", "claude-opus-5", {})
        trajectory = meter.trajectory("task-1", "semantic_author", max_usd=1.00)
        turn_1 = P.Usage(input_tokens=80_000, output_tokens=16_000)
        trajectory.reserve(P.estimate_turn_usd(rates=opus, max_tokens=8192, prompt="x" * 400))
        spent = trajectory.charge(usage=turn_1, rates=opus, elapsed_ms=1200)
        self.assertAlmostEqual(spent, 0.80)
        self.assertAlmostEqual(trajectory.spent_usd, 0.80)
        self.assertAlmostEqual(trajectory.remaining_usd, 0.20)
        # The next turn's estimate (state-machine rule: last input at the
        # input rate, max(last output, 0.25 x max_tokens) at the output rate)
        # would breach the trajectory's own cap: refused, nothing spent.
        estimate = P.estimate_turn_usd(rates=opus, max_tokens=8192, last_usage=turn_1)
        self.assertAlmostEqual(estimate, 0.80)
        with self.assertRaises(P.BudgetExceededError) as ctx:
            trajectory.reserve(estimate)
        self.assertEqual(ctx.exception.scope, "role")
        self.assertIsInstance(ctx.exception, P.RoleCapExceeded)
        self.assertAlmostEqual(meter.total_usd, 0.80)            # unchanged
        self.assertAlmostEqual(trajectory.spent_usd, 0.80)       # unchanged
        self.assertEqual(trajectory.attempt_count, 1)
        self.assertTrue(trajectory.would_exceed(estimate))
        self.assertFalse(trajectory.would_exceed(0.10))
        # The TASK budget is the meter's, a different scope.
        tight = P.CostMeter(budget_per_task_usd=1.00)
        tight_trajectory = tight.trajectory("task-1", "semantic_author")
        tight_trajectory.charge(usage=turn_1, rates=opus)
        with self.assertRaises(P.BudgetExceededError) as ctx:
            tight_trajectory.reserve(estimate)
        self.assertEqual(ctx.exception.scope, "task")
        self.assertIs(type(ctx.exception), P.BudgetExceededError)
        self.assertAlmostEqual(tight.total_usd, 0.80)

    def test_session_reserve_raises_before_transport_call(self):
        """A session INIT reserve (`max_usd` + nested ceiling) the task budget
        cannot absorb raises with ZERO transport calls, nothing spent, nothing
        recorded — as does a one-shot call whose prompt alone breaches."""
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport([anthropic_tool_response([VALID_FINDING])])
            meter = P.CostMeter(budget_per_task_usd=0.50)
            provider = _routed(tmp, transport, meter=meter)
            with self.assertRaises(P.BudgetExceededError) as ctx:
                provider.meter.reserve(
                    task_id="task-1", role_name="repair_proposer",
                    est_usd=0.80 + 0.56,
                )
            self.assertEqual(ctx.exception.scope, "task")
            self.assertEqual(transport.calls, [])
            self.assertEqual(meter.total_usd, 0.0)
            self.assertEqual(meter.per_role, {})
            # The one-shot path reserves the prompt floor before the call.
            tiny = P.CostMeter(budget_per_task_usd=0.001)
            provider = _routed(tmp, transport, meter=tiny)
            prompt = "x" * 4000  # ~1,000 tokens at $2/MTok = $0.002 > $0.001
            with self.assertRaises(P.BudgetExceededError) as ctx:
                provider.complete(CouncilRole.AMBIGUITY_CRITIC, prompt)
            self.assertIn("refused before the call", str(ctx.exception))
            self.assertEqual(transport.calls, [])
            self.assertEqual(tiny.total_usd, 0.0)
            sha = P.transcript_key("ambiguity_critic", prompt)
            self.assertFalse(
                (Path(tmp) / "transcripts" / "ambiguity_critic" / f"{sha}.json").exists()
            )
            self.assertEqual(provider.exchange_evidence, [])

    def test_role_cap_is_enforced_beside_the_task_budget(self):
        """A declared per-role cap (`session.max_usd`) trips on its own; a
        role without one is uncapped, so today's one-shot paths are unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport([
                anthropic_tool_response([VALID_FINDING], input_tokens=300_000, output_tokens=0),
                anthropic_tool_response([VALID_FINDING], input_tokens=300_000, output_tokens=0),
            ])
            meter = P.CostMeter(
                budget_per_task_usd=100.0,
                budget_per_role_usd={"ambiguity_critic": 0.50},
            )
            provider = _routed(tmp, transport, meter=meter)
            self.assertEqual(meter.role_cap_usd("ambiguity_critic"), 0.50)
            self.assertIsNone(meter.role_cap_usd("shortcut_attacker"))
            # 300k input tokens on sonnet = $0.60 > the $0.50 cap: the call
            # is made (the floor estimate is tiny), recorded, then the cap
            # breach raises with scope "role".
            with self.assertRaises(P.BudgetExceededError) as ctx:
                provider.complete(CouncilRole.AMBIGUITY_CRITIC, "p")
            self.assertEqual(ctx.exception.scope, "role")
            self.assertIsInstance(ctx.exception, P.RoleCapExceeded)
            self.assertAlmostEqual(meter.total_usd, 0.60)
            sha = P.transcript_key("ambiguity_critic", "p")
            self.assertTrue(
                (Path(tmp) / "transcripts" / "ambiguity_critic" / f"{sha}.json").is_file()
            )
            # An uncapped role on the same meter spends freely.
            provider.complete(CouncilRole.SHORTCUT_ATTACKER, "q")
            self.assertEqual(len(transport.calls), 2)

    def test_role_cap_breach_is_role_cap_exceeded_not_infrastructure(self):
        """A role's OWN cap is the agent-attributable `LIMIT_USD` stop of SoT
        T4 (the session maps it to disposition blocked_limit, limit usd), not
        a harness fault: both `reserve` and `charge` raise RoleCapExceeded, a
        BudgetExceededError subclass whose NAME is not in the engine's
        infrastructure set, while the task and total scopes stay the base
        class exactly (infrastructure, exit 2)."""
        from elt_taskgen import engine as engine_mod

        self.assertTrue(issubclass(P.RoleCapExceeded, P.BudgetExceededError))
        self.assertIn("RoleCapExceeded", P.__all__)
        self.assertIn("BudgetExceededError", engine_mod._INFRA_EXCEPTION_NAMES)
        self.assertNotIn(P.RoleCapExceeded.__name__, engine_mod._INFRA_EXCEPTION_NAMES)
        self.assertEqual(P.RoleCapExceeded("x").scope, "role")
        self.assertEqual(P.RoleCapExceeded("x", scope="role").scope, "role")
        with self.assertRaises(ValueError):
            P.RoleCapExceeded("x", scope="task")
        opus = P.rate_card_for("anthropic", "claude-opus-5", {})
        one_dollar = P.Usage(input_tokens=200_000, output_tokens=0)  # $5/MTok in
        self.assertAlmostEqual(opus.usd_for(one_dollar), 1.00)
        meter = P.CostMeter(
            budget_per_task_usd=100.0,
            budget_total_usd=100.0,
            budget_per_role_usd={"ambiguity_critic": 0.50},
        )
        # `reserve`: the cap branch refuses with nothing spent.
        with self.assertRaises(P.RoleCapExceeded) as ctx:
            meter.reserve(task_id="t", role_name="ambiguity_critic", est_usd=0.60)
        self.assertEqual(ctx.exception.scope, "role")
        self.assertIn("refused before the call, nothing spent", str(ctx.exception))
        self.assertEqual(meter.total_usd, 0.0)
        # `charge`: the cap branch raises AFTER accounting.
        trajectory = meter.trajectory("t", "ambiguity_critic")
        with self.assertRaises(P.RoleCapExceeded) as ctx:
            trajectory.charge(usage=one_dollar, rates=opus)
        self.assertEqual(ctx.exception.scope, "role")
        self.assertIn("IS recorded", str(ctx.exception))
        self.assertAlmostEqual(trajectory.spent_usd, 1.00)
        self.assertAlmostEqual(meter.total_usd, 1.00)
        # An explicit per-trajectory cap is the same stop.
        with self.assertRaises(P.RoleCapExceeded):
            meter.trajectory("t", "semantic_author", max_usd=0.10).reserve(0.20)
        # The task and total budgets: the base class, exactly, from both verbs.
        for kwargs, scope in (
            ({"budget_per_task_usd": 0.50}, "task"),
            ({"budget_per_task_usd": 100.0, "budget_total_usd": 0.50}, "total"),
        ):
            with self.subTest(scope=scope):
                tight = P.CostMeter(**kwargs)
                with self.assertRaises(P.BudgetExceededError) as ctx:
                    tight.reserve(task_id="t", role_name="ambiguity_critic", est_usd=0.60)
                self.assertIs(type(ctx.exception), P.BudgetExceededError)
                self.assertEqual(ctx.exception.scope, scope)
                with self.assertRaises(P.BudgetExceededError) as ctx:
                    tight.trajectory("t", "ambiguity_critic").charge(usage=one_dollar, rates=opus)
                self.assertIs(type(ctx.exception), P.BudgetExceededError)
                self.assertEqual(ctx.exception.scope, scope)

    def test_one_shot_critic_three_maxed_attempts_never_trip_a_disabled_session_cap(self):
        """With every seat at `session.enabled: false`, a one-shot critic
        exchange of 1 + SCHEMA_RETRIES MAXED Opus attempts (about $0.46 each,
        together above the declared $1.00 sized for 'two maxed-out turns')
        completes exactly as at baseline — never scope=role — through the
        whole RoutedProvider path: with the production meter (enforced caps)
        AND with a meter wrongly built from the DECLARED caps, because the
        trajectory of a declared-but-disabled seat ignores the role cap. The
        identical exchange on an ENABLED seat trips RoleCapExceeded after the
        recorded attempt that crossed the cap."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agents.yaml"
            path.write_text(
                "providers:\n  anthropic:\n    api_key: sk-test\n"
                "roles:\n"
                "  ambiguity_critic:\n"
                "    provider: anthropic\n    model: claude-opus-5\n"
                "    max_tokens: 16384\n    effort: high\n"
                "    session: {enabled: false, mode: one_shot, max_model_calls: 3,"
                " max_tool_calls: 0, max_wall_s: 300, max_usd: 1.00}\n"
                "  shortcut_attacker:\n"
                "    provider: anthropic\n    model: claude-opus-5\n"
                "    max_tokens: 16384\n    effort: high\n"
                "    session: {enabled: true, mode: one_shot, max_model_calls: 3,"
                " max_tool_calls: 0, max_wall_s: 300, max_usd: 1.00}\n"
            )
            routing = P.load_role_routing(path)
            disabled = routing.for_role("ambiguity_critic")
            enabled = routing.for_role("shortcut_attacker")
            self.assertEqual((disabled.max_usd, disabled.session_enabled), (1.00, False))
            self.assertTrue(disabled.cap_declared_but_disabled)
            self.assertFalse(disabled.cap_enforced)
            self.assertTrue(enabled.cap_enforced)
            self.assertFalse(enabled.cap_declared_but_disabled)
            self.assertEqual(routing.usd_caps(), {"shortcut_attacker": 1.00})
            self.assertEqual(
                routing.declared_usd_caps(), {"ambiguity_critic": 1.00, "shortcut_attacker": 1.00}
            )
            rates = P.rate_card_for("anthropic", "claude-opus-5", {})
            per_attempt = rates.usd_for(P.Usage(input_tokens=10_000, output_tokens=16_384))
            self.assertLess(2 * per_attempt, 1.00)                        # the cap's sizing
            self.assertGreater((1 + P.SCHEMA_RETRIES) * per_attempt, 1.00)  # the one-shot max

            def maxed_exchange():
                attempts = [anthropic_bad_tool_response() for _ in range(P.SCHEMA_RETRIES)]
                attempts.append(anthropic_tool_response([VALID_FINDING]))
                for response in attempts:
                    response["model"] = "claude-opus-5"
                    response["usage"] = {"input_tokens": 10_000, "output_tokens": 16_384}
                return attempts

            # (1) The production wiring: the meter holds the ENFORCED caps.
            transport = FakeTransport(maxed_exchange())
            meter = P.CostMeter(budget_per_task_usd=100.0, budget_per_role_usd=routing.usd_caps())
            provider = _routed(Path(tmp) / "a", transport, meter=meter, routing=routing)
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "p")
            self.assertEqual(len(transport.calls), 1 + P.SCHEMA_RETRIES)
            reading = meter.per_role["ambiguity_critic"]
            self.assertEqual(reading["attempts"], 1 + P.SCHEMA_RETRIES)
            self.assertGreater(reading["usd"], 1.00)
            self.assertAlmostEqual(reading["usd"], (1 + P.SCHEMA_RETRIES) * per_attempt)
            self.assertEqual(provider.exchange_evidence[0]["attempt_count"], 1 + P.SCHEMA_RETRIES)
            # (2) A meter built from the DECLARED caps: the disabled seat's
            # trajectory ignores the role cap, so the same exchange completes.
            transport = FakeTransport(maxed_exchange())
            declared = P.CostMeter(
                budget_per_task_usd=100.0, budget_per_role_usd=routing.declared_usd_caps()
            )
            self.assertEqual(declared.role_cap_usd("ambiguity_critic"), 1.00)
            self.assertIsNone(declared.trajectory("t", "ambiguity_critic", enforce_role_cap=False).max_usd)
            self.assertEqual(declared.trajectory("t", "ambiguity_critic").max_usd, 1.00)
            self.assertEqual(  # an explicit override still applies
                declared.trajectory("t", "ambiguity_critic", max_usd=0.5, enforce_role_cap=False).max_usd,
                0.5,
            )
            provider = _routed(Path(tmp) / "b", transport, meter=declared, routing=routing)
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "p")
            self.assertEqual(len(transport.calls), 1 + P.SCHEMA_RETRIES)
            self.assertGreater(declared.per_role["ambiguity_critic"]["usd"], 1.00)
            # (3) The ENABLED seat: the third recorded attempt crosses $1.00
            # and the exchange stops as RoleCapExceeded, transcript on disk.
            transport = FakeTransport(maxed_exchange())
            meter = P.CostMeter(budget_per_task_usd=100.0, budget_per_role_usd=routing.usd_caps())
            provider = _routed(Path(tmp) / "c", transport, meter=meter, routing=routing)
            with self.assertRaises(P.RoleCapExceeded) as ctx:
                provider.complete(CouncilRole.SHORTCUT_ATTACKER, "p")
            self.assertEqual(ctx.exception.scope, "role")
            self.assertEqual(len(transport.calls), 1 + P.SCHEMA_RETRIES)
            self.assertEqual(meter.per_role["shortcut_attacker"]["attempts"], 1 + P.SCHEMA_RETRIES)
            key = provider.transcript_key_for(CouncilRole.SHORTCUT_ATTACKER, "p")
            self.assertTrue(
                (Path(tmp) / "c" / "transcripts" / "shortcut_attacker" / f"{key}.json").is_file()
            )


class ChargePerAttemptTest(unittest.TestCase):
    """Cost T8 item 4: charge per API attempt, including the attempts of a
    backend that raises (32% of the fivetran run's spend was invisible)."""

    def test_charge_covers_every_api_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            # (a) A schema retry then success: TWO attempts, both metered.
            transport = FakeTransport([
                anthropic_bad_tool_response(),
                anthropic_tool_response([VALID_FINDING]),
            ])
            meter = P.CostMeter(budget_per_task_usd=100.0)
            provider = _routed(tmp, transport, meter=meter)
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "p")
            reading = meter.per_role["ambiguity_critic"]
            self.assertEqual(reading["attempts"], 2)
            self.assertEqual(reading["input_tokens"], 2000)
            self.assertEqual(reading["output_tokens"], 400)
            self.assertAlmostEqual(reading["usd"], 2 * (1000 * 2.00 + 200 * 10.00) / 1e6)
            self.assertEqual(meter.attempt_count, 2)
            # (b) Retries exhausted: the backend RAISES, and all three paid
            # attempts still reach the meter before the fault propagates.
            transport = FakeTransport([anthropic_bad_tool_response() for _ in range(3)])
            meter = P.CostMeter(budget_per_task_usd=100.0)
            provider = _routed(tmp, transport, meter=meter)
            with self.assertRaises(ProviderProtocolError):
                provider.complete(CouncilRole.SHORTCUT_ATTACKER, "p")
            self.assertEqual(len(transport.calls), 1 + P.SCHEMA_RETRIES)
            reading = meter.per_role["shortcut_attacker"]
            self.assertEqual(reading["attempts"], 3)
            self.assertEqual(reading["input_tokens"], 3000)
            self.assertAlmostEqual(reading["usd"], 3 * (1000 * 5.00 + 200 * 25.00) / 1e6)
            self.assertAlmostEqual(meter.total_usd, reading["usd"])
            # Nothing valid was produced, so nothing is recorded or evidenced.
            self.assertEqual(provider.exchange_evidence, [])

    def test_transport_fault_on_second_attempt_meters_the_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            fault = RuntimeError("provider HTTP 529 from https://api: overloaded")
            transport = _FaultingTransport([anthropic_bad_tool_response()], fault)
            meter = P.CostMeter(budget_per_task_usd=100.0)
            provider = _routed(tmp, transport, meter=meter)
            with self.assertRaises(RuntimeError) as ctx:
                provider.complete(CouncilRole.AMBIGUITY_CRITIC, "p")
            self.assertIs(ctx.exception, fault)
            self.assertEqual(len(P.backend_attempts(fault)), 1)
            self.assertEqual(meter.per_role["ambiguity_critic"]["attempts"], 1)
            self.assertAlmostEqual(
                meter.total_usd, (1000 * 2.00 + 200 * 10.00) / 1e6
            )

    def test_budget_breach_inside_a_raising_backend_chains_onto_the_fault(self):
        """The attempts of a raising backend are charged; when THEY breach the
        budget, the breach is what propagates (the fault stays as context)."""
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport([
                anthropic_bad_tool_response() for _ in range(3)
            ])
            meter = P.CostMeter(budget_per_task_usd=0.005)  # 3 x $0.004
            provider = _routed(tmp, transport, meter=meter)
            with self.assertRaises(P.BudgetExceededError) as ctx:
                provider.complete(CouncilRole.AMBIGUITY_CRITIC, "p")
            self.assertIsInstance(ctx.exception.__context__, ProviderProtocolError)
            # All three transports happened before post-call cap enforcement;
            # the first breach is retained, but no paid attempt is hidden.
            self.assertEqual(meter.per_role["ambiguity_critic"]["attempts"], 3)
            self.assertAlmostEqual(meter.total_usd, 3 * 0.004)


class ExchangeInstrumentationTest(unittest.TestCase):
    """Cost T8 items 6-7: elapsed_ms and the served model id per exchange,
    cache counters in the transcript `usage`, T8 fields on evidence rows."""

    def test_transcript_entry_records_cache_fields_elapsed_and_served_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            response = anthropic_tool_response(
                [VALID_FINDING], input_tokens=400, output_tokens=50,
                model="claude-sonnet-5-20260901",
            )
            response["usage"]["cache_read_input_tokens"] = 6000
            response["usage"]["cache_creation_input_tokens"] = 1500
            transport = FakeTransport([response])
            meter = P.CostMeter(budget_per_task_usd=100.0)
            provider = _routed(tmp, transport, meter=meter)
            provider.begin_task_evidence("task-1", "a" * 64)
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            sha = P.transcript_key("ambiguity_critic", "the prompt")
            entry = json.loads(
                (Path(tmp) / "transcripts" / "ambiguity_critic" / f"{sha}.json").read_text()
            )
            self.assertEqual(entry["usage"], {
                "input_tokens": 400, "output_tokens": 50,
                "cache_read_input_tokens": 6000,
                "cache_creation_input_tokens": 1500,
            })
            self.assertIsInstance(entry["elapsed_ms"], int)
            self.assertGreaterEqual(entry["elapsed_ms"], 0)
            self.assertEqual(entry["served_model"], "claude-sonnet-5-20260901")
            self.assertEqual(entry["model"], "claude-sonnet-5")  # what was asked for
            # Priced at four rates: 400 x 2 + 50 x 10 + 6000 x 0.2 + 1500 x 2.5
            expected = (400 * 2.00 + 50 * 10.00 + 6000 * 0.20 + 1500 * 2.50) / 1e6
            self.assertAlmostEqual(meter.total_usd, expected)
            row = provider.exchange_evidence[0]
            self.assertEqual(row["usage"], {
                "input": 400, "output": 50, "cache_read": 6000, "cache_write": 1500,
            })
            self.assertAlmostEqual(row["usd"], expected)
            self.assertEqual(row["wall_ms"], entry["elapsed_ms"])
            self.assertFalse(row["replayed"])
            # A replayed exchange evidences the same usage at $0 and 0 ms.
            replay = P.RoutedProvider(
                make_routing(),
                P.TranscriptStore(Path(tmp) / "transcripts"),
                P.CostMeter(budget_per_task_usd=100.0),
                replay_only=True,
                task_id="task-1",
                transports={"anthropic": FakeTransport([])},
            )
            replay.begin_task_evidence("task-1", "a" * 64)
            replay.complete(CouncilRole.AMBIGUITY_CRITIC, "the prompt")
            replayed_row = replay.exchange_evidence[0]
            self.assertTrue(replayed_row["replayed"])
            self.assertEqual(replayed_row["usd"], 0.0)
            self.assertEqual(replayed_row["wall_ms"], 0)
            self.assertEqual(replayed_row["usage"]["cache_read"], 6000)

    def test_legacy_transcript_without_cache_fields_still_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = Path(tmp) / "fixtures"
            # Keep this a legacy ONE-SHOT replay.  Shortcut attacker now has
            # a session route binding and correctly refuses its former key.
            sha = P.transcript_key("ambiguity_critic", "seen prompt")
            (fixtures / "ambiguity_critic").mkdir(parents=True)
            (fixtures / "ambiguity_critic" / f"{sha}.json").write_text(json.dumps({
                "prompt_sha256": sha, "provider": "anthropic",
                "model": "claude-sonnet-5", "response": '{"findings": []}',
                "usage": {"input_tokens": 12, "output_tokens": 3},
            }))
            provider = P.RoutedProvider(
                make_routing(api_key=""),
                P.TranscriptStore(Path(tmp) / "transcripts", fixtures_dir=fixtures),
                P.CostMeter(budget_per_task_usd=100.0),
                replay_only=True,
                task_id="task-1",
                transports={"anthropic": FakeTransport([])},
            )
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "seen prompt")
            row = provider.exchange_evidence[0]
            self.assertEqual(row["usage"], {"input": 12, "output": 3, "cache_read": 0, "cache_write": 0})
            self.assertEqual(row["wall_ms"], 0)


class PromptCacheGateTest(unittest.TestCase):
    """Cost T8 item 1: cache_control breakpoints exist in `_payload` but are
    OFF for every role today, so one-shot payloads (and the recorded fixtures
    keyed on them) are byte-identical; session roles turn it on in Phase 1."""

    def test_one_shot_payload_carries_no_cache_control_for_any_routed_role(self):
        # Only critics, author/proposer, and witnesses declare disabled loop
        # limits; no role enables prompt caching.
        declared_caps = {
            "ambiguity_critic": 1.00,
            "population_adversary": 1.00,
            "shortcut_attacker": 1.00,
            "feasibility_reviewer": 1.00,
            # Batch-repair D9 (2026-09-09): the author cap follows its third
            # turn (the block's `max_usd` = its hard cap).
            "semantic_author": 2.75,
            "repair_proposer": 4.00,
            "independent_implementer": 0.50,
            "independent_loader": 0.20,
        }
        for routing in (P.load_role_routing(), P.load_role_routing(P.default_agents_config_path())):
            for role_name, route in sorted(routing.roles.items()):
                with self.subTest(source=routing.source, role=role_name):
                    self.assertFalse(route.prompt_cache)
                    self.assertEqual(route.max_usd, declared_caps.get(role_name))
                    if route.provider != "anthropic":
                        continue
                    payload = P.AnthropicBackend._payload(
                        model=route.model, prompt="PROMPT",
                        max_tokens=route.max_tokens, effort=route.effort,
                        role_name=role_name,
                        schema_mode=P.uses_findings_schema(role_name),
                        prompt_cache=route.prompt_cache,
                    )
                    self.assertNotIn("cache_control", json.dumps(payload))
                    self.assertEqual(
                        payload["messages"], [{"role": "user", "content": "PROMPT"}]
                    )
                    if "system" in payload:
                        self.assertIsInstance(payload["system"], str)
        # The live backend sends exactly the flag-off payload by default.
        transport = FakeTransport([anthropic_tool_response([VALID_FINDING])])
        backend = P.AnthropicBackend("sk-test", transport=transport)
        backend.complete(
            role_name="ambiguity_critic", model="claude-sonnet-5",
            prompt="PROMPT", max_tokens=1024, effort="medium",
        )
        self.assertNotIn("cache_control", json.dumps(transport.calls[0][2]))

    def test_prompt_cache_flag_adds_two_breakpoints(self):
        off = P.AnthropicBackend._payload(
            model="claude-opus-5", prompt="TASK VIEW", max_tokens=1024,
            effort="high", role_name="ambiguity_critic", schema_mode=True,
        )
        on = P.AnthropicBackend._payload(
            model="claude-opus-5", prompt="TASK VIEW", max_tokens=1024,
            effort="high", role_name="ambiguity_critic", schema_mode=True,
            prompt_cache=True,
        )
        # Block 1: the system block (caches tools + system ahead of it).
        self.assertEqual(
            on["system"],
            [{"type": "text", "text": off["system"], "cache_control": {"type": "ephemeral"}}],
        )
        # Block 2: the task view, the first user turn.
        self.assertEqual(
            on["messages"],
            [{"role": "user", "content": [
                {"type": "text", "text": "TASK VIEW", "cache_control": {"type": "ephemeral"}}
            ]}],
        )
        # Everything else on the wire is untouched by the flag.
        self.assertEqual(on["tools"], off["tools"])
        self.assertEqual(on["tool_choice"], off["tool_choice"])
        self.assertEqual(on["output_config"], off["output_config"])
        self.assertEqual(
            {k: v for k, v in on.items() if k not in ("system", "messages")},
            {k: v for k, v in off.items() if k not in ("system", "messages")},
        )

    def test_routing_parses_session_max_usd_and_prompt_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agents.yaml"
            path.write_text(
                "providers:\n  anthropic:\n    api_key: k\n"
                "roles:\n"
                "  ambiguity_critic:\n"
                "    provider: anthropic\n    model: claude-opus-5\n"
                "    max_tokens: 4096\n    effort: high\n"
                "    session: {enabled: false, mode: one_shot, max_model_calls: 3,"
                " max_tool_calls: 0, max_wall_s: 300, max_usd: 1.00}\n"
                "  semantic_author:\n"
                "    provider: anthropic\n    model: claude-opus-5\n"
                "    max_tokens: 8192\n    effort: high\n"
                "    session: {enabled: false, max_usd: 1.00, prompt_cache: true}\n"
                "  feasibility_reviewer:\n"
                "    provider: anthropic\n    model: claude-haiku-4-5\n"
                "    max_tokens: 2048\n"
            )
            routing = P.load_role_routing(path)
            self.assertEqual(routing.for_role("ambiguity_critic").max_usd, 1.00)
            self.assertFalse(routing.for_role("ambiguity_critic").prompt_cache)
            self.assertTrue(routing.for_role("semantic_author").prompt_cache)
            self.assertIsNone(routing.for_role("feasibility_reviewer").max_usd)
            # DECLARED caps are read for every role; ENFORCED caps only for a
            # role whose session is enabled (both blocks here are disabled).
            self.assertEqual(
                routing.declared_usd_caps(),
                {"ambiguity_critic": 1.00, "semantic_author": 1.00},
            )
            self.assertEqual(routing.usd_caps(), {})
            # Neither key enters the route binding recorded beside transcripts
            # (it is provider, model, max_tokens, effort plus the Phase 0.E
            # behaviour, tool and policy digests); `max_usd` moves the
            # behaviour digest through `loop_limits` instead.
            block = P.RoutedProvider._route_block(routing.for_role("semantic_author"))
            self.assertNotIn("max_usd", block)
            self.assertNotIn("prompt_cache", block)
            self.assertEqual(
                set(block),
                {"provider", "model", "max_tokens", "effort", "behavior_sha256",
                 "tools_sha256", "policy_sha256", "diagnostics_version", "entry_schema"},
            )
            meter = P.CostMeter(budget_per_role_usd=routing.declared_usd_caps())
            self.assertEqual(meter.role_cap_usd("ambiguity_critic"), 1.00)
            self.assertIsNone(meter.role_cap_usd("feasibility_reviewer"))
            self.assertIsNone(
                P.CostMeter(budget_per_role_usd=routing.usd_caps()).role_cap_usd("ambiguity_critic")
            )

    def test_one_shot_role_cap_not_enforced_when_session_disabled(self):
        """A `session.max_usd` sized for a bounded session ($1.00 'covers two
        maxed-out turns') must not trip a ONE-SHOT seat: the one-shot path
        permits 1 + SCHEMA_RETRIES maxed attempts, so with `session.enabled:
        false` the cap is declared (hashed into the manifest) but never
        enforced — `--budget-per-task` still bounds the seat. An enabled
        session's cap trips as scope=role; a top-level `max_usd` outside any
        session block is an explicit one-shot cap and is enforced."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agents.yaml"
            path.write_text(
                "roles:\n"
                "  ambiguity_critic:\n"
                "    provider: anthropic\n    model: claude-opus-5\n"
                "    max_tokens: 16384\n    effort: high\n"
                "    session: {enabled: false, mode: one_shot, max_model_calls: 3,"
                " max_tool_calls: 0, max_wall_s: 300, max_usd: 1.00}\n"
                "  feasibility_reviewer:\n"
                "    provider: anthropic\n    model: claude-haiku-4-5\n"
                "    max_tokens: 8192\n"
                "    session: {enabled: true, max_usd: 0.25}\n"
                "  semantic_author:\n"
                "    provider: anthropic\n    model: claude-opus-5\n"
                "    max_tokens: 8192\n    max_usd: 2.00\n"
            )
            routing = P.load_role_routing(path)
            self.assertFalse(routing.for_role("ambiguity_critic").session_enabled)
            self.assertTrue(routing.for_role("feasibility_reviewer").session_enabled)
            self.assertTrue(routing.for_role("semantic_author").max_usd_top_level)
            self.assertEqual(routing.usd_caps(), {"feasibility_reviewer": 0.25, "semantic_author": 2.00})
            self.assertEqual(
                routing.declared_usd_caps(),
                {"ambiguity_critic": 1.00, "feasibility_reviewer": 0.25, "semantic_author": 2.00},
            )
            meter = P.CostMeter(budget_per_task_usd=100.0, budget_per_role_usd=routing.usd_caps())
            rates = P.rate_card_for("anthropic", "claude-opus-5", {})
            maxed = P.Usage(input_tokens=10_000, output_tokens=16_384)
            self.assertGreater((1 + P.SCHEMA_RETRIES) * rates.usd_for(maxed), 1.00)
            trajectory = meter.trajectory("t1", "ambiguity_critic")
            for _ in range(1 + P.SCHEMA_RETRIES):
                trajectory.reserve(
                    P.estimate_turn_usd(rates=rates, max_tokens=16_384, prompt="x" * 40_000)
                )
                trajectory.charge(usage=maxed, rates=rates)
            self.assertGreater(trajectory.spent_usd, 1.00)  # never raised scope=role
            enabled = meter.trajectory("t1", "feasibility_reviewer")
            with self.assertRaises(P.BudgetExceededError) as ctx:
                for _ in range(1 + P.SCHEMA_RETRIES):
                    enabled.charge(usage=maxed, rates=rates)
            self.assertEqual(ctx.exception.scope, "role")
            # The repository profile enables the six useful bounded roles;
            # only the two advisory readers remain one-shot.  Thus all eight
            # caps are declared and the active six are enforced.
            live = P.load_role_routing(None)
            self.assertEqual(
                live.usd_caps(),
                {
                    "independent_implementer": 0.5,
                    "independent_loader": 0.2,
                    "population_adversary": 1.0,
                    "repair_proposer": 4.0,
                    "semantic_author": 2.75,
                    "shortcut_attacker": 1.0,
                },
            )
            self.assertEqual(
                set(live.declared_usd_caps()),
                {"ambiguity_critic", "population_adversary", "shortcut_attacker",
                 "feasibility_reviewer", "semantic_author", "repair_proposer",
                 "independent_implementer", "independent_loader"},
            )
            for role_name in ("ambiguity_critic", "feasibility_reviewer"):
                self.assertTrue(live.for_role(role_name).cap_declared_but_disabled, role_name)
            for role_name in live.usd_caps():
                self.assertFalse(live.for_role(role_name).cap_declared_but_disabled, role_name)


# ---------------------------------------------------------------------------
# Phase 0.E: behaviour manifest, transcript key v3, entry schema 2
# ---------------------------------------------------------------------------

class BehaviorManifestTest(unittest.TestCase):
    """`role_behavior_sha256` is the sha256 of the canonical behaviour
    manifest; the manifest's `tools[]` are the bytes `_payload` sends; the
    transcript key and the route binding cover the tool protocol."""

    def setUp(self):
        P.clear_behavior_caches()
        self.addCleanup(P.clear_behavior_caches)

    @staticmethod
    def _routing():
        return P.load_role_routing(None)

    def test_role_behavior_sha256_is_sha256_of_canonical_manifest(self):
        from elt_taskgen.models import canonical_json

        for role in ("ambiguity_critic", "semantic_author", "audit_triage",
                     "independent_implementer", "repair_proposer"):
            manifest = P.role_behavior_manifest(role)
            self.assertEqual(
                P.role_behavior_sha256(role), sha256_hex(canonical_json(manifest)), role
            )
            # Every SoT T7 field is present, and nothing environmental is.
            self.assertEqual(
                set(manifest),
                {"role", "system_prompt_sha256", "tools", "tool_choice_policy",
                 "harness_validators", "policy_sha256", "loop_limits",
                 "correction_text_sha256", "schema_retries", "api_version",
                 "validators", "projections", "sandbox"},
            )
            self.assertEqual(manifest["schema_retries"], P.SCHEMA_RETRIES)
            self.assertEqual(manifest["api_version"], P.AnthropicBackend.API_VERSION)
            self.assertEqual(manifest["correction_text_sha256"], sha256_hex(P.CORRECTION_TEXT))
            self.assertEqual(manifest["validators"], {"code": {}, "binaries": {}})
            self.assertEqual(manifest["projections"], {})
            expected_harness = (
                [{"name": "check_prose"}] if role == "semantic_author"
                # The witness's submission dry run (2026-09-11, batch10 run O).
                else [{"name": "check_submission"}] if role == "independent_implementer"
                else []
            )
            self.assertEqual(manifest["harness_validators"], expected_harness)
            self.assertEqual(
                set(manifest["sandbox"]), {"runtime", "image_digest", "workspace_template_sha256"}
            )
        # Same manifest, same digest, in a fresh call (no clock, path or seed).
        self.assertEqual(
            P.role_behavior_manifest("shortcut_attacker"),
            P.role_behavior_manifest("shortcut_attacker"),
        )

    def test_behavior_manifest_equals_wire_tools(self):
        """The manifest's `tools[]` and `tool_choice_policy` are BYTE-EQUAL to
        what `AnthropicBackend._payload` sends, for every role: the forced
        `report_findings` / `report_triage` object for a schema role
        (roadmap R-G), nothing for a prose role."""
        routing = self._routing()
        roles = sorted(set(routing.roles) | {P.AUDIT_TRIAGE_ROLE, "solver__tier"})
        for role in roles:
            with self.subTest(role=role):
                manifest = P.role_behavior_manifest(role)
                policy = P.session_policy_for(role)
                if P.role_is_agentic(role):
                    # Enabled bounded roles travel through the session wire;
                    # one-shot `_payload` deliberately has no prose tools.
                    payload = P.AnthropicBackend._session_payload(
                        model="claude-opus-5", messages=[{"role": "user", "content": "VIEW"}],
                        max_tokens=64, effort=None, role_name=role,
                        tools=[dict(tool) for tool in policy.wire_tools],
                        tool_choice=P.session_tool_choice(policy),
                    )
                else:
                    payload = P.AnthropicBackend._payload(
                        model="claude-opus-5", prompt="VIEW", max_tokens=64, effort=None,
                        role_name=role, schema_mode=P.uses_findings_schema(role),
                    )
                self.assertEqual(
                    json.dumps(manifest["tools"], sort_keys=True),
                    json.dumps(payload.get("tools", []), sort_keys=True),
                )
                self.assertEqual(manifest["tool_choice_policy"], payload.get("tool_choice"))
                if P.uses_findings_schema(role):
                    self.assertEqual(manifest["tools"][0]["name"], P.tool_name_for(role))
                    self.assertEqual(manifest["tools"][0]["input_schema"], P.tool_schema_for(role))
                    self.assertTrue(manifest["tools"][0]["strict"])
                    self.assertEqual(
                        manifest["tool_choice_policy"],
                        {"type": "tool", "name": P.tool_name_for(role)},
                    )
                elif not P.role_is_agentic(role):
                    self.assertEqual(manifest["tools"], [])
                    self.assertIsNone(manifest["tool_choice_policy"])
                # The digests in the route block are digests of the same list.
                self.assertEqual(
                    P.role_tools_sha256(role), P.session_policy_for(role).tools_sha256()
                )
        # The chat-completions backend sends the SAME tool, field for field.
        transport = FakeTransport([openai_tool_response([VALID_FINDING])])
        backend = P.OpenAICompatBackend("http://oss:8000/v1", "", transport=transport)
        backend.complete(
            role_name="ambiguity_critic", model="m", prompt="VIEW",
            max_tokens=64, effort=None,
        )
        function = transport.calls[0][2]["tools"][0]["function"]
        wire = P.role_behavior_manifest("ambiguity_critic")["tools"][0]
        self.assertEqual(function["name"], wire["name"])
        self.assertEqual(function["description"], wire["description"])
        self.assertEqual(function["parameters"], wire["input_schema"])

    def test_transcript_key_covers_tool_protocol(self):
        """Editing the forced tool's description, its schema, the tool-choice
        policy, the correction text, `SCHEMA_RETRIES`, a loop limit or the
        sandbox pin re-keys the role's transcripts. Before Phase 0.E none of
        these moved the key (it hashed the system prompt alone)."""
        role = "ambiguity_critic"
        prompt = "VIEW"
        before = P.transcript_key(role, prompt)
        before_manifest = P.role_behavior_sha256(role)
        moved: dict[str, str] = {}

        def key_under(label, patcher):
            with patcher:
                P.clear_behavior_caches()
                moved[label] = P.transcript_key(role, prompt)
                self.assertNotEqual(P.role_behavior_sha256(role), before_manifest, label)
            P.clear_behavior_caches()

        key_under(
            "tool description",
            mock.patch.object(P, "_tool_description_for", lambda r: "Report findings (edited)."),
        )
        key_under(
            "tool schema",
            mock.patch.object(
                P, "tool_schema_for",
                lambda r: {**P.findings_tool_schema(r), "description": "edited"},
            ),
        )
        key_under(
            "tool choice policy",
            mock.patch.object(P, "tool_choice_for", lambda r, **kw: {"type": "any"}),
        )
        key_under("correction text", mock.patch.object(P, "CORRECTION_TEXT", "Fix it: {problem}"))
        key_under("schema retries", mock.patch.object(P, "SCHEMA_RETRIES", 3))
        doc = json.loads(json.dumps(P._agents_doc()))
        doc["roles"][role]["session"]["max_wall_s"] = 301
        key_under("loop limits", mock.patch.object(P, "_agents_doc", lambda: doc))
        pinned = json.loads(json.dumps(P._agents_doc()))
        pinned["metrology"]["sandbox"] = {
            "runtime": "docker", "image_digest": "sha256:" + "a" * 64,
            "workspace_template_sha256": "b" * 64,
        }
        key_under("sandbox pin", mock.patch.object(P, "_agents_doc", lambda: pinned))
        for label, key in moved.items():
            self.assertNotEqual(key, before, label)
        self.assertEqual(len(set(moved.values())), len(moved), "each edit keys apart")
        # Restored, the key is exactly what it was: content, not events.
        self.assertEqual(P.transcript_key(role, prompt), before)
        # ... and the fingerprint the admission binds to moved with each edit.
        from elt_taskgen.review import metrology as metrology_mod

        routing = self._routing()
        fp_before = metrology_mod.council_routing_fingerprint(routing)
        with mock.patch.object(P, "_tool_description_for", lambda r: "edited"):
            P.clear_behavior_caches()
            self.assertNotEqual(metrology_mod.council_routing_fingerprint(routing), fp_before)
        P.clear_behavior_caches()
        self.assertEqual(metrology_mod.council_routing_fingerprint(routing), fp_before)

    def test_fingerprint_moves_when_tool_schema_description_policy_limits_or_pin_change(self):
        """The `test_fingerprint_moves_when_*` family, provider side: each
        manifest input the roadmap names moves `council_routing_fingerprint`
        through the critic's `role_behavior_sha256`."""
        from elt_taskgen.review import metrology as metrology_mod

        routing = self._routing()
        before = metrology_mod.council_routing_fingerprint(routing)
        doc = json.loads(json.dumps(P._agents_doc()))
        doc["roles"]["shortcut_attacker"]["session"]["max_model_calls"] = 4
        pinned = json.loads(json.dumps(P._agents_doc()))
        pinned["metrology"]["sandbox"]["runtime"] = "docker"
        cases = {
            "tool schema": mock.patch.object(
                P, "tool_schema_for", lambda r: {**P.findings_tool_schema(r), "title": "x"}
            ),
            "tool description": mock.patch.object(P, "_tool_description_for", lambda r: "x"),
            "tool policy": mock.patch.object(P, "tool_choice_for", lambda r, **kw: {"type": "auto"}),
            "loop limits": mock.patch.object(P, "_agents_doc", lambda: doc),
            "correction text": mock.patch.object(P, "CORRECTION_TEXT", "x {problem}"),
            "sandbox pin": mock.patch.object(P, "_agents_doc", lambda: pinned),
        }
        for label, patcher in cases.items():
            with self.subTest(change=label), patcher:
                P.clear_behavior_caches()
                self.assertNotEqual(metrology_mod.council_routing_fingerprint(routing), before)
            P.clear_behavior_caches()
        self.assertEqual(metrology_mod.council_routing_fingerprint(routing), before)

    def test_custom_agents_config_moves_manifest_key_and_route_block(self):
        """The behaviour manifest, transcript key and route block hash the
        agents document the RoutedProvider was loaded from (`--agents-config`,
        `agents_config_of(routing)`), never always the repository default. A
        custom document whose session block differs moves the critic's
        digest and key (an unchanged role's stay put, and the default path
        is the zero-argument document); its transcripts are recorded and
        served under that key, and a provider on the DEFAULT document refuses
        them as a behaviour-manifest route mismatch."""
        from elt_taskgen.models import canonical_json

        doc = json.loads(json.dumps(P._agents_doc()))
        doc.setdefault("providers", {}).setdefault("anthropic", {})["api_key"] = "sk-test"
        doc["roles"]["ambiguity_critic"]["session"]["max_wall_s"] = 301
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agents.yaml"
            path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
            routing = P.load_role_routing(path)
            self.assertEqual(P.agents_config_of(routing), path)
            self.assertIsNone(P.agents_config_of(P.load_role_routing(None)))
            self.assertIsNone(P.agents_config_of(P.load_role_routing(P.default_agents_config_path())))
            self.assertIsNone(P.agents_config_of(make_routing()))
            self.assertEqual(
                P.role_behavior_sha256("ambiguity_critic"),
                P.role_behavior_sha256(
                    "ambiguity_critic", agents_config=P.default_agents_config_path()
                ),
            )
            # The changed role moves; an unchanged role does not.
            custom = P.role_behavior_manifest("ambiguity_critic", agents_config=path)
            self.assertEqual(custom["loop_limits"]["max_wall_s"], 301)
            self.assertEqual(
                P.role_behavior_manifest("ambiguity_critic")["loop_limits"]["max_wall_s"], 300
            )
            custom_sha = P.role_behavior_sha256("ambiguity_critic", agents_config=path)
            self.assertEqual(custom_sha, sha256_hex(canonical_json(custom)))
            self.assertNotEqual(custom_sha, P.role_behavior_sha256("ambiguity_critic"))
            self.assertEqual(
                P.role_behavior_sha256("shortcut_attacker", agents_config=path),
                P.role_behavior_sha256("shortcut_attacker"),
            )
            self.assertEqual(P.sandbox_pin(agents_config=path), P.sandbox_pin())
            self.assertEqual(P.role_loop_limits("ambiguity_critic", agents_config=path)["max_wall_s"], 301)
            custom_key = P.transcript_key("ambiguity_critic", "p", agents_config=path)
            default_key = P.transcript_key("ambiguity_critic", "p")
            self.assertNotEqual(custom_key, default_key)
            self.assertEqual(
                P.transcript_key("shortcut_attacker", "p", agents_config=path),
                P.transcript_key("shortcut_attacker", "p"),
            )
            route = routing.for_role("ambiguity_critic")
            default_block = P.RoutedProvider._route_block(route)
            custom_block = P.RoutedProvider._route_block(route, agents_config=path)
            self.assertEqual(custom_block["behavior_sha256"], custom_sha)
            self.assertNotEqual(custom_block["behavior_sha256"], default_block["behavior_sha256"])
            self.assertNotEqual(custom_block["policy_sha256"], default_block["policy_sha256"])
            self.assertEqual(custom_block["tools_sha256"], default_block["tools_sha256"])
            self.assertEqual(
                {k: v for k, v in custom_block.items() if k not in ("behavior_sha256", "policy_sha256")},
                {k: v for k, v in default_block.items() if k not in ("behavior_sha256", "policy_sha256")},
            )
            # Through the provider: recorded under the custom key with the
            # custom digests, then served back with zero HTTP.
            transport = FakeTransport(
                [anthropic_tool_response([VALID_FINDING], model="claude-opus-5")]
            )
            store_dir = Path(tmp) / "transcripts"
            provider = P.RoutedProvider(
                routing, P.TranscriptStore(store_dir), P.CostMeter(budget_per_task_usd=100.0),
                task_id="task-1", transports={"anthropic": transport},
            )
            self.assertEqual(provider.agents_config, path)
            self.assertEqual(provider.transcript_key_for(CouncilRole.AMBIGUITY_CRITIC, "p"), custom_key)
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "p")
            recorded = store_dir / "ambiguity_critic" / f"{custom_key}.json"
            self.assertTrue(recorded.is_file())
            self.assertFalse((store_dir / "ambiguity_critic" / f"{default_key}.json").exists())
            entry = json.loads(recorded.read_text(encoding="utf-8"))
            self.assertEqual(entry["route"], custom_block)
            self.assertEqual(entry["system_sha256"], custom_sha)
            provider.complete(CouncilRole.AMBIGUITY_CRITIC, "p")
            self.assertEqual(len(transport.calls), 1)
            # A provider on the DEFAULT document never serves it: a copy filed
            # under the default key is a behaviour-manifest route mismatch,
            # fail closed under replay-only.
            copied = dict(entry, prompt_sha256=default_key)
            (store_dir / "ambiguity_critic" / f"{default_key}.json").write_text(
                json.dumps(copied), encoding="utf-8"
            )
            replay = P.RoutedProvider(
                P.load_role_routing(None), P.TranscriptStore(store_dir),
                P.CostMeter(budget_per_task_usd=100.0), task_id="task-1",
                replay_only=True, transports={"anthropic": FakeTransport([])},
            )
            self.assertIsNone(replay.agents_config)
            with self.assertRaises(P.TranscriptRouteMismatchError) as ctx:
                replay.complete(CouncilRole.AMBIGUITY_CRITIC, "p")
            self.assertIn("behaviour manifest", str(ctx.exception))
            self.assertIsNotNone(P.transcript_route_mismatch(copied, route))
            self.assertIsNone(P.transcript_route_mismatch(copied, route, agents_config=path))

    def test_disabled_session_block_keeps_harness_validators_off_the_manifest(self):
        """A critic seat's declared-but-disabled block (SoT T1.1) puts its
        harness validators neither on the wire nor in the manifest's
        `harness_validators`; flipping `enabled` is the Phase 4 re-earn."""
        disabled_doc = json.loads(json.dumps(P._agents_doc()))
        disabled_doc["roles"]["population_adversary"]["session"]["enabled"] = False
        with mock.patch.object(P, "_agents_doc", lambda: disabled_doc):
            P.clear_behavior_caches()
            manifest = P.role_behavior_manifest("population_adversary")
        self.assertEqual(manifest["loop_limits"]["harness_validators"], ["compile_proposal"])
        self.assertFalse(manifest["loop_limits"]["enabled"])
        self.assertEqual(manifest["harness_validators"], [])
        self.assertEqual([t["name"] for t in manifest["tools"]], [P.FINDINGS_TOOL_NAME])
        enabled_doc = json.loads(json.dumps(disabled_doc))
        enabled_doc["roles"]["population_adversary"]["session"]["enabled"] = True
        with mock.patch.object(P, "_agents_doc", lambda: enabled_doc):
            P.clear_behavior_caches()
            enabled = P.role_behavior_manifest("population_adversary")
        self.assertEqual(enabled["harness_validators"], [{"name": "compile_proposal"}])
        self.assertNotEqual(
            sha256_hex(json.dumps(enabled, sort_keys=True)),
            sha256_hex(json.dumps(manifest, sort_keys=True)),
        )

    def test_transcript_key_v3_one_shot_turn_equals_transcript_key(self):
        """Turn 0 of a role under its declared policy IS the one-shot key, so a
        zero-tool session and `complete()` share their recorded entries; a
        different policy, different tools or a longer prefix key apart."""
        role = "ambiguity_critic"
        policy = P.session_policy_for(role)
        turn0 = [{"role": "user", "content": "VIEW"}]
        self.assertEqual(P.transcript_key_v3(role, policy, turn0), P.transcript_key(role, "VIEW"))
        self.assertEqual(len(P.transcript_key(role, "VIEW")), 64)
        longer = turn0 + [{"role": "assistant", "content": "…"}, {"role": "user", "content": "more"}]
        self.assertNotEqual(P.transcript_key_v3(role, policy, longer), P.transcript_key(role, "VIEW"))
        other_policy = P.SessionPolicy(
            role=role, submit_tool=policy.submit_tool, limits=policy.limits,
            wire_tools=policy.wire_tools, nudge_text="edited nudge",
        )
        self.assertNotEqual(other_policy.sha256(), policy.sha256())
        self.assertNotEqual(
            P.transcript_key_v3(role, other_policy, turn0), P.transcript_key(role, "VIEW")
        )
        other_tools = P.SessionPolicy(
            role=role, submit_tool=policy.submit_tool, limits=policy.limits,
            wire_tools=policy.wire_tools + ({"name": "dev_query", "input_schema": {}},),
        )
        self.assertNotEqual(
            P.transcript_key_v3(role, other_tools, turn0), P.transcript_key(role, "VIEW")
        )
        # The salt is not policy: it never moves policy_sha256.
        salted = P.SessionPolicy(
            role=role, submit_tool=policy.submit_tool, limits=policy.limits,
            wire_tools=policy.wire_tools, session_salt=7,
        )
        self.assertEqual(salted.sha256(), policy.sha256())

    def test_route_mismatch_on_tool_protocol_change(self):
        """A stored entry whose route block names another tool protocol (or
        behaviour, or policy) is REFUSED even when its key matches: a miss in
        live mode (re-recorded), fail-closed under replay-only."""
        role = "ambiguity_critic"
        route = P.RoleRoute(role, "anthropic", "claude-sonnet-5", 1024, "medium")
        good = P.RoutedProvider._route_block(route)
        entry = {"prompt_sha256": "k", "response": "R", "route": dict(good)}
        self.assertIsNone(P.transcript_route_mismatch(entry, route))
        for field, label in (
            ("tools_sha256", "tool protocol"),
            ("behavior_sha256", "behaviour manifest"),
            ("policy_sha256", "session policy"),
        ):
            stale = {"prompt_sha256": "k", "response": "R", "route": {**good, field: "0" * 64}}
            reason = P.transcript_route_mismatch(stale, route)
            self.assertIsNotNone(reason, field)
            self.assertIn(label, reason)
        # `diagnostics_version` is recorded, never compared (roadmap R-H).
        recorded_only = {"prompt_sha256": "k", "response": "R",
                         "route": {**good, "diagnostics_version": "999"}}
        self.assertIsNone(P.transcript_route_mismatch(recorded_only, route))
        # End to end: the same key, a stale tool digest -> replay-only refuses,
        # live mode re-records under the current digest.
        with tempfile.TemporaryDirectory() as tmp:
            store = P.TranscriptStore(Path(tmp) / "transcripts")
            sha = P.transcript_key(role, "p")
            store.record(role, sha, {
                "prompt_sha256": sha, "provider": "anthropic",
                "model": "claude-sonnet-5", "response": "stale",
                "route": {**good, "tools_sha256": "0" * 64},
            })
            replay = P.RoutedProvider(
                make_routing(), store, P.CostMeter(), replay_only=True, task_id="t",
            )
            with self.assertRaises(P.TranscriptRouteMismatchError) as ctx:
                replay.complete(CouncilRole.AMBIGUITY_CRITIC, "p")
            self.assertIn("tool protocol", str(ctx.exception))
            transport = FakeTransport([anthropic_tool_response([VALID_FINDING])])
            live = P.RoutedProvider(
                make_routing(), store, P.CostMeter(budget_per_task_usd=100.0),
                task_id="t", transports={"anthropic": transport},
            )
            live.complete(CouncilRole.AMBIGUITY_CRITIC, "p")
            self.assertEqual(len(transport.calls), 1)
            fresh = store.lookup(role, sha)
            self.assertEqual(fresh["route"]["tools_sha256"], P.role_tools_sha256(role))
            self.assertEqual(fresh["route"]["entry_schema"], P.TRANSCRIPT_ENTRY_SCHEMA)
            self.assertEqual(fresh["system_sha256"], P.role_behavior_sha256(role))

    def test_readers_tolerate_legacy_entry_schema_and_never_upgrade(self):
        """Entries with no route block (schema 0) or a bare route block
        (schema 1) are served for a one-shot role exactly as before and the
        stored bytes are left alone (roadmap R-E); once a role's session is
        enabled a legacy entry no longer serves it."""
        role = "ambiguity_critic"
        route = P.RoleRoute(role, "anthropic", "claude-sonnet-5", 1024, "medium")
        legacy0 = {"prompt_sha256": "k", "provider": "anthropic",
                   "model": "claude-sonnet-5", "response": "legacy"}
        legacy1 = {**legacy0, "route": {"provider": "anthropic", "model": "claude-sonnet-5",
                                        "max_tokens": 1024, "effort": "medium"}}
        self.assertEqual(P.transcript_entry_schema(legacy0), 0)
        self.assertEqual(P.transcript_entry_schema(legacy1), 1)
        self.assertIsNone(P.transcript_route_mismatch(legacy0, route))
        self.assertIsNone(P.transcript_route_mismatch(legacy1, route))
        with tempfile.TemporaryDirectory() as tmp:
            store = P.TranscriptStore(Path(tmp) / "transcripts")
            sha = P.transcript_key(role, "p")
            path = store.record(role, sha, {**legacy1, "prompt_sha256": sha})
            before = path.read_bytes()
            provider = P.RoutedProvider(
                make_routing(), store, P.CostMeter(), replay_only=True, task_id="t",
            )
            self.assertEqual(provider.complete(CouncilRole.AMBIGUITY_CRITIC, "p"), "legacy")
            self.assertEqual(path.read_bytes(), before, "a stored entry is never upgraded")
            self.assertEqual(P.transcript_entry_schema(json.loads(path.read_text())), 1)
        doc = json.loads(json.dumps(P._agents_doc()))
        doc["roles"][role]["session"]["enabled"] = True
        with mock.patch.object(P, "_agents_doc", lambda: doc):
            P.clear_behavior_caches()
            reason = P.transcript_route_mismatch(legacy1, route)
        self.assertIsNotNone(reason)
        self.assertIn("legacy", reason)

    def test_sandbox_pin_is_read_from_config_and_fails_closed_on_unknown_keys(self):
        self.assertEqual(
            P.sandbox_pin(),
            {"runtime": "none", "image_digest": "", "workspace_template_sha256": ""},
        )
        with mock.patch.object(P, "_agents_doc", lambda: {"metrology": {}}):
            self.assertEqual(P.sandbox_pin(), dict(P.DEFAULT_SANDBOX_PIN))
        bad = {"metrology": {"sandbox": {"runtime": "docker", "observed_at": "now"}}}
        with mock.patch.object(P, "_agents_doc", lambda: bad):
            with self.assertRaises(ValueError):
                P.sandbox_pin()
        # The thresholds loader sets the pin aside instead of refusing it.
        from elt_taskgen.review import metrology as metrology_mod

        thresholds = metrology_mod.load_metrology_thresholds(None)
        self.assertEqual(thresholds.min_recall, 0.75)

    def test_a_second_response_to_one_prompt_never_destroys_the_first(self):
        """batch50 2026-09-10: the store is keyed by PROMPT, so a seat asked
        the same question twice (a repair round, a re-ingest) and sampling a
        different answer clobbered the earlier entry — and with it every
        evidence row naming it, which `pipeline_readiness` reports as
        "review transcript <role> digest changed". Release then refuses the
        task; it killed 2 of the 6 that reached the last gate. The displaced
        entry is kept beside its prompt under its own response digest, and
        replay still reads the newest from the prompt-keyed path."""
        import json as json_mod
        import tempfile
        from pathlib import Path as PathType

        with tempfile.TemporaryDirectory() as tmp:
            root = PathType(tmp)
            store = P.TranscriptStore(record_dir=root)
            prompt = "a" * 64
            first = {"prompt_sha256": prompt, "response_sha256": "1" * 64, "n": 1}
            second = {"prompt_sha256": prompt, "response_sha256": "2" * 64, "n": 2}
            store.record("shortcut_attacker", prompt, first)
            live = store.record("shortcut_attacker", prompt, second)

            # Replay is unchanged: the prompt-keyed path holds the NEWEST.
            self.assertEqual(
                json_mod.loads(live.read_text())["response_sha256"], "2" * 64
            )
            # And the displaced observation is still verifiable.
            kept = root / "shortcut_attacker" / f"{prompt}.{'1' * 16}.json"
            self.assertTrue(kept.is_file(), sorted(x.name for x in kept.parent.iterdir()))
            self.assertEqual(
                json_mod.loads(kept.read_text())["response_sha256"], "1" * 64
            )
            # Re-recording the SAME response is a no-op, not a second copy.
            store.record("shortcut_attacker", prompt, second)
            self.assertEqual(
                len(list((root / "shortcut_attacker").glob("*.json"))), 2
            )

    def test_loop_limits_are_the_declared_session_block_verbatim(self):
        block = P.role_loop_limits("population_adversary")
        self.assertEqual(
            block,
            {"enabled": True, "mode": "harness_validated",
             "harness_validators": ["compile_proposal"], "max_model_calls": 3,
             "max_compile_corrections": 1, "max_tool_calls": 0, "max_wall_s": 300,
             "max_usd": 1.0, "max_oracle_bits": 6, "measured_match_bit": False},
        )
        # Pin the enabled author block. Its defaults rose after two revisions
        # still left most tasks short of prose completeness.
        self.assertEqual(
            P.role_loop_limits("semantic_author"),
            {"enabled": True,
             "hard_caps": {"compile_corrections": 4, "revisions": 4, "tool_calls": 5, "turns": 5,
                           "usd": 2.75, "wall_clock_s": 1800},
             "harness_validators": ["check_prose"], "max_compile_corrections": 4,
             "max_revisions": 4, "max_tool_calls": 5, "max_turns": 5, "max_usd": 2.75,
             "wall_clock_s": 900},
        )
        # Phase 2 (roadmap Table 6, 2.a config row; SoT T1 IMP): the
        # implementer's enabled WITNESS block, verbatim.  Enabled runner
        # blocks also enter the behaviour manifest unchanged.
        self.assertEqual(
            P.role_loop_limits("independent_implementer"),
            {"enabled": True,
             "hard_caps": {"compile_corrections": 2, "tool_calls": 24, "turns": 14, "usd": 0.5, "wall_clock_s": 1800},
             "harness_validators": ["check_submission"], "max_compile_corrections": 2,
             "max_tool_calls": 20, "max_turns": 12, "max_usd": 0.5,
             "per_tool": {"dev_query": 8, "dry_run_sql": 8, "run_mart_sql_dev": 2},
             "wall_clock_s": 1500},
        )
        self.assertEqual(
            P.role_manifest_limits("independent_implementer"),
            P.role_loop_limits("independent_implementer"),
        )
        self.assertEqual(P.role_loop_limits("audit_triage"), {})
        # The author's two harness-validated-submit keys are DERIVED (R0.2):
        # a block omitting them hashes the same as one declaring them, and a
        # disagreeing declaration is refused rather than silently overridden.
        doc = json.loads(json.dumps(P._agents_doc()))
        block = doc["roles"]["semantic_author"]["session"]
        declared = dict(block)
        block.pop("harness_validators")
        block.pop("max_compile_corrections")
        with mock.patch.object(P, "_agents_doc", lambda: doc):
            P.clear_behavior_caches()
            self.assertEqual(P.role_loop_limits("semantic_author")["harness_validators"], ["check_prose"])
            # Derived from max_revisions when the block omits it (4 since the
            # 2026-09-10 rerun raised the author to four revisions).
            self.assertEqual(P.role_loop_limits("semantic_author")["max_compile_corrections"], 4)
            self.assertEqual(P.role_loop_limits("semantic_author"), S.SessionLimits.from_block(declared).as_manifest())
            block["harness_validators"] = ["compile_probe"]
            P.clear_behavior_caches()
            with self.assertRaises(ValueError):
                P.role_loop_limits("semantic_author")
            block["harness_validators"] = ["check_prose"]
            # A declared count that DISAGREES with the max_revisions derivation
            # (4 since the 2026-09-10 rerun) is refused.
            block["max_compile_corrections"] = 1
            P.clear_behavior_caches()
            with self.assertRaises(ValueError):
                P.role_loop_limits("semantic_author")
        P.clear_behavior_caches()
        policy = P.session_policy_for("feasibility_reviewer")
        self.assertEqual(policy.limits.max_usd, 1.00)
        self.assertEqual(policy.limits.max_model_calls, 3)
        self.assertEqual(policy.limits.max_tool_calls, 0)
        self.assertEqual(policy.mode, "one_shot")
        self.assertEqual(policy.allowlist, ())
        self.assertEqual(policy.submit_tool, P.FINDINGS_TOOL_NAME)
        self.assertEqual(P.session_policy_for("semantic_author").submit_tool, "")

    def test_session_policy_for_carries_the_stuck_override_and_session_defaults(self):
        """SoT T5 "Exemptions": a `stuck:` override in the role's session
        block is hashed into `policy_sha256` AND is what the policy hands
        the runner's detector (`stuck_thresholds`); the document's top-level
        `session.format_error_disposition` rides beside the block as its
        un-hashed default."""
        base = json.loads(json.dumps(P._agents_doc()))
        # Exercise the explicit disabled rollback independently of the
        # shipped enabled default.
        base["roles"]["repair_proposer"]["session"]["enabled"] = False
        doc = json.loads(json.dumps(base))
        doc["roles"]["repair_proposer"]["session"]["stuck"] = {"identical_pairs_nudge": 2, "identical_pairs_halt": 3}
        doc["session"] = {"format_error_disposition": "stage_fail"}
        with mock.patch.object(P, "_agents_doc", lambda: base):
            P.clear_behavior_caches()
            default = P.session_policy_for("repair_proposer")
        self.assertEqual(dict(default.stuck_thresholds), dict(S.STUCK_DETECTOR_THRESHOLDS))
        self.assertEqual(default.limits.format_error_disposition, "halt")
        # Finding 1-0: while the runner block is DISABLED nothing enforces
        # its `stuck:` override, so the declared policy hashes neither it
        # nor any other key of the block (the session-wide default still
        # rides beside it, un-hashed).
        with mock.patch.object(P, "_agents_doc", lambda: doc):
            P.clear_behavior_caches()
            inert = P.session_policy_for("repair_proposer")
            self.assertEqual(dict(inert.stuck_thresholds), dict(S.STUCK_DETECTOR_THRESHOLDS))
            self.assertEqual(inert.sha256(), default.sha256())
            self.assertEqual(inert.limits.format_error_disposition, "stage_fail")
        doc["roles"]["repair_proposer"]["session"]["enabled"] = True
        with mock.patch.object(P, "_agents_doc", lambda: doc):
            P.clear_behavior_caches()
            policy = P.session_policy_for("repair_proposer")
            self.assertEqual(policy.stuck_thresholds["identical_pairs_nudge"], 2)
            self.assertEqual(policy.stuck_thresholds["identical_pairs_halt"], 3)
            self.assertEqual(policy.stuck_thresholds["error_streak_nudge"], 3)
            self.assertEqual(dict(policy.stuck_thresholds), dict(policy.limits.stuck))
            self.assertEqual(policy.as_manifest()["stuck_thresholds"], dict(policy.stuck_thresholds))
            self.assertNotEqual(policy.sha256(), default.sha256())
            self.assertEqual(policy.limits.format_error_disposition, "stage_fail")
            self.assertNotIn("format_error_disposition", policy.limits.as_manifest())
            # A role block's own key overrides the session-wide default.
            doc["roles"]["repair_proposer"]["session"]["format_error_disposition"] = "halt"
            P.clear_behavior_caches()
            self.assertEqual(P.session_policy_for("repair_proposer").limits.format_error_disposition, "halt")
            # A role without a block still reads the session-wide default.
            self.assertEqual(P.session_policy_for("audit_triage").limits.format_error_disposition, "stage_fail")
        with mock.patch.object(P, "_agents_doc", lambda: base):
            P.clear_behavior_caches()
            self.assertEqual(P.session_policy_for("repair_proposer").sha256(), default.sha256())



class WitnessSessionRolesTest(unittest.TestCase):
    """Roadmap Phase 2 (Table 6, 2.a; SoT T1 IMP / LDR): the two cross-family
    WITNESS roles ship with bounded sessions, join `SESSION_RUNNER_ROLES`, and
    retain an explicit one-shot rollback: a disabled witness block folds to
    exactly `{}` (`WITNESS_RUNNER_ROLES`), so the legacy committed
    `independent_implementer` fixture key does NOT move under that rollback. The two
    CLI seams the roadmap names for the witnesses (`cmd_record_transcripts`
    seeding whole trajectories, `_ensure_independent_build` lifting a
    `SessionFault` to could-not-measure) are pinned here beside them."""

    IMPLEMENTER = "independent_implementer"
    LOADER = "independent_loader"

    def setUp(self):
        P.clear_behavior_caches()
        self.addCleanup(P.clear_behavior_caches)

    @staticmethod
    def _patched(doc):
        return mock.patch.object(P, "_agents_doc", lambda: doc)

    def test_record_transcripts_warnings_have_two_distinct_shapes(self):
        """Finding 1-0 (sanctioned in docs/plans/bounded_agents_phase2.md §2):
        with both witness sessions DISABLED (the shipped config), the seeding
        try is SPLIT, so a `load_gold` / `begin_task_evidence` failure prints a
        DIFFERENT WARNING from the `emit_variant` / `complete` failure. Both
        shapes are pinned here; exit code stays 0 on both paths."""
        import contextlib
        import io
        from types import SimpleNamespace

        from elt_taskgen import cli, demo_fixture
        from elt_taskgen.export import eltbench as eltbench_mod
        from elt_taskgen.reference import gold as gold_mod
        from elt_taskgen.reference import independent
        from elt_taskgen.review import council

        class _Stub:
            replay_only = False

            def __init__(self, routing, store, meter, **kwargs):
                self.calls: list[str] = []

            def complete(self, role, prompt):
                name = getattr(role, "value", str(role))
                self.calls.append(name)
                if name == "semantic_author":
                    return "Solver-visible prose recorded by the stub."
                if name.startswith("independent"):
                    return "{}"
                return json.dumps({"findings": []})

            def begin_task_evidence(self, task_id, content_hash):
                pass

        def run(*, load_gold, emit_variant):
            def factory(routing, store, meter, **kwargs):
                return _Stub(routing, store, meter, **kwargs)

            with tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp) / "ws"
                engine = cli._open_engine(workspace)
                try:
                    stored = demo_fixture.demo_task().model_copy(
                        update={"solver_prompt": "Authored prose already stored."}
                    )
                    engine.save_task(stored)
                    (cli._answer_key_dir(engine, stored)).mkdir(parents=True)
                    (engine.task_dir(stored.task_id) / "populations").mkdir(parents=True)
                finally:
                    engine.close()
                args = SimpleNamespace(
                    workspace=str(workspace), task_id=stored.task_id, out=str(workspace / "out"),
                    agents_config=None, loader_only=False, force=False,
                    budget_per_task=P.DEFAULT_BUDGET_PER_TASK_USD, budget_total=None,
                )
                out = io.StringIO()
                with self._patched(json.loads(json.dumps(P._agents_doc()))), mock.patch.object(
                    P, "credential_problems", lambda routing, roles: []
                ), mock.patch.object(P, "RoutedProvider", factory), mock.patch.object(
                    cli, "_admission_gate", lambda provider, workspace: (None, {})
                ), mock.patch.object(
                    council, "author_prose", lambda task, provider: provider.complete("semantic_author", "v")
                ), mock.patch.object(council, "run_council", lambda task, provider: []), mock.patch.object(
                    gold_mod, "load_gold", load_gold
                ), mock.patch.object(eltbench_mod, "emit_variant", emit_variant), mock.patch.object(
                    independent, "load_sample_prompt", lambda task, bundle, i: f"loader prompt {i}"
                ), contextlib.redirect_stdout(out):
                    P.clear_behavior_caches()
                    rc = cli.cmd_record_transcripts(args)
                P.clear_behavior_caches()
            return rc, out.getvalue()

        # Path 1: the frozen-reference read fails (load_gold raises).
        def boom_gold(path):
            raise RuntimeError("frozen reference unreadable")

        rc, printed = run(load_gold=boom_gold, emit_variant=lambda *a, **k: None)
        self.assertEqual(rc, 0, printed)
        self.assertIn("seeding FAILED before the frozen reference could be read", printed)
        self.assertIn("RuntimeError", printed)

        # Path 2: the frozen reference reads, but the variant emit fails.
        def boom_emit(*a, **k):
            raise RuntimeError("variant emit failed")

        rc, printed = run(load_gold=lambda path: object(), emit_variant=boom_emit)
        self.assertEqual(rc, 0, printed)
        self.assertIn("independent_loader seeding FAILED", printed)
        self.assertIn("el-independent-load", printed)
        # The two shapes are DISTINCT: the frozen-reference clause is not on
        # the emit path.
        self.assertNotIn("before the frozen reference could be read", printed)

    def test_witness_blocks_follow_the_sot_and_fold_to_empty_while_disabled(self):
        expected = {
            self.IMPLEMENTER: {
                "enabled": True, "max_turns": 12, "max_tool_calls": 20,
                "per_tool": {"dev_query": 8, "dry_run_sql": 8, "run_mart_sql_dev": 2},
                "harness_validators": ["check_submission"], "max_compile_corrections": 2,
                "max_usd": 0.50, "wall_clock_s": 1500,
                "hard_caps": {"turns": 14, "tool_calls": 24, "compile_corrections": 2, "usd": 0.50, "wall_clock_s": 1800},
            },
            self.LOADER: {
                "enabled": True, "max_turns": 2, "max_tool_calls": 2,
                "max_usd": 0.20, "wall_clock_s": 600,
                "hard_caps": {"turns": 2, "tool_calls": 2, "usd": 0.20, "wall_clock_s": 600},
            },
        }
        for role, block in expected.items():
            self.assertIn(role, P.SESSION_RUNNER_ROLES, role)
            self.assertIn(role, P.WITNESS_RUNNER_ROLES, role)
            declared = P.role_loop_limits(role)
            self.assertEqual(declared, S.SessionLimits.from_block(block).as_manifest(), role)
            # The embedded defaults mirror the file.
            self.assertEqual(P.DEFAULT_ROUTING_DOC["roles"][role]["session"], block, role)
            # A default never exceeds its cap (SoT R0.1).
            limits = S.SessionLimits.from_block(declared)
            self.assertLessEqual(limits.max_turns, limits.hard_caps["turns"], role)
            self.assertLessEqual(limits.max_tool_calls, limits.hard_caps["tool_calls"], role)
            self.assertLessEqual(limits.max_usd, limits.hard_caps["usd"], role)
            self.assertLessEqual(limits.session_wall_seconds, limits.hard_caps["wall_clock_s"], role)
            # Enabled: the enforced limits and tool surface are fully hashed.
            self.assertEqual(P.role_manifest_limits(role), block, role)
            self.assertEqual(P.role_behavior_manifest(role)["loop_limits"], block, role)
            self.assertEqual(P.session_policy_for(role).limits.as_manifest(), block, role)
            self.assertTrue(P.role_is_agentic(role), role)
        self.assertEqual(
            sorted(P.WITNESS_RUNNER_ROLES), [self.IMPLEMENTER, self.LOADER]
        )
        self.assertTrue(set(P.WITNESS_RUNNER_ROLES) <= set(P.SESSION_RUNNER_ROLES))
        disabled = json.loads(json.dumps(P._agents_doc()))
        for role in P.WITNESS_RUNNER_ROLES:
            disabled["roles"][role]["session"]["enabled"] = False
        with self._patched(disabled):
            P.clear_behavior_caches()
            for role in P.WITNESS_RUNNER_ROLES:
                self.assertEqual(P.role_manifest_limits(role), {}, role)
                self.assertEqual(P.role_behavior_manifest(role)["loop_limits"], {}, role)
                self.assertEqual(P.session_policy_for(role).limits.as_manifest(), {}, role)
                self.assertEqual(P.role_behavior_manifest(role)["harness_validators"], [], role)
                self.assertFalse(P.role_is_agentic(role), role)
        P.clear_behavior_caches()

    def test_committed_implementer_fixture_tracks_enabled_profile_and_disabled_key_is_stable(self):
        from elt_taskgen import demo_fixture
        from elt_taskgen.reference import independent

        live = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "transcripts" / self.IMPLEMENTER
        fixtures = sorted(p for p in live.glob("*.json"))
        self.assertEqual(len(fixtures), 1)
        entry = json.loads(fixtures[0].read_text(encoding="utf-8"))
        # The live fixture follows the shipping, enabled witness profile.
        self.assertEqual(entry["system_sha256"], P.role_behavior_sha256(self.IMPLEMENTER))
        # The implementer stimulus is reconstructable from the byte-preserved
        # legacy author response.  Reading that text as static TaskIR prose does
        # not make the stale author entry replayable; the migration tests enforce
        # that distinction explicitly.
        author_dir = live.parents[1] / "transcripts_legacy" / "semantic_author"
        prose = json.loads(next(author_dir.glob("*.json")).read_text(encoding="utf-8"))["response"]
        task = demo_fixture.demo_task().model_copy(update={"solver_prompt": str(prose)})
        prompts = {i: independent.sample_prompt(task, i) for i in range(4)}
        self.assertIn(
            fixtures[0].stem,
            {P.transcript_key(self.IMPLEMENTER, prompt) for prompt in prompts.values()},
        )
        base = json.loads(json.dumps(P._agents_doc()))
        for role in P.WITNESS_RUNNER_ROLES:
            base["roles"][role]["session"]["enabled"] = False
        # The explicit one-shot rollback has a different key, but retains its
        # own byte-identical stability contract under disabled-limit edits.
        with self._patched(base):
            P.clear_behavior_caches()
            keys = {P.transcript_key(self.IMPLEMENTER, prompt) for prompt in prompts.values()}
            self.assertNotIn(fixtures[0].stem, keys)
            digests = {role: P.role_behavior_sha256(role) for role in P.WITNESS_RUNNER_ROLES}
            loader_key = P.transcript_key(self.LOADER, "a loader prompt")
        # (1) Editing the DISABLED block's limits moves nothing.
        edited = json.loads(json.dumps(base))
        edited["roles"][self.IMPLEMENTER]["session"].update({"max_usd": 0.25, "max_turns": 3})
        edited["roles"][self.LOADER]["session"].update({"max_usd": 0.10})
        with self._patched(edited):
            P.clear_behavior_caches()
            self.assertEqual(P.role_loop_limits(self.IMPLEMENTER)["max_turns"], 3)
            for role in P.WITNESS_RUNNER_ROLES:
                self.assertEqual(P.role_manifest_limits(role), {}, role)
                self.assertEqual(P.role_behavior_sha256(role), digests[role], role)
            self.assertNotIn(fixtures[0].stem, {P.transcript_key(self.IMPLEMENTER, p) for p in prompts.values()})
            self.assertEqual(P.transcript_key(self.LOADER, "a loader prompt"), loader_key)
        # (2) Dropping the declaration is the SAME manifest for a witness
        # (`{}` is what it had before the declaration existed).
        dropped = json.loads(json.dumps(base))
        for role in P.WITNESS_RUNNER_ROLES:
            del dropped["roles"][role]["session"]
        with self._patched(dropped):
            P.clear_behavior_caches()
            for role in P.WITNESS_RUNNER_ROLES:
                self.assertEqual(P.role_loop_limits(role), {}, role)
                self.assertEqual(P.role_behavior_sha256(role), digests[role], role)
            self.assertNotIn(fixtures[0].stem, {P.transcript_key(self.IMPLEMENTER, p) for p in prompts.values()})
        # (3) Enabling the block: the declared limits enter the manifest
        # verbatim and the digest and the key move (the session is a
        # different behaviour; its trajectories are seeded whole).
        enabled = json.loads(json.dumps(base))
        for role in P.WITNESS_RUNNER_ROLES:
            enabled["roles"][role]["session"]["enabled"] = True
        with self._patched(enabled):
            P.clear_behavior_caches()
            for role in P.WITNESS_RUNNER_ROLES:
                manifest = P.role_behavior_manifest(role)
                self.assertEqual(manifest["loop_limits"], P.role_loop_limits(role), role)
                self.assertTrue(manifest["loop_limits"]["enabled"], role)
                self.assertNotEqual(P.role_behavior_sha256(role), digests[role], role)
            self.assertIn(fixtures[0].stem, {P.transcript_key(self.IMPLEMENTER, p) for p in prompts.values()})
            self.assertNotEqual(P.transcript_key(self.LOADER, "a loader prompt"), loader_key)
        P.clear_behavior_caches()
        self.assertNotEqual(P.role_behavior_sha256(self.IMPLEMENTER), digests[self.IMPLEMENTER])
        # A disabled witness block is still VALIDATED at load (SoT R0.1).
        bad = json.loads(json.dumps(base))
        bad["roles"][self.LOADER]["session"]["max_turns"] = 9
        with self._patched(bad):
            P.clear_behavior_caches()
            with self.assertRaises(ValueError):
                P.role_manifest_limits(self.LOADER)
        P.clear_behavior_caches()

    def test_session_fault_in_build_is_could_not_measure_not_red_gate(self):
        """C7 (roadmap 2.a): a harness fault inside the witness — the build
        worker died, a tool deadline, a sandbox fault — reaches the gates
        stage as a note carrying the fault's CLASS NAME: nothing is recorded,
        `cli._transport_marker` lifts the name, and the engine classifies the
        stage as infrastructure (halt, exit 2, no repair round), never as a
        red dual-build gate that a repair round would try to fix."""
        from elt_taskgen import cli, demo_fixture
        from elt_taskgen import engine as engine_mod
        from elt_taskgen.engine import Engine, StagePayload
        from elt_taskgen.reference import independent

        task = demo_fixture.demo_task()
        faults = (
            S.SandboxFault("independent build worker died", code=independent.WORKER_FAILED_CODE),
            S.ToolDeadlineExceeded("independent_build", deadline_s=300.0),
            S.ToolHarnessFault("dev_query", code="harness_exception", cause_type="RuntimeError"),
        )
        for fault in faults:
            with tempfile.TemporaryDirectory() as tmp:
                engine = Engine(Path(tmp))
                try:
                    with mock.patch.object(
                        independent, "run_independent_build", side_effect=fault
                    ):
                        note = cli._ensure_independent_build(engine, task, object(), object())
                    name = type(fault).__name__
                    self.assertTrue(note.startswith(f"independent build not performed: {name}:"), note)
                    self.assertIsNone(independent.load_build_result(engine.workspace, task.task_id))
                    # The marker the gates runner puts on its FAIL outcome
                    # (`infrastructure=_transport_marker([build_note])`), read
                    # by the engine's halt expression: a halt, never a round.
                    self.assertEqual(cli._transport_marker([note]), name)
                    self.assertEqual(engine_mod._infra_marker_for(fault), name)
                    outcome = engine_mod.StageOutcome(
                        engine_mod.VERDICT_FAIL,
                        StagePayload(error=note),
                        infrastructure=cli._transport_marker([note]),
                    )
                    self.assertEqual(
                        engine_mod._marker_text(
                            outcome.infrastructure
                            or engine_mod._infrastructure_failure(outcome.payload)
                        ),
                        engine_mod._marker_text(name),
                    )
                    # Nothing but the class, the boundary and the code: no
                    # value, no count, no path.
                    self.assertNotIn("answer_key", note)
                    self.assertNotIn("rows", note)
                finally:
                    engine.close()
        # A model-caused fault is NOT lifted: the seat's own policy fault is a
        # measured outcome and never a could-not-measure.
        self.assertEqual(cli._transport_marker(["ForbiddenArgument: forbidden_argument"]), "")

    def test_record_transcripts_seeds_whole_trajectories(self):
        """Roadmap 2.a (`cli.py` row): with a witness session ENABLED,
        `cmd_record_transcripts` seeds the WHOLE trajectory by running the
        same entry point the gates stage replays (`run_independent_build` /
        `run_independent_load_build`, every turn recorded through
        `provider.run_session`) instead of one one-shot exchange; with an
        explicit `enabled: false` rollback the one-shot seed is byte-identical."""
        import contextlib
        import io
        from types import SimpleNamespace

        from elt_taskgen import cli, demo_fixture
        from elt_taskgen.export import eltbench as eltbench_mod
        from elt_taskgen.reference import gold as gold_mod
        from elt_taskgen.reference import independent
        from elt_taskgen.review import council

        class _Stub:
            replay_only = False

            def __init__(self, routing, store, meter, **kwargs):
                self.routing = routing
                self.calls: list[str] = []
                self.evidence: list[tuple[str, str]] = []

            def complete(self, role, prompt):
                name = getattr(role, "value", str(role))
                self.calls.append(name)
                if name == "semantic_author":
                    return "Solver-visible prose recorded by the stub."
                if name.startswith("independent"):
                    return "{}"
                return json.dumps({"findings": []})

            def begin_task_evidence(self, task_id, content_hash):
                self.evidence.append((task_id, content_hash))

        gold_sentinel = object()

        def run(doc):
            made: list[_Stub] = []
            builds: list[tuple] = []
            loads: list[tuple] = []

            def factory(routing, store, meter, **kwargs):
                stub = _Stub(routing, store, meter, **kwargs)
                made.append(stub)
                return stub

            def build(task, workspace, provider, gold, **kwargs):
                builds.append((task.task_id, Path(workspace), provider, gold))
                return SimpleNamespace(samples=(1, 2), status=independent.STATUS_AGREED)

            def load(task, workspace, provider, gold, **kwargs):
                loads.append((task.task_id, Path(workspace), provider, gold))
                return SimpleNamespace(samples=(1,), status=independent.STATUS_AGREED)

            with tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp) / "ws"
                engine = cli._open_engine(workspace)
                try:
                    stored = demo_fixture.demo_task().model_copy(
                        update={"solver_prompt": "Authored prose already stored."}
                    )
                    engine.save_task(stored)
                    (cli._answer_key_dir(engine, stored)).mkdir(parents=True)
                    (engine.task_dir(stored.task_id) / "populations").mkdir(parents=True)
                finally:
                    engine.close()
                args = SimpleNamespace(
                    workspace=str(workspace), task_id=stored.task_id, out=str(workspace / "out"),
                    agents_config=None, loader_only=False, force=False,
                    budget_per_task=P.DEFAULT_BUDGET_PER_TASK_USD, budget_total=None,
                )
                out = io.StringIO()
                with self._patched(doc), mock.patch.object(
                    P, "credential_problems", lambda routing, roles: []
                ), mock.patch.object(P, "RoutedProvider", factory), mock.patch.object(
                    cli, "_admission_gate", lambda provider, workspace: (None, {})
                ), mock.patch.object(
                    council, "author_prose", lambda task, provider: provider.complete("semantic_author", "v")
                ), mock.patch.object(council, "run_council", lambda task, provider: []), mock.patch.object(
                    gold_mod, "load_gold", lambda path: gold_sentinel
                ), mock.patch.object(eltbench_mod, "emit_variant", lambda *a, **k: None), mock.patch.object(
                    independent, "load_sample_prompt", lambda task, bundle, i: f"loader prompt {i}"
                ), mock.patch.object(
                    independent, "run_independent_build", build
                ), mock.patch.object(independent, "run_independent_load_build", load), contextlib.redirect_stdout(out):
                    P.clear_behavior_caches()
                    rc = cli.cmd_record_transcripts(args)
                P.clear_behavior_caches()
            return rc, made[0], builds, loads, out.getvalue()

        base = json.loads(json.dumps(P._agents_doc()))
        for role in (self.IMPLEMENTER, self.LOADER):
            base["roles"][role]["session"]["enabled"] = False
        # Explicit rollback: both witnesses one-shot, seeded as before.
        rc, stub, builds, loads, printed = run(base)
        self.assertEqual(rc, 0, printed)
        self.assertEqual(stub.calls.count(self.IMPLEMENTER), 1)
        self.assertEqual(stub.calls.count(self.LOADER), 1)
        self.assertEqual((builds, loads), ([], []))
        self.assertIn("independent_loader exchange seeded (sample 0)", printed)
        # Both witness sessions enabled: no one-shot exchange for either; the
        # whole trajectory is seeded by the gate's own entry point, against
        # the frozen reference, with the SAME provider object.
        enabled = json.loads(json.dumps(base))
        for role in (self.IMPLEMENTER, self.LOADER):
            enabled["roles"][role]["session"]["enabled"] = True
        rc, stub, builds, loads, printed = run(enabled)
        self.assertEqual(rc, 0, printed)
        self.assertNotIn(self.IMPLEMENTER, stub.calls)
        self.assertNotIn(self.LOADER, stub.calls)
        self.assertEqual(len(builds), 1)
        self.assertEqual(len(loads), 1)
        self.assertEqual(builds[0][0], demo_fixture.DEMO_TASK_ID)
        self.assertIs(builds[0][2], stub)
        self.assertIs(builds[0][3], gold_sentinel)
        self.assertIs(loads[0][2], stub)
        self.assertIn("independent_implementer trajectories seeded (2 session(s)", printed)
        self.assertIn("independent_loader trajectories seeded (1 session(s)", printed)
        self.assertTrue(stub.evidence, "the second pass binds the task evidence before seeding")
        # Only the implementer enabled: the loader keeps its one-shot seed.
        mixed = json.loads(json.dumps(base))
        mixed["roles"][self.IMPLEMENTER]["session"]["enabled"] = True
        rc, stub, builds, loads, printed = run(mixed)
        self.assertEqual(rc, 0, printed)
        self.assertNotIn(self.IMPLEMENTER, stub.calls)
        self.assertEqual(stub.calls.count(self.LOADER), 1)
        self.assertEqual((len(builds), len(loads)), (1, 0))


# ---------------------------------------------------------------------------
# Phase 4: the harness-validated critic prompt is derived from the block
# (roadmap Table 8 `config/agents.yaml`, `review/prompts.py` row; BRIEF_PHASE4 item 7)
# ---------------------------------------------------------------------------

class HarnessValidatedPromptTest(unittest.TestCase):
    def setUp(self):
        P.clear_behavior_caches()
        self.addCleanup(P.clear_behavior_caches)

    @staticmethod
    def _doc(**session_updates) -> dict:
        doc = json.loads(json.dumps(P._agents_doc()))
        for role, keys in session_updates.items():
            doc["roles"][role].setdefault("session", {}).update(keys)
        return doc

    def test_no_tools_sentence_leaves_pop_and_shc_prompts_only_when_enabled(self):
        """The "no tools, no files" sentence of SHARED_PREFIX is removed from
        the population adversary's and the shortcut attacker's system prompt
        ONLY while the seat's `session:` block says `enabled: true` — derived
        from the block, so under an explicit `enabled: false` rollback the
        prompt bytes, `system_prompt_sha256`, the behaviour digest and therefore
        the transcript keys preserve the one-shot state; every other role
        keeps SHARED_PREFIX verbatim whatever its block says; and the wire
        (`_payload`, `_session_payload`) sends the same derived prompt the
        manifest hashes."""
        from elt_taskgen.review import prompts

        self.assertIn(prompts.NO_TOOLS_SENTENCE, prompts.SHARED_PREFIX)
        self.assertEqual(prompts.HARNESS_VALIDATED_PROMPT_ROLES, ("population_adversary", "shortcut_attacker"))
        rollback_digests = {}
        disabled = self._doc(population_adversary={"enabled": False}, shortcut_attacker={"enabled": False})
        with mock.patch.object(P, "_agents_doc", lambda: disabled):
            P.clear_behavior_caches()
            for role in ("population_adversary", "shortcut_attacker", "ambiguity_critic", "feasibility_reviewer"):
                with self.subTest(rollback=role):
                    self.assertFalse(prompts.harness_validated_prompt_enabled(role))
                    self.assertEqual(prompts.role_system_prompt(role), prompts.ROLE_SYSTEM[role])
                    self.assertIn(prompts.NO_TOOLS_SENTENCE, prompts.role_system_prompt(role))
                    manifest = P.role_behavior_manifest(role)
                    self.assertEqual(manifest["system_prompt_sha256"], sha256_hex(prompts.ROLE_SYSTEM[role]))
                    rollback_digests[role] = P.role_behavior_sha256(role)
                    payload = P.AnthropicBackend._payload(
                        model="claude-opus-5", prompt="VIEW", max_tokens=64, effort="high",
                        role_name=role, schema_mode=True,
                    )
                    self.assertEqual(payload["system"], prompts.ROLE_SYSTEM[role])
        P.clear_behavior_caches()
        for role in ("population_adversary", "shortcut_attacker"):
            self.assertTrue(prompts.harness_validated_prompt_enabled(role), role)
            self.assertNotIn(prompts.NO_TOOLS_SENTENCE, prompts.role_system_prompt(role))
            self.assertIn(prompts.HARNESS_VALIDATED_SENTENCE, prompts.role_system_prompt(role))
        for role in ("ambiguity_critic", "feasibility_reviewer"):
            self.assertFalse(prompts.harness_validated_prompt_enabled(role), role)
            self.assertEqual(prompts.role_system_prompt(role), prompts.ROLE_SYSTEM[role])
        # Flip POP only: its prompt drops the sentence for the harness-validated
        # one, its digests move; SHC (still disabled), AMB and FEA are byte-identical.
        enabled = self._doc(population_adversary={"enabled": True}, shortcut_attacker={"enabled": False})
        with mock.patch.object(P, "_agents_doc", lambda: enabled):
            P.clear_behavior_caches()
            self.assertTrue(prompts.harness_validated_prompt_enabled("population_adversary"))
            derived = prompts.role_system_prompt("population_adversary")
            self.assertNotIn(prompts.NO_TOOLS_SENTENCE, derived)
            self.assertIn(prompts.HARNESS_VALIDATED_SENTENCE, derived)
            self.assertEqual(
                derived,
                prompts.ROLE_SYSTEM["population_adversary"].replace(
                    prompts.NO_TOOLS_SENTENCE, prompts.HARNESS_VALIDATED_SENTENCE, 1
                ),
            )
            self.assertEqual(prompts.ROLE_SYSTEM["population_adversary"].count(prompts.NO_TOOLS_SENTENCE), 1)
            self.assertNotIn("no way to ask a follow-up question", derived)
            self.assertEqual(
                P.role_behavior_manifest("population_adversary")["system_prompt_sha256"], sha256_hex(derived)
            )
            self.assertNotEqual(P.role_behavior_sha256("population_adversary"), rollback_digests["population_adversary"])
            for role in ("shortcut_attacker", "ambiguity_critic", "feasibility_reviewer"):
                with self.subTest(unchanged=role):
                    self.assertEqual(prompts.role_system_prompt(role), prompts.ROLE_SYSTEM[role])
                    self.assertEqual(P.role_behavior_sha256(role), rollback_digests[role])
            # The wire sends the derived prompt (one-shot and session payload alike).
            payload = P.AnthropicBackend._payload(
                model="claude-opus-5", prompt="VIEW", max_tokens=64, effort="high",
                role_name="population_adversary", schema_mode=True,
            )
            self.assertEqual(payload["system"], derived)
            policy = P.session_policy_for("population_adversary")
            session = P.AnthropicBackend._session_payload(
                model="claude-opus-5", messages=[{"role": "user", "content": "VIEW"}], max_tokens=64,
                effort="high", role_name="population_adversary",
                tools=[dict(t) for t in policy.wire_tools], tool_choice=P.session_tool_choice(policy),
            )
            self.assertEqual(session["system"], derived)
            # A role that is NOT a harness-validated seat keeps the sentence
            # even with its block enabled (the AMB one-shot block).
            amb_enabled = self._doc(population_adversary={"enabled": True}, ambiguity_critic={"enabled": True})
            with mock.patch.object(P, "_agents_doc", lambda: amb_enabled):
                P.clear_behavior_caches()
                self.assertFalse(prompts.harness_validated_prompt_enabled("ambiguity_critic"))
                self.assertEqual(prompts.role_system_prompt("ambiguity_critic"), prompts.ROLE_SYSTEM["ambiguity_critic"])
        P.clear_behavior_caches()
        # A custom --agents-config document disabling SHC restores SHC's
        # one-shot prompt for THAT document (manifest, transcript key and
        # backend all read the same document); the default stays bounded.
        with tempfile.TemporaryDirectory() as tmp:
            doc = self._doc(shortcut_attacker={"enabled": False})
            path = Path(tmp) / "agents.yaml"
            path.write_text(yaml.safe_dump(doc), encoding="utf-8")
            self.assertFalse(prompts.harness_validated_prompt_enabled("shortcut_attacker", agents_config=path))
            self.assertTrue(prompts.harness_validated_prompt_enabled("shortcut_attacker"))
            custom = prompts.role_system_prompt("shortcut_attacker", agents_config=path)
            self.assertIn(prompts.NO_TOOLS_SENTENCE, custom)
            self.assertNotEqual(prompts.role_system_prompt("shortcut_attacker"), prompts.ROLE_SYSTEM["shortcut_attacker"])
            self.assertEqual(
                P.role_behavior_manifest("shortcut_attacker", agents_config=path)["system_prompt_sha256"],
                sha256_hex(custom),
            )
            self.assertNotEqual(P.role_behavior_sha256("shortcut_attacker"), rollback_digests["shortcut_attacker"])
            backend = P.AnthropicBackend("sk-test", transport=FakeTransport([]), agents_config=path)
            payload = backend._payload(
                model="claude-opus-5", prompt="VIEW", max_tokens=64, effort="high",
                role_name="shortcut_attacker", schema_mode=True, agents_config=backend._agents_config,
            )
            self.assertEqual(payload["system"], custom)
            self.assertNotEqual(
                P.transcript_key("shortcut_attacker", "VIEW", agents_config=path),
                P.transcript_key("shortcut_attacker", "VIEW"),
            )


class OpenAICompatReasoningEffortTest(unittest.TestCase):
    """batch10 run L (2026-09-11): the OSS witness answered with no
    reasoning at all and broke rules it had in front of it. The route's
    declared effort now travels as the wire's `reasoning.effort`; a route
    with no effort sends no such field."""

    def test_effort_travels_as_the_reasoning_object(self):
        transport = FakeTransport([openai_tool_response([VALID_FINDING])])
        backend = P.OpenAICompatBackend("http://oss:8000/v1", "k", transport=transport)
        backend.complete(role_name="ambiguity_critic", model="m", prompt="P", max_tokens=64, effort="high")
        self.assertEqual(transport.calls[0][2].get("reasoning"), {"effort": "high"})

    def test_no_effort_sends_no_reasoning_field(self):
        transport = FakeTransport([openai_tool_response([VALID_FINDING])])
        backend = P.OpenAICompatBackend("http://oss:8000/v1", "k", transport=transport)
        backend.complete(role_name="ambiguity_critic", model="m", prompt="P", max_tokens=64, effort=None)
        self.assertNotIn("reasoning", transport.calls[0][2])

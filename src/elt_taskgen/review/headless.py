"""Adapt Claude Code and Codex harnesses to bounded review sessions.

The session runner still permits, executes, sanitizes, records, and limits every tool
call. Harnesses receive only policy tools, and replay-only mode never starts them. Usage
since the prior synthesized turn is attached to the next turn for pricing.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import queue
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from elt_taskgen.models import canonical_json, sha256_hex
from elt_taskgen.review.council import ProviderProtocolError
from elt_taskgen.review.providers import (
    CORRECTION_TEXT,
    SCHEMA_RETRIES,
    AttemptRecord,
    BackendResult,
    BackendTurn,
    ProviderFault,
    RoleCapExceeded,
    Usage,
    _attach_attempts,
    _plain_data,
    _system_prompt,
    normalized_text_for,
    uses_findings_schema,
    validate_payload_for,
    wire_tools_for,
)

#: The MCP server name the harness sees the pipeline's tools under. Claude
#: Code prefixes tool names as ``mcp__<server>__<tool>``; the backend maps
#: them back to the policy's own names before the runner sees them.
TOOL_SERVER_NAME = "taskgen"

#: Refuse off-wire tool calls up to `MAX_WIRE_REFUSALS`, then let the runner
#: enforce its own limit.
WIRE_REFUSAL_TEXT = (
    "refused: this turn permits only {names}. Call one of them now; no other "
    "tool will run."
)
MAX_WIRE_REFUSALS = 3

#: What a pending tool call is answered with when the session ends before
#: the runner answered it (a terminal stop, a halt, the wall): the harness
#: turn is interrupted right after.
SESSION_ENDED_TEXT = "refused: the session has ended; no further tool call will run."

#: The harness-internal message the driver reads a queue with (seconds):
#: short, so a SIGALRM wall deadline raised in the main thread lands promptly.
_POLL_SECONDS = 0.2

#: A backstop on the harness's own API-step count per session: the runner's
#: ``max_turns`` binds first (every tool call is one runner turn); this only
#: stops a harness that loops without calling a tool.
_HARNESS_STEP_BACKSTOP = 64

#: Retry transient provider errors with exponential backoff; deterministic
#: errors fail immediately.
HARNESS_RETRIES = 4
HARNESS_RETRY_BACKOFF_SECONDS = 2.0
HARNESS_RETRY_TEXT = (
    "The previous request failed with a transient API error before the model "
    "answered. Continue from where you were; nothing you did was lost."
)
_CLAUDE_TRANSIENT_ERRORS = frozenset({"rate_limit", "server_error", "unknown"})
_CODEX_TRANSIENT_MARKERS = (
    "429", "500", "502", "503", "504", "529", "overloaded", "rate limit", "rate_limit",
    "timeout", "timed out", "temporarily", "unavailable", "enotfound", "econnreset",
    "econnrefused", "network", "connection", "server error", "internal error",
)


def _codex_error_is_transient(message: str) -> bool:
    text = (message or "").lower()
    return any(marker in text for marker in _CODEX_TRANSIENT_MARKERS)


def _retry_backoff_seconds(attempt: int) -> float:
    return float(HARNESS_RETRY_BACKOFF_SECONDS) * (2 ** max(0, int(attempt)))


class HeadlessHarnessError(ProviderFault):
    """A fault of the harness process or its SDK, classified as
    infrastructure (``ProviderFault``): the transport failed, not the task."""


# ---------------------------------------------------------------------------
# The bridge between the harness thread and the runner's main thread
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """One tool call the harness made, owed an answer by the runner."""

    id: str
    name: str
    args: dict
    _done: threading.Event = field(default_factory=threading.Event, repr=False)
    text: str = ""
    is_error: bool = False

    def resolve(self, text: str, is_error: bool) -> None:
        if self._done.is_set():
            return
        self.text = str(text)
        self.is_error = bool(is_error)
        self._done.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    @property
    def answered(self) -> bool:
        return self._done.is_set()


@dataclass
class TurnEnd:
    """A harness turn ended without a pending tool call: the model's text,
    why the harness stopped, and the harness's own accounting snapshot."""

    text: str
    reason: str = "completed"
    is_error: bool = False
    detail: str = ""
    harness_cost_usd: float | None = None
    steps: int = 0


class HarnessBridge:
    """Thread-safe hand-off of tool calls and turn ends from the harness
    thread to the runner, and of the runner's answers back.

    ``permitted`` is the current wire (None = the policy's full wire): a
    call outside a narrowed wire is refused here, as ``WIRE_REFUSAL_TEXT``,
    at most ``MAX_WIRE_REFUSALS`` consecutive times.
    """

    def __init__(self, tool_names: Sequence[str]) -> None:
        self.tool_names: tuple[str, ...] = tuple(str(n) for n in tool_names)
        self.events: queue.Queue = queue.Queue()
        self.permitted: frozenset[str] | None = None
        self._wire_refusals = 0
        self.log: list[dict] = []
        self.usage = Usage()
        self._usage_lock = threading.Lock()
        self.closed = False
        #: API steps the harness started and settled (their usage reported):
        #: a tool call is priced on its own turn once its step has settled.
        self.steps_started = 0
        self.steps_settled = 0

    # -- called from the harness thread -------------------------------------
    def call(self, name: str, args: Mapping[str, Any]) -> ToolCall:
        call = ToolCall(id=f"harness_{uuid.uuid4().hex[:16]}", name=str(name), args=dict(args or {}))
        if self.closed:
            call.resolve(SESSION_ENDED_TEXT, True)
            return call
        permitted = self.permitted
        if permitted is not None and call.name not in permitted:
            if self._wire_refusals < MAX_WIRE_REFUSALS:
                self._wire_refusals += 1
                call.resolve(WIRE_REFUSAL_TEXT.format(names=", ".join(sorted(permitted))), True)
                self.log.append({"event": "wire_refusal", "tool": call.name, "id": call.id})
                return call
        self.log.append({"event": "tool_call", "tool": call.name, "id": call.id,
                         "args_sha256": sha256_hex(canonical_json(_plain_data(call.args)))})
        self.events.put(("tool_call", call))
        return call

    def turn_ended(self, end: TurnEnd) -> None:
        self.log.append({"event": "turn_end", "reason": end.reason, "is_error": end.is_error,
                         "text_sha256": sha256_hex(end.text or ""), "steps": end.steps})
        self.events.put(("turn_end", end))

    def fault(self, exc: BaseException) -> None:
        self.log.append({"event": "fault", "exception_type": type(exc).__name__})
        self.events.put(("fault", exc))

    def add_usage(self, usage: Usage) -> None:
        with self._usage_lock:
            self.usage = self.usage + usage

    def set_usage(self, usage: Usage) -> None:
        """A cumulative snapshot from the harness (Codex reports totals)."""
        with self._usage_lock:
            self.usage = usage

    def step_started(self) -> None:
        with self._usage_lock:
            self.steps_started += 1

    def step_settled(self) -> None:
        with self._usage_lock:
            self.steps_settled = min(self.steps_started, self.steps_settled + 1)

    def settle(self, timeout: float = 1.0) -> None:
        """Wait (briefly) until every started step has reported its usage,
        so the turn that carries a tool call is priced with the step that
        produced it rather than the next one."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._usage_lock:
                if self.steps_settled >= self.steps_started:
                    return
            time.sleep(0.02)

    def usage_snapshot(self) -> Usage:
        with self._usage_lock:
            return Usage(
                input_tokens=self.usage.input_tokens,
                output_tokens=self.usage.output_tokens,
                cache_read_input_tokens=self.usage.cache_read_input_tokens,
                cache_creation_input_tokens=self.usage.cache_creation_input_tokens,
            )

    # -- called from the runner's thread -------------------------------------
    def set_wire(self, names: Sequence[str] | None) -> None:
        new = None if names is None else frozenset(str(n) for n in names)
        if new != self.permitted:
            self._wire_refusals = 0
        self.permitted = new

    def next_event(self, poll_seconds: float = _POLL_SECONDS) -> tuple[str, Any]:
        """Block until the harness produced an event. Polls so a signal
        raised in this thread (the session wall) interrupts the wait."""
        while True:
            try:
                return self.events.get(timeout=poll_seconds)
            except queue.Empty:
                continue

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Drivers: one harness process per session, in a worker thread
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HarnessStart:
    """What a driver needs to open one harness session."""

    system_prompt: str
    prompt: str
    tools: tuple[Mapping[str, Any], ...]
    model: str
    effort: str | None
    max_budget_usd: float | None
    cwd: Path
    role_name: str


class HarnessDriver:
    """The per-session harness process. ``start`` opens it and issues the
    first prompt; ``send`` issues a follow-up user message once the previous
    turn ended; ``interrupt`` cancels the running turn; ``close`` ends the
    process. Every event reaches the runner through the bridge."""

    kind = "harness"

    def __init__(self, bridge: HarnessBridge, config: Mapping[str, Any]) -> None:
        self.bridge = bridge
        self.config = dict(config or {})

    def start(self, spec: HarnessStart) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def send(self, text: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def interrupt(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self, timeout: float = 15.0) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def identity(self) -> dict:
        return {"harness": self.kind}


def _tool_annotations_for(tool: Mapping[str, Any]) -> dict:
    """MCP tool annotations for one wire tool: never destructive (every
    tool runs against the runner's held trial copy and the runner decides
    what it may touch), never open-world (no network); not declared
    read-only, since the wire carries no such declaration."""
    return {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }


def _binary_version(path: str | Path | None, fallback: str = "") -> str:
    if not path:
        return fallback
    try:
        out = subprocess.run(
            [str(path), "--version"], check=False, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return fallback
    text = (out.stdout or out.stderr or "").strip().splitlines()
    return text[0].strip() if text else fallback


def _check_version_pin(observed: str, pinned: str, *, harness: str) -> None:
    """A declared ``cli_version`` must appear in the binary's own report:
    an upgraded harness is a changed harness (refused, never silently run)."""
    pinned = str(pinned or "").strip()
    if not pinned:
        return
    if pinned not in observed:
        raise HeadlessHarnessError(
            f"{harness} binary reports {observed!r}, agents.yaml pins cli_version "
            f"{pinned!r}; update the pin deliberately or install the pinned version",
            code="harness_version_mismatch",
        )


class ClaudeCodeDriver(HarnessDriver):
    """Claude Code through the Claude Agent SDK: ``tools=[]`` removes every
    built-in tool; the pipeline's tools are an in-process SDK MCP server;
    settings, skills, hooks and CLAUDE.md are not loaded; the role's own
    system prompt REPLACES Claude Code's; a ``PreToolUse`` hook denies any
    tool that is not one of ours as a second guard."""

    kind = "claude_code"

    def __init__(self, bridge: HarnessBridge, config: Mapping[str, Any]) -> None:
        super().__init__(bridge, config)
        self._commands: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._version = ""
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._steps = 0
        self._seen_message_ids: set[str] = set()
        self._cost_usd: float | None = None

    def identity(self) -> dict:
        try:
            import claude_agent_sdk

            sdk_version = str(getattr(claude_agent_sdk, "__version__", "") or "")
        except Exception:  # noqa: BLE001 - identity only
            sdk_version = ""
        return {"harness": self.kind, "cli_version": self._version, "sdk_version": sdk_version}

    def _cli_path(self) -> str | None:
        declared = str(self.config.get("cli_path") or "").strip()
        return declared or None

    def start(self, spec: HarnessStart) -> None:
        try:
            from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport  # noqa: F401
        except ImportError as exc:
            raise HeadlessHarnessError(
                "claude_headless needs the claude-agent-sdk package (install the "
                "`headless` extra)",
                code="harness_missing",
            ) from exc
        cli = self._cli_path()
        if cli is None:
            try:
                import claude_agent_sdk

                bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
                cli = str(bundled) if bundled.is_file() else None
            except Exception:  # noqa: BLE001
                cli = None
        self._version = _binary_version(cli, fallback="unknown")
        _check_version_pin(self._version, str(self.config.get("cli_version") or ""), harness="Claude Code")
        self._thread = threading.Thread(
            target=self._run, args=(spec, cli), name=f"claude-headless-{spec.role_name}", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=float(self.config.get("start_timeout_s") or 120.0))
        if self._start_error is not None:
            raise HeadlessHarnessError(
                f"Claude Code did not start: {type(self._start_error).__name__}",
                code="harness_start",
            ) from self._start_error
        if not self._ready.is_set():
            raise HeadlessHarnessError("Claude Code did not start in time", code="harness_start")

    def send(self, text: str) -> None:
        self._commands.put(("prompt", str(text)))

    def interrupt(self) -> None:
        """Cancel the running turn from the harness loop (not queued: the
        worker is inside the turn's message stream while a turn runs)."""
        loop, client = self._loop, self._client
        if loop is None or client is None or not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(client.interrupt(), loop)
        except Exception:  # noqa: BLE001 - the loop may be closing
            pass

    def close(self, timeout: float = 15.0) -> None:
        self._commands.put(("close", None))
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        if thread is not None and thread.is_alive():
            loop, client = self._loop, self._client
            if loop is not None and client is not None and loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(client.disconnect(), loop)
                except Exception:  # noqa: BLE001
                    pass
                thread.join(timeout=5.0)

    # -- the worker thread ----------------------------------------------------
    def _run(self, spec: HarnessStart, cli: str | None) -> None:
        try:
            asyncio.run(self._main(spec, cli))
        except BaseException as exc:  # noqa: BLE001 - every escape is a harness fault
            if not self._ready.is_set():
                self._start_error = exc
                self._ready.set()
            else:
                self.bridge.fault(exc)

    def _build_tools(self, spec: HarnessStart) -> tuple[Any, list[str]]:
        from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

        bridge = self.bridge
        sdk_tools = []
        allowed: list[str] = []
        for wire in spec.tools:
            name = str(wire.get("name") or "")
            description = str(wire.get("description") or "")
            schema = dict(wire.get("input_schema") or {"type": "object", "properties": {}})
            annotations = ToolAnnotations(**_tool_annotations_for(wire))

            def make_handler(tool_name: str):
                async def handler(args: dict[str, Any]) -> dict[str, Any]:
                    call = bridge.call(tool_name, args)
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, call.wait)
                    return {
                        "content": [{"type": "text", "text": call.text}],
                        "is_error": bool(call.is_error),
                    }

                return handler

            sdk_tools.append(tool(name, description, schema, annotations=annotations)(make_handler(name)))
            allowed.append(f"mcp__{TOOL_SERVER_NAME}__{name}")
        server = create_sdk_mcp_server(name=TOOL_SERVER_NAME, version="1.0.0", tools=sdk_tools)
        return server, allowed

    def _options(self, spec: HarnessStart, cli: str | None) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

        server, allowed = self._build_tools(spec)
        prefix = f"mcp__{TOOL_SERVER_NAME}__"

        async def guard(input_data: Any, tool_use_id: Any, context: Any) -> dict:
            name = str((input_data or {}).get("tool_name") or "")
            if name.startswith(prefix):
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "only the task generation tools are permitted",
                }
            }

        env: dict[str, str] = {}
        api_key = str(self.config.get("api_key") or "")
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        workspace_id = str(self.config.get("workspace_id") or "")
        if workspace_id:
            env["ANTHROPIC_CUSTOM_HEADERS"] = f"anthropic-workspace-id: {workspace_id}"
        thinking_mode = str(self.config.get("thinking") or "adaptive")
        thinking = {"type": "disabled"} if thinking_mode == "disabled" else {"type": "adaptive"}
        effort = spec.effort if spec.effort in ("low", "medium", "high", "xhigh", "max") else None
        return ClaudeAgentOptions(
            tools=[],
            allowed_tools=allowed,
            mcp_servers={TOOL_SERVER_NAME: server},
            strict_mcp_config=True,
            permission_mode="dontAsk",
            setting_sources=[],
            skills=[],
            system_prompt=spec.system_prompt,
            model=spec.model or None,
            effort=effort,
            thinking=thinking,
            max_turns=_HARNESS_STEP_BACKSTOP,
            max_budget_usd=spec.max_budget_usd,
            cwd=str(spec.cwd),
            cli_path=cli,
            env=env,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[guard])]},
            # Meter partial output as it streams because terminal tool calls may
            # not emit a final result message.
            include_partial_messages=True,
            extra_args={"bare": None},
        )

    async def _main(self, spec: HarnessStart, cli: str | None) -> None:
        from claude_agent_sdk import ClaudeSDKClient

        self._loop = asyncio.get_running_loop()
        client = ClaudeSDKClient(self._options(spec, cli))
        await client.connect()
        self._client = client
        self._ready.set()
        prompt: str | None = spec.prompt
        try:
            while True:
                if prompt is not None:
                    await client.query(prompt)
                    prompt = None
                    await self._consume_turn(client)
                command, payload = await self._loop.run_in_executor(None, self._commands.get)
                if command == "prompt":
                    prompt = str(payload)
                elif command == "close":
                    break
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _consume_turn(self, client: Any) -> None:
        """Consume one harness turn; a turn that ends on a transient API
        error is re-issued (`HARNESS_RETRIES` times, exponential backoff)
        before it is reported as an error."""
        retries = 0
        while True:
            outcome = await self._consume_turn_once(client)
            if outcome is None:
                return
            category, end = outcome
            if category not in _CLAUDE_TRANSIENT_ERRORS or retries >= HARNESS_RETRIES:
                self.bridge.turn_ended(end)
                return
            retries += 1
            self.bridge.log.append({"event": "harness_retry", "attempt": retries, "error": category})
            await asyncio.sleep(_retry_backoff_seconds(retries - 1))
            await client.query(HARNESS_RETRY_TEXT)

    async def _consume_turn_once(self, client: Any) -> tuple[str, TurnEnd] | None:
        """One pass over the turn's messages. Returns None when the turn
        ended normally (posted to the bridge), else the error category and
        the `TurnEnd` the caller may post or retry."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, TextBlock

        text_parts: list[str] = []
        last_error = ""
        # Output tokens metered as they stream, per API step: the step's
        # running count from `message_delta`, settled against the turn's
        # result when one arrives.
        step_output = 0
        turn_output_metered = 0
        async for message in client.receive_response():
            if isinstance(message, StreamEvent):
                event = message.event if isinstance(message.event, Mapping) else {}
                kind = str(event.get("type") or "")
                if kind == "message_start":
                    step_output = 0
                    self.bridge.step_started()
                elif kind == "message_delta":
                    usage = event.get("usage") if isinstance(event.get("usage"), Mapping) else {}
                    running = int(usage.get("output_tokens") or 0)
                    if running > step_output:
                        self.bridge.add_usage(Usage(output_tokens=running - step_output))
                        turn_output_metered += running - step_output
                        step_output = running
                    self.bridge.step_settled()
                continue
            if isinstance(message, AssistantMessage):
                if message.error:
                    last_error = str(message.error)
                if message.message_id and message.message_id not in self._seen_message_ids:
                    self._seen_message_ids.add(message.message_id)
                    self._steps += 1
                    usage = message.usage or {}
                    # Per-step input and cache counts are accurate here; the
                    # output count is a placeholder (metered from the stream).
                    self.bridge.add_usage(
                        Usage(
                            input_tokens=int(usage.get("input_tokens") or 0),
                            cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
                            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
                        )
                    )
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(str(block.text))
            elif isinstance(message, ResultMessage):
                usage = message.usage or {}
                turn_output = int(usage.get("output_tokens") or 0)
                if turn_output > turn_output_metered:
                    self.bridge.add_usage(Usage(output_tokens=turn_output - turn_output_metered))
                self._cost_usd = message.total_cost_usd
                reason = str(message.terminal_reason or ("error" if message.is_error else "completed"))
                details = list(message.errors or [])
                if last_error:
                    details.append(last_error)
                if message.api_error_status is not None:
                    details.append(f"http {message.api_error_status}")
                end = TurnEnd(
                    text="".join(text_parts),
                    reason=reason,
                    is_error=bool(message.is_error),
                    detail="; ".join(details) if details else str(message.subtype or ""),
                    harness_cost_usd=message.total_cost_usd,
                    steps=self._steps,
                )
                if message.is_error:
                    return (last_error or "unknown"), end
                self.bridge.turn_ended(end)
                return None
        end = TurnEnd(text="".join(text_parts), reason="stream_closed", is_error=True, steps=self._steps)
        return "unknown", end


def _free_loopback_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _identifier(name: str) -> bool:
    return name.isidentifier() and not name.startswith("__")


class CodexDriver(HarnessDriver):
    """Codex through the Codex app-server SDK: the pipeline's tools are served
    over a loopback streamable-HTTP MCP server; the shell tool, web search
    and session persistence are off; approvals are never asked (the tools
    carry honest non-destructive annotations, which Codex runs without an
    approval step); the sandbox is read-only and the working directory an
    empty scratch tree; the role's system prompt is the thread's base
    instructions."""

    kind = "codex"

    def __init__(self, bridge: HarnessBridge, config: Mapping[str, Any]) -> None:
        super().__init__(bridge, config)
        self._commands: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._http_server: Any = None
        self._http_thread: threading.Thread | None = None
        self._codex: Any = None
        self._codex_thread: Any = None
        self._handle: Any = None
        self._handle_lock = threading.Lock()
        self._version = ""
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._steps = 0

    def identity(self) -> dict:
        try:
            import openai_codex

            sdk_version = str(getattr(openai_codex, "__version__", "") or "")
        except Exception:  # noqa: BLE001
            sdk_version = ""
        return {"harness": self.kind, "cli_version": self._version, "sdk_version": sdk_version}

    def _codex_bin(self) -> str | None:
        declared = str(self.config.get("cli_path") or "").strip()
        if declared:
            return declared
        try:
            import codex_cli_bin  # type: ignore[import-not-found]

            candidate = Path(codex_cli_bin.__file__).parent / "bin" / "codex"
            return str(candidate) if candidate.is_file() else None
        except Exception:  # noqa: BLE001
            return None

    def _serve_tools(self, spec: HarnessStart) -> str:
        """Start the loopback MCP server for this session; returns its URL."""
        try:
            import uvicorn
            from mcp.server.mcpserver import MCPServer
            from mcp.types import ToolAnnotations
        except ImportError as exc:
            raise HeadlessHarnessError(
                "codex_headless needs the mcp and uvicorn packages (install the "
                "`headless` extra)",
                code="harness_missing",
            ) from exc
        bridge = self.bridge
        server = MCPServer(name=TOOL_SERVER_NAME, instructions="Task generation tools.")
        for wire in spec.tools:
            name = str(wire.get("name") or "")
            description = str(wire.get("description") or "")
            schema = dict(wire.get("input_schema") or {"type": "object", "properties": {}})
            properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
            required = set(schema.get("required") or ())
            param_names = [p for p in properties if _identifier(p)]

            def make_fn(tool_name: str, names: list[str]):
                async def fn(**kwargs: Any) -> str:
                    call = bridge.call(tool_name, kwargs)
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, call.wait)
                    if call.is_error:
                        # An error result reaches the model as the tool's text
                        # with the error flag, exactly as the runner phrased it.
                        from mcp.server.mcpserver.exceptions import ToolError  # type: ignore[import-not-found]

                        raise ToolError(call.text)
                    return call.text

                params = [
                    inspect.Parameter(
                        p,
                        inspect.Parameter.KEYWORD_ONLY,
                        default=(inspect.Parameter.empty if p in required else None),
                        annotation=Any,
                    )
                    for p in names
                ]
                fn.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
                fn.__name__ = tool_name
                fn.__annotations__ = {p: Any for p in names}
                return fn

            hints = _tool_annotations_for(wire)
            server.add_tool(
                make_fn(name, param_names),
                name=name,
                description=description,
                annotations=ToolAnnotations(
                    readOnlyHint=hints["readOnlyHint"],
                    destructiveHint=hints["destructiveHint"],
                    idempotentHint=hints["idempotentHint"],
                    openWorldHint=hints["openWorldHint"],
                ),
            )
            # The exact declared schema, not the one derived from the signature.
            registered = server._tool_manager._tools.get(name)  # noqa: SLF001 - no public override
            if registered is not None:
                registered.parameters = schema
        port = _free_loopback_port()
        app = server.streamable_http_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
        http = uvicorn.Server(config)
        self._http_server = http
        self._http_thread = threading.Thread(target=http.run, name=f"taskgen-mcp-{spec.role_name}", daemon=True)
        self._http_thread.start()
        deadline = time.monotonic() + 30.0
        while not getattr(http, "started", False):
            if time.monotonic() > deadline:
                raise HeadlessHarnessError("the loopback MCP server did not start", code="harness_start")
            time.sleep(0.05)
        return f"http://127.0.0.1:{port}/mcp"

    def start(self, spec: HarnessStart) -> None:
        try:
            from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
        except ImportError as exc:
            raise HeadlessHarnessError(
                "codex_headless needs the openai-codex package (install the `headless` extra)",
                code="harness_missing",
            ) from exc
        codex_bin = self._codex_bin()
        self._version = _binary_version(codex_bin, fallback="unknown")
        _check_version_pin(self._version, str(self.config.get("cli_version") or ""), harness="Codex")
        url = self._serve_tools(spec)
        overrides = (
            f"mcp_servers.{TOOL_SERVER_NAME}.url={url}",
            "features.shell_tool=false",
            "features.web_search=false",
            "features.multi_agent=false",
            'web_search="disabled"',
            'history.persistence="none"',
            'approval_policy="never"',
            'sandbox_mode="read-only"',
        )
        env = dict(os.environ)
        api_key = str(self.config.get("api_key") or "")
        if api_key:
            env["CODEX_API_KEY"] = api_key
        start_timeout = float(self.config.get("start_timeout_s") or 120.0)
        failure: list[BaseException] = []

        def open_thread() -> None:
            try:
                self._codex = Codex(
                    CodexConfig(codex_bin=codex_bin, cwd=str(spec.cwd), env=env, config_overrides=overrides)
                )
                self._codex_thread = self._codex.thread_start(
                    approval_mode=ApprovalMode.deny_all,
                    sandbox=Sandbox.read_only,
                    ephemeral=True,
                    cwd=str(spec.cwd),
                    base_instructions=spec.system_prompt,
                    model=spec.model or None,
                )
            except BaseException as exc:  # noqa: BLE001 - classified below
                failure.append(exc)

        # `initialize` and `thread/start` are SDK requests with no reply
        # deadline of their own: bound them here, then tear down on overrun.
        if not self._bounded(open_thread, start_timeout, "codex-start") or failure:
            self.close()
            if failure:
                raise HeadlessHarnessError(
                    f"Codex did not start: {type(failure[0]).__name__}", code="harness_start"
                ) from failure[0]
            raise HeadlessHarnessError(
                f"Codex did not start within {start_timeout:.0f}s", code="harness_start"
            )
        self._ready.set()
        self._thread = threading.Thread(
            target=self._run, args=(spec,), name=f"codex-headless-{spec.role_name}", daemon=True
        )
        self._thread.start()

    def send(self, text: str) -> None:
        self._commands.put(("prompt", str(text)))

    #: Bound SDK requests because its JSON-RPC waiter is unbounded; abandon a
    #: stuck helper thread at this timeout and tear down the process.
    SDK_REQUEST_TIMEOUT_S = 10.0

    @staticmethod
    def _bounded(fn: Any, timeout: float, name: str) -> bool:
        """Run `fn` in a daemon thread; True when it returned within `timeout`."""
        done = threading.Event()

        def run() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001 - the caller only needs completion
                pass
            finally:
                done.set()

        threading.Thread(target=run, name=name, daemon=True).start()
        return done.wait(timeout)

    def interrupt(self) -> None:
        with self._handle_lock:
            handle = self._handle
        if handle is not None:
            self._bounded(handle.interrupt, self.SDK_REQUEST_TIMEOUT_S, "codex-interrupt")

    def close(self, timeout: float = 15.0) -> None:
        self._commands.put(("close", None))
        self.interrupt()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        codex = self._codex
        self._codex = None
        if codex is not None:
            # `Codex.close` terminates the app-server (bounded: terminate, wait
            # 2 s, kill), which also releases any waiter still parked on it.
            self._bounded(codex.close, self.SDK_REQUEST_TIMEOUT_S, "codex-close")
        http = self._http_server
        self._http_server = None
        if http is not None:
            http.should_exit = True
            if self._http_thread is not None and self._http_thread.is_alive():
                self._http_thread.join(timeout=timeout)

    # -- the worker thread ----------------------------------------------------
    def _run(self, spec: HarnessStart) -> None:
        prompt: str | None = spec.prompt
        try:
            while True:
                if prompt is not None:
                    self._run_turn(prompt, spec)
                    prompt = None
                command, payload = self._commands.get()
                if command == "prompt":
                    prompt = str(payload)
                elif command == "close":
                    break
        except BaseException as exc:  # noqa: BLE001 - every escape is a harness fault
            self.bridge.fault(exc)

    def _run_turn(self, prompt: str, spec: HarnessStart) -> None:
        retries = 0
        while True:
            outcome = self._run_turn_once(prompt, spec)
            if outcome is None:
                return
            end = outcome
            if not _codex_error_is_transient(end.detail) or retries >= HARNESS_RETRIES:
                self.bridge.turn_ended(end)
                return
            retries += 1
            self.bridge.log.append({"event": "harness_retry", "attempt": retries, "error": end.detail[:120]})
            time.sleep(_retry_backoff_seconds(retries - 1))
            prompt = HARNESS_RETRY_TEXT

    def _run_turn_once(self, prompt: str, spec: HarnessStart) -> TurnEnd | None:
        """One Codex turn. Returns None when it completed (posted to the
        bridge), else the failed `TurnEnd` the caller may post or retry."""
        effort: Any = None
        if spec.effort in ("minimal", "low", "medium", "high", "xhigh", "max"):
            from openai_codex.generated.v2_all import ReasoningEffort

            effort = ReasoningEffort(spec.effort)
        handle = self._codex_thread.turn(prompt, effort=effort)
        with self._handle_lock:
            self._handle = handle
        text_parts: list[str] = []
        status = "completed"
        error_detail = ""
        try:
            for notification in handle.stream():
                method = str(getattr(notification, "method", "") or "")
                payload = getattr(notification, "payload", None)
                if method == "thread/tokenUsage/updated":
                    total = getattr(getattr(payload, "token_usage", None), "total", None)
                    if total is not None:
                        self.bridge.set_usage(_codex_usage(total))
                elif method == "item/completed":
                    item = getattr(payload, "item", None)
                    root = getattr(item, "root", item)
                    kind = str(getattr(root, "type", "") or type(root).__name__)
                    if kind in ("agentMessage", "AgentMessageThreadItem"):
                        text_parts.append(str(getattr(root, "text", "") or ""))
                    self._steps += 1
                elif method == "turn/completed":
                    turn = getattr(payload, "turn", None)
                    status = str(getattr(getattr(turn, "status", None), "value", getattr(turn, "status", "completed")) or "completed")
                    error = getattr(turn, "error", None)
                    if error is not None:
                        error_detail = str(getattr(error, "message", "") or "")
                    usage = getattr(turn, "usage", None) or getattr(turn, "token_usage", None)
                    total = getattr(usage, "total", None)
                    if total is not None:
                        self.bridge.set_usage(_codex_usage(total))
        finally:
            with self._handle_lock:
                self._handle = None
        is_error = status not in ("completed",) and status != "TurnStatus.completed"
        reason = status.replace("TurnStatus.", "")
        end = TurnEnd(
            text="".join(text_parts),
            reason=reason,
            is_error=bool(is_error and reason != "interrupted"),
            detail=error_detail,
            steps=self._steps,
        )
        if end.is_error:
            return end
        self.bridge.turn_ended(end)
        return None


def _codex_usage(total: Any) -> Usage:
    """Codex reports cumulative totals with the cached part INSIDE
    ``inputTokens`` (the probe: 43906 input of which 31360 cached, 384
    output, 44290 total); reasoning tokens are inside ``outputTokens``."""
    data = total.model_dump(by_alias=True) if hasattr(total, "model_dump") else dict(total or {})
    input_tokens = int(data.get("inputTokens") or 0)
    cached = int(data.get("cachedInputTokens") or 0)
    cached = max(0, min(cached, input_tokens))
    return Usage(
        input_tokens=input_tokens - cached,
        output_tokens=int(data.get("outputTokens") or 0),
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=int(data.get("cacheWriteInputTokens") or 0),
    )


# ---------------------------------------------------------------------------
# The backend: the session-turn source `RoutedProvider._turn` calls
# ---------------------------------------------------------------------------


@dataclass
class _HeadlessSession:
    role_name: str
    bridge: HarnessBridge
    driver: HarnessDriver
    scratch: Path
    model: str
    accounted: Usage = field(default_factory=Usage)
    pending: ToolCall | None = None
    turn_open: bool = True
    turns: int = 0
    last_end: TurnEnd | None = None

    def usage_delta(self) -> Usage:
        now = self.bridge.usage_snapshot()
        delta = Usage(
            input_tokens=max(0, now.input_tokens - self.accounted.input_tokens),
            output_tokens=max(0, now.output_tokens - self.accounted.output_tokens),
            cache_read_input_tokens=max(0, now.cache_read_input_tokens - self.accounted.cache_read_input_tokens),
            cache_creation_input_tokens=max(
                0, now.cache_creation_input_tokens - self.accounted.cache_creation_input_tokens
            ),
        )
        self.accounted = now
        return delta


def _user_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, (list, tuple)):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
    return "".join(parts)


def _tool_result_for(message: Mapping[str, Any], tool_use_id: str) -> tuple[str, bool] | None:
    """The runner's answer to `tool_use_id` in the last user message, as
    `_with_tool_results` phrased it: (text, is_error); None when absent."""
    content = message.get("content")
    if not isinstance(content, (list, tuple)):
        return None
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "tool_result":
            continue
        if str(block.get("tool_use_id") or "") != tool_use_id:
            continue
        inner = block.get("content")
        if isinstance(inner, str):
            text = inner
        elif isinstance(inner, (list, tuple)):
            text = "".join(
                str(b.get("text") or "") for b in inner if isinstance(b, Mapping) and b.get("type") == "text"
            )
        else:
            text = ""
        return text, bool(block.get("is_error", False))
    return None


class HeadlessBackend:
    """A session backend whose model turns come from a headless coding-agent
    harness. Implements the same ``step``/``complete`` surface as the API
    backends, so ``RoutedProvider._turn`` keys, records, reserves and charges
    it unchanged."""

    provider_key = "headless"
    driver_class: type[HarnessDriver] = HarnessDriver

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        agents_config: Any = None,
        driver_factory: Callable[[HarnessBridge, Mapping[str, Any]], HarnessDriver] | None = None,
    ) -> None:
        self._config = dict(config or {})
        self._agents_config = agents_config
        self._driver_factory = driver_factory or (lambda bridge, cfg: self.driver_class(bridge, cfg))
        self._sessions: dict[str, _HeadlessSession] = {}

    # -- lifecycle -----------------------------------------------------------
    def close_session(self, role_name: str) -> None:
        """End the harness of `role_name`'s session (idempotent): a pending
        tool call is answered with the session-ended refusal, the running
        turn interrupted, the process closed, the scratch tree removed."""
        session = self._sessions.pop(str(role_name), None)
        if session is None:
            return
        session.bridge.close()
        if session.pending is not None and not session.pending.answered:
            session.pending.resolve(SESSION_ENDED_TEXT, True)
        try:
            session.driver.interrupt()
        except Exception:  # noqa: BLE001
            pass
        try:
            session.driver.close()
        finally:
            shutil.rmtree(session.scratch, ignore_errors=True)

    def close(self) -> None:
        for role_name in list(self._sessions):
            self.close_session(role_name)

    def harness_log(self, role_name: str) -> list[dict]:
        session = self._sessions.get(str(role_name))
        return list(session.bridge.log) if session is not None else []

    # -- the session-turn source -------------------------------------------
    def step(
        self,
        *,
        role_name: str,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        max_tokens: int,
        effort: str | None,
        tools: Sequence[Mapping[str, Any]] = (),
        tool_choice: Any = None,
        prompt_cache: bool = False,
    ) -> BackendTurn:
        prefix = [dict(m) for m in messages]
        if not prefix or str(prefix[-1].get("role") or "") != "user":
            raise ValueError("a headless turn opens on a user message")
        role_name = str(role_name)
        session = self._sessions.get(role_name)
        started = time.monotonic()
        if session is None:
            if len(prefix) != 1:
                raise HeadlessHarnessError(
                    f"headless session for role {role_name!r} cannot be continued from "
                    f"a served prefix of {len(prefix)} messages; a harness session runs "
                    "live from its first turn or replays whole from the store",
                    code="headless_prefix",
                )
            session = self._open(role_name, model, effort, tools, _user_text(prefix[0]))
        else:
            self._deliver(session, prefix[-1])
        wire_names = [str(t.get("name") or "") for t in tools]
        full_wire = session.bridge.tool_names
        session.bridge.set_wire(None if set(wire_names) == set(full_wire) else wire_names)
        kind, payload = session.bridge.next_event()
        if kind == "tool_call":
            session.bridge.settle()
        elapsed_ms = max(0, int(round((time.monotonic() - started) * 1000.0)))
        delta = session.usage_delta()
        session.turns += 1
        identity = session.driver.identity()
        if kind == "tool_call":
            call: ToolCall = payload
            session.pending = call
            session.turn_open = True
            content = ({"type": "tool_use", "id": call.id, "name": call.name, "input": dict(call.args)},)
            record = AttemptRecord(usage=delta, elapsed_ms=elapsed_ms, served_model=model, stop_reason="tool_use")
            return BackendTurn(
                content=content,
                stop_reason="tool_use",
                model=model,
                raw={"headless": identity, "event": "tool_call", "tool": call.name, "id": call.id},
                attempts=(record,),
            )
        if kind == "turn_end":
            end: TurnEnd = payload
            session.pending = None
            session.turn_open = False
            session.last_end = end
            record = AttemptRecord(usage=delta, elapsed_ms=elapsed_ms, served_model=model, stop_reason="end_turn")
            if end.reason == "budget_exceeded":
                exc = RoleCapExceeded(
                    f"the {identity.get('harness', 'harness')} budget cap ended the session "
                    f"for role {role_name!r}",
                    scope="role",
                )
                _attach_attempts(exc, (record,))
                raise exc
            if end.is_error:
                exc = HeadlessHarnessError(
                    f"{identity.get('harness', 'harness')} turn for role {role_name!r} failed: "
                    f"{end.reason} {end.detail}".strip(),
                    code="transport",
                )
                _attach_attempts(exc, (record,))
                raise exc
            return BackendTurn(
                content=({"type": "text", "text": end.text},),
                stop_reason="end_turn",
                model=model,
                raw={
                    "headless": identity,
                    "event": "turn_end",
                    "reason": end.reason,
                    "harness_steps": end.steps,
                    "harness_cost_usd": end.harness_cost_usd,
                },
                attempts=(record,),
            )
        exc = payload if isinstance(payload, BaseException) else RuntimeError(str(payload))
        fault = HeadlessHarnessError(
            f"{identity.get('harness', 'harness')} session for role {role_name!r} faulted: "
            f"{type(exc).__name__}",
            code="transport",
        )
        _attach_attempts(fault, (AttemptRecord(usage=delta, elapsed_ms=elapsed_ms, served_model=model),))
        raise fault from exc

    def _open(
        self, role_name: str, model: str, effort: str | None, tools: Sequence[Mapping[str, Any]], prompt: str
    ) -> _HeadlessSession:
        system = _system_prompt(role_name, schema_mode=uses_findings_schema(role_name), agents_config=self._agents_config)
        bridge = HarnessBridge([str(t.get("name") or "") for t in tools])
        scratch = Path(tempfile.mkdtemp(prefix=f"headless-{role_name}-"))
        driver = self._driver_factory(bridge, self._config)
        session = _HeadlessSession(role_name=role_name, bridge=bridge, driver=driver, scratch=scratch, model=model)
        self._sessions[role_name] = session
        spec = HarnessStart(
            system_prompt=system or "",
            prompt=prompt,
            tools=tuple(dict(t) for t in tools),
            model=model,
            effort=effort,
            max_budget_usd=self._budget_backstop(),
            cwd=scratch,
            role_name=role_name,
        )
        try:
            driver.start(spec)
        except BaseException:
            self.close_session(role_name)
            raise
        return session

    def _budget_backstop(self) -> float | None:
        raw = self._config.get("max_budget_usd")
        try:
            value = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            value = None
        return value if value is not None and value > 0 else None

    def _deliver(self, session: _HeadlessSession, last: Mapping[str, Any]) -> None:
        """Hand the runner's answer back to the harness: the tool_result of
        the pending call, or a user text (a correction or nudge) as the next
        prompt of an ended turn."""
        if session.pending is not None:
            answer = _tool_result_for(last, session.pending.id)
            if answer is None:
                # The runner answered a text-only correction to a call it
                # never executed (a policy correction): the fixed text goes
                # back as the call's error result.
                answer = (_user_text(last) or SESSION_ENDED_TEXT, True)
            session.pending.resolve(*answer)
            session.pending = None
            return
        if not session.turn_open:
            session.driver.send(_user_text(last))
            session.turn_open = True
            return
        raise HeadlessHarnessError(
            f"headless session for role {session.role_name!r} received an answer "
            "with no pending call and no ended turn",
            code="headless_protocol",
        )

    # -- one-shot exchanges ----------------------------------------------------
    def complete(
        self,
        *,
        role_name: str,
        model: str,
        prompt: str,
        max_tokens: int,
        effort: str | None,
        prompt_cache: bool = False,
    ) -> BackendResult:
        """Run one harness exchange.

        Prose roles return final text with no tools. Schema roles use the forced submit
        tool, validate its input through the normal provider contract, and retry with
        the fixed correction up to `SCHEMA_RETRIES`.
        """
        role_name = str(role_name)
        if role_name in self._sessions:
            raise RuntimeError(f"a headless session for role {role_name!r} is already open")
        if uses_findings_schema(role_name):
            return self._complete_schema(role_name=role_name, model=model, prompt=prompt, effort=effort)
        started = time.monotonic()
        session = self._open(role_name, model, effort, (), prompt)
        try:
            kind, payload = session.bridge.next_event()
            delta = session.usage_delta()
            elapsed_ms = max(0, int(round((time.monotonic() - started) * 1000.0)))
            if kind != "turn_end":
                exc = payload if isinstance(payload, BaseException) else RuntimeError(str(payload))
                raise HeadlessHarnessError(
                    f"one-shot headless exchange for role {role_name!r} faulted: {type(exc).__name__}",
                    code="transport",
                ) from exc
            end: TurnEnd = payload
            if end.is_error:
                raise HeadlessHarnessError(
                    f"one-shot headless exchange for role {role_name!r} failed: {end.reason} {end.detail}".strip(),
                    code="transport",
                )
            record = AttemptRecord(usage=delta, elapsed_ms=elapsed_ms, served_model=model, stop_reason="end_turn")
            return BackendResult(
                text=str(end.text or "").strip(),
                model=model,
                input_tokens=delta.input_tokens + delta.cache_read_input_tokens + delta.cache_creation_input_tokens,
                output_tokens=delta.output_tokens,
                raw_attempts=({"headless": session.driver.identity(), "reason": end.reason,
                               "harness_cost_usd": end.harness_cost_usd},),
                attempts=(record,),
            )
        finally:
            self.close_session(role_name)


    def _complete_schema(self, *, role_name: str, model: str, prompt: str, effort: str | None) -> BackendResult:
        tools = tuple(dict(t) for t in wire_tools_for(role_name, schema_mode=True))
        started = time.monotonic()
        session = self._open(role_name, model, effort, tools, prompt)
        records: list[AttemptRecord] = []
        raw_attempts: list[dict] = []
        problem = "no attempt made"
        try:
            for attempt_no in range(1 + SCHEMA_RETRIES):
                kind, payload = session.bridge.next_event()
                if kind == "tool_call":
                    session.bridge.settle()
                delta = session.usage_delta()
                elapsed_ms = max(0, int(round((time.monotonic() - started) * 1000.0)))
                if kind == "fault":
                    exc = payload if isinstance(payload, BaseException) else RuntimeError(str(payload))
                    fault = HeadlessHarnessError(
                        f"one-shot headless exchange for role {role_name!r} faulted: {type(exc).__name__}",
                        code="transport",
                    )
                    _attach_attempts(fault, tuple(records))
                    raise fault from exc
                if kind == "turn_end":
                    end: TurnEnd = payload
                    records.append(AttemptRecord(usage=delta, elapsed_ms=elapsed_ms, served_model=model, stop_reason="end_turn"))
                    raw_attempts.append({"headless": session.driver.identity(), "reason": end.reason, "tool_call": False})
                    if end.is_error:
                        fault = HeadlessHarnessError(
                            f"one-shot headless exchange for role {role_name!r} failed: {end.reason} {end.detail}".strip(),
                            code="transport",
                        )
                        _attach_attempts(fault, tuple(records))
                        raise fault
                    problem = "the response made no tool call; answer by calling the tool"
                    if attempt_no < SCHEMA_RETRIES:
                        session.driver.send(CORRECTION_TEXT.format(problem=problem))
                        session.turn_open = True
                    continue
                call: ToolCall = payload
                records.append(AttemptRecord(usage=delta, elapsed_ms=elapsed_ms, served_model=model, stop_reason="tool_use"))
                raw_attempts.append({"headless": session.driver.identity(), "tool_call": True, "tool": call.name, "id": call.id})
                data = json.loads(json.dumps(_plain_data(call.args)))
                problem = validate_payload_for(role_name, data) if isinstance(data, dict) else "the tool input is not an object"
                if problem is None:
                    text = normalized_text_for(role_name, data)
                    call.resolve("accepted", False)
                    total = Usage()
                    for record in records:
                        total = total + record.usage
                    return BackendResult(
                        text=text,
                        model=model,
                        input_tokens=total.input_tokens + total.cache_read_input_tokens + total.cache_creation_input_tokens,
                        output_tokens=total.output_tokens,
                        raw_attempts=tuple(raw_attempts),
                        attempts=tuple(records),
                    )
                call.resolve(CORRECTION_TEXT.format(problem=problem), True)
            exhausted = ProviderProtocolError(
                f"{session.driver.identity().get('harness', 'harness')} backend: role {role_name!r} "
                f"produced no valid response after {1 + SCHEMA_RETRIES} attempts: {problem}"
            )
            _attach_attempts(exhausted, tuple(records))
            raise exhausted
        finally:
            self.close_session(role_name)


class ClaudeHeadlessBackend(HeadlessBackend):
    provider_key = "claude_headless"
    driver_class = ClaudeCodeDriver


class CodexHeadlessBackend(HeadlessBackend):
    provider_key = "codex_headless"
    driver_class = CodexDriver


HEADLESS_PROVIDER_KINDS: tuple[str, ...] = ("claude_headless", "codex_headless")


def credential_problems_for(provider: str, cfg: Mapping[str, Any]) -> list[str]:
    """Static credential presence for one headless provider kind (names
    only; nothing is contacted)."""
    problems: list[str] = []
    if provider == "claude_headless":
        if not str(cfg.get("api_key") or "").strip():
            problems.append("ANTHROPIC_API_KEY is not set (claude_headless provider)")
    elif provider == "codex_headless":
        if not str(cfg.get("api_key") or "").strip():
            home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
            if not (home / "auth.json").is_file():
                problems.append(
                    "CODEX_API_KEY is not set and no Codex login is saved "
                    "(codex_headless provider: run `codex login` or set the key)"
                )
    return problems


def _register() -> None:
    from elt_taskgen.review import providers as providers_mod

    providers_mod.PROVIDER_BACKENDS["claude_headless"] = ClaudeHeadlessBackend
    providers_mod.PROVIDER_BACKENDS["codex_headless"] = CodexHeadlessBackend


_register()

"""Anthropic and OpenAI-compatible wire adapters and bounded schema retries."""

from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from elt_taskgen.models import canonical_json
from elt_taskgen.review.council import ProviderProtocolError
from elt_taskgen.review import provider_transport as _review_provider_transport
from elt_taskgen.review import provider_types as _review_provider_types
from elt_taskgen.review import provider_protocol as _provider_protocol


class MissingCredentialsError(RuntimeError):
    """A live call is needed but the provider has no credentials configured
    and no transcript can serve the prompt (fail closed, with instructions)."""


def _with_correction(payload: dict, last_response: dict, problem: str) -> dict:
    """Return retry messages containing the rejected turn and a specific correction reason.

    The reason is included because some response constraints cannot be represented in
    JSON Schema.
    """
    assistant = last_response.get("content")
    correction = _provider_protocol.CORRECTION_TEXT.format(problem=problem)
    tool_use_ids = _review_provider_types._tool_use_ids(assistant) if assistant else []
    messages = _review_provider_types._with_tool_results(
        list(payload.get("messages") or []),
        assistant if assistant else None,
        [_review_provider_types.ToolResultBlock(tool_use_id, correction, True) for tool_use_id in tool_use_ids],
        text=correction,
    )
    return {**payload, "messages": messages}

_CHAT_ASSISTANT_KEYS = ("role", "content", "tool_calls")


def _chat_assistant_turn(message: Mapping[str, Any]) -> dict:
    """The rejected assistant turn, reduced to the wire-legal fields."""
    turn: dict = {
        key: message[key] for key in _CHAT_ASSISTANT_KEYS if key in message
    }
    turn["role"] = "assistant"
    # A tool-calling reply carries content=None; some endpoints reject a null
    # content on the way back in, and "" is the same statement.
    if turn.get("content") is None:
        turn["content"] = ""
    return turn


def _with_correction_chat(payload: dict, last_response: dict, problem: str) -> dict:
    """`_with_correction` for the OpenAI chat-completions shape.

    SAME PROTOCOL RULE, OTHER WIRE: every `tool_calls` entry must be answered by
    a `role: "tool"` message with the matching `tool_call_id` before any further
    user turn, so a bare user message leaves the call unanswered (a 400 on a
    strict upstream). A trailing user turn repeats the correction for models
    that ignore tool output; prose roles keep the plain user turn.
    """
    messages = list(payload.get("messages") or [])
    choices = last_response.get("choices") or []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    assistant = first.get("message")
    correction = _provider_protocol.CORRECTION_TEXT.format(problem=problem)
    tool_call_ids: list[str] = []
    if isinstance(assistant, Mapping) and assistant:
        messages.append(_chat_assistant_turn(assistant))
        tool_call_ids = [
            str(call.get("id"))
            for call in (assistant.get("tool_calls") or [])
            if isinstance(call, Mapping) and call.get("id")
        ]
    for tool_call_id in tool_call_ids:
        messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": correction}
        )
    messages.append({"role": "user", "content": correction})
    return {**payload, "messages": messages}


def _anthropic_tool_choice(choice: Any) -> dict | None:
    """`tool_choice` for the Messages API: `"any"` / `"auto"` by name, a tool
    NAME as the forced tool, a mapping verbatim, None / `"none"` as absent."""
    if choice is None or choice == "none":
        return None
    if isinstance(choice, Mapping):
        return dict(choice)
    if choice in ("any", "auto"):
        return {"type": str(choice)}
    return {"type": "tool", "name": str(choice)}


def _chat_tool_choice(choice: Any) -> Any:
    """The chat-completions spelling of the same choice: `"required"` for
    `any`, `"auto"`, or the forced function object."""
    if choice is None or choice == "none":
        return None
    if isinstance(choice, Mapping):
        kind = str(choice.get("type") or "")
        if kind == "tool":
            return {"type": "function", "function": {"name": str(choice.get("name") or "")}}
        if kind == "any":
            return "required"
        if kind == "auto":
            return "auto"
        return dict(choice)
    if choice == "any":
        return "required"
    if choice == "auto":
        return "auto"
    return {"type": "function", "function": {"name": str(choice)}}


def _chat_tools(tools: Sequence[Mapping[str, Any]]) -> list[dict]:
    """The chat-completions function objects of the wire tools: name,
    description and schema field for field (what the manifest hashes)."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        }
        for tool in tools
    ]


def _arguments_text(value: Any) -> str:
    if isinstance(value, (Mapping, list, tuple)):
        return canonical_json(_review_provider_types._plain_data(value))
    if value is None:
        return "{}"
    return json.dumps(value)


def _chat_messages(messages: Sequence[Mapping[str, Any]], system: str | None) -> list[dict]:
    """Render the canonical message list for the chat-completions wire: the
    system prompt first; an assistant turn's `tool_use` blocks as
    `tool_calls`; a user turn's `tool_result` blocks as one `role: "tool"`
    message per id (every id answered, in order) followed by its text as a
    user message; plain strings unchanged."""
    out: list[dict] = []
    if system is not None:
        out.append({"role": "system", "content": system})
    for message in messages:
        role = str(message.get("role") or "")
        content = message.get("content")
        if role == "assistant" and isinstance(content, (list, tuple)):
            text = "".join(
                str(b.get("text", ""))
                for b in content
                if isinstance(b, Mapping) and b.get("type") == "text"
            )
            calls = [
                {
                    "id": str(b.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(b.get("name") or ""),
                        "arguments": _arguments_text(b.get("input")),
                    },
                }
                for b in _review_provider_types._tool_use_blocks(content)
            ]
            turn: dict = {"role": "assistant", "content": text}
            if calls:
                turn["tool_calls"] = calls
            out.append(turn)
        elif role == "user" and isinstance(content, (list, tuple)):
            texts: list[str] = []
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") == "tool_result":
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(block.get("tool_use_id") or ""),
                            "content": str(block.get("content") or ""),
                        }
                    )
                elif block.get("type") == "text":
                    texts.append(str(block.get("text", "")))
            if texts:
                out.append({"role": "user", "content": "\n\n".join(texts)})
        else:
            out.append({"role": role, "content": content if isinstance(content, str) else _review_provider_types._plain_data(content)})
    return out


def _chat_content_blocks(message: Mapping[str, Any]) -> list[dict]:
    """A chat-completions assistant message as canonical content blocks:
    its text, then one `tool_use` per `tool_calls` entry (arguments decoded;
    an undecodable string becomes `input: None`, which `BackendTurn` reports
    as `invalid_arguments`; a missing id is synthesised so it can be
    answered)."""
    blocks: list[dict] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        blocks.append({"type": "text", "text": text})
    for index, call in enumerate(message.get("tool_calls") or []):
        if not isinstance(call, Mapping):
            continue
        function = call.get("function") or {}
        arguments = function.get("arguments") if isinstance(function, Mapping) else None
        parsed: Any = None
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except (json.JSONDecodeError, ValueError):
                parsed = None
        elif isinstance(arguments, Mapping):
            parsed = dict(arguments)
        blocks.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or f"call_{index}"),
                "name": str(function.get("name") or "") if isinstance(function, Mapping) else "",
                "input": parsed,
            }
        )
    return blocks


def _cache_first_user_turn(messages: list[dict]) -> list[dict]:
    """The Cost T8 item 1 second breakpoint: `cache_control` on the FIRST
    user turn (the task view). A string turn is wrapped in a text block; a
    block list gets the marker on its last block."""
    out = [dict(m) for m in messages]
    for message in out:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = [
                {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
            ]
        elif isinstance(content, list) and content:
            blocks = [dict(b) if isinstance(b, Mapping) else b for b in content]
            if isinstance(blocks[-1], dict):
                blocks[-1]["cache_control"] = {"type": "ephemeral"}
            message["content"] = blocks
        break
    return out


class AnthropicBackend:
    """Anthropic Messages API backend (stdlib HTTP; no SDK dependency).

    Critic roles get a tool-forced, strict-schema report_findings call; prose
    roles a plain completion. NO SAMPLING PARAMETERS ARE EVER SENT — the current
    models reject temperature/top_p/top_k with a 400, so determinism is
    transcript memoization's job, never the request's.
    """

    API_VERSION = "2023-06-01"
    #: Anthropic models that accept output_config.effort (claude-haiku-4-5
    #: rejects the parameter, so routes on it must leave effort unset).
    _EFFORT_MODELS_PREFIXES = ("claude-opus-5", "claude-sonnet-5")

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.anthropic.com",
        transport: _review_provider_transport.Transport | None = None,
        agents_config: Path | str | None = None,
        workspace_id: str = "",
    ):
        if not api_key:
            raise MissingCredentialsError(
                "ANTHROPIC_API_KEY is not configured — set it (or seed "
                "transcripts with 'elt-taskgen record-transcripts') before "
                "running live council stages"
            )
        self._api_key = api_key
        #: An API key that is not scoped to a workspace must name one per
        #: request (`anthropic-workspace-id`); a scoped key must not, so the
        #: header is sent only when configured. Never logged: it is an
        #: account identifier, not a secret, but it is nobody else's business.
        self._workspace_id = str(workspace_id or "").strip()
        self._url = base_url.rstrip("/") + "/v1/messages"
        self._transport = transport or _review_provider_transport._http_post_json
        #: The agents document the system prompts are derived from (None =
        #: the repository default): what `RoutedProvider` was loaded from.
        self._agents_config = agents_config

    def _headers(self) -> dict[str, str]:
        """The wire headers for every Messages call: the key, the pinned API
        version, and the workspace id ONLY when one is configured."""
        headers = {"x-api-key": self._api_key, "anthropic-version": self.API_VERSION}
        if self._workspace_id:
            headers["anthropic-workspace-id"] = self._workspace_id
        return headers

    # -- request building --------------------------------------------------

    @classmethod
    def _payload(cls, *, model: str, prompt: str, max_tokens: int,
                 effort: str | None, role_name: str, schema_mode: bool,
                 prompt_cache: bool = False,
                 agents_config: Path | str | None = None) -> dict:
        # Live and batch requests share this payload builder. Prompt caching
        # marks the system/tools and first user turn when the route enables it.
        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if prompt_cache:
            payload["messages"] = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ]
        if effort and model.startswith(cls._EFFORT_MODELS_PREFIXES):
            payload["output_config"] = {"effort": effort}
        system = _provider_protocol._system_prompt(role_name, schema_mode=schema_mode, agents_config=agents_config)
        if system is not None:
            if prompt_cache:
                payload["system"] = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                payload["system"] = system
        if schema_mode:
            # THE SAME LIST THE BEHAVIOUR MANIFEST HASHES (Phase 0.E): the
            # forced submit tool plus the role's registered tools (none yet).
            payload["tools"] = _provider_protocol.wire_tools_for(role_name, schema_mode=True)
            payload["tool_choice"] = _provider_protocol.tool_choice_for(role_name, schema_mode=True)
        return payload

    # -- response parsing ---------------------------------------------------

    @staticmethod
    def _usage(resp: dict) -> tuple[int, int]:
        usage = resp.get("usage") or {}
        return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)

    @staticmethod
    def _attempt_record(resp: Mapping[str, Any], elapsed_ms: int) -> _review_provider_types.AttemptRecord:
        return _review_provider_types.AttemptRecord(
            usage=_review_provider_types.Usage.from_anthropic(resp),
            elapsed_ms=int(elapsed_ms),
            served_model=str(resp.get("model") or ""),
            stop_reason=(
                str(resp["stop_reason"]) if resp.get("stop_reason") else None
            ),
        )

    @staticmethod
    def _tool_input(resp: dict, tool_name: str = _provider_protocol.FINDINGS_TOOL_NAME) -> Any:
        for block in resp.get("content") or []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == tool_name
            ):
                return block.get("input")
        return None

    @staticmethod
    def _text(resp: dict) -> str:
        return "".join(
            block.get("text", "")
            for block in resp.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        )

    # -- public -------------------------------------------------------------

    def complete(self, *, role_name: str, model: str, prompt: str,
                 max_tokens: int, effort: str | None,
                 prompt_cache: bool = False) -> _review_provider_types.BackendResult:
        schema_mode = _provider_protocol.uses_findings_schema(role_name)
        payload = self._payload(
            model=model, prompt=prompt, max_tokens=max_tokens,
            effort=effort, role_name=role_name, schema_mode=schema_mode,
            prompt_cache=prompt_cache, agents_config=self._agents_config,
        )
        attempts: list[dict] = []
        records: list[_review_provider_types.AttemptRecord] = []
        in_tokens = out_tokens = 0
        problem = "no attempt made"
        for attempt_no in range(1 + _provider_protocol.SCHEMA_RETRIES):
            if attempt_no:
                # Feed the rejection BACK: an identical payload cannot fix a
                # SEMANTIC validation failure, so retries without it are three
                # guaranteed-identical failures at three times the cost.
                payload = _with_correction(payload, attempts[-1], problem)
            started = time.monotonic()
            try:
                resp = self._transport(
                    self._url,
                    self._headers(),
                    payload,
                )
            except Exception as exc:
                # The attempts BEFORE this one were paid for; hand them to the
                # meter with the failure rather than losing them.
                _review_provider_types._attach_attempts(exc, records)
                raise
            attempts.append(resp)
            records.append(self._attempt_record(resp, _review_provider_types._elapsed_ms(started)))
            i, o = self._usage(resp)
            in_tokens += i
            out_tokens += o
            if schema_mode:
                data = self._tool_input(resp, _provider_protocol.tool_name_for(role_name))
                problem = _provider_protocol.validate_payload_for(role_name, data)
                if problem is not None and resp.get("stop_reason") == "max_tokens":
                    # A tool call cut off by the token budget arrives as an
                    # empty/partial input dict; name the fix in the failure
                    # rather than reporting "missing 'findings' list" thrice.
                    problem += (
                        " (response hit max_tokens mid-tool-call — raise this "
                        "role's max_tokens in config/agents.yaml)"
                    )
                if problem is None:
                    return _review_provider_types.BackendResult(
                        text=_provider_protocol.normalized_text_for(role_name, data),
                        model=model,
                        input_tokens=in_tokens,
                        output_tokens=out_tokens,
                        raw_attempts=tuple(attempts),
                        attempts=tuple(records),
                    )
            else:
                text = self._text(resp)
                if text.strip():
                    return _review_provider_types.BackendResult(
                        text=text, model=model,
                        input_tokens=in_tokens, output_tokens=out_tokens,
                        raw_attempts=tuple(attempts),
                        attempts=tuple(records),
                    )
                problem = "empty prose response"
        # Postmortem material in the failure itself: intermittent invalid tool
        # calls look identical from outside without the shape that arrived.
        shapes = []
        snippet = ""
        for att in attempts:
            data = self._tool_input(att, _provider_protocol.tool_name_for(role_name))
            shapes.append(
                f"keys={sorted(data)}" if isinstance(data, dict)
                else type(data).__name__
            )
            # Keep the misfire's exact bytes in the error; otherwise they are
            # discarded on raise and must be paid for again to reproduce.
            if not snippet and isinstance(data, dict):
                findings = data.get("findings")
                if isinstance(findings, str):
                    snippet = findings[:400]
                elif isinstance(findings, list):
                    bad = next(
                        (f for f in findings
                         if isinstance(f, dict) and f.get("proposed_case")),
                        None,
                    )
                    if bad is not None:
                        try:
                            snippet = json.dumps(
                                bad.get("proposed_case"), default=str
                            )[:400]
                        except (TypeError, ValueError):
                            snippet = repr(bad.get("proposed_case"))[:400]
        exhausted = ProviderProtocolError(
            f"anthropic backend: role {role_name!r} produced no valid "
            f"response after {1 + _provider_protocol.SCHEMA_RETRIES} attempts: {problem} "
            f"(attempt tool-input shapes: {'; '.join(shapes)}; "
            f"stop_reasons: {[a.get('stop_reason') for a in attempts]}"
            + (f"; undecodable findings head: {snippet!r}" if snippet else "")
            + ")"
        )
        # Every one of those attempts was billed: they reach the meter.
        _review_provider_types._attach_attempts(exhausted, records)
        raise exhausted

    # -- sessions (Phase 1.R): one turn, N tools, no schema retries ----------

    @classmethod
    def _session_payload(
        cls,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        max_tokens: int,
        effort: str | None,
        role_name: str,
        tools: Sequence[Mapping[str, Any]],
        tool_choice: Any,
        prompt_cache: bool = False,
        agents_config: Path | str | None = None,
    ) -> dict:
        """Build one Anthropic Messages session request.

        Include the full canonical message prefix, exact turn tools and choice, system
        prompt, token limit, and cache controls. Do not mutate caller messages.
        """
        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [dict(m) for m in messages],
        }
        if prompt_cache:
            payload["messages"] = _cache_first_user_turn(payload["messages"])
        if effort and model.startswith(cls._EFFORT_MODELS_PREFIXES):
            payload["output_config"] = {"effort": effort}
        system = _provider_protocol._system_prompt(
            role_name, schema_mode=_provider_protocol.uses_findings_schema(role_name), agents_config=agents_config
        )
        if system is not None:
            if prompt_cache:
                payload["system"] = [
                    {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                ]
            else:
                payload["system"] = system
        if tools:
            payload["tools"] = [dict(t) for t in tools]
            choice = _anthropic_tool_choice(tool_choice)
            if choice is not None:
                payload["tool_choice"] = choice
        return payload

    @staticmethod
    def _content_blocks(resp: Mapping[str, Any]) -> list[dict]:
        """EVERY content block of the reply, verbatim (text, tool_use, and
        anything else the wire carried, so a later turn can echo it)."""
        return [
            _review_provider_types._plain_data(block)
            for block in (resp.get("content") or [])
            if isinstance(block, Mapping)
        ]

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
    ) -> _review_provider_types.BackendTurn:
        """ONE session turn: one HTTP exchange over the message prefix with
        `tools` and `tool_choice`, returned as a `BackendTurn` whatever the
        model did (no tool call, several, a truncation) — the runner, not the
        backend, decides the correction. Every `tool_use` block is returned;
        `BackendTurn.tool_call()` refuses more than one. A transport fault
        propagates with no paid attempt attached (nothing was billed)."""
        payload = self._session_payload(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            effort=effort,
            role_name=role_name,
            tools=tools,
            tool_choice=tool_choice,
            prompt_cache=prompt_cache,
            agents_config=self._agents_config,
        )
        started = time.monotonic()
        try:
            resp = self._transport(
                self._url,
                self._headers(),
                payload,
            )
        except Exception as exc:
            _review_provider_types._attach_attempts(exc, ())
            raise
        record = self._attempt_record(resp, _review_provider_types._elapsed_ms(started))
        return _review_provider_types.BackendTurn(
            content=tuple(self._content_blocks(resp)),
            stop_reason=record.stop_reason,
            model=model,
            raw=_review_provider_types._plain_data(resp),
            attempts=(record,),
        )


class OpenAICompatBackend:
    """Chat-completions backend for OpenAI-compatible endpoints, and the
    mandatory cross-family backend for the IndependentImplementer. Same schema
    enforcement + retry-then-raise discipline as the Anthropic backend.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        transport: _review_provider_transport.Transport | None = None,
        agents_config: Path | str | None = None,
    ):
        if not base_url:
            raise MissingCredentialsError(
                "ELT_TASKGEN_OSS_BASE_URL is not configured — set the "
                "OpenAI-compatible endpoint (and ELT_TASKGEN_OSS_MODEL / "
                "ELT_TASKGEN_OSS_API_KEY as needed), or seed transcripts "
                "with 'elt-taskgen record-transcripts'"
            )
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key  # optional for self-hosted endpoints
        self._transport = transport or _review_provider_transport._http_post_json
        self._agents_config = agents_config

    def _headers(self) -> dict[str, str]:
        if self._api_key:
            return {"authorization": f"Bearer {self._api_key}"}
        return {}

    @staticmethod
    def _usage(resp: dict) -> tuple[int, int]:
        usage = resp.get("usage") or {}
        return (
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )

    @staticmethod
    def _reported_usd(resp: dict) -> float | None:
        """`usage.cost` when the endpoint states one, else None.

        It is the amount actually billed for THAT route, and a gateway may serve
        one model at different prices, so it beats the configured table. None
        means "fall back to configured rates", NEVER "free".
        """
        usage = resp.get("usage")
        if not isinstance(usage, Mapping):
            return None
        cost = usage.get("cost")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            return None
        return float(cost)

    @staticmethod
    def _message(resp: dict) -> dict:
        choices = resp.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return {}
        return choices[0].get("message") or {}

    def complete(self, *, role_name: str, model: str, prompt: str,
                 max_tokens: int, effort: str | None,
                 prompt_cache: bool = False) -> _review_provider_types.BackendResult:
        # `prompt_cache` is accepted for signature parity with the Anthropic
        # backend and ignored: chat-completions endpoints cache prompts on
        # their own (no cache_control on this wire); any cached share comes
        # back in `prompt_tokens_details.cached_tokens` and is metered.
        del prompt_cache
        if not model:
            raise MissingCredentialsError(
                "openai_compat model is not configured — set "
                "ELT_TASKGEN_OSS_MODEL (or the model in config/agents.yaml)"
            )
        schema_mode = _provider_protocol.uses_findings_schema(role_name)
        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if effort:
            # See `session_turn`: the route's effort is the wire's reasoning
            # effort on this backend too.
            payload["reasoning"] = {"effort": effort}
        system = _provider_protocol._system_prompt(
            role_name, schema_mode=schema_mode, agents_config=self._agents_config
        )
        if system is not None:
            payload["messages"].insert(
                0, {"role": "system", "content": system}
            )
        if schema_mode:
            # The chat-completions shape of the SAME wire tools the manifest
            # hashes: name, description and schema field for field.
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["input_schema"],
                    },
                }
                for tool in _provider_protocol.wire_tools_for(role_name, schema_mode=True)
            ]
            forced = _provider_protocol.tool_choice_for(role_name, schema_mode=True) or {}
            payload["tool_choice"] = {
                "type": "function",
                "function": {"name": forced["name"]},
            }

        attempts: list[dict] = []
        records: list[_review_provider_types.AttemptRecord] = []
        in_tokens = out_tokens = 0
        # None until an attempt reports one; every reporting attempt is added
        # (all attempts are billed, retries included).
        reported_usd: float | None = None
        problem = "no attempt made"
        for attempt_no in range(1 + _provider_protocol.SCHEMA_RETRIES):
            if attempt_no:
                # Same rule as the anthropic backend, chat-completions shape:
                # an identical retry cannot fix a semantic rejection.
                payload = _with_correction_chat(payload, attempts[-1], problem)
            started = time.monotonic()
            try:
                resp = self._transport(self._url, self._headers(), payload)
            except Exception as exc:
                _review_provider_types._attach_attempts(exc, records)
                raise
            attempts.append(resp)
            i, o = self._usage(resp)
            in_tokens += i
            out_tokens += o
            attempt_usd = self._reported_usd(resp)
            if attempt_usd is not None:
                reported_usd = (reported_usd or 0.0) + attempt_usd
            message = self._message(resp)
            first_choice = (resp.get("choices") or [{}])[0]
            finish = (
                first_choice.get("finish_reason")
                if isinstance(first_choice, dict) else None
            )
            records.append(
                _review_provider_types.AttemptRecord(
                    usage=_review_provider_types.Usage.from_chat(resp),
                    elapsed_ms=_review_provider_types._elapsed_ms(started),
                    served_model=str(resp.get("model") or ""),
                    stop_reason=str(finish) if finish else None,
                    reported_usd=attempt_usd,
                )
            )
            if schema_mode:
                calls = message.get("tool_calls") or []
                data: Any = None
                if calls and isinstance(calls[0], dict):
                    arguments = (calls[0].get("function") or {}).get("arguments")
                    if isinstance(arguments, str):
                        try:
                            data = json.loads(arguments)
                        except (json.JSONDecodeError, ValueError):
                            data = None
                    elif isinstance(arguments, dict):
                        data = arguments
                problem = _provider_protocol.validate_payload_for(role_name, data)
                if problem is None:
                    return _review_provider_types.BackendResult(
                        text=_provider_protocol.normalized_text_for(role_name, data),
                        model=model,
                        input_tokens=in_tokens,
                        output_tokens=out_tokens,
                        raw_attempts=tuple(attempts),
                        reported_usd=reported_usd,
                        attempts=tuple(records),
                    )
            else:
                text = message.get("content") or ""
                if isinstance(text, str) and text.strip():
                    return _review_provider_types.BackendResult(
                        text=text, model=model,
                        input_tokens=in_tokens, output_tokens=out_tokens,
                        raw_attempts=tuple(attempts),
                        reported_usd=reported_usd,
                        attempts=tuple(records),
                    )
                problem = "empty prose response"
        exhausted = ProviderProtocolError(
            f"openai_compat backend: role {role_name!r} produced no valid "
            f"response after {1 + _provider_protocol.SCHEMA_RETRIES} attempts: {problem}"
        )
        _review_provider_types._attach_attempts(exhausted, records)
        raise exhausted

    # -- sessions (Phase 1.R) ---------------------------------------------------

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
    ) -> _review_provider_types.BackendTurn:
        """Run one chat-completions session turn and return canonical `BackendTurn`
        content.

        Translate tool calls into Messages-API blocks, normalize usage and truncation,
        and retain all calls so the runner can enforce its single-call protocol.
        """
        del prompt_cache
        if not model:
            raise MissingCredentialsError(
                "openai_compat model is not configured — set "
                "ELT_TASKGEN_OSS_MODEL (or the model in config/agents.yaml)"
            )
        system = _provider_protocol._system_prompt(
            role_name,
            schema_mode=_provider_protocol.uses_findings_schema(role_name),
            agents_config=self._agents_config,
        )
        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _chat_messages(messages, system),
        }
        if effort:
            # The route's declared effort is the wire's reasoning effort
            # (OpenRouter's unified `reasoning` object; a model without
            # reasoning ignores it). Without it the witness answered with
            # no reasoning at all and broke stated rules it had in front
            # of it (dlt__workable, dbt__twitter_ads, batch10 run L).
            payload["reasoning"] = {"effort": effort}
        if tools:
            payload["tools"] = _chat_tools(tools)
            choice = _chat_tool_choice(tool_choice)
            if choice is not None:
                payload["tool_choice"] = choice
        started = time.monotonic()
        try:
            resp = self._transport(self._url, self._headers(), payload)
        except Exception as exc:
            _review_provider_types._attach_attempts(exc, ())
            raise
        elapsed = _review_provider_types._elapsed_ms(started)
        message = self._message(resp)
        first_choice = (resp.get("choices") or [{}])[0]
        finish = first_choice.get("finish_reason") if isinstance(first_choice, dict) else None
        attempt_usd = self._reported_usd(resp)
        record = _review_provider_types.AttemptRecord(
            usage=_review_provider_types.Usage.from_chat(resp),
            elapsed_ms=elapsed,
            served_model=str(resp.get("model") or ""),
            stop_reason=str(finish) if finish else None,
            reported_usd=attempt_usd,
        )
        return _review_provider_types.BackendTurn(
            content=tuple(_chat_content_blocks(message)),
            stop_reason=record.stop_reason,
            model=model,
            raw=_review_provider_types._plain_data(resp),
            attempts=(record,),
            reported_usd=attempt_usd,
        )


class _HeadlessBackendPlaceholder:
    """Stands in `PROVIDER_BACKENDS` for a headless kind until
    `review.headless` is imported (it imports this module)."""

PROVIDER_BACKENDS: dict[str, type] = {
    "anthropic": AnthropicBackend,
    "openai_compat": OpenAICompatBackend,
    "claude_headless": _HeadlessBackendPlaceholder,
    "codex_headless": _HeadlessBackendPlaceholder,
}

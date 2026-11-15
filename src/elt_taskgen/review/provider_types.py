"""Provider usage, attempt records, turns, and tool-result wire values."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from elt_taskgen.review.session import ToolProtocolFault


@dataclass(frozen=True)
class Usage:
    """Represent token usage for one attempt or an aggregate.

    Track uncached input, cache creation, cache reads, and output separately. Totals are
    nonnegative and addition preserves each category.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=(
                self.cache_read_input_tokens + other.cache_read_input_tokens
            ),
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
        )

    def as_dict(self) -> dict[str, int]:
        """The transcript `usage` block (the four wire-named counters)."""
        return {
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "cache_read_input_tokens": int(self.cache_read_input_tokens),
            "cache_creation_input_tokens": int(self.cache_creation_input_tokens),
        }

    @classmethod
    def from_dict(cls, usage: Mapping[str, Any] | None) -> "Usage":
        """A transcript `usage` block back into a Usage (legacy entries carry
        only the two token counters; absent cache counters read as 0)."""
        usage = usage or {}
        return cls(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_creation_input_tokens=int(
                usage.get("cache_creation_input_tokens") or 0
            ),
        )

    @classmethod
    def from_anthropic(cls, resp: Mapping[str, Any]) -> "Usage":
        """Messages API `usage` (cache counters present only when caching)."""
        return cls.from_dict(resp.get("usage") or {})

    @classmethod
    def from_chat(cls, resp: Mapping[str, Any]) -> "Usage":
        """Chat-completions `usage`: `prompt_tokens` INCLUDES the cached part
        (`prompt_tokens_details.cached_tokens`), which is split out here."""
        usage = resp.get("usage") or {}
        if not isinstance(usage, Mapping):
            usage = {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        details = usage.get("prompt_tokens_details")
        cached = 0
        if isinstance(details, Mapping):
            cached = int(details.get("cached_tokens") or 0)
        cached = max(0, min(cached, prompt_tokens))
        return cls(
            input_tokens=prompt_tokens - cached,
            output_tokens=int(usage.get("completion_tokens") or 0),
            cache_read_input_tokens=cached,
            cache_creation_input_tokens=0,
        )


@dataclass(frozen=True)
class AttemptRecord:
    """One API attempt as the meter sees it: what it cost, how long it took,
    what actually served it. Every attempt is paid for, so every attempt is
    recorded — including the attempts of an exchange that ends in a raise."""

    usage: Usage
    #: Harness-measured wall of the transport call (request to parsed body),
    #: on the `DbtCommandEvidence.elapsed_ms` pattern: measured by the caller,
    #: never taken from the provider.
    elapsed_ms: int
    #: The model id the PROVIDER reports in the response body ('' when the
    #: wire carried none). What was asked for is `BackendResult.model`.
    served_model: str
    stop_reason: str | None = None
    #: The provider's own billed figure for THIS attempt (gateways report
    #: `usage.cost`), None when the wire carried none.
    reported_usd: float | None = None


#: Attribute under which a raising backend leaves the attempts it already paid
#: for, so `RoutedProvider` can meter them (see `backend_attempts`).
_BACKEND_ATTEMPTS_ATTR = "backend_attempts"


def _attach_attempts(exc: BaseException, attempts: Sequence[AttemptRecord]) -> None:
    """Pin the paid-for attempts onto an exception about to propagate."""
    try:
        setattr(exc, _BACKEND_ATTEMPTS_ATTR, tuple(attempts))
    except (AttributeError, TypeError):  # pragma: no cover - exotic exception
        pass


def backend_attempts(exc: BaseException) -> tuple[AttemptRecord, ...]:
    """The API attempts a raising backend made before it raised (may be empty).

    A backend that exhausts its schema retries, or whose transport fails on
    the second attempt, has still PAID for the earlier attempts; they reach the
    meter through this hook rather than being lost with the exception.
    """
    found = getattr(exc, _BACKEND_ATTEMPTS_ATTR, ())
    if isinstance(found, (list, tuple)) and all(
        isinstance(a, AttemptRecord) for a in found
    ):
        return tuple(found)
    return ()


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since a `time.monotonic()` mark (never negative)."""
    return max(0, int(round((time.monotonic() - started) * 1000.0)))


@dataclass(frozen=True)
class BackendResult:
    """One completed provider exchange (possibly after schema retries)."""

    text: str                      # normalized text served to the council
    model: str
    input_tokens: int              # summed across all attempts (all are paid)
    output_tokens: int
    raw_attempts: tuple[dict, ...]  # raw API response bodies, in order
    #: USD the PROVIDER reported for this exchange, or None when the wire
    #: carried no cost. Gateways price per upstream route, so a reported figure
    #: beats any local table. See CostMeter.charge.
    reported_usd: float | None = None
    #: Per-attempt usage, wall and served model, in order (one per raw
    #: attempt). The meter charges PER ATTEMPT from this tuple.
    attempts: tuple[AttemptRecord, ...] = ()

    @property
    def usage(self) -> Usage:
        """Four-counter usage summed across every attempt."""
        total = Usage()
        for attempt in self.attempts:
            total = total + attempt.usage
        if not self.attempts:
            total = Usage(
                input_tokens=self.input_tokens, output_tokens=self.output_tokens
            )
        return total

    @property
    def elapsed_ms(self) -> int:
        """Harness-measured transport wall summed across every attempt."""
        return sum(a.elapsed_ms for a in self.attempts)

    @property
    def served_model(self) -> str:
        """The provider-reported model id of the attempt that produced the
        served text (the last one); '' when no attempt reported one."""
        for attempt in reversed(self.attempts):
            if attempt.served_model:
                return attempt.served_model
        return ""


#: Wire stop reasons that mean the output was cut off by the token budget:
#: the Messages API's `max_tokens` and the chat-completions `length`. In a
#: session the first one is `OUTPUT_TRUNCATED` (SoT T4); the one-shot
#: `complete()` keeps its three attempts.
_TRUNCATED_STOP_REASONS: frozenset[str] = frozenset({"max_tokens", "length"})


def _plain_data(value: Any) -> Any:
    """A JSON-plain copy (mappings to dicts, sequences to lists)."""
    if isinstance(value, Mapping):
        return {str(k): _plain_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_data(v) for v in value]
    return value


def _tool_use_blocks(content: Any) -> list[dict]:
    """Every `tool_use` block of one assistant content list, in order."""
    if not isinstance(content, (list, tuple)):
        return []
    return [
        block
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "tool_use"
    ]


def _tool_use_ids(content: Any) -> list[str]:
    return [str(block.get("id")) for block in _tool_use_blocks(content) if block.get("id")]


@dataclass(frozen=True)
class BackendTurn:
    """Represent one canonical model turn for a bounded session.

    `content` preserves every text and tool-use block in Messages-API form across
    providers. Multiple tool calls remain present so the runner can reject them. The
    record also carries stop/truncation state, usage, memo identity, route, replay,
    pricing, and admission metadata.
    """

    content: tuple[Mapping[str, Any], ...]
    stop_reason: str | None
    model: str
    raw: Mapping[str, Any] = field(default_factory=dict)
    attempts: tuple[AttemptRecord, ...] = ()
    reported_usd: float | None = None
    key: str = ""
    turn_index: int = 0
    replayed: bool = False
    text: str = ""
    usd: float = 0.0
    route: Mapping[str, Any] = field(default_factory=dict)
    admission: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", tuple(_plain_data(b) for b in self.content))
        object.__setattr__(self, "route", _plain_data(self.route or {}))
        object.__setattr__(self, "admission", {str(k): str(v) for k, v in dict(self.admission or {}).items()})

    @property
    def memo_key(self) -> str:
        """The transcript key this turn is recorded under (`key`)."""
        return self.key

    @property
    def usage(self) -> Usage:
        total = Usage()
        for attempt in self.attempts:
            total = total + attempt.usage
        return total

    @property
    def elapsed_ms(self) -> int:
        return sum(a.elapsed_ms for a in self.attempts)

    @property
    def served_model(self) -> str:
        for attempt in reversed(self.attempts):
            if attempt.served_model:
                return attempt.served_model
        return ""

    @property
    def truncated(self) -> bool:
        """True when the wire cut the output off (`OUTPUT_TRUNCATED` in a
        session, halting on the first occurrence)."""
        return self.stop_reason in _TRUNCATED_STOP_REASONS

    @property
    def tool_uses(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(_tool_use_blocks(self.content))

    @property
    def tool_use_ids(self) -> tuple[str, ...]:
        return tuple(_tool_use_ids(self.content))

    @property
    def text_content(self) -> str:
        return "".join(
            str(block.get("text", ""))
            for block in self.content
            if isinstance(block, Mapping) and block.get("type") == "text"
        )

    def protocol_fault(self) -> ToolProtocolFault | None:
        """The dispatcher's protocol verdict on this turn: `no_tool_call`
        (nothing to dispatch), `multiple_tool_use` (parallel calls are refused,
        never executed), `invalid_arguments` (the input is not an object);
        None when exactly one well-formed `tool_use` arrived. Unknown names
        and schema-invalid arguments against a tool's schema are the
        registry's verdict, not this one."""
        uses = self.tool_uses
        if not uses:
            return ToolProtocolFault("no_tool_call")
        if len(uses) > 1:
            return ToolProtocolFault(
                "multiple_tool_use",
                tool=str(uses[0].get("name") or ""),
                detail=f"{len(uses)} tool_use blocks in one turn",
            )
        if not isinstance(uses[0].get("input"), Mapping):
            return ToolProtocolFault("invalid_arguments", tool=str(uses[0].get("name") or ""))
        return None

    def tool_call(self) -> Mapping[str, Any]:
        """The ONE `tool_use` block of this turn, or raise the protocol
        fault (`ToolProtocolFault`) the caller answers with a correction."""
        fault = self.protocol_fault()
        if fault is not None:
            raise fault
        return self.tool_uses[0]


@dataclass(frozen=True)
class ToolResultBlock:
    """One answer to one `tool_use` id: the sanitized result bytes
    (`serialize_for_transport` output, or a `Diagnostic.render()` text) or a
    correction (`is_error`). `content` is TEXT: the controller sends only
    what the projection layer already vetted."""

    tool_use_id: str
    content: str
    is_error: bool = False


def _with_tool_results(
    messages: Sequence[Mapping[str, Any]],
    assistant_content: Any,
    results: Sequence[ToolResultBlock | tuple],
    *,
    text: str | None = None,
) -> list[dict]:
    """Return a new message list containing an assistant turn and its tool results.

    Every tool-use id receives a leading `tool_result` block. Optional explanatory text
    follows; input messages are not mutated.
    """
    out = [dict(m) for m in messages]
    if assistant_content:
        out.append({"role": "assistant", "content": _plain_data(assistant_content)})
    blocks: list[dict] = []
    for result in results:
        if isinstance(result, ToolResultBlock):
            tool_use_id, content, is_error = result.tool_use_id, result.content, result.is_error
        else:
            tool_use_id, content = result[0], result[1]
            is_error = bool(result[2]) if len(result) > 2 else False
        block: dict = {"type": "tool_result", "tool_use_id": str(tool_use_id)}
        if is_error:
            block["is_error"] = True
        block["content"] = str(content)
        blocks.append(block)
    if blocks:
        if text is not None:
            blocks.append({"type": "text", "text": text})
        out.append({"role": "user", "content": blocks})
    elif text is not None:
        out.append({"role": "user", "content": text})
    return out


def protocol_correction_results(
    turn: BackendTurn, fault: ToolProtocolFault, *, text: str | None = None
) -> list[ToolResultBlock]:
    """The `is_error` answers to EVERY `tool_use` id of a turn the dispatcher
    refused (`ToolProtocolFault`): one fixed code, the same text on each id,
    nothing executed. Empty for `no_tool_call` (there is no id to answer; the
    caller sends the text alone). The default text is the policy's refusal
    text with the fault's code."""
    if text is None:
        from elt_taskgen.review.session import REFUSAL_TEXT

        text = REFUSAL_TEXT.format(code=fault.code)
    return [ToolResultBlock(tool_use_id, text, True) for tool_use_id in turn.tool_use_ids]

"""Define bounded-session policies, faults, records, and the runner.

`SessionFault` marks harness infrastructure failures; `PolicyFault` marks model-caused
violations. `SessionPolicy` hashes the allowed tools, limits, schemas, and fixed
responses. The runner permits calls before dispatch, sanitizes outputs, records a
trajectory chain, and either returns a typed terminal result or raises a typed halt with
the partial result attached.
"""

from __future__ import annotations

import json
import re
import signal
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from elt_taskgen.models import canonical_json, sha256_hex
from elt_taskgen.review import trajectory as _trajectory
from elt_taskgen.review.council import ProviderProtocolError
from elt_taskgen.training.contract import MAX_WORKSPACE_ACTIONS, WorkspaceFailureClass
from elt_taskgen.training.models import _CODE_RE, WorkspaceActionTraceEntry

#: Admission-visible version of the live model-call deadline semantics.
#: Increment whenever the session/one-shot wall enforcement boundary changes.
MODEL_CALL_DEADLINE_VERSION = "interruptible-model-call-v1"

__all__ = [
    "ABORT_REASON_CODES",
    "ABORT_TOOL_SCHEMA",
    "CANONICAL_INDEX_RE",
    "CERTIFY_TOOL_NAME",
    "COUNCIL_ROLES_WITHOUT_WORKING_LIMIT",
    "FORMAT_ERROR_DISPOSITIONS",
    "HARD_CAP_KEYS",
    "INFRASTRUCTURE_FAULT_NAMES",
    "MEASURED_MATCH_BIT_VALIDATOR",
    "MODEL_CALL_DEADLINE_VERSION",
    "NON_CANONICAL_INDEX_CODE",
    "NUDGE_TEXT",
    "POLICY_FAULT_NAMES",
    "PROTOCOL_FAULT_CODES",
    "PROTOCOL_FAULT_LIMIT",
    "REFUSAL_TEXT",
    "PRIVATE_PROBE_CODE",
    "SCHEMA_OUTRANKING_ARGUMENT_RULES",
    "SCHEMA_RETRIES",
    "SESSION_FAULT_BOUNDARIES",
    "STUCK_DETECTOR_THRESHOLDS",
    "TOOL_OUTPUT_CAP_BYTES",
    "TRANSIENT_HARNESS_CODES",
    "Boundary",
    "DiagnosticTripwire",
    "FaultRecord",
    "ForbiddenArgument",
    "InProcessValidatorWorker",
    "InterruptibleValidatorWorker",
    "LeakTripwire",
    "OracleCapExceeded",
    "OutputTruncated",
    "PolicyFault",
    "ProviderFault",
    "SandboxFault",
    "SessionFault",
    "SessionLimits",
    "SessionPolicy",
    "SessionPolicyViolation",
    "SessionProtocolError",
    "SessionResult",
    "TaskDefectFault",
    "TerminalState",
    "ToolDeadlineExceeded",
    "ToolHarnessFault",
    "ToolNotPermitted",
    "ToolOutcome",
    "ToolProtocolFault",
    "TurnRecord",
    "ValidatorHook",
    "ValidatorWorker",
    "WorkerResult",
    "WriteOutsideSurface",
    "abort_tool_wire",
    "canonical_list_index",
    "cap_tool_output",
    "locator_argument_problem",
    "plain_messages",
    "run_bounded_session",
    "terminal_for_exception",
    "validate_args",
]

Boundary = Literal["provider", "tool", "sandbox", "sanitizer", "task"]

#: The boundaries a `SessionFault` may name (SoT T6).
SESSION_FAULT_BOUNDARIES: frozenset[str] = frozenset(
    {"provider", "tool", "sandbox", "sanitizer", "task"}
)

#: The seven canonical names `engine._INFRA_EXCEPTION_NAMES` carries for this
#: taxonomy (SoT T6). Subclasses classify through the MRO; `LeakTripwire` is
#: the same class as `DiagnosticTripwire` and adds nothing.
INFRASTRUCTURE_FAULT_NAMES: frozenset[str] = frozenset(
    {
        "SessionFault",
        "ProviderFault",
        "ToolHarnessFault",
        "ToolDeadlineExceeded",
        "SandboxFault",
        "DiagnosticTripwire",
        "TaskDefectFault",
    }
)

#: Model-caused classes. NONE of these names may ever enter the engine's
#: infrastructure set: a policy fault is label-eligible, a harness fault is not.
POLICY_FAULT_NAMES: frozenset[str] = frozenset(
    {
        "PolicyFault",
        "ToolProtocolFault",
        "ToolNotPermitted",
        "OracleCapExceeded",
        "WriteOutsideSurface",
        "ForbiddenArgument",
    }
)

#: Consecutive protocol faults that exhaust a session (SoT T5 R5: one more
#: than `providers.SCHEMA_RETRIES`).
PROTOCOL_FAULT_LIMIT = 3

#: Schema and compile corrections share this total budget; compile corrections
#: also have their own cap. Once either is spent, accept red output for screening.
SCHEMA_RETRIES = PROTOCOL_FAULT_LIMIT - 1

#: The optional population match-bit validator is configured by its session
#: flag, not `harness_validators`, and runs at most once per session.
MEASURED_MATCH_BIT_VALIDATOR = "measured_match_bit"

#: `TurnRecord.outcome_code` of a terminal turn whose payload was accepted
#: as submitted while a harness validator or hook was still red (the
#: compile-correction budget or `SCHEMA_RETRIES` was spent); the names ride
#: on `SessionResult.red_validators_at_submit` for the post-session screen.
VALIDATOR_RED_AT_SUBMIT_CODE = "validator_red_at_submit"

#: A task-aware correction hook returns None or a value-free Diagnostic, which
#: is serialized and gatekept like tool output rather than joined to free text.
ValidatorHook = Callable[[Any, Mapping[str, Any]], Any]

#: The fixed codes a `ToolProtocolFault` may carry back to the model, one per
#: dispatcher refusal (SoT T6): no tool call, a name no role registers,
#: schema-invalid arguments, more than one `tool_use` block.
PROTOCOL_FAULT_CODES: frozenset[str] = frozenset(
    {"no_tool_call", "unknown_tool", "invalid_arguments", "multiple_tool_use"}
)


# ---------------------------------------------------------------------------
# Harness-side faults (infrastructure: exit 2, reward None, no round)
# ---------------------------------------------------------------------------

class SessionFault(RuntimeError):
    """Base of the harness-side family. `boundary` is pinned per subclass and
    is the constructor argument of the base itself (`SessionFault(boundary)`).

    Never model-visible, never a training label: `failure_class` is one of the
    classes whose `label_eligible` is False, and `terminal` is the SoT T4 name
    the runner reports."""

    boundary: str = ""
    failure_class: ClassVar[WorkspaceFailureClass] = WorkspaceFailureClass.HARNESS_DEFECT
    terminal: ClassVar[str] = "HARNESS_FAULT"
    #: A stable code naming WHAT tripped (never a value); '' when the class
    #: alone says it.
    code: str = ""

    def __init__(
        self,
        message: str = "",
        *,
        boundary: str | None = None,
        code: str | None = None,
    ) -> None:
        if boundary is not None:
            if type(self) is not SessionFault and boundary != self.boundary:
                raise ValueError(
                    f"{type(self).__name__} is pinned to the {self.boundary!r} "
                    f"boundary; it cannot be raised at {boundary!r}"
                )
            self.boundary = str(boundary)
        if self.boundary not in SESSION_FAULT_BOUNDARIES:
            raise ValueError(
                f"{type(self).__name__} needs a boundary in "
                f"{sorted(SESSION_FAULT_BOUNDARIES)}, not {self.boundary!r}"
            )
        if code is not None:
            self.code = str(code)
        if not message:
            message = (
                f"{type(self).__name__} at the {self.boundary} boundary"
                + (f" ({self.code})" if self.code else "")
                + "; a harness fault, not a task defect"
            )
        super().__init__(message)

    @property
    def label_eligible(self) -> bool:
        """Always False: a harness fault is reward `None`, never `0.0`."""
        return self.failure_class.label_eligible


class ProviderFault(SessionFault):
    """D0/D1: the transport after its retries, credentials, the TASK budget
    (`BudgetExceededError`), a replay miss. Terminal `PROVIDER_FAULT`; the
    runner resumes once from the last recorded turn, then halts."""

    boundary = "provider"
    failure_class = WorkspaceFailureClass.TRANSIENT_INFRASTRUCTURE
    terminal = "PROVIDER_FAULT"

    def __init__(self, message: str = "", *, code: str = "") -> None:
        super().__init__(message, code=code)


class ToolHarnessFault(SessionFault):
    """D3: the `Tool.run` wrapper caught something the tool did not report as
    a diagnostic (an unexpected exception, a trusted-failure class, malformed
    worker IPC, a non-projection result). THE RAW OUTPUT IS NEVER SENT: the
    fault names the tool, a code and the exception CLASS, never its text."""

    boundary = "tool"
    failure_class = WorkspaceFailureClass.HARNESS_DEFECT
    terminal = "HARNESS_FAULT"

    def __init__(
        self,
        tool: str,
        *,
        code: str = "harness_exception",
        cause_type: str = "",
    ) -> None:
        self.tool = str(tool)
        self.cause_type = str(cause_type)
        raised = f" raised {self.cause_type}" if self.cause_type else ""
        super().__init__(
            f"tool harness fault: tool {self.tool!r}{raised} ({code}); its "
            "output was withheld — a harness fault, not a task defect and not "
            "a policy failure",
            code=code,
        )

    @classmethod
    def from_exception(
        cls, tool: str, exc: BaseException, *, code: str = "harness_exception"
    ) -> "ToolHarnessFault":
        """Wrap an exception a tool raised. Only its CLASS NAME is kept; the
        message (DuckDB errors, paths, values) stays with the traceback."""
        return cls(tool, code=code, cause_type=type(exc).__name__)


class ToolDeadlineExceeded(SessionFault):
    """D3: the worker supervisor killed a tool at its HARNESS-SIDE deadline.

    Contrast the candidate's own `dbt_timeout` / `dev_query`
    `execution_timeout`, which are measured codes fed back to the model; this
    one is never a policy failure."""

    boundary = "tool"
    failure_class = WorkspaceFailureClass.TRANSIENT_INFRASTRUCTURE
    terminal = "HARNESS_FAULT"

    def __init__(self, tool: str, *, deadline_s: float) -> None:
        self.tool = str(tool)
        self.deadline_s = float(deadline_s)
        if self.deadline_s <= 0:
            raise ValueError("deadline_s must be > 0")
        super().__init__(
            f"tool {self.tool!r} exceeded its harness-side deadline of "
            f"{self.deadline_s:g} s and was killed; a harness fault, never a "
            "policy failure",
            code="deadline_exceeded",
        )


class SandboxFault(SessionFault):
    """D2/D3: provisioning failed, a worker or container died, an OOM kill.
    Retried once with a fresh worker replaying the recorded prefix."""

    boundary = "sandbox"
    failure_class = WorkspaceFailureClass.TRANSIENT_INFRASTRUCTURE
    terminal = "HARNESS_FAULT"

    def __init__(self, message: str = "", *, code: str = "") -> None:
        super().__init__(message, code=code)


class TaskDefectFault(SessionFault):
    """A package or gold inconsistency discovered mid-session: the task is
    quarantined, the session halts, nothing is rejected by the model's doing."""

    boundary = "task"
    failure_class = WorkspaceFailureClass.TASK_DEFECT
    terminal = "HARNESS_FAULT"

    def __init__(self, message: str = "", *, code: str = "") -> None:
        super().__init__(message, code=code)


# ---------------------------------------------------------------------------
# Model-caused faults (label-eligible; never infrastructure)
# ---------------------------------------------------------------------------

class PolicyFault(RuntimeError):
    """Base of the model-caused family. `code` is the ONE fixed code the model
    may see (inside `tool_result{is_error: true}`); `detail` is for operators
    and must not carry a value either."""

    failure_class: ClassVar[WorkspaceFailureClass] = WorkspaceFailureClass.POLICY_FAILURE
    terminal: ClassVar[str] = ""
    #: True when one occurrence ends the session; False for a correction turn.
    ends_session: ClassVar[bool] = True
    #: True when the halt is also a `security_event` (a violation, not a slip).
    security_event: ClassVar[bool] = False
    #: The code a subclass carries when it has exactly one.
    fixed_code: ClassVar[str] = ""

    def __init__(
        self, code: str | None = None, *, tool: str = "", detail: str = ""
    ) -> None:
        code = self.fixed_code if code is None else str(code)
        if self.fixed_code and code != self.fixed_code:
            raise ValueError(
                f"{type(self).__name__} carries the fixed code "
                f"{self.fixed_code!r}, not {code!r}"
            )
        if _CODE_RE.fullmatch(code) is None:
            raise ValueError("a policy fault code must be a lowercase stable code")
        self.code = code
        self.tool = str(tool)
        self.detail = str(detail)
        where = f" by tool {self.tool!r}" if self.tool else ""
        tail = f" — {self.detail}" if self.detail else ""
        super().__init__(f"{type(self).__name__}{where}: {self.code}{tail}")

    @property
    def label_eligible(self) -> bool:
        return self.failure_class.label_eligible


class ToolProtocolFault(PolicyFault):
    """Dispatcher: no tool call, an unknown tool name, schema-invalid
    arguments, more than one `tool_use`. A CORRECTION turn, not a halt; the
    third consecutive one is `PROTOCOL_EXHAUSTED` via `SessionProtocolError`."""

    failure_class = WorkspaceFailureClass.POLICY_FAILURE
    terminal = "PROTOCOL_EXHAUSTED"
    ends_session = False

    def __init__(self, code: str, *, tool: str = "", detail: str = "") -> None:
        if code not in PROTOCOL_FAULT_CODES:
            raise ValueError(
                f"protocol fault code {code!r} is not one of "
                f"{sorted(PROTOCOL_FAULT_CODES)}"
            )
        super().__init__(code, tool=tool, detail=detail)


class ToolNotPermitted(PolicyFault):
    """Dispatcher: a REGISTERED tool of another role. One fixed code, then the
    session ends as `POLICY_VIOLATION` with a security event."""

    failure_class = WorkspaceFailureClass.POLICY_VIOLATION
    terminal = "POLICY_VIOLATION"
    security_event = True
    fixed_code = "tool_not_permitted"

    def __init__(self, tool: str, *, detail: str = "") -> None:
        super().__init__(tool=tool, detail=detail)


class OracleCapExceeded(PolicyFault):
    """PERMIT bit accounting: `max_oracle_bits` exhausted. A LIMIT (terminal
    `LIMIT_ORACLE`, measured `0.0`), not a violation: no security event and
    never wrapped into `SessionPolicyViolation`."""

    failure_class = WorkspaceFailureClass.POLICY_FAILURE
    terminal = "LIMIT_ORACLE"
    fixed_code = "oracle_cap_exceeded"

    def __init__(self, *, tool: str = "", detail: str = "") -> None:
        super().__init__(tool=tool, detail=detail)


class WriteOutsideSurface(PolicyFault):
    """`validate_scope` escape check, `_check_candidate_path`, a private
    surface path name. `POLICY_VIOLATION` with a security event."""

    failure_class = WorkspaceFailureClass.POLICY_VIOLATION
    terminal = "POLICY_VIOLATION"
    security_event = True
    fixed_code = "write_outside_surface"

    def __init__(self, *, tool: str = "", detail: str = "") -> None:
        super().__init__(tool=tool, detail=detail)


class ForbiddenArgument(PolicyFault):
    """SQL policy (`ATTACH`/`SET`/`read_*`/`information_schema`), a forbidden
    HCL construct, a path naming a denied tree, cap evasion.
    `POLICY_VIOLATION` with a security event."""

    failure_class = WorkspaceFailureClass.POLICY_VIOLATION
    terminal = "POLICY_VIOLATION"
    security_event = True
    fixed_code = "forbidden_argument"

    def __init__(self, *, tool: str = "", detail: str = "") -> None:
        super().__init__(tool=tool, detail=detail)


# ---------------------------------------------------------------------------
# Council wrappers: the halt reaches the engine through ProviderProtocolError
# ---------------------------------------------------------------------------

class SessionProtocolError(ProviderProtocolError):
    """`PROTOCOL_EXHAUSTED`: the third consecutive protocol fault. Halt, exit
    2, no round, through the `ProviderProtocolError` name (roadmap R-C)."""

    terminal: ClassVar[str] = "PROTOCOL_EXHAUSTED"

    def __init__(
        self,
        role: str,
        *,
        faults: int = PROTOCOL_FAULT_LIMIT,
        last_code: str = "",
    ) -> None:
        self.role = str(role)
        self.faults = int(faults)
        self.last_code = str(last_code)
        if self.faults < PROTOCOL_FAULT_LIMIT:
            raise ValueError(
                f"a session is exhausted at {PROTOCOL_FAULT_LIMIT} consecutive "
                f"protocol faults, not {self.faults}"
            )
        last = f" (last: {self.last_code})" if self.last_code else ""
        super().__init__(
            f"session protocol exhausted for role {self.role!r}: {self.faults} "
            f"consecutive protocol faults{last}; the stage halts as a harness "
            "fault (exit 2) and spends no repair round"
        )


class SessionPolicyViolation(ProviderProtocolError):
    """`POLICY_VIOLATION`: a terminal `PolicyFault` other than
    `OracleCapExceeded` (a limit) or `ToolProtocolFault` (a correction). Halt
    plus `security_event`, no round, the task is NOT rejected."""

    terminal: ClassVar[str] = "POLICY_VIOLATION"
    security_event: ClassVar[bool] = True

    def __init__(self, fault: PolicyFault, *, role: str = "") -> None:
        if not isinstance(fault, PolicyFault):
            raise TypeError("SessionPolicyViolation wraps a PolicyFault")
        if isinstance(fault, OracleCapExceeded):
            raise TypeError(
                "an oracle-cap stop is LIMIT_ORACLE (a limit), not a policy violation"
            )
        if not fault.ends_session:
            raise TypeError(
                f"{type(fault).__name__} is a correction turn; three in a row are "
                "SessionProtocolError, never a policy violation"
            )
        self.fault = fault
        self.role = str(role)
        self.code = fault.code
        for_role = f" for role {self.role!r}" if self.role else ""
        super().__init__(
            f"session policy violation{for_role}: {type(fault).__name__} "
            f"({fault.code}); the session halts with a security event, no repair "
            "round is spent and the task is not rejected"
        )

# Declared session policy objects and their fixed repeated-call response.
# The response is part of `policy_sha256`, so changes re-key transcripts.
NUDGE_TEXT = (
    "That call was not executed: it repeats a call whose result you already "
    "have (code: repeated_call). Use the result you were given, change the "
    "arguments, or submit."
)

#: The fixed refusal a dispatcher returns for a call the policy denies at no
#: cost (a per-tool ceiling, an unpermitted route). Part of `policy_sha256`.
REFUSAL_TEXT = (
    "That call was refused by the session policy and was not executed "
    "(code: {code}). It spent nothing; continue within the tools and limits "
    "you were given."
)

#: Stuck-detector thresholds (SoT T5): identical (action, observation) pairs
#: and same-action error streaks nudge at 3 and halt at 4; the format streak
#: (R5) halts at `PROTOCOL_FAULT_LIMIT`. Declared here so they enter
#: `policy_sha256`; the detector itself is Phase 1 (`review/loopguard.py`).
STUCK_DETECTOR_THRESHOLDS: Mapping[str, int] = MappingProxyType(
    {
        "identical_pairs_nudge": 3,
        "identical_pairs_halt": 4,
        "error_streak_nudge": 3,
        "error_streak_halt": 4,
        "format_streak_halt": PROTOCOL_FAULT_LIMIT,
    }
)


def _plain(value: Any) -> Any:
    """A JSON-serialisable copy: mappings to dicts, sequences to lists,
    everything else as is (canonical_json refuses what it cannot encode)."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    return value


@dataclass(frozen=True)
class SessionLimits:
    """The role's declared loop limits: the `roles.<role>.session` block of
    config/agents.yaml, VERBATIM and canonicalised, so production and
    metrology read ONE object (SoT R0.2). Typed accessors read the SoT T1
    keys; anything else in the block travels through `as_manifest` untouched.

    Absent keys mean today's one-shot behaviour: `max_model_calls` is
    1 + `SCHEMA_RETRIES` (`PROTOCOL_FAULT_LIMIT`), no model-initiated tool,
    no wall and no USD cap beyond the task budget."""

    block: Mapping[str, Any] = field(default_factory=dict)
    #: Document-wide session defaults that roles may override. They are not
    #: identity-bearing or included in the role manifest hash.
    session_defaults: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.block, Mapping):
            raise TypeError("SessionLimits.block must be a mapping")
        if not isinstance(self.session_defaults, Mapping):
            raise TypeError("SessionLimits.session_defaults must be a mapping")
        plain = dict(_plain(self.block))
        _refuse_defaults_above_hard_caps(plain)
        _refuse_turns_above_revisions(plain)
        object.__setattr__(self, "block", MappingProxyType(plain))
        object.__setattr__(
            self, "session_defaults", MappingProxyType(dict(_plain(self.session_defaults)))
        )
        disposition = self.format_error_disposition
        if disposition not in FORMAT_ERROR_DISPOSITIONS:
            raise ValueError(
                f"session.format_error_disposition must be one of "
                f"{sorted(FORMAT_ERROR_DISPOSITIONS)}, not {disposition!r}"
            )

    @classmethod
    def from_block(
        cls,
        block: Mapping[str, Any] | None,
        *,
        session_defaults: Mapping[str, Any] | None = None,
    ) -> "SessionLimits":
        return cls(dict(block or {}), session_defaults=dict(session_defaults or {}))

    def with_session_defaults(self, defaults: Mapping[str, Any] | None) -> "SessionLimits":
        """The same declared block under the session-wide defaults
        `defaults` (the document's top-level `session:` mapping)."""
        return SessionLimits(dict(self.block), session_defaults=dict(defaults or {}))

    @classmethod
    def declare(cls, **keys: Any) -> "SessionLimits":
        """A block from keyword arguments (`SessionLimits.declare(max_turns=5,
        max_tool_calls=8)`); `hard_caps=` takes the nested mapping."""
        return cls({key: value for key, value in keys.items() if value is not None})

    # -- Phase 1 accessors (SoT T1 keys; absent = no bound of that kind) ------

    @property
    def max_turns(self) -> int:
        """Model calls per session, corrections included (`max_turns`; a
        one-shot block's `max_model_calls`; else 1 + SCHEMA_RETRIES)."""
        return int(self.block.get("max_turns", self.block.get("max_model_calls", PROTOCOL_FAULT_LIMIT)))

    @property
    def max_revisions(self) -> int:
        return int(self.block.get("max_revisions", 0))

    @property
    def max_certify(self) -> int:
        return int(self.block.get("max_certify", 0))

    @property
    def max_output_tokens(self) -> int | None:
        value = self.block.get("max_output_tokens", self.block.get("max_output_tokens_total"))
        return None if value is None else int(value)

    @property
    def max_total_tokens(self) -> int | None:
        value = self.block.get("max_total_tokens")
        return None if value is None else int(value)

    @property
    def session_wall_seconds(self) -> float | None:
        """The session wall, transport waits and validator time included
        (`wall_clock_s` of the loop roles; `max_wall_s` of the critic blocks)."""
        value = self.block.get("wall_clock_s", self.block.get("max_wall_s"))
        return None if value is None else float(value)

    @property
    def tool_wall_seconds_default(self) -> float:
        return float(self.block.get("tool_wall_seconds_default", 10.0))

    @property
    def turn_wall_seconds(self) -> float:
        return float(self.block.get("turn_wall_seconds", 600.0))

    @property
    def max_writes(self) -> int | None:
        value = self.block.get("max_writes")
        return None if value is None else int(value)

    @property
    def max_tool_wall_s(self) -> float | None:
        """POL only (`LIMIT_WORKING` over `elapsed_tool_ms`); the runner
        ignores it for every council role."""
        value = self.block.get("max_tool_wall_s")
        return None if value is None else float(value)

    @property
    def nested_ceiling_usd(self) -> float:
        """What INIT reserves beside `max_usd` (the RPR route's nested
        certification ceiling); 0 for every other role."""
        return float(self.block.get("nested_ceiling_usd", 0.0) or 0.0)

    @property
    def hard_caps(self) -> Mapping[str, Any]:
        caps = self.block.get("hard_caps")
        return MappingProxyType(dict(caps)) if isinstance(caps, Mapping) else MappingProxyType({})

    @property
    def stuck(self) -> Mapping[str, int]:
        """The stuck-detector thresholds in force (a `stuck:` override in the
        block, else `STUCK_DETECTOR_THRESHOLDS`)."""
        declared = self.block.get("stuck")
        if isinstance(declared, Mapping):
            merged = dict(STUCK_DETECTOR_THRESHOLDS)
            merged.update({str(k): int(v) for k, v in declared.items()})
            return MappingProxyType(merged)
        return STUCK_DETECTOR_THRESHOLDS

    @property
    def format_error_disposition(self) -> str:
        """`halt` (R-C, the default) or `stage_fail` (declared, default off):
        the role block's key when it declares one, else the session-wide
        `session.format_error_disposition` of the document (SoT T4
        PROTOCOL_EXHAUSTED row), else `halt`."""
        declared = self.block.get("format_error_disposition")
        if declared is None:
            declared = self.session_defaults.get("format_error_disposition")
        return str(declared or "halt")

    @property
    def enabled(self) -> bool:
        return bool(self.block.get("enabled", False))

    @property
    def mode(self) -> str:
        return str(self.block.get("mode") or "one_shot")

    @property
    def max_model_calls(self) -> int:
        return int(self.block.get("max_model_calls", PROTOCOL_FAULT_LIMIT))

    @property
    def max_tool_calls(self) -> int:
        return int(self.block.get("max_tool_calls", 0))

    @property
    def max_compile_corrections(self) -> int:
        return int(self.block.get("max_compile_corrections", 0))

    @property
    def max_oracle_bits(self) -> int:
        return int(self.block.get("max_oracle_bits", 0))

    @property
    def max_wall_s(self) -> float | None:
        value = self.block.get("max_wall_s")
        return None if value is None else float(value)

    @property
    def max_usd(self) -> float | None:
        value = self.block.get("max_usd")
        return None if value is None else float(value)

    @property
    def harness_validators(self) -> tuple[str, ...]:
        return tuple(str(v) for v in (self.block.get("harness_validators") or ()))

    @property
    def measured_match_bit(self) -> bool:
        """The population adversary's flag F (SoT T1.1; A24): the 1-bit
        `measured_match_bit` validator, default off."""
        return bool(self.block.get("measured_match_bit", False))

    @property
    def active_harness_validators(self) -> tuple[str, ...]:
        """The harness validators the runner RUNS on every submitted payload:
        the declared `harness_validators`, plus `measured_match_bit` when the
        block's flag is on (it is declared by the flag, never by the list).
        What the behaviour manifest names once the block is enabled
        (`providers._harness_validators_for`)."""
        names = [str(v) for v in self.harness_validators]
        if self.measured_match_bit and MEASURED_MATCH_BIT_VALIDATOR not in names:
            names.append(MEASURED_MATCH_BIT_VALIDATOR)
        return tuple(names)

    def as_manifest(self) -> dict:
        """The block as canonical JSON data, keys sorted (the manifest's
        `loop_limits`)."""
        return {key: _plain(self.block[key]) for key in sorted(self.block)}


#: `hard_caps` key -> the default key it bounds (SoT R0.1: a default never
#: exceeds its cap; an unknown cap key is refused so a typo cannot silently
#: bind nothing).
HARD_CAP_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "turns": "max_turns",
        "model_calls": "max_model_calls",
        "tool_calls": "max_tool_calls",
        "usd": "max_usd",
        "wall_clock_s": "wall_clock_s",
        "max_wall_s": "max_wall_s",
        "revisions": "max_revisions",
        "certify": "max_certify",
        "oracle_bits": "max_oracle_bits",
        "output_tokens": "max_output_tokens",
        "writes": "max_writes",
        "compile_corrections": "max_compile_corrections",
    }
)


def _refuse_defaults_above_hard_caps(block: Mapping[str, Any]) -> None:
    """The loader half of SoT R0.1: a `session:` block whose default exceeds
    its own `hard_caps` entry is refused at declaration time, before it can
    enter a manifest, a transcript key or a runner."""
    caps = block.get("hard_caps")
    if caps is None:
        return
    if not isinstance(caps, Mapping):
        raise ValueError("session.hard_caps must be a mapping of cap name -> value")
    for cap_name, cap_value in caps.items():
        default_key = HARD_CAP_KEYS.get(str(cap_name))
        if default_key is None:
            raise ValueError(
                f"session.hard_caps names unknown cap {cap_name!r}; known caps: "
                f"{sorted(HARD_CAP_KEYS)}"
            )
        try:
            cap = float(cap_value)
        except (TypeError, ValueError):
            raise ValueError(f"session.hard_caps.{cap_name} must be a number") from None
        default = block.get(default_key)
        if default is None:
            continue
        try:
            value = float(default)
        except (TypeError, ValueError):
            raise ValueError(f"session.{default_key} must be a number") from None
        if value > cap:
            raise ValueError(
                f"session.{default_key} = {default!r} exceeds its hard cap "
                f"hard_caps.{cap_name} = {cap_value!r}; a default never exceeds "
                "its cap (SoT R0.1) — raise the cap deliberately or lower the default"
            )


#: The two dispositions of the third consecutive protocol fault (roadmap
#: R-C): `halt` (PROTOCOL_EXHAUSTED via `SessionProtocolError`, exit 2, no
#: round) and the declared, default-off `stage_fail`.
FORMAT_ERROR_DISPOSITIONS: frozenset[str] = frozenset({"halt", "stage_fail"})


def _refuse_turns_above_revisions(block: Mapping[str, Any]) -> None:
    """Validate the relation between author revisions and model turns.

    `max_revisions=n` requires exactly one initial draft plus up to `n` correction
    turns. Refuse contradictory declarations.
    """
    if "max_revisions" not in block or "max_turns" not in block:
        return
    try:
        revisions = int(block["max_revisions"])
        turns = int(block["max_turns"])
    except (TypeError, ValueError):
        raise ValueError("session.max_revisions and session.max_turns must be integers") from None
    if revisions > 0 and turns > revisions + 1:
        raise ValueError(
            f"session.max_turns = {turns} exceeds max_revisions + 1 = {revisions + 1}; "
            "a revision block's turns are its draft plus its revisions (SoT T1 AUT) — "
            "raise max_revisions deliberately or lower max_turns"
        )


def _tool_manifest(tool: Any) -> dict:
    """Return all policy-relevant properties of a tool.

    The manifest binds arguments, description, cost limits, roles, write and validator
    behavior, wire visibility, terminal behavior, trust domain, and implementation
    versions. Any semantic change moves `policy_sha256`.
    """
    cost = getattr(tool, "cost", None)
    return {
        "name": str(getattr(tool, "name", "")),
        "description": str(getattr(tool, "description", "") or ""),
        "input_schema": _plain(getattr(tool, "input_schema", {}) or {}),
        "cost": {
            "oracle_bits": int(getattr(cost, "oracle_bits", 0) or 0),
            "wall_s": float(getattr(cost, "wall_s", 0.0) or 0.0),
            "per_session": getattr(cost, "per_session", None),
            "idempotent_read": bool(getattr(cost, "idempotent_read", False)),
            "no_cost_codes": sorted(str(c) for c in (getattr(cost, "no_cost_codes", ()) or ())),
        },
        "permitted_roles": sorted(str(r) for r in (getattr(tool, "permitted_roles", ()) or ())),
        "surface_write": bool(getattr(tool, "surface_write", False)),
        "validator": bool(getattr(tool, "validator", False)),
        "harness_only": bool(getattr(tool, "harness_only", False)),
        "terminal": bool(getattr(tool, "terminal", False)),
        "auto_validators": sorted(str(v) for v in (getattr(tool, "auto_validators", ()) or ())),
        "dev_population_only": bool(getattr(tool, "dev_population_only", False)),
        "trust_domain": str(getattr(tool, "trust_domain", "") or ""),
        "version": str(getattr(tool, "version", "") or ""),
        "sanitizer_version": str(getattr(tool, "sanitizer_version", "") or ""),
    }


#: The fields of `_tool_manifest` that describe what the RUNNER does with a
#: tool (never sent on the wire; folded into `policy_sha256`).
TOOL_MANIFEST_BEHAVIOUR_FIELDS: tuple[str, ...] = (
    "surface_write",
    "validator",
    "harness_only",
    "terminal",
    "auto_validators",
    "dev_population_only",
    "trust_domain",
    "version",
    "sanitizer_version",
)


@dataclass(frozen=True)
class SessionPolicy:
    """Declare one role's allowed bounded-session behavior.

    The frozen policy contains tools, terminals, limits, schemas, fixed
    refusal/correction text, and stuck thresholds. Its hashes bind the runner and wire
    behavior without runtime state.
    """

    role: str
    tools: tuple[Any, ...] = ()
    submit_tool: str = ""
    abort_tool: str = "abort"
    nudge_text: str = NUDGE_TEXT
    refusal_text: str = REFUSAL_TEXT
    limits: SessionLimits = field(default_factory=SessionLimits)
    mode: str = "one_shot"
    wire_tools: tuple[Mapping[str, Any], ...] = ()
    stuck_thresholds: Mapping[str, int] = STUCK_DETECTOR_THRESHOLDS
    session_salt: int = 0

    def __post_init__(self) -> None:
        if not self.role or not isinstance(self.role, str):
            raise ValueError("a session policy needs a role name")
        if not isinstance(self.limits, SessionLimits):
            raise TypeError("SessionPolicy.limits must be a SessionLimits")
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(
            self,
            "wire_tools",
            tuple(MappingProxyType(dict(_plain(t))) for t in self.wire_tools),
        )
        # The thresholds the runner's detector enforces are the role's
        # declared `stuck:` override (SoT T5 "Exemptions": versioned in
        # `policy_sha256`, enforced by the runner): a caller that left the
        # module default gets `limits.stuck`, which IS the module default
        # when the block declares nothing.
        thresholds = self.stuck_thresholds
        if thresholds is STUCK_DETECTOR_THRESHOLDS:
            thresholds = self.limits.stuck
        object.__setattr__(
            self,
            "stuck_thresholds",
            MappingProxyType({str(k): int(v) for k, v in dict(thresholds).items()}),
        )
        # `population` is a constant of the tool context (DEVELOPMENT), never
        # an argument: a tool that declares one is refused at declaration.
        for tool in self.tools:
            schema = getattr(tool, "input_schema", None) or {}
            properties = schema.get("properties") if isinstance(schema, Mapping) else None
            if isinstance(properties, Mapping) and FORBIDDEN_ARG_NAME in properties:
                raise ValueError(
                    f"tool {getattr(tool, 'name', '')!r} declares a "
                    f"{FORBIDDEN_ARG_NAME!r} argument; the population is a "
                    "constant of the tool context, never an argument"
                )

    @property
    def allowlist(self) -> tuple[str, ...]:
        """The model-initiated tool names, sorted (empty for one-shot roles)."""
        return tuple(sorted(str(getattr(t, "name", "")) for t in self.tools))

    # -- Phase 1 turn helpers (pure; not part of the digest) -----------------

    @property
    def tool_map(self) -> Mapping[str, Any]:
        """name -> tool object, for every tool the policy names (terminal
        tools included when they were passed as objects)."""
        return MappingProxyType({str(getattr(t, "name", "")): t for t in self.tools})

    @property
    def terminal_tool_names(self) -> tuple[str, ...]:
        """The submit and abort tool names (the empty submit of a prose role
        omitted): the only calls a last permitted turn may make."""
        return tuple(name for name in (self.submit_tool, self.abort_tool) if name)

    def is_last_permitted_turn(self, turn_index: int) -> bool:
        """Is model turn `turn_index` (0-based) the last `limits.max_turns`
        allows? On it the wire forces the choice to submit or abort and the
        runner refuses every other call as `LIMIT_TURNS`."""
        return int(turn_index) + 1 >= self.limits.max_turns

    def wire_tools_for_turn(
        self, turn_index: int, *, tool_calls_exhausted: bool = False
    ) -> tuple[Mapping[str, Any], ...]:
        """The `tools[]` the payload of model turn `turn_index` sends: every
        wire tool, or on the last permitted turn — and on any turn once
        `max_tool_calls` is spent (`tool_calls_exhausted`, the runner's
        accounting) — only the terminal tools (`tool_choice: any` over
        submit and abort IS the forcing: a model that spent exactly its
        tool calls still gets its submit-or-abort turn)."""
        if not (self.is_last_permitted_turn(turn_index) or tool_calls_exhausted):
            return self.wire_tools
        terminal = set(self.terminal_tool_names)
        forced = tuple(t for t in self.wire_tools if str(t.get("name", "")) in terminal)
        return forced or self.wire_tools

    def as_manifest(self) -> dict:
        """The canonical policy document `sha256()` hashes (no salt)."""
        return {
            "role": self.role,
            "mode": self.mode,
            "allowlist": [_tool_manifest(t) for t in sorted(self.tools, key=lambda t: str(getattr(t, "name", "")))],
            "submit_tool": self.submit_tool,
            "abort_tool": self.abort_tool,
            "nudge_text": self.nudge_text,
            "refusal_text": self.refusal_text,
            "stuck_thresholds": dict(self.stuck_thresholds),
            "limits": self.limits.as_manifest(),
        }

    def sha256(self) -> str:
        """`policy_sha256`: sha256 of the canonical policy document."""
        return sha256_hex(canonical_json(self.as_manifest()))

    def tools_sha256(self) -> str:
        """`tools_sha256`: sha256 of the canonical wire `tools[]` list."""
        return sha256_hex(canonical_json([_plain(t) for t in self.wire_tools]))


def plain_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict]:
    """The message prefix as canonical JSON data (for `transcript_key_v3`)."""
    return [_plain(m) for m in messages]


# ---------------------------------------------------------------------------
# Phase 1.R: terminal states
# ---------------------------------------------------------------------------

class TerminalState(str, Enum):
    """SoT T4. Serialized values are the lowercase names; `STUCK` serializes
    as `stuck_in_a_loop` so the trainer's `mask_out_reason` vocabulary reads
    unchanged. `LIMIT_WORKING` exists for the L2 policy only and is disabled
    for every council role (`COUNCIL_ROLES_WITHOUT_WORKING_LIMIT`)."""

    SUBMITTED = "submitted"
    ABSTAINED = "abstained"
    LIMIT_TURNS = "limit_turns"
    LIMIT_TOOL_CALLS = "limit_tool_calls"
    LIMIT_TOKENS = "limit_tokens"
    LIMIT_USD = "limit_usd"
    LIMIT_WALL = "limit_wall"
    LIMIT_WORKING = "limit_working"
    LIMIT_ORACLE = "limit_oracle"
    STUCK = "stuck_in_a_loop"
    PROTOCOL_EXHAUSTED = "protocol_exhausted"
    OUTPUT_TRUNCATED = "output_truncated"
    POLICY_VIOLATION = "policy_violation"
    LEAK_TRIPWIRE = "leak_tripwire"
    HARNESS_FAULT = "harness_fault"
    PROVIDER_FAULT = "provider_fault"

    @classmethod
    def from_name(cls, name: str) -> "TerminalState":
        """The member for a SoT T4 NAME (`"LIMIT_USD"`) or serialized value."""
        try:
            return cls[str(name)]
        except KeyError:
            return cls(str(name))

    @property
    def is_scored(self) -> bool:
        return self in (TerminalState.SUBMITTED, TerminalState.ABSTAINED)

    @property
    def is_limit_stop(self) -> bool:
        return self in _LIMIT_STOPS

    @property
    def is_halt(self) -> bool:
        return self in _HALTS

    @property
    def auto_submits(self) -> bool:
        """Return whether this council terminal may auto-submit the last validator-green
        draft.

        Submission and configured limit/stuck stops qualify; abstention, policy
        violations, protocol faults, and infrastructure halts do not.
        """
        return self in _AUTO_SUBMIT_STOPS

    @property
    def limit_kind(self) -> str:
        """The `session_limit:<kind>` suffix of a limit stop ('' otherwise)."""
        return _LIMIT_KINDS.get(self, "")


_LIMIT_STOPS: frozenset[TerminalState] = frozenset(
    {
        TerminalState.LIMIT_TURNS,
        TerminalState.LIMIT_TOOL_CALLS,
        TerminalState.LIMIT_TOKENS,
        TerminalState.LIMIT_USD,
        TerminalState.LIMIT_WALL,
        TerminalState.LIMIT_WORKING,
        TerminalState.LIMIT_ORACLE,
        TerminalState.STUCK,
    }
)
_HALTS: frozenset[TerminalState] = frozenset(
    {
        TerminalState.PROTOCOL_EXHAUSTED,
        TerminalState.OUTPUT_TRUNCATED,
        TerminalState.POLICY_VIOLATION,
        TerminalState.LEAK_TRIPWIRE,
        TerminalState.HARNESS_FAULT,
        TerminalState.PROVIDER_FAULT,
    }
)
_AUTO_SUBMIT_STOPS: frozenset[TerminalState] = frozenset(
    {
        TerminalState.LIMIT_TURNS,
        TerminalState.LIMIT_TOOL_CALLS,
        TerminalState.LIMIT_TOKENS,
        TerminalState.LIMIT_USD,
        TerminalState.LIMIT_WORKING,
        TerminalState.LIMIT_ORACLE,
        TerminalState.STUCK,
    }
)
_LIMIT_KINDS: Mapping[TerminalState, str] = MappingProxyType(
    {
        TerminalState.LIMIT_TURNS: "turns",
        TerminalState.LIMIT_TOOL_CALLS: "tool_calls",
        TerminalState.LIMIT_TOKENS: "tokens",
        TerminalState.LIMIT_USD: "usd",
        TerminalState.LIMIT_WALL: "wall",
        TerminalState.LIMIT_WORKING: "working",
        TerminalState.LIMIT_ORACLE: "oracle",
        TerminalState.STUCK: "stuck_in_a_loop",
    }
)

#: Council roles for which `LIMIT_WORKING` is disabled (SoT T4: enabled for
#: the L2 policy only, over tool time, until OQ-10 measures attribution).
COUNCIL_ROLES_WITHOUT_WORKING_LIMIT: frozenset[str] = frozenset(
    {
        "semantic_author",
        "ambiguity_critic",
        "population_adversary",
        "shortcut_attacker",
        "feasibility_reviewer",
        "independent_loader",
        "independent_implementer",
        "repair_proposer",
    }
)


class OutputTruncated(ProviderFault):
    """`OUTPUT_TRUNCATED`: the wire reported `stop_reason == "max_tokens"` /
    `finish_reason == "length"` on a session turn. A CONFIGURATION fault
    (raise the role's `max_tokens`), halted on the FIRST truncation and never
    counted toward the three-fault protocol limit; reward `None`. Classified
    by the engine through its `ProviderFault` base (exit 2, no round)."""

    terminal = "OUTPUT_TRUNCATED"

    def __init__(self, role: str, *, turn_index: int, stop_reason: str = "max_tokens") -> None:
        self.role = str(role)
        self.turn_index = int(turn_index)
        self.stop_reason = str(stop_reason)
        super().__init__(
            f"session output truncated for role {self.role!r} on turn "
            f"{self.turn_index} (stop_reason {self.stop_reason!r}); halted on the "
            "first truncation — raise this role's max_tokens in config/agents.yaml",
            code="output_truncated",
        )


def terminal_for_exception(exc: BaseException) -> TerminalState:
    """The SoT T4 terminal a raised exception maps to, by CLASS (the boundary
    that caught it), never by message: every Phase 0 fault class, the two
    council wrappers, the provider layer's budget and transcript errors."""
    from_class = getattr(exc, "terminal", "")
    if isinstance(exc, PolicyFault):
        if isinstance(exc, ToolProtocolFault):
            return TerminalState.PROTOCOL_EXHAUSTED
        if isinstance(exc, OracleCapExceeded):
            return TerminalState.LIMIT_ORACLE
        return TerminalState.POLICY_VIOLATION
    if isinstance(from_class, str) and from_class:
        try:
            return TerminalState.from_name(from_class)
        except ValueError:
            pass
    if isinstance(exc, ProviderProtocolError):
        return TerminalState.PROVIDER_FAULT
    name = type(exc).__name__
    scope = str(getattr(exc, "scope", "") or "")
    if name == "RoleCapExceeded" or (name == "BudgetExceededError" and scope == "role"):
        return TerminalState.LIMIT_USD
    return TerminalState.PROVIDER_FAULT


# ---------------------------------------------------------------------------
# Phase 1.R: argument rules, the abort tool, output caps
# ---------------------------------------------------------------------------

#: The one argument name no tool may take (SoT T3 / matrix §4): the population
#: is a `ToolContext` constant.
FORBIDDEN_ARG_NAME = "population"

#: `abort(reason_code)`: the closed reason vocabulary (SoT T3), no free text.
ABORT_REASON_CODES: tuple[str, ...] = (
    "infeasible",
    "spec_conflict",
    "out_of_scope",
    "insufficient_information",
    "cannot_repair",
)

ABORT_TOOL_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "properties": {"reason_code": {"type": "string", "enum": list(ABORT_REASON_CODES)}},
        "required": ["reason_code"],
        "additionalProperties": False,
    }
)


def abort_tool_wire(name: str = "abort") -> dict:
    """The wire `tools[]` object of the abort tool."""
    return {
        "name": str(name),
        "description": (
            "Stop without submitting. reason_code is one of "
            + ", ".join(ABORT_REASON_CODES)
            + "; no free text."
        ),
        "strict": True,
        "input_schema": _plain(ABORT_TOOL_SCHEMA),
    }


#: Caps on the sanitized bytes a tool result may deliver (04 §6): code-only
#: projections 8 KiB, DEVELOPMENT rows 16 KiB. The observation digest is
#: taken over the FULL canonical bytes; only the delivered text is cut.
TOOL_OUTPUT_CAP_BYTES: Mapping[str, int] = MappingProxyType(
    {"diagnostic": 8 * 1024, "diagnostic_text": 8 * 1024, "dev_rows": 16 * 1024}
)
_TRUNCATION_MARKER = "\n(truncated)"


def _truncation_tail(digest: str) -> str:
    """The marker a cut tool_result ends with. With `digest` (the sha256 of
    the FULL observation payload) the marker binds the whole observation
    into the model-bound bytes, so two observations that differ only past
    the cap render differently and the next turn's memo key (the message
    prefix) moves with them: a replay whose tool output diverged beyond the
    cap is a miss, never a silently served turn. A hex digest is
    value-free."""
    if digest:
        return f"\n(full observation sha256 {digest})" + _TRUNCATION_MARKER
    return _TRUNCATION_MARKER


def cap_tool_output(kind: str, text: str, *, digest: str = "") -> tuple[str, bool]:
    """Cut `text` at the cap for `kind` (UTF-8 bytes); returns (text,
    truncated). When `digest` is given a cut text ends with the full
    observation's digest before the `(truncated)` marker
    (`_truncation_tail`)."""
    cap = TOOL_OUTPUT_CAP_BYTES.get(str(kind), TOOL_OUTPUT_CAP_BYTES["diagnostic"])
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text, False
    tail = _truncation_tail(str(digest or ""))
    keep = raw[: max(0, cap - len(tail.encode("utf-8")))]
    return keep.decode("utf-8", errors="ignore") + tail, True


_SQL_ARG_NAMES: frozenset[str] = frozenset({"sql", "query", "statement"})
_FORBIDDEN_SQL_RE = re.compile(
    r"\b(?:ATTACH|DETACH|INSTALL|LOAD|PRAGMA|SET|RESET|COPY|EXPORT|IMPORT|CALL)\b"
    r"|\bread_[a-z_]*\s*\(|\binformation_schema\b|\bduckdb_[a-z_]+\s*\(",
    re.IGNORECASE,
)
_PATH_LIKE_RE = re.compile(r"[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+")
#: Argument names that carry a path by construction: the ONLY arguments the
#: absolute / climb / denied-component / denied-basename rules apply to. A
#: dotted field locator is included: `answer_key/gold` or `runs/x` as a
#: locator names a private tree, never a field.
_PATH_ARG_NAMES: frozenset[str] = frozenset(
    {
        "path", "file", "filename", "artifact", "relative_path", "dir", "directory",
        "field", "field_path", "locator",
    }
)
#: The path-typed arguments that carry a DOTTED FIELD LOCATOR into
#: `task_ir.json` (`read_field.field`, `apply_edit_trial.locator`): the ONLY
#: arguments the canonical-index rule below applies to (a load-plan path
#: legitimately holds `parts/01.parquet`).
_LOCATOR_ARG_NAMES: frozenset[str] = frozenset({"field", "field_path", "locator"})
#: List indexes use one unsigned canonical spelling. Reject signs, padding,
#: whitespace, separators, and non-ASCII digits so guards and resolvers agree.
CANONICAL_INDEX_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
#: A locator segment Python's `int()` would accept as a list index (optional
#: whitespace, an optional sign, digit runs joined by single underscores;
#: `\d` matches every Unicode decimal digit `int()` reads).
_INT_ALIAS_RE = re.compile(r"^\s*[+-]?\d+(?:_\d+)*\s*$")
#: The PERMIT violation code of a locator whose list-index segment is an
#: alias of the canonical spelling (SoT T4 `ForbiddenArgument`: an aliasing
#: index is a probe of the withheld-slot guard, never a slip).
NON_CANONICAL_INDEX_CODE = "non_canonical_index"
#: The argument rules that OUTRANK the schema check at PERMIT (04 §1 "PARSE
#: before PERMIT" names the population rule; the canonical-index rule joins
#: it): the documented anti-evasion rules under which a call is a violation
#: whatever shape it came in, so a strict schema never turns the probe into
#: a free correction.
SCHEMA_OUTRANKING_ARGUMENT_RULES: frozenset[str] = frozenset(
    {"population_argument", NON_CANONICAL_INDEX_CODE}
)
#: The private trees whose NAME inside a SQL-typed value (a string literal a
#: query would hand to a reader) is a violation (the unambiguous ones;
#: `target`, `attacks` or `gold` alone can be plain words).
_DENIED_TREE_TOKENS: frozenset[str] = frozenset(
    {"answer_key", "attack_cases", "private", "secrets", ".workspace-runtime", ".git", ".terraform", "runs"}
)
#: The correction code a FREE-TEXT argument (prose, an edit's `new`, a
#: rationale) earns when it references a private tree or a credential-shaped
#: file: a slip the model rewrites, never a terminal violation and never a
#: security event — no tool takes a path, so text cannot reach one.
FREE_TEXT_PRIVATE_PATH_CODE = "private_path_in_text"
#: Refuse private-surface names and other trials' nonces before validation.
#: Both executor paths record the same zero-tolerance private-probe code.
PRIVATE_PROBE_CODE = "private_probe"


def canonical_list_index(segment: str) -> int | None:
    """The list index a locator segment names, or None: ONLY the canonical
    unsigned spelling (`CANONICAL_INDEX_RE`) is an index. The one resolver
    rule shared by `read_field`, `apply_patch_text` and the
    withheld-condition guard, so `populations.-1.conditions.2` (the last
    population under `int()`) and `populations.01.conditions.2` name
    nothing anywhere."""
    text = str(segment)
    if CANONICAL_INDEX_RE.fullmatch(text) is None:
        return None
    return int(text)


def locator_argument_problem(value: str) -> str:
    """The locator rule over ONE locator-typed argument value: a dotted
    segment `int()` would read as a list index that is not the canonical
    spelling is `NON_CANONICAL_INDEX_CODE`; '' when clean. Never the value."""
    for segment in str(value).split("."):
        if _INT_ALIAS_RE.fullmatch(segment) and CANONICAL_INDEX_RE.fullmatch(segment) is None:
            return NON_CANONICAL_INDEX_CODE
    return ""


def _path_argument_problem(value: str) -> str:
    """The path rules over ONE path-typed argument value."""
    from elt_taskgen.review.tools.registry import DENIED_BASENAME_RE, DENIED_PATH_COMPONENTS

    # A NUL byte or a backslash is refused the way `registry.check_tool_path`
    # refuses them: a NUL crashes `Path(...).resolve()` ('embedded null byte')
    # and a backslash is a non-POSIX separator, so a load-plan path carrying
    # either must never reach the static checker (finding 3-4).
    if "\x00" in value or "\\" in value:
        return "invalid_path_char"
    stripped = value.strip()
    if stripped.startswith("/") or stripped.startswith("~"):
        return "absolute_path"
    parts = [p for p in stripped.strip("\"'").split("/") if p]
    if ".." in parts:
        return "path_climb"
    if any(p in DENIED_PATH_COMPONENTS for p in parts):
        return "denied_tree"
    if parts and DENIED_BASENAME_RE.search(parts[-1]):
        return "denied_file"
    return ""


def _denied_token_problem(value: str) -> str:
    """A path-like TOKEN naming a denied tree or file inside a SQL-typed
    value (a string literal a reader function would open)."""
    from elt_taskgen.review.tools.registry import DENIED_BASENAME_RE

    for match in _PATH_LIKE_RE.finditer(value):
        token = match.group(0).strip(" \"'")
        parts = [p for p in token.split("/") if p]
        if len(parts) >= 2 and any(p in _DENIED_TREE_TOKENS for p in parts):
            return "denied_tree"
        if parts and DENIED_BASENAME_RE.search(parts[-1]):
            return "denied_file"
    return ""


def _walk_string_arguments(args: Mapping[str, Any]):
    """(argument name, string value) for every string anywhere in `args`,
    nested mappings and sequences included; a nested key that is the
    population argument yields (FORBIDDEN_ARG_NAME, '')."""
    stack: list[tuple[str, Any]] = [(str(k), v) for k, v in args.items()]
    while stack:
        name, value = stack.pop(0)
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if str(key) == FORBIDDEN_ARG_NAME:
                    yield FORBIDDEN_ARG_NAME, ""
                stack.append((str(key), nested))
        elif isinstance(value, (list, tuple)):
            stack.extend((name, nested) for nested in value)
        elif isinstance(value, str):
            yield name, value


def _sql_keyword_scan_text(value: str) -> str:
    """Blank SQL comments and single-quoted literals before scanning keywords.

    Preserve positions so policy checks recognize executable SQL syntax without treating
    quoted prose or comments as commands.
    """
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == "'":
            i += 1
            while i < n:
                if value[i] == "'":
                    if i + 1 < n and value[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
            continue
        if ch == "-" and i + 1 < n and value[i + 1] == "-":
            while i < n and value[i] != "\n":
                i += 1
            out.append(" ")
            continue
        if ch == "/" and i + 1 < n and value[i + 1] == "*":
            i += 2
            while i + 1 < n and not (value[i] == "*" and value[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _forbidden_argument(args: Mapping[str, Any]) -> str:
    """Return a terminal permit violation code, or an empty string.

    Reject any population argument, forbidden SQL/path material in typed arguments,
    absolute or climbing paths, denied trees, credentials, and noncanonical locator
    indexes. Free-text arguments are handled as correctable faults and values are never
    returned.
    """
    if FORBIDDEN_ARG_NAME in args:
        return "population_argument"
    for name, value in _walk_string_arguments(args):
        if name == FORBIDDEN_ARG_NAME:
            return "population_argument"
        if name in _SQL_ARG_NAMES:
            if _FORBIDDEN_SQL_RE.search(_sql_keyword_scan_text(value)):
                return "forbidden_sql"
            found = _denied_token_problem(value)
            if found:
                return found
        elif name in _PATH_ARG_NAMES:
            if name in _LOCATOR_ARG_NAMES:
                found = locator_argument_problem(value)
                if found:
                    return found
            found = _path_argument_problem(value)
            if found:
                return found
    return ""


def _outranking_argument_problem(args: Mapping[str, Any]) -> str:
    """Apply argument rules that outrank schema validation.

    Reject population arguments and noncanonical indexes in locator-typed arguments
    regardless of other shape errors. Return only the reason code.
    """
    if FORBIDDEN_ARG_NAME in args:
        return "population_argument"
    for name, value in _walk_string_arguments(args):
        if name == FORBIDDEN_ARG_NAME:
            return "population_argument"
        if name in _LOCATOR_ARG_NAMES:
            found = locator_argument_problem(value)
            if found in SCHEMA_OUTRANKING_ARGUMENT_RULES:
                return found
    return ""


def _free_text_problem(args: Mapping[str, Any]) -> str:
    """The CORRECTION-class rule over free-text arguments (everything that is
    neither SQL- nor path-typed): a reference to a private tree or a
    credential-shaped file, detected with the gatekeeper's own anchored path
    detector (`projection._PATH_RE`: `answer_key/`, `/Users/`, `runs/`, a
    `.duckdb` file ... at a token boundary). Returns
    `FREE_TEXT_PRIVATE_PATH_CODE` or ''; never the value."""
    from elt_taskgen.review.tools.projection import _PATH_RE

    for name, value in _walk_string_arguments(args):
        if name == FORBIDDEN_ARG_NAME or name in _SQL_ARG_NAMES or name in _PATH_ARG_NAMES:
            continue
        if _PATH_RE.search(value):
            return FREE_TEXT_PRIVATE_PATH_CODE
    return ""


_JSON_TYPES: Mapping[str, tuple[type, ...]] = MappingProxyType(
    {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "array": (list, tuple),
        "object": (Mapping,),
        "null": (type(None),),
    }
)


def validate_args(schema: Mapping[str, Any], args: Any, *, path: str = "") -> str | None:
    """Minimal strict JSON-schema check for tool arguments (the subset the
    strict tool schemas use: object with `properties`, `required`,
    `additionalProperties: false`; typed scalars, `enum`, `minLength` /
    `maxLength`, `minimum` / `maximum`, `pattern`, typed `items`). Returns a
    problem CODE-shaped string, or None when valid; never echoes a value."""
    where = path or "$"
    if not isinstance(schema, Mapping):
        return None
    expected = schema.get("type")
    if expected is not None:
        options = expected if isinstance(expected, (list, tuple)) else [expected]
        ok = False
        for option in options:
            kinds = _JSON_TYPES.get(str(option))
            if kinds is None:
                continue
            if option in ("integer", "number") and isinstance(args, bool):
                continue
            if isinstance(args, kinds):
                ok = True
                break
        if not ok:
            return f"{where}: wrong type"
    if "enum" in schema and args not in list(schema["enum"]):
        return f"{where}: not in enum"
    if isinstance(args, str):
        if "minLength" in schema and len(args) < int(schema["minLength"]):
            return f"{where}: too short"
        if "maxLength" in schema and len(args) > int(schema["maxLength"]):
            return f"{where}: too long"
        pattern = schema.get("pattern")
        if pattern and re.search(str(pattern), args) is None:
            return f"{where}: pattern mismatch"
    if isinstance(args, (int, float)) and not isinstance(args, bool):
        if "minimum" in schema and args < schema["minimum"]:
            return f"{where}: below minimum"
        if "maximum" in schema and args > schema["maximum"]:
            return f"{where}: above maximum"
    if isinstance(args, (list, tuple)):
        if "maxItems" in schema and len(args) > int(schema["maxItems"]):
            return f"{where}: too many items"
        if "minItems" in schema and len(args) < int(schema["minItems"]):
            return f"{where}: too few items"
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(args):
                problem = validate_args(items, item, path=f"{where}[{index}]")
                if problem:
                    return problem
    if isinstance(args, Mapping):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or ():
            if str(name) not in args:
                return f"{where}: missing required property"
        if schema.get("additionalProperties") is False:
            extra = [k for k in args if str(k) not in properties]
            if extra:
                return f"{where}: additional property"
        for name, sub in properties.items():
            if name in args and isinstance(sub, Mapping):
                problem = validate_args(sub, args[name], path=f"{where}.{name}")
                if problem:
                    return problem
    return None


# ---------------------------------------------------------------------------
# Phase 1.R: envelopes and records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolOutcome:
    """The runner-internal envelope around one sanitized projection (SoT T7):
    `ok`, the outcome `code`, the `observation` (a `Diagnostic`, `DevRows` or
    `DiagnosticText`), its transport digest, whether the delivered text was
    cut at the cap, the error class (`''`, `tool_expected`, `policy`,
    `harness`), whether it was executed now (`fresh`) and the digest of the
    raw output the worker projected (when it reported one)."""

    ok: bool
    code: str
    observation: Any
    observation_sha256: str
    truncated: bool = False
    error_class: str = ""
    fresh: bool = True
    raw_output_sha256: str = ""
    #: The tool_result text as delivered (capped).
    text: str = ""
    #: The oracle bits this outcome CHARGED (`cost.oracle_bits` when the
    #: tool ran; 0 for a code in the tool's declared `no_cost_codes`, the
    #: refusals it answers without measuring anything).
    oracle_bits_charged: int = 0


_ZERO_USAGE: Mapping[str, int] = MappingProxyType(
    {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
)

#: Model-turn categories (SoT T5 identity): every model turn ends in exactly one.
MODEL_TURN_CATEGORIES: tuple[str, ...] = (
    "tool_call",
    "refused",
    "nudge",
    "correction",
    "terminal",
    "limit_stop",
)
#: Tool-side turn kinds.
TOOL_TURN_KINDS: tuple[str, ...] = ("tool", "refused", "nudge", "validator")


@dataclass(frozen=True)
class TurnRecord:
    """Represent one model or tool-side turn in the trajectory chain.

    Stable semantic fields and stable route identity are chained. Timing, USD, replay
    status, token usage, admission provenance, diagnostic version, and entry schema
    remain recorded but unchained so equivalent live and replay runs share a digest.
    """

    turn_index: int
    kind: str
    model_turn: int = -1
    category: str = ""
    memo_key: str = ""
    prompt_sha256: str = ""
    response_sha256: str = ""
    tool_name: str = ""
    args_sha256: str = ""
    output_sha256: str = ""
    outcome_code: str = ""
    refused: bool = False
    action_fingerprint: str = ""
    observation_fingerprint: str = ""
    surface_fingerprint: str = ""
    state_epoch: int = 0
    usage: Mapping[str, int] = _ZERO_USAGE
    usd: float = 0.0
    elapsed_model_ms: int = 0
    elapsed_tool_ms: int = 0
    stop_reason: str = ""
    route: Mapping[str, Any] = field(default_factory=dict)
    admission: Mapping[str, Any] = field(default_factory=dict)
    replayed: bool = False
    fresh: bool = True

    def __post_init__(self) -> None:
        if self.kind not in ("model",) + TOOL_TURN_KINDS:
            raise ValueError(f"unknown turn kind {self.kind!r}")
        if self.kind == "model" and self.category not in MODEL_TURN_CATEGORIES:
            raise ValueError(f"a model turn needs a category, not {self.category!r}")
        object.__setattr__(self, "usage", MappingProxyType({k: int(v) for k, v in dict(self.usage).items()}))
        object.__setattr__(self, "route", MappingProxyType(dict(_plain(self.route))))
        object.__setattr__(self, "admission", MappingProxyType(dict(_plain(self.admission))))

    def chain_fields(self) -> dict:
        """What the hash chain binds: everything but the measured timings,
        the accounting fields (`usd`, `replayed`, `usage`) and the
        `admission` provenance, which `as_dict` carries; `route` reduced to
        its stable identity (`trajectory.chained_route`)."""
        return {
            "turn_index": self.turn_index,
            "kind": self.kind,
            "model_turn": self.model_turn,
            "category": self.category,
            "memo_key": self.memo_key,
            "prompt_sha256": self.prompt_sha256,
            "response_sha256": self.response_sha256,
            "tool_name": self.tool_name,
            "args_sha256": self.args_sha256,
            "output_sha256": self.output_sha256,
            "outcome_code": self.outcome_code,
            "refused": self.refused,
            "action_fingerprint": self.action_fingerprint,
            "observation_fingerprint": self.observation_fingerprint,
            "surface_fingerprint": self.surface_fingerprint,
            "state_epoch": self.state_epoch,
            "stop_reason": self.stop_reason,
            "route": _trajectory.chained_route(self.route),
            "fresh": self.fresh,
        }

    def as_dict(self) -> dict:
        data = self.chain_fields()
        data["route"] = dict(self.route)
        data["usage"] = dict(self.usage)
        data["admission"] = dict(self.admission)
        data["usd"] = self.usd
        data["replayed"] = self.replayed
        data["elapsed_model_ms"] = self.elapsed_model_ms
        data["elapsed_tool_ms"] = self.elapsed_tool_ms
        return data


@dataclass(frozen=True)
class FaultRecord:
    """Why a session halted: the terminal, the exception CLASS, the boundary,
    a code, the tool and the turn — never message text (SoT T6)."""

    terminal: str
    exception_type: str
    boundary: str = ""
    code: str = ""
    tool: str = ""
    turn_index: int = -1
    failure_class: str = ""

    @classmethod
    def from_exception(cls, exc: BaseException, *, turn_index: int = -1) -> "FaultRecord":
        terminal = terminal_for_exception(exc)
        failure_class = getattr(exc, "failure_class", None)
        return cls(
            terminal=terminal.name,
            exception_type=type(exc).__name__,
            boundary=str(getattr(exc, "boundary", "") or ""),
            code=str(getattr(exc, "code", "") or ""),
            tool=str(getattr(exc, "tool", "") or ""),
            turn_index=int(turn_index),
            failure_class=str(getattr(failure_class, "value", failure_class) or ""),
        )

    def as_dict(self) -> dict:
        return {
            "terminal": self.terminal,
            "exception_type": self.exception_type,
            "boundary": self.boundary,
            "code": self.code,
            "tool": self.tool,
            "turn_index": self.turn_index,
            "failure_class": self.failure_class,
        }


@dataclass(frozen=True)
class SessionResult:
    """Contain a bounded session's terminal output and evidence.

    The result records the chain digest, normalized final payload, counters, text-free
    fault data, detector/security events, and workspace action trace.
    """

    role: str
    terminal: TerminalState
    session_sha256: str
    turns: tuple[TurnRecord, ...] = ()
    chain_hashes: tuple[str, ...] = ()
    final: Any = None
    task_content_hash: str = ""
    tools_sha256: str = ""
    policy_sha256: str = ""
    session_salt: int = 0
    model_call_count: int = 0
    tool_call_count: int = 0
    refused_count: int = 0
    nudge_count: int = 0
    validator_run_count: int = 0
    correction_count: int = 0
    correction_kinds: Mapping[str, int] = field(default_factory=lambda: {"schema": 0, "compile": 0})
    #: Names of validators still red when correction budget expires. Screening
    #: voids those findings after submission; grammar errors never spend a round.
    red_validators_at_submit: tuple[str, ...] = ()
    terminal_count: int = 0
    limit_stop_count: int = 0
    live_model_call_count: int = 0
    stale_tool_result_count: int = 0
    usage: Mapping[str, int] = _ZERO_USAGE
    usd: float = 0.0
    wall_ms: int = 0
    tool_wall_ms: int = 0
    oracle_bits_used: int = 0
    state_epoch: int = 0
    fault: FaultRecord | None = None
    detector_events: tuple[Any, ...] = ()
    security_events: tuple[Mapping[str, Any], ...] = ()
    action_trace: tuple[WorkspaceActionTraceEntry, ...] = ()
    blocked_on: str = ""
    limit: str = ""
    limit_scope: str = ""
    auto_submitted: bool = False
    #: The one-shot resumes the retry policy took (04 §6): a transport
    #: `ProviderFault` re-issued on the same recorded prefix, a
    #: `SandboxFault` re-dispatched on a fresh worker, a transient
    #: `ToolHarnessFault` retried on fresh scratch — each at most once per
    #: session. Boundary, code and class only; no message text.
    resume_count: int = 0
    resume_events: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.terminal, TerminalState):
            object.__setattr__(self, "terminal", TerminalState.from_name(self.terminal))
        object.__setattr__(self, "turns", tuple(self.turns))
        object.__setattr__(self, "chain_hashes", tuple(str(h) for h in self.chain_hashes))
        object.__setattr__(self, "detector_events", tuple(self.detector_events))
        object.__setattr__(self, "security_events", tuple(MappingProxyType(dict(e)) for e in self.security_events))
        object.__setattr__(self, "resume_events", tuple(MappingProxyType(dict(e)) for e in self.resume_events))
        if self.resume_count != len(self.resume_events):
            raise ValueError("resume_count must equal the number of resume events")
        object.__setattr__(self, "action_trace", tuple(self.action_trace))
        object.__setattr__(self, "usage", MappingProxyType({k: int(v) for k, v in dict(self.usage).items()}))
        object.__setattr__(self, "correction_kinds", MappingProxyType({str(k): int(v) for k, v in dict(self.correction_kinds).items()}))
        object.__setattr__(self, "red_validators_at_submit", tuple(str(n) for n in self.red_validators_at_submit))
        categories = (
            self.tool_call_count
            + self.refused_count
            + self.nudge_count
            + self.correction_count
            + self.terminal_count
            + self.limit_stop_count
        )
        if self.model_call_count != categories:
            raise ValueError(
                "SoT T5 identity broken: model_call_count "
                f"{self.model_call_count} != tool_call + refused + nudge + "
                f"correction + terminal + limit_stop = {categories}"
            )
        expected_turns = (
            self.model_call_count
            + self.tool_call_count
            + self.refused_count
            + self.nudge_count
            + self.validator_run_count
        )
        if len(self.turns) != expected_turns:
            raise ValueError(
                f"SoT T5 identity broken: {len(self.turns)} turns != model_call + "
                f"tool_call + refused + nudge + validator_run = {expected_turns}"
            )
        if len(self.chain_hashes) != len(self.turns):
            raise ValueError("the hash chain must carry one link per turn")
        if self.nudge_count > 1:
            raise ValueError("at most one nudge per session")
        if len(self.action_trace) > MAX_WORKSPACE_ACTIONS:
            raise ValueError(f"action_trace exceeds MAX_WORKSPACE_ACTIONS = {MAX_WORKSPACE_ACTIONS}")
        if self.blocked_on and self.auto_submitted:
            raise ValueError("a session is either blocked on a limit or auto-submitted, not both")
        if self.terminal.is_halt and self.fault is None:
            raise ValueError(f"a halt ({self.terminal.name}) carries a FaultRecord")

    @property
    def trajectory_sha256(self) -> str:
        return self.session_sha256

    def verify_chain(self) -> bool:
        """Recompute the chain over `turns` under the recorded identity."""
        try:
            _trajectory.verify_chain(
                self.turns,
                self.chain_hashes,
                task_content_hash=self.task_content_hash,
                tools_sha256=self.tools_sha256,
                policy_sha256=self.policy_sha256,
                role=self.role,
            )
        except _trajectory.ChainError:
            return False
        return True

    @property
    def submitted_with_red_validators(self) -> int:
        """SoT T8 metrology counter: how many validators / hooks were still
        red on the accepted submission (0 for a green or unsubmitted one)."""
        return len(self.red_validators_at_submit)

    def as_dict(self) -> dict:
        """The persisted form (`<ws>/tasks/<id>/reports/sessions/...json`)."""
        return {
            "role": self.role,
            "terminal": self.terminal.value,
            "terminal_name": self.terminal.name,
            "session_sha256": self.session_sha256,
            "trajectory_sha256": self.session_sha256,
            "task_content_hash": self.task_content_hash,
            "tools_sha256": self.tools_sha256,
            "policy_sha256": self.policy_sha256,
            "session_salt": self.session_salt,
            "final": _plain(self.final),
            "turns": [t.as_dict() for t in self.turns],
            "chain_hashes": list(self.chain_hashes),
            "model_call_count": self.model_call_count,
            "tool_call_count": self.tool_call_count,
            "refused_count": self.refused_count,
            "nudge_count": self.nudge_count,
            "validator_run_count": self.validator_run_count,
            "correction_count": self.correction_count,
            "correction_kinds": dict(self.correction_kinds),
            "red_validators_at_submit": list(self.red_validators_at_submit),
            "submitted_with_red_validators": self.submitted_with_red_validators,
            "terminal_count": self.terminal_count,
            "limit_stop_count": self.limit_stop_count,
            "live_model_call_count": self.live_model_call_count,
            "stale_tool_result_count": self.stale_tool_result_count,
            "usage": dict(self.usage),
            "usd": self.usd,
            "wall_ms": self.wall_ms,
            "tool_wall_ms": self.tool_wall_ms,
            "oracle_bits_used": self.oracle_bits_used,
            "state_epoch": self.state_epoch,
            "fault": self.fault.as_dict() if self.fault else None,
            "detector_events": [_plain(getattr(e, "__dict__", e)) for e in self.detector_events],
            "security_events": [dict(e) for e in self.security_events],
            "action_trace": [e.model_dump(mode="json") for e in self.action_trace],
            "blocked_on": self.blocked_on,
            "limit": self.limit,
            "limit_scope": self.limit_scope,
            "auto_submitted": self.auto_submitted,
            "resume_count": self.resume_count,
            "resume_events": [dict(e) for e in self.resume_events],
        }


# ---------------------------------------------------------------------------
# Phase 1.R: the validator worker seam (D3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkerResult:
    """What the worker hands back across the D3/D1 boundary: the projection
    object, the canonical transport bytes `serialize_for_transport` produced
    (the gatekeeper re-checks exactly these) and, when the worker knows it,
    the digest of the raw output it projected."""

    observation: Any
    payload: str
    raw_output_sha256: str = ""


@runtime_checkable
class ValidatorWorker(Protocol):
    """Define the boundary where one validator runs under its role profile.

    Implementations return a sanitized `ToolOutcome` or raise typed session faults; raw
    exceptions and output do not cross into model-visible text.
    """

    def run(self, tool: Any, ctx: Any, args: Mapping[str, Any], *, deadline_s: float) -> WorkerResult: ...


class InProcessValidatorWorker:
    """Run a validator and projector in the current process.

    Apply deadlines and sanitizer checks around the trusted tool boundary. This worker
    is suitable only where in-process execution is explicitly allowed.
    """

    def __init__(
        self,
        *,
        surface_fingerprint: Callable[[Any], str | None] | None = None,
        current_draft: Callable[[Any], Any] | None = None,
    ) -> None:
        self._surface = surface_fingerprint
        self._draft = current_draft

    def surface_fingerprint(self, ctx: Any) -> str | None:
        return None if self._surface is None else self._surface(ctx)

    def current_draft(self, ctx: Any) -> Any:
        return None if self._draft is None else self._draft(ctx)

    @staticmethod
    def _invoke(tool: Any, ctx: Any, args: Mapping[str, Any]) -> WorkerResult:
        from elt_taskgen.review.tools.projection import serialize_for_transport
        from elt_taskgen.review.tools.registry import ToolRegistry

        name = str(getattr(tool, "name", "tool"))
        registry = ToolRegistry(ctx.role, [tool])
        observation = registry.dispatch(ctx, name, args)
        task = getattr(ctx, "task", None)
        if task is None:
            raise ToolHarnessFault(name, code="no_task_for_projector")
        payload = serialize_for_transport(
            observation,
            task=task,
            package=getattr(ctx, "package", None),
            route=getattr(ctx, "route", None),
        )
        return WorkerResult(observation=observation, payload=payload)

    def run(self, tool: Any, ctx: Any, args: Mapping[str, Any], *, deadline_s: float) -> WorkerResult:
        name = str(getattr(tool, "name", "tool"))
        outcome: dict[str, Any] = {}

        def target() -> None:
            try:
                outcome["result"] = self._invoke(tool, ctx, args)
            except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
                outcome["error"] = exc

        worker = threading.Thread(target=target, name=f"validator-worker:{name}", daemon=True)
        worker.start()
        worker.join(max(0.0, float(deadline_s)))
        if worker.is_alive():
            raise ToolDeadlineExceeded(name, deadline_s=float(deadline_s))
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]


class InterruptibleValidatorWorker(InProcessValidatorWorker):
    """Run in-process validators under an interrupting deadline.

    Read-only calls may use the legacy fallback where interval timers are unavailable.
    Writes from unsupported threads fail closed and are never abandoned in a daemon.
    """

    def run(self, tool: Any, ctx: Any, args: Mapping[str, Any], *, deadline_s: float) -> WorkerResult:
        name = str(getattr(tool, "name", "tool"))
        deadline = float(deadline_s)
        if deadline <= 0:
            raise ToolDeadlineExceeded(name, deadline_s=max(0.0, deadline))
        can_interrupt = (
            hasattr(signal, "SIGALRM")
            and hasattr(signal, "setitimer")
            and threading.current_thread() is threading.main_thread()
        )
        if not can_interrupt:
            if bool(getattr(tool, "surface_write", False)):
                raise ToolHarnessFault(name, code="write_deadline_unsupported")
            return super().run(tool, ctx, args, deadline_s=deadline)

        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)

        def deadline_handler(_signum, _frame) -> None:
            raise ToolDeadlineExceeded(name, deadline_s=deadline)

        signal.signal(signal.SIGALRM, deadline_handler)
        signal.setitimer(signal.ITIMER_REAL, deadline)
        try:
            return self._invoke(tool, ctx, args)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer != (0.0, 0.0):
                signal.setitimer(signal.ITIMER_REAL, *previous_timer)


# ---------------------------------------------------------------------------
# Phase 1.R: the runner
# ---------------------------------------------------------------------------

def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _turn_attr(turn: Any, name: str, default: Any = None) -> Any:
    if isinstance(turn, Mapping):
        return turn.get(name, default)
    return getattr(turn, name, default)


def _usage_of(turn: Any) -> dict[str, int]:
    """The four counters of a turn under the SoT T8 short names, whatever
    shape the provider handed back (a `Usage`, its `as_dict()`, a wire
    `usage` block, or nothing)."""
    usage = _turn_attr(turn, "usage", None)
    if usage is None:
        return dict(_ZERO_USAGE)
    as_dict = getattr(usage, "as_dict", None)
    if callable(as_dict):
        usage = as_dict()
    usage = _as_mapping(usage)

    def pick(*names: str) -> int:
        for key in names:
            if key in usage and usage[key] is not None:
                try:
                    return int(usage[key])
                except (TypeError, ValueError):
                    return 0
        return 0

    return {
        "input": pick("input", "input_tokens"),
        "output": pick("output", "output_tokens"),
        "cache_read": pick("cache_read", "cache_read_input_tokens"),
        "cache_write": pick("cache_write", "cache_creation_input_tokens"),
    }


def _content_blocks(turn: Any) -> list[Any]:
    content = _turn_attr(turn, "content", None)
    if content is None:
        raw = _as_mapping(_turn_attr(turn, "raw", None))
        content = raw.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, (list, tuple)):
        return [_plain(block) for block in content]
    return []


def _tool_use_blocks(blocks: Sequence[Any]) -> list[dict]:
    return [
        block
        for block in blocks
        if isinstance(block, Mapping) and block.get("type") == "tool_use"
    ]


class _Stop(Exception):
    """Internal: unwind the loop into a returned SessionResult."""

    def __init__(self, terminal: TerminalState, *, scope: str = "") -> None:
        super().__init__(terminal.name)
        self.terminal = terminal
        self.scope = scope


class _ModelTurnWallDeadline(BaseException):
    """Internal signal escape that transport ``except Exception`` blocks
    cannot accidentally reclassify as a provider fault.

    A bounded session's wall limit includes a blocking model transport.  It
    therefore has to interrupt the model turn itself, not merely check the
    clock between turns.  ``BaseException`` is intentional and private: the
    runner catches it immediately and converts it to ``LIMIT_WALL``.
    """


#: `ToolHarnessFault` codes a tool may raise TRANSIENTLY (04 §6, S2 §3.7: a
#: DEV database not yet fresh, a runtime not yet up): the runner retries the
#: tool ONCE on fresh scratch; every other harness code halts on the first.
#: Compared case-insensitively (the executors spell them in upper case).
TRANSIENT_HARNESS_CODES: frozenset[str] = frozenset({"database_not_fresh", "runtime_unavailable"})

#: The canonical name of the proposer's certify verb (SoT T3): the one tool
#: whose per-session ceiling the role's `max_certify` limit also bounds.
CERTIFY_TOOL_NAME = "certify"

#: The exception names the provider layer raises for a replay miss, a route
#: mismatch, missing credentials and a budget refusal: each halts on the
#: FIRST occurrence (no transport retry can conjure a transcript, a key or a
#: budget), so the one-shot provider resume never applies to them.
_UNRESUMABLE_PROVIDER_NAMES: frozenset[str] = frozenset(
    {
        "TranscriptMissingError",
        "TranscriptRouteMismatchError",
        "MissingCredentialsError",
        "BudgetExceededError",
    }
)


def _hook_name(hook: Any) -> str:
    """The name a payload validator is recorded under: its `validator_name`
    attribute, else its `__name__`, else its class name — as a stable code
    (`_CODE_RE`), so the action trace and the turn record can carry it."""
    for attr in ("validator_name", "__name__"):
        value = getattr(hook, attr, None)
        if isinstance(value, str) and value:
            break
    else:
        value = type(hook).__name__
    code = re.sub(r"[^a-z0-9_]", "_", str(value).lower()).strip("_")
    if not code or _CODE_RE.fullmatch(code) is None:
        return "payload_validator"
    return code[:128]


def _accepts_keyword(fn: Any, name: str) -> bool:
    """Does callable `fn` take keyword argument `name` (named, or through
    `**kwargs`)? A scripted provider double that predates a keyword is
    called without it."""
    import inspect

    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == name and parameter.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            return True
    return False


class _Session:
    """The mutable state of one run of `run_bounded_session`."""

    def __init__(
        self,
        role: str,
        initial_view: str,
        tools: Sequence[Any],
        policy: SessionPolicy,
        limits: SessionLimits,
        *,
        provider: Any,
        ctx: Any,
        worker: Any,
        detector: Any,
        clock: Callable[[], float],
        expected_observations: Sequence[str] | None = None,
        payload_validators: Sequence[ValidatorHook] = (),
    ) -> None:
        from elt_taskgen.review.tools.registry import ToolRegistry

        self.role = str(role)
        self.policy = policy
        self.limits = limits
        self.provider = provider
        # The correction channel's hooks (roadmap Phase 3 item 3), run on
        # every submitted payload BEFORE the declared harness validators.
        self.payload_validators: tuple[ValidatorHook, ...] = tuple(payload_validators or ())
        for hook in self.payload_validators:
            if not callable(hook):
                raise TypeError("every payload validator must be callable(task, payload)")
        # REPLAY (0-1): the full `output_sha256` of every executed tool-side
        # turn of the recorded session this run replays, in order (None: a
        # live run, nothing to verify against). Each observation the runner
        # executes is compared at its index; a divergence counts toward
        # `stale_tool_result_count` and halts as `replay_mismatch`.
        self.expected_observations: tuple[str, ...] | None = (
            None if expected_observations is None else tuple(str(d) for d in expected_observations)
        )
        self.executed_observations = 0
        self.stale_tool_result_count = 0
        # The full digests of the tool-side turns since the previous model
        # turn: folded into the next turn's memo key (the prefix carries only
        # the capped text). A scripted double without the keyword is called
        # without it.
        self.pending_observations: list[str] = []
        self._turn_takes_observations = _accepts_keyword(getattr(provider, "_turn", None), "observations")
        self.ctx = ctx
        self.worker = worker
        self.clock = clock
        self.terminal_names = set(policy.terminal_tool_names)
        self.tool_objects: dict[str, Any] = {str(getattr(t, "name", "")): t for t in tools}
        model_facing = [t for t in tools if str(getattr(t, "name", "")) not in self.terminal_names]
        self.registry = ToolRegistry(self.role, model_facing)
        self.detector = detector
        self.messages: list[dict] = [{"role": "user", "content": str(initial_view)}]
        self.turns: list[TurnRecord] = []
        task = getattr(ctx, "task", None)
        content_hash = ""
        if task is not None:
            hasher = getattr(task, "content_hash", None)
            content_hash = str(hasher()) if callable(hasher) else ""
        self.task_content_hash = content_hash
        self.tools_sha256 = policy.tools_sha256()
        self.policy_sha256 = policy.sha256()
        self.chain = _trajectory.TrajectoryChain(
            task_content_hash=self.task_content_hash,
            tools_sha256=self.tools_sha256,
            policy_sha256=self.policy_sha256,
            role=self.role,
        )
        # Counters (SoT T5 / T8).
        self.model_call_count = 0
        self.tool_call_count = 0
        self.refused_count = 0
        self.nudge_count = 0
        self.validator_run_count = 0
        self.correction_count = 0
        self.correction_kinds = {"schema": 0, "compile": 0}
        #: The harness validators / hooks still RED on the payload that was
        #: accepted as submitted because the compile-correction budget was
        #: spent (SoT T1.1 "a second compile failure is accepted and screened
        #: post-session") — names only; the post-session screen reads them.
        self.red_validators_at_submit: tuple[str, ...] = ()
        self.terminal_count = 0
        self.limit_stop_count = 0
        self.live_model_call_count = 0
        self.usage = dict(_ZERO_USAGE)
        self.usd = 0.0
        self.tool_wall_ms = 0
        #: The measured duration (ms, on `self.clock`) of the LAST worker
        #: attempt `_dispatch` ran — the answering attempt, or the one that
        #: refused. `tool_wall_ms` (the scored `LIMIT_WORKING` clock) charges
        #: only this, so a retried/dead attempt, the gatekeeper and a harness
        #: fault's time never inflate it (SoT T4 `LIMIT_WORKING`; finding 2-4).
        self._last_attempt_ms = 0
        self.oracle_bits_used = 0
        self.per_tool_calls: dict[str, int] = {}
        self.writes = 0
        self.protocol_streak = 0
        self.compile_corrections = 0
        self.security_events: list[dict] = []
        self.action_trace: list[WorkspaceActionTraceEntry] = []
        self.final: Any = None
        self.auto_submitted = False
        self.green_epoch = -1
        self.green_draft: Any = None
        self.last_write_args: Any = None
        self.started = float(clock())
        self.wall_ms = 0
        self.terminal: TerminalState | None = None
        self.fault: FaultRecord | None = None
        self.limit_scope = ""
        #: The partial record fields of the model turn PARSE has not yet
        #: categorised (set by `model_turn`, cleared by `_record_model`): a
        #: halt raised while the submit is being validated closes that turn
        #: as a terminal with the fault's code (`_attach`), so the SoT T5
        #: identities hold on the partial result the fault carries.
        self.pending_model_fields: dict | None = None
        # The one-shot resumes of the retry policy (04 §6), each spent at
        # most once per session; the recorded turns and every counter stay
        # intact across a resume.
        self.provider_resume_used = False
        self.sandbox_resume_used = False
        self.harness_retry_used = False
        self.resume_events: list[dict] = []

    # -- accounting helpers --------------------------------------------------

    @property
    def state_epoch(self) -> int:
        return int(self.detector.state_epoch)

    @property
    def calls_toward_cap(self) -> int:
        """Executed calls, refused calls and harness validator runs all count
        toward `max_tool_calls` (SoT T1, T5); nudges count as turns only."""
        return self.tool_call_count + self.refused_count + self.validator_run_count

    @property
    def tool_calls_exhausted(self) -> bool:
        """Is `max_tool_calls` spent? The next model turn is still issued —
        with the wire narrowed to submit and abort (last-turn forcing) — and
        a model-initiated call on it is `LIMIT_TOOL_CALLS`. A declared 0 (no
        model-initiated tool) is spent from the start: a hard zero, never
        "no cap"."""
        return self.calls_toward_cap >= self.limits.max_tool_calls

    def _oracle_bits_of(self, tool: Any) -> int:
        return int(getattr(getattr(tool, "cost", None), "oracle_bits", 0) or 0)

    def _would_exceed_oracle(self, bits: int) -> bool:
        """PERMIT bit accounting: would charging `bits` cross
        `max_oracle_bits`? (0 bits never does.)"""
        return bool(bits) and self.oracle_bits_used + int(bits) > self.limits.max_oracle_bits

    def _per_session_cap(self, tool: Any) -> int | None:
        """The per-session ceiling of `tool`: its `cost.per_session`, and for
        the certify verb also the role's declared `max_certify` (SoT T1 RPR:
        `certify` <= 2) — the tighter of the two."""
        cap = getattr(getattr(tool, "cost", None), "per_session", None)
        cap = None if cap is None else int(cap)
        if str(getattr(tool, "name", "")) == CERTIFY_TOOL_NAME and self.limits.max_certify > 0:
            cap = self.limits.max_certify if cap is None else min(cap, self.limits.max_certify)
        return cap

    def _note_resume(self, boundary: str, *, exc: BaseException, turn_index: int, tool: str = "") -> None:
        """One resume of the retry policy: the boundary, the exception CLASS
        and its code — never the message."""
        self.resume_events.append(
            {
                "boundary": str(boundary),
                "exception_type": type(exc).__name__,
                "code": str(getattr(exc, "code", "") or ""),
                "turn_index": int(turn_index),
                "tool": str(tool),
            }
        )

    def _fresh_worker(self) -> Any:
        """A fresh worker for the SandboxFault resume: the worker's own
        `rebuild()` when it has one (a spawned worker respawns; the recorded
        prefix is the controller's and needs no replay into it), else the
        same in-process worker, which holds no state a death could lose."""
        rebuild = getattr(self.worker, "rebuild", None)
        if callable(rebuild):
            fresh = rebuild()
            if fresh is not None:
                self.worker = fresh
        return self.worker

    def _fresh_scratch(self) -> None:
        """Fresh scratch for the transient-harness retry: the context's or
        the worker's `fresh_scratch()` hook when either exposes one."""
        for holder in (self.ctx, self.worker):
            refresh = getattr(holder, "fresh_scratch", None)
            if callable(refresh):
                refresh()
                return

    def _append(self, record: TurnRecord) -> None:
        """RECORD: the turn enters the chain before anything is returned. A
        tool-side record's `output_sha256` (the FULL observation digest) is
        queued for the next model turn's memo key."""
        self.turns.append(record)
        self.chain.append(record)
        if record.kind != "model" and record.output_sha256:
            self.pending_observations.append(record.output_sha256)

    def _trace(self, action: str, *, success: bool, outcome_code: str, observation_sha256: str = "") -> None:
        if len(self.action_trace) >= MAX_WORKSPACE_ACTIONS:
            return
        self.action_trace.append(
            WorkspaceActionTraceEntry(
                sequence=len(self.action_trace),
                action=action if _CODE_RE.fullmatch(action) else "tool",
                success=bool(success),
                outcome_code=outcome_code if _CODE_RE.fullmatch(outcome_code) else "unclassified",
                observation_sha256=observation_sha256,
            )
        )

    def _elapsed_ms(self) -> int:
        return max(0, int(round((float(self.clock()) - self.started) * 1000.0)))

    def _security_event(self, code: str, *, tool: str, turn_index: int, detail: str = "") -> None:
        self.security_events.append(
            {"code": str(code), "tool": str(tool), "turn_index": int(turn_index), "detail": str(detail)}
        )

    def _close_pending_model_turn(self, exc: BaseException) -> None:
        """A halt raised between MODEL_TURN and the turn's category record
        (a harness fault or tripwire from a submit-time validator — the
        correction channel's hooks or the declared harness validators)
        closes the turn as a terminal carrying the fault's code, so the
        partial result's SoT T5 identities hold and the chain is complete."""
        fields = self.pending_model_fields
        if fields is None:
            return
        categorised = (
            self.tool_call_count + self.refused_count + self.nudge_count
            + self.correction_count + self.terminal_count + self.limit_stop_count
        )
        if self.model_call_count <= categorised:
            self.pending_model_fields = None
            return
        code = str(getattr(exc, "code", "") or "harness_fault")
        terminal = terminal_for_exception(exc)
        outcome_code = "sanitizer_tripwire" if terminal is TerminalState.LEAK_TRIPWIRE else (
            code if _CODE_RE.fullmatch(code) else "harness_fault"
        )
        self._record_model(fields, "terminal", tool_name=self.policy.submit_tool, outcome_code=outcome_code)

    def _attach(self, exc: BaseException, *, turn_index: int) -> BaseException:
        """A halt: the partial result rides on the exception (never
        model-visible) so the caller can persist what was recorded."""
        self.fault = FaultRecord.from_exception(exc, turn_index=turn_index)
        self.terminal = TerminalState.from_name(self.fault.terminal)
        self._close_pending_model_turn(exc)
        try:
            setattr(exc, "session_result", self.result())
        except (AttributeError, TypeError):  # pragma: no cover - exotic exception type
            pass
        return exc

    # -- the result ------------------------------------------------------------

    def result(self) -> SessionResult:
        terminal = self.terminal or TerminalState.PROVIDER_FAULT
        blocked_on = ""
        limit = terminal.limit_kind
        if terminal.is_limit_stop and not self.auto_submitted:
            blocked_on = f"session_limit:{limit}"
        return SessionResult(
            role=self.role,
            terminal=terminal,
            session_sha256=self.chain.digest,
            turns=tuple(self.turns),
            chain_hashes=tuple(self.chain.hashes),
            final=self.final,
            task_content_hash=self.task_content_hash,
            tools_sha256=self.tools_sha256,
            policy_sha256=self.policy_sha256,
            session_salt=int(self.policy.session_salt),
            model_call_count=self.model_call_count,
            tool_call_count=self.tool_call_count,
            refused_count=self.refused_count,
            nudge_count=self.nudge_count,
            validator_run_count=self.validator_run_count,
            correction_count=self.correction_count,
            correction_kinds=dict(self.correction_kinds),
            red_validators_at_submit=tuple(self.red_validators_at_submit),
            terminal_count=self.terminal_count,
            limit_stop_count=self.limit_stop_count,
            live_model_call_count=self.live_model_call_count,
            stale_tool_result_count=self.stale_tool_result_count,
            usage=dict(self.usage),
            usd=self.usd,
            wall_ms=self._elapsed_ms(),
            tool_wall_ms=self.tool_wall_ms,
            oracle_bits_used=self.oracle_bits_used,
            state_epoch=self.state_epoch,
            fault=self.fault,
            detector_events=tuple(getattr(self.detector, "events", ())),
            security_events=tuple(self.security_events),
            action_trace=tuple(self.action_trace),
            blocked_on=blocked_on,
            limit=limit,
            limit_scope=self.limit_scope if terminal is TerminalState.LIMIT_USD else "",
            auto_submitted=self.auto_submitted,
            resume_count=len(self.resume_events),
            resume_events=tuple(self.resume_events),
        )

    # -- INIT ------------------------------------------------------------------

    def init_reserve(self) -> None:
        """INIT: refuse to start when the remaining task or total budget is
        below the session cap plus the nested ceiling (04 §1). The refusal is
        the transport's `BudgetExceededError` (scope task / total): a
        PROVIDER_FAULT halt, nothing spent."""
        cap = self.limits.max_usd
        if cap is None:
            return
        meter = getattr(self.provider, "meter", None)
        headroom_of = getattr(meter, "headroom_usd", None)
        if not callable(headroom_of):
            return
        headroom = headroom_of(str(getattr(self.ctx, "task_id", "")))
        if headroom is None:
            return
        needed = float(cap) + self.limits.nested_ceiling_usd
        if float(headroom) < needed:
            from elt_taskgen.review.providers import BudgetExceededError

            raise BudgetExceededError(
                f"session INIT for role {self.role!r} refused: the remaining task "
                f"budget ${float(headroom):.4f} is below the session reserve "
                f"${needed:.4f} (max_usd plus nested ceiling); nothing spent",
                scope="task",
            )

    # -- CHECK_LIMITS (before a model turn) --------------------------------------

    def check_limits(self) -> None:
        wall = self.limits.session_wall_seconds
        if wall is not None and (float(self.clock()) - self.started) >= wall:
            raise _Stop(TerminalState.LIMIT_WALL)
        if self.model_call_count >= self.limits.max_turns:
            raise _Stop(TerminalState.LIMIT_TURNS)
        # At the tool-call cap, the next turn offers only submit or abort; another
        # tool call triggers LIMIT_TOOL_CALLS. Reaching the cap alone is not a stop.
        max_output = self.limits.max_output_tokens
        if max_output is not None and self.usage["output"] >= max_output:
            raise _Stop(TerminalState.LIMIT_TOKENS)
        max_total = self.limits.max_total_tokens
        if max_total is not None and sum(self.usage.values()) >= max_total:
            raise _Stop(TerminalState.LIMIT_TOKENS)
        working = self.limits.max_tool_wall_s
        if (
            working is not None
            and self.role not in COUNCIL_ROLES_WITHOUT_WORKING_LIMIT
            and self.tool_wall_ms >= working * 1000.0
        ):
            raise _Stop(TerminalState.LIMIT_WORKING)

    # -- MODEL_TURN ------------------------------------------------------------

    def model_turn(self) -> tuple[Any, list[Any], dict]:
        """One `provider._turn`. Returns (turn, content blocks, the partial
        model record fields); the record is appended once PARSE has decided
        the turn's category."""
        turn_index = self.model_call_count
        prompt_sha = sha256_hex(canonical_json(plain_messages(self.messages)))
        started = float(self.clock())
        try:
            turn = self._issue_turn_with_wall_deadline(turn_index)
        except _Stop:
            raise
        except SessionFault as exc:
            raise self._attach(exc, turn_index=turn_index)
        except ProviderProtocolError as exc:
            raise self._attach(exc, turn_index=turn_index)
        except Exception as exc:  # noqa: BLE001 - classified below by class, never by text
            name = type(exc).__name__
            scope = str(getattr(exc, "scope", "") or "")
            is_budget = name == "BudgetExceededError" or any(
                base.__name__ == "BudgetExceededError" for base in type(exc).__mro__
            )
            if name == "RoleCapExceeded" or (is_budget and scope == "role"):
                # The session's OWN cap: LIMIT_USD, a stop, never infrastructure.
                self.limit_scope = "role"
                raise _Stop(TerminalState.LIMIT_USD, scope="role") from exc
            if is_budget or name in ("TranscriptMissingError", "TranscriptRouteMismatchError", "MissingCredentialsError"):
                # The task / total budget, a replay miss, missing credentials:
                # the engine already names these; re-raised unchanged.
                raise self._attach(exc, turn_index=turn_index)
            fault = ProviderFault(
                f"provider turn {turn_index} for role {self.role!r} failed at the "
                f"transport ({name}); a harness fault",
                code="transport",
            )
            raise self._attach(fault, turn_index=turn_index) from exc
        # The observations that fed this turn are now bound into its key.
        self.pending_observations = []
        elapsed_ms = max(0, int(round((float(self.clock()) - started) * 1000.0)))
        blocks = _content_blocks(turn)
        usage = _usage_of(turn)
        for key, value in usage.items():
            self.usage[key] += int(value)
        usd = float(_turn_attr(turn, "usd", 0.0) or 0.0)
        self.usd += usd
        replayed = bool(_turn_attr(turn, "replayed", False))
        self.model_call_count += 1
        if not replayed:
            self.live_model_call_count += 1
        reported_ms = _turn_attr(turn, "elapsed_ms", None)
        fields = {
            "model_turn": turn_index,
            "memo_key": str(_turn_attr(turn, "memo_key", "") or ""),
            "prompt_sha256": prompt_sha,
            "response_sha256": sha256_hex(canonical_json(_plain(blocks))),
            "usage": usage,
            "usd": usd,
            "elapsed_model_ms": int(reported_ms) if reported_ms is not None else elapsed_ms,
            "stop_reason": str(_turn_attr(turn, "stop_reason", "") or ""),
            "route": _as_mapping(_turn_attr(turn, "route", None)),
            "admission": _as_mapping(_turn_attr(turn, "admission", None)),
            "replayed": replayed,
            "state_epoch": self.state_epoch,
        }
        self.pending_model_fields = fields
        return turn, blocks, fields

    def _issue_turn_with_wall_deadline(self, turn_index: int) -> Any:
        """Issue one provider turn within the session's remaining wall time.

        Raise the typed wall-limit stop when no time remains or the interrupting
        deadline expires; never leave an unbounded transport call running.
        """
        wall = self.limits.session_wall_seconds
        if wall is None:
            return self._issue_turn(turn_index)
        remaining = float(wall) - (float(self.clock()) - self.started)
        if remaining <= 0:
            raise _Stop(TerminalState.LIMIT_WALL)
        can_interrupt = (
            hasattr(signal, "SIGALRM")
            and hasattr(signal, "setitimer")
            and threading.current_thread() is threading.main_thread()
        )
        if not can_interrupt:
            raise ProviderFault(
                "bounded model transport cannot enforce its wall deadline "
                "outside the worker process's main thread",
                code="wall_deadline_unsupported",
            )

        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)

        def deadline_handler(_signum, _frame) -> None:
            raise _ModelTurnWallDeadline()

        signal.signal(signal.SIGALRM, deadline_handler)
        try:
            signal.setitimer(signal.ITIMER_REAL, remaining)
            try:
                return self._issue_turn(turn_index)
            except _ModelTurnWallDeadline:
                raise _Stop(TerminalState.LIMIT_WALL) from None
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer != (0.0, 0.0):
                signal.setitimer(signal.ITIMER_REAL, *previous_timer)

    def _resumable_provider_fault(self, exc: BaseException) -> bool:
        """Is `exc` the transport half of `ProviderFault` the retry policy
        resumes ONCE from the last recorded turn (04 §6; SoT T6): a
        transport fault after the transport's own retries — never a
        truncation (a configuration fault, halted on the first), a replay
        miss, a route mismatch, missing credentials or a budget refusal
        (nothing a re-issue can change), and never a fault of another
        boundary or the model's own."""
        if isinstance(exc, (OutputTruncated, ProviderProtocolError, PolicyFault, _Stop)):
            return False
        if any(base.__name__ in _UNRESUMABLE_PROVIDER_NAMES for base in type(exc).__mro__):
            return False
        if isinstance(exc, SessionFault):
            return isinstance(exc, ProviderFault)
        # An unclassified transport exception: `model_turn` wraps it as a
        # `ProviderFault(code="transport")`, so it is the same resume.
        return isinstance(exc, Exception)

    def _issue_turn(self, turn_index: int) -> Any:
        """Issue one provider turn with a single transport-fault resume.

        The retry uses the same recorded prefix without moving counters. When tool calls
        are spent, the wire narrows to terminal tools. Full observation digests enter
        the memo key.
        """
        kwargs: dict[str, Any] = {}
        if self.tool_calls_exhausted and not self.policy.is_last_permitted_turn(turn_index):
            narrowed = self.policy.wire_tools_for_turn(turn_index, tool_calls_exhausted=True)
            if narrowed is not self.policy.wire_tools:
                kwargs["tools"] = narrowed
        if self.pending_observations and self._turn_takes_observations:
            kwargs["observations"] = tuple(self.pending_observations)
        try:
            return self.provider._turn(self.role, list(self.messages), self.policy, turn_index, **kwargs)
        except Exception as exc:  # noqa: BLE001 - classified by `_resumable_provider_fault`
            if self.provider_resume_used or not self._resumable_provider_fault(exc):
                raise
            self.provider_resume_used = True
            self._note_resume("provider", exc=exc, turn_index=turn_index)
        return self.provider._turn(self.role, list(self.messages), self.policy, turn_index, **kwargs)

    def _record_model(self, fields: dict, category: str, **extra: Any) -> None:
        self._append(TurnRecord(turn_index=len(self.turns), kind="model", category=category, **fields, **extra))
        self.pending_model_fields = None
        if category == "tool_call":
            pass  # the tool-side record follows
        elif category == "refused":
            self.refused_count += 1
        elif category == "nudge":
            self.nudge_count += 1
        elif category == "correction":
            self.correction_count += 1
        elif category == "terminal":
            self.terminal_count += 1
        elif category == "limit_stop":
            self.limit_stop_count += 1

    # -- message shaping ---------------------------------------------------------

    def _answer(self, blocks: Sequence[Any], results: Sequence[tuple[str, str, bool]], *, text_only: str = "") -> None:
        """Append the assistant turn and the user turn answering EVERY
        tool_use id (wire legality), or a plain text when there was none —
        through `providers._with_tool_results`, the ONE builder of the
        canonical `tool_result` turn, so the prefix a runner-driven session
        keys its next turn on is byte-for-byte the prefix a resumed or
        replayed session rebuilds from the store."""
        from elt_taskgen.review.providers import _with_tool_results

        self.messages = _with_tool_results(
            self.messages,
            [_plain(b) for b in blocks],
            [(tool_use_id, text, bool(is_error)) for tool_use_id, text, is_error in results],
            text=None if results else text_only,
        )

    # -- PARSE / CORRECT -------------------------------------------------------------

    def correction(self, fields: dict, blocks: Sequence[Any], code: str, *, kind: str = "schema", tool: str = "") -> None:
        """A protocol fault: one fixed code back inside `tool_result{is_error}`
        for every tool_use id (or a text when there was none); the third
        consecutive one is `PROTOCOL_EXHAUSTED` via `SessionProtocolError`."""
        fault = ToolProtocolFault(code, tool=tool) if code in PROTOCOL_FAULT_CODES else None
        self.protocol_streak += 1
        self.correction_kinds[kind] = self.correction_kinds.get(kind, 0) + 1
        self._record_model(fields, "correction", tool_name=tool, outcome_code=code)
        note = getattr(self.detector, "note_correction", None)
        if callable(note):
            note()
        text = REFUSAL_TEXT.format(code=code)
        ids = [str(b.get("id") or "") for b in _tool_use_blocks(blocks)]
        self._answer(blocks, [(i, text, True) for i in ids], text_only=text)
        self._exhaust_protocol_if_due(code, fault, model_turn=fields["model_turn"])

    def _exhaust_protocol_if_due(self, code: str, fault: PolicyFault | None, *, model_turn: int) -> None:
        """R5: the third consecutive protocol fault is `PROTOCOL_EXHAUSTED`
        — a halt through `SessionProtocolError`, or under the declared
        `stage_fail` disposition a RETURNED stop with its fault record."""
        if self.protocol_streak < PROTOCOL_FAULT_LIMIT:
            return
        exc = SessionProtocolError(self.role, faults=self.protocol_streak, last_code=code)
        if fault is not None:
            exc.__cause__ = fault
        if self.limits.format_error_disposition == "stage_fail":
            # The declared, default-off alternative (R-C): the session
            # RETURNS `PROTOCOL_EXHAUSTED` with its fault record and the
            # stage fails on the empty-output route instead of halting.
            self.fault = FaultRecord.from_exception(exc, turn_index=model_turn)
            raise _Stop(TerminalState.PROTOCOL_EXHAUSTED)
        raise self._attach(exc, turn_index=model_turn)

    def _clean_step(self) -> None:
        self.protocol_streak = 0

    # -- terminals ----------------------------------------------------------------

    def _abort(self, fields: dict, blocks: Sequence[Any], block: Mapping[str, Any]) -> None:
        args = _as_mapping(block.get("input"))
        problem = validate_args(ABORT_TOOL_SCHEMA, dict(args))
        if problem is not None:
            self.correction(fields, blocks, "invalid_arguments", tool=self.policy.abort_tool)
            return
        self._clean_step()
        self.final = {"reason_code": str(args["reason_code"])}
        self._record_model(fields, "terminal", tool_name=self.policy.abort_tool, outcome_code="abstained",
                           args_sha256=sha256_hex(canonical_json(dict(args))))
        self._trace(self.policy.abort_tool, success=True, outcome_code="abstained")
        self.messages.append({"role": "assistant", "content": [_plain(b) for b in blocks]})
        raise _Stop(TerminalState.ABSTAINED)

    def _submit(self, fields: dict, blocks: Sequence[Any], block: Mapping[str, Any]) -> None:
        from elt_taskgen.review.providers import (
            normalized_text_for,
            uses_findings_schema,
            validate_payload_for,
        )

        name = self.policy.submit_tool
        args = _as_mapping(block.get("input"))
        # The anti-evasion rules outrank the SUBMIT schema too (finding
        # p4-2-1): a submitted payload naming a population or spelling a
        # locator index as an alias is a violation whatever shape it came in,
        # so a strict payload schema cannot turn the probe into a free
        # `invalid_arguments` correction.
        outranking = _outranking_argument_problem(args)
        if outranking:
            self._violation(fields, blocks, ForbiddenArgument(tool=name, detail=outranking))
        tool = self.tool_objects.get(name)
        schema = getattr(tool, "input_schema", None) if tool is not None else None
        if isinstance(schema, Mapping):
            problem = validate_args(schema, dict(args))
            if problem is not None:
                self.correction(fields, blocks, "invalid_arguments", tool=name)
                return
        normalized: str | None = None
        if uses_findings_schema(self.role):
            # PARSE of a schema role's forced submit tool: `validate_payload_for`
            # (04 §1), the validator `complete()` applies, and the SAME
            # normalized text `complete()` returns, so a zero-tool session's
            # `final` is byte-identical to the one-shot response
            # (test_session_zero_tool_turns_is_byte_identical_to_complete).
            data = json.loads(json.dumps(_plain(args)))
            problem = validate_payload_for(self.role, data)
            if problem is not None:
                self.correction(fields, blocks, "invalid_arguments", tool=name)
                return
            normalized = normalized_text_for(self.role, data)
        forbidden = _forbidden_argument(args)
        if forbidden:
            self._violation(fields, blocks, ForbiddenArgument(tool=name, detail=forbidden))
        problem = _free_text_problem(args)
        if problem:
            # Prose naming a private tree is a slip to rewrite (a correction
            # turn), never a violation: no tool takes a path.
            self.correction(fields, blocks, problem, tool=name)
            return
        self._clean_step()
        payload = dict(_plain(args))
        args_sha = sha256_hex(canonical_json(payload))
        # HARNESS_VALIDATE: the correction channel's task-aware hooks and the
        # declared validators run on every submitted payload (SoT T1.1); a
        # red one is a compile correction while the compile budget AND the
        # shared total (`SCHEMA_RETRIES`) allow, else the payload is accepted
        # as is and the certifier (or the post-session screen) decides.
        red_names: list[str] = []
        reds: list[str] = []
        for hook_name, text in self._run_payload_validators(payload, model_turn=fields["model_turn"]):
            red_names.append(hook_name)
            reds.append(text)
        validators = [v for v in self.limits.active_harness_validators if v in self.registry]
        if validators:
            # The validators see the submitted payload as the current draft.
            self.last_write_args = payload
            for validator_name in validators:
                validator = self.registry.get(validator_name)
                cap = self._per_session_cap(validator)
                if cap is not None and self.per_tool_calls.get(validator_name, 0) >= cap:
                    # A validator the harness may run at most `per_session`
                    # times (the adversary's 1-bit `measured_match_bit`:
                    # at most one call) is skipped, nothing charged.
                    continue
                if self._validator_would_exceed_oracle(validator_name):
                    # The priced validator cannot run again under
                    # `max_oracle_bits`: LIMIT_ORACLE (the last green draft,
                    # if any, auto-submits; else BLOCKED with a salt).
                    self._record_model(fields, "limit_stop", tool_name=validator_name,
                                       outcome_code="oracle_cap_exceeded", refused=True, args_sha256=args_sha)
                    self._trace(validator_name, success=False, outcome_code="oracle_cap_exceeded")
                    raise _Stop(TerminalState.LIMIT_ORACLE)
                self.per_tool_calls[validator_name] = self.per_tool_calls.get(validator_name, 0) + 1
                outcome = self._run_validator(validator_name, payload, model_turn=fields["model_turn"])
                if not outcome.ok:
                    red_names.append(validator_name)
                    reds.append(outcome.text)
        if reds and self._compile_correction_available():
            self.compile_corrections += 1
            self.correction_kinds["compile"] += 1
            self.correction_count += 1
            self._append(TurnRecord(turn_index=len(self.turns), kind="model", category="correction",
                                    tool_name=name, outcome_code="validator_red", args_sha256=args_sha, **fields))
            self._answer(blocks, [(str(block.get("id") or ""), "\n".join(reds), True)])
            return
        # Accepted as submitted. A payload still red past the correction
        # budget is NOT silently green: the red validators are recorded on
        # the result (`red_validators_at_submit`) and the terminal turn says
        # so, for the post-session screen (finding 2-3).
        outcome_code = "submitted"
        if reds:
            self.red_validators_at_submit = tuple(dict.fromkeys(red_names))
            outcome_code = VALIDATOR_RED_AT_SUBMIT_CODE
        self.final = payload if normalized is None else normalized
        self._record_model(fields, "terminal", tool_name=name, outcome_code=outcome_code, args_sha256=args_sha)
        self._trace(name, success=True, outcome_code=outcome_code)
        self.messages.append({"role": "assistant", "content": [_plain(b) for b in blocks]})
        raise _Stop(TerminalState.SUBMITTED)

    def _compile_correction_available(self) -> bool:
        """May a red validator result become a compile correction? Only while
        `max_compile_corrections` allows AND the session's total corrections
        of any kind are under `SCHEMA_RETRIES` (SoT T1.1: "schema + compile
        corrections share the 2"; roadmap Phase 3 "Correction channel").
        Past either bound the payload is accepted as submitted."""
        return (
            self.compile_corrections < self.limits.max_compile_corrections
            and self.correction_count < self._correction_total_budget()
        )

    def _correction_total_budget(self) -> int:
        """Return the shared correction budget for the session.

        Schema corrections retain at least `SCHEMA_RETRIES`; explicit compile and format
        allowances may increase the total but cannot reduce that floor.
        """
        return max(SCHEMA_RETRIES, int(self.limits.max_compile_corrections))

    def _run_payload_validators(self, payload: Mapping[str, Any], *, model_turn: int) -> list[tuple[str, str]]:
        """Run task-aware validators on a submitted payload.

        Red diagnostics are projected and gatechecked before becoming bounded compile
        corrections. Green or absent diagnostics pass. Every run is recorded as a
        validator turn; exceptions or non-diagnostic returns are harness faults. Return
        each red hook name with its rendered result.
        """
        reds: list[tuple[str, str]] = []
        if not self.payload_validators:
            return reds
        from elt_taskgen.review.tools.projection import (
            Diagnostic,
            assert_value_free,
            serialize_for_transport,
        )

        task = getattr(self.ctx, "task", None)
        if task is None:
            raise self._attach(ToolHarnessFault("payload_validators", code="no_task_for_gatekeeper"), turn_index=model_turn)
        route = getattr(self.ctx, "route", None)
        args_sha = sha256_hex(canonical_json(dict(payload)))
        for hook in self.payload_validators:
            name = _hook_name(hook)
            started = float(self.clock())
            transport = ""
            diag: Any = None
            try:
                # A copy: a hook never edits the payload the model submitted.
                diag = hook(task, json.loads(json.dumps(dict(payload))))
                if diag is not None:
                    if not isinstance(diag, Diagnostic):
                        raise ToolHarnessFault(name, code="non_projection_result", cause_type=type(diag).__name__)
                    transport = serialize_for_transport(
                        diag, task=task, package=getattr(self.ctx, "package", None), route=route
                    )
                    assert_value_free(transport.encode("utf-8"), task=task, route=route)
            except Exception as exc:  # noqa: BLE001 - classified below, recorded, re-raised
                if not isinstance(exc, SessionFault):
                    # The hook crashed (a `PolicyFault` from harness code is a
                    # wiring defect too): the class name only, text withheld.
                    exc = ToolHarnessFault.from_exception(name, exc)
                elapsed = max(0, int(round((float(self.clock()) - started) * 1000.0)))
                code = str(getattr(exc, "code", "") or "harness_fault")
                terminal = terminal_for_exception(exc)
                outcome_code = "sanitizer_tripwire" if terminal is TerminalState.LEAK_TRIPWIRE else (
                    code if _CODE_RE.fullmatch(code) else "harness_fault"
                )
                self.validator_run_count += 1
                self._append(TurnRecord(turn_index=len(self.turns), kind="validator", model_turn=model_turn,
                                        tool_name=name, args_sha256=args_sha, outcome_code=outcome_code,
                                        state_epoch=self.state_epoch, elapsed_tool_ms=elapsed, fresh=True))
                self._trace(name, success=False, outcome_code=outcome_code)
                raise self._attach(exc, turn_index=model_turn)
            elapsed = max(0, int(round((float(self.clock()) - started) * 1000.0)))
            self.tool_wall_ms += elapsed
            ok = diag is None or bool(diag.ok)
            code = "ok" if diag is None else str(diag.code)
            obs_sha = sha256_hex(transport) if transport else ""
            self.validator_run_count += 1
            self._append(TurnRecord(turn_index=len(self.turns), kind="validator", model_turn=model_turn,
                                    tool_name=name, args_sha256=args_sha, output_sha256=obs_sha,
                                    outcome_code=code, observation_fingerprint=obs_sha,
                                    state_epoch=self.state_epoch, elapsed_tool_ms=elapsed, fresh=True))
            self._trace(name, success=ok, outcome_code=code, observation_sha256=obs_sha)
            if obs_sha:
                self._check_replay_observation(name, obs_sha, model_turn=model_turn)
            if not ok:
                text, _truncated = cap_tool_output("diagnostic", diag.render(), digest=obs_sha)
                reds.append((name, text))
        return reds

    def _submit_text(self, fields: dict, blocks: Sequence[Any], text: str) -> None:
        """The submission of a ZERO-TOOL prose session (no wire tool, no submit
        tool, no model-initiated tool: the one-shot prose exchange run through
        the state machine): the text reply IS the payload, exactly what
        `complete()` returns for the same prompt (the parity gate)."""
        self._clean_step()
        self.final = text
        self._record_model(fields, "terminal", outcome_code="submitted",
                           args_sha256=sha256_hex(text))
        self._trace("text_reply", success=True, outcome_code="submitted")
        self.messages.append({"role": "assistant", "content": [_plain(b) for b in blocks]})
        raise _Stop(TerminalState.SUBMITTED)

    def _violation(self, fields: dict, blocks: Sequence[Any], fault: PolicyFault) -> None:
        """A terminal `PolicyFault`: one fixed code, a security event, the
        session halts through `SessionPolicyViolation` (no round, task not
        rejected). The call is never executed."""
        self._clean_step()
        self._record_model(fields, "terminal", tool_name=fault.tool, outcome_code=fault.code)
        if fault.security_event:
            self._security_event(fault.code, tool=fault.tool, turn_index=fields["model_turn"], detail=fault.detail)
        self._trace(fault.tool or "policy", success=False, outcome_code=fault.code)
        raise self._attach(SessionPolicyViolation(fault, role=self.role), turn_index=fields["model_turn"])

    # -- RUN_TOOL / SANITIZE / RECORD ---------------------------------------------------

    def _execute(self, tool: Any, args: Mapping[str, Any], *, kind: str, model_turn: int, action_fp: str = "") -> ToolOutcome:
        """RUN_TOOL in the worker, SANITIZE through the gatekeeper, RECORD the
        tool-side turn. Raises the harness fault (recorded first)."""
        from elt_taskgen.review.tools.projection import assert_value_free

        name = str(getattr(tool, "name", ""))
        cost = getattr(tool, "cost", None)
        deadline = float(getattr(cost, "wall_s", 0.0) or self.limits.tool_wall_seconds_default)
        is_write = bool(getattr(tool, "surface_write", False))
        fingerprint_of = getattr(self.worker, "surface_fingerprint", None)
        pre_surface = fingerprint_of(self.ctx) if (is_write and callable(fingerprint_of)) else None
        args_sha = sha256_hex(canonical_json(dict(_plain(args))))
        try:
            result = self._dispatch(tool, name, args, deadline, model_turn=model_turn)
            task = getattr(self.ctx, "task", None)
            if task is None:
                raise ToolHarnessFault(name, code="no_task_for_gatekeeper")
            assert_value_free(str(result.payload).encode("utf-8"), task=task, route=getattr(self.ctx, "route", None))
        except _Stop as stop:
            # A tool whose effective deadline was the *remaining session wall*
            # is a session limit, not a stalled-tool harness fault.  The model
            # turn was already recorded as a tool call, so retain one tool-side
            # attempt row/counter to keep the trajectory identities complete;
            # no observation or oracle bits escaped the expired call.
            if stop.terminal is not TerminalState.LIMIT_WALL:
                raise
            elapsed = int(self._last_attempt_ms)
            if kind == "validator":
                self.validator_run_count += 1
            else:
                self.tool_call_count += 1
            self._append(
                TurnRecord(
                    turn_index=len(self.turns),
                    kind=kind,
                    model_turn=model_turn,
                    tool_name=name,
                    args_sha256=args_sha,
                    outcome_code="session_wall_deadline",
                    action_fingerprint=action_fp,
                    state_epoch=self.state_epoch,
                    elapsed_tool_ms=elapsed,
                    fresh=True,
                )
            )
            self._trace(
                name, success=False, outcome_code="session_wall_deadline"
            )
            raise
        except PolicyFault as exc:
            # A model-caused refusal: charge the refusing attempt's MEASURED
            # time (never the gatekeeper or a retried attempt) to the scored
            # clock (finding 2-4).
            elapsed = int(self._last_attempt_ms)
            self.tool_wall_ms += elapsed
            if kind == "validator":
                self.validator_run_count += 1
            else:
                self.tool_call_count += 1
            self._append(TurnRecord(turn_index=len(self.turns), kind=kind, model_turn=model_turn, tool_name=name,
                                    args_sha256=args_sha, outcome_code=exc.code, action_fingerprint=action_fp,
                                    state_epoch=self.state_epoch, elapsed_tool_ms=elapsed, fresh=True))
            raise
        except Exception as exc:  # noqa: BLE001 - a harness fault, recorded then re-raised
            # A raw exception the tool/gatekeeper did not type as a diagnostic
            # is wrapped as a `ToolHarnessFault` before it leaves the runner,
            # so it never reaches the engine as an unclassified red-gate stage
            # error to be keyword-routed on its text (finding 2-1). Already
            # typed faults (a `SessionFault`, a tripwire) pass through.
            if not isinstance(exc, SessionFault):
                exc = ToolHarnessFault.from_exception(name, exc)
            # A HARNESS FAULT's time is NOT charged to the scored clock: a
            # worker death or a stalled deadline is infrastructure, never
            # candidate-attributable tool time (finding 2-4). Recorded for
            # evidence, but not summed into `tool_wall_ms`.
            elapsed = int(self._last_attempt_ms)
            code = str(getattr(exc, "code", "") or "harness_fault")
            terminal = terminal_for_exception(exc)
            outcome_code = "sanitizer_tripwire" if terminal is TerminalState.LEAK_TRIPWIRE else (
                code if _CODE_RE.fullmatch(code) else "harness_fault"
            )
            if kind == "validator":
                self.validator_run_count += 1
            else:
                self.tool_call_count += 1
            self._append(TurnRecord(turn_index=len(self.turns), kind=kind, model_turn=model_turn, tool_name=name,
                                    args_sha256=args_sha, outcome_code=outcome_code, action_fingerprint=action_fp,
                                    state_epoch=self.state_epoch, elapsed_tool_ms=elapsed, fresh=True))
            self._trace(name, success=False, outcome_code=outcome_code)
            raise self._attach(exc, turn_index=model_turn)
        # SUCCESS: charge the answering attempt's MEASURED time only, so the
        # gatekeeper and any retried/dead attempt are excluded (finding 2-4).
        elapsed = int(self._last_attempt_ms)
        self.tool_wall_ms += elapsed
        observation = result.observation
        obs_sha = sha256_hex(str(result.payload))
        ok = bool(getattr(observation, "ok", True))
        code = str(getattr(observation, "code", "") or ("ok" if ok else "failed"))
        # R4: a write that left the surface unchanged is a no-op — it does not
        # bump the epoch and counts as an error toward R2.
        post_surface = fingerprint_of(self.ctx) if (is_write and callable(fingerprint_of)) else None
        noop = is_write and (
            code == "noop" or (pre_surface is not None and post_surface is not None and pre_surface == post_surface)
        )
        if noop:
            ok, code = False, "noop"
        elif is_write and ok:
            self.detector.bump_epoch()
            self.writes += 1
            self.last_write_args = dict(_plain(args))
        kind_name = {"Diagnostic": "diagnostic", "DiagnosticText": "diagnostic_text", "DevRows": "dev_rows"}.get(
            type(observation).__name__, "diagnostic"
        )
        render = getattr(observation, "render", None)
        # A cut text carries the FULL observation digest in its marker, so the
        # next turn's memo key binds the whole observation, not just the
        # delivered prefix (a replay whose output diverged past the cap
        # re-keys instead of being served).
        text, truncated = cap_tool_output(
            kind_name, render() if callable(render) else str(result.payload), digest=obs_sha
        )
        # Oracle bits are charged for what the tool MEASURED: a code the tool
        # declared as a no-cost refusal (`cost.no_cost_codes`) reports that
        # nothing ran and charges nothing; every other outcome of a priced
        # tool — a harness validator run included (04 §5 item 5) — charges
        # its `cost.oracle_bits`.
        bits = self._oracle_bits_of(tool)
        no_cost = frozenset(getattr(cost, "no_cost_codes", ()) or ())
        charged = bits if (bits and code not in no_cost) else 0
        self.oracle_bits_used += charged
        outcome = ToolOutcome(
            ok=ok,
            code=code,
            observation=observation,
            observation_sha256=obs_sha,
            truncated=truncated,
            error_class="" if ok else "tool_expected",
            fresh=True,
            raw_output_sha256=str(getattr(result, "raw_output_sha256", "") or ""),
            text=text,
            oracle_bits_charged=charged,
        )
        if kind == "validator":
            self.validator_run_count += 1
        else:
            self.tool_call_count += 1
        self._append(TurnRecord(turn_index=len(self.turns), kind=kind, model_turn=model_turn, tool_name=name,
                                args_sha256=args_sha, output_sha256=obs_sha, outcome_code=code,
                                action_fingerprint=action_fp, observation_fingerprint=obs_sha,
                                surface_fingerprint=str(post_surface or ""), state_epoch=self.state_epoch,
                                elapsed_tool_ms=elapsed, fresh=True))
        self._trace(name, success=ok, outcome_code=code, observation_sha256=obs_sha)
        self._check_replay_observation(name, obs_sha, model_turn=model_turn)
        if action_fp:
            self.detector.observe(action_fp, obs_sha, ok=ok, code=code)
        is_validator = bool(getattr(tool, "validator", False)) or kind == "validator"
        if is_validator and ok:
            # Preserve the last validator-green draft for automatic submission
            # on a limit stop, even when the validator installed it directly.
            self.green_epoch = self.state_epoch
            draft_of = getattr(self.worker, "current_draft", None)
            draft = draft_of(self.ctx) if callable(draft_of) else None
            self.green_draft = draft if draft is not None else self.last_write_args
        return outcome

    def _check_replay_observation(self, name: str, obs_sha: str, *, model_turn: int) -> None:
        """Verify each replayed observation against its recorded full digest.

        A mismatch increments stale-result evidence and raises
        `SessionReplayMismatchError`. Extra observations are caught by the final chain
        verification.
        """
        index = self.executed_observations
        self.executed_observations += 1
        expected = self.expected_observations
        if expected is None or index >= len(expected):
            return
        if expected[index] == obs_sha:
            return
        from elt_taskgen.review.providers import SessionReplayMismatchError

        self.stale_tool_result_count += 1
        raise self._attach(
            SessionReplayMismatchError(
                name,
                detail=(
                    f"observation {index} of tool {name!r} (model turn {model_turn}) has "
                    f"digest {obs_sha[:12]}, the recording has {expected[index][:12]}"
                ),
            ),
            turn_index=model_turn,
        )

    def _dispatch(self, tool: Any, name: str, args: Mapping[str, Any], deadline: float, *, model_turn: int) -> WorkerResult:
        """Execute a tool with one bounded resume for sandbox or transient harness faults.

        Retries use a fresh worker/scratch state and do not add a turn for the failed
        attempt. A repeated or non-transient fault propagates.
        """
        def run_on(worker: Any) -> WorkerResult:
            wall = self.limits.session_wall_seconds
            session_bound = False
            attempt_deadline = float(deadline)
            if wall is not None:
                remaining = float(wall) - (float(self.clock()) - self.started)
                if remaining <= 0.0:
                    self._last_attempt_ms = 0
                    raise _Stop(TerminalState.LIMIT_WALL)
                session_bound = remaining <= attempt_deadline
                attempt_deadline = min(attempt_deadline, remaining)
            started = float(self.clock())
            try:
                result = worker.run(
                    tool,
                    self.ctx,
                    dict(args),
                    deadline_s=attempt_deadline,
                )
            except ToolDeadlineExceeded as exc:
                if session_bound:
                    raise _Stop(TerminalState.LIMIT_WALL) from exc
                raise
            except BaseException as exc:
                if (
                    wall is not None
                    and (float(self.clock()) - self.started) >= float(wall)
                ):
                    raise _Stop(TerminalState.LIMIT_WALL) from exc
                raise
            finally:
                self._last_attempt_ms = max(0, int(round((float(self.clock()) - started) * 1000.0)))
            # A protocol-conforming worker enforces ``attempt_deadline``.  This
            # postcondition also fails closed for a custom worker that returns
            # after the session wall instead of raising its deadline exception.
            if (
                wall is not None
                and (float(self.clock()) - self.started) >= float(wall)
            ):
                raise _Stop(TerminalState.LIMIT_WALL)
            return result

        try:
            return run_on(self.worker)
        except SandboxFault as exc:
            if self.sandbox_resume_used:
                raise
            self.sandbox_resume_used = True
            self._note_resume("sandbox", exc=exc, turn_index=model_turn, tool=name)
            return run_on(self._fresh_worker())
        except ToolHarnessFault as exc:
            transient = str(getattr(exc, "code", "") or "").lower() in TRANSIENT_HARNESS_CODES
            if self.harness_retry_used or not transient:
                raise
            self.harness_retry_used = True
            self._note_resume("tool", exc=exc, turn_index=model_turn, tool=name)
            self._fresh_scratch()
            return run_on(self.worker)

    def _run_validator(self, name: str, payload: Mapping[str, Any], *, model_turn: int) -> ToolOutcome:
        """A HARNESS-run validator (never model-initiated): recorded as a tool
        turn with `fresh = True`, counted in `validator_run_count`, and
        charged its `cost.oracle_bits` like any priced tool; the caller
        checks `_validator_would_exceed_oracle` before asking."""
        tool = self.registry.get(name)
        args = {} if not isinstance(payload, Mapping) else dict(payload)
        schema = getattr(tool, "input_schema", None)
        if isinstance(schema, Mapping) and validate_args(schema, args) is not None:
            args = {}
        return self._execute(tool, args, kind="validator", model_turn=model_turn)

    def _validator_would_exceed_oracle(self, name: str) -> bool:
        """Would the next run of harness validator `name` cross
        `max_oracle_bits`? Then the session ends as `LIMIT_ORACLE` (04 §5
        item 5: exhaustion ends the session) instead of running it."""
        return self._would_exceed_oracle(self._oracle_bits_of(self.registry.get(name)))

    # -- one model turn, end to end ------------------------------------------------------

    def step(self) -> None:
        self.check_limits()
        turn, blocks, fields = self.model_turn()
        turn_index = fields["model_turn"]
        stop_reason = fields["stop_reason"]
        if stop_reason in ("max_tokens", "length"):
            # OUTPUT_TRUNCATED: halt on the FIRST truncation, never a protocol fault.
            self._record_model(fields, "terminal", outcome_code="output_truncated")
            raise self._attach(OutputTruncated(self.role, turn_index=turn_index, stop_reason=stop_reason),
                               turn_index=turn_index)
        uses = _tool_use_blocks(blocks)
        if not uses:
            zero_tool_prose = not (self.policy.wire_tools or self.policy.tools or self.policy.submit_tool)
            text = "".join(
                str(b.get("text", "")) for b in blocks if isinstance(b, Mapping) and b.get("type") == "text"
            )
            if zero_tool_prose and text.strip():
                self._submit_text(fields, blocks, text)
                return
            self.correction(fields, blocks, "no_tool_call")
            return
        if len(uses) > 1:
            self.correction(fields, blocks, "multiple_tool_use")
            return
        block = uses[0]
        name = str(block.get("name") or "")
        tool_use_id = str(block.get("id") or "")
        args = _as_mapping(block.get("input"))
        if name and name == self.policy.abort_tool:
            # A policy with NO abort tool (`abort_tool=""`, the critic
            # seats: abstention is an empty findings list) never resolves
            # an unnamed call to the abort terminal.
            self._abort(fields, blocks, block)
            return
        if name and name == self.policy.submit_tool:
            self._submit(fields, blocks, block)
            return
        # The last permitted turn admits only submit or abort: anything else
        # is the turn limit binding, and the call is never executed.
        if self.policy.is_last_permitted_turn(turn_index):
            self._clean_step()
            self._record_model(fields, "limit_stop", tool_name=name, outcome_code="limit_turns", refused=True)
            raise _Stop(TerminalState.LIMIT_TURNS)
        # PERMIT
        from elt_taskgen.review.tools.registry import ToolLookupError, permit_refusal

        try:
            tool = self.registry.lookup(self.ctx, name)
        except ToolLookupError as exc:
            fault = exc.as_policy_fault()
            if isinstance(fault, ToolNotPermitted):
                self._violation(fields, blocks, fault)
            if fault is None:
                # A context built for another role: the controller's own
                # error, a harness fault, never the model's.
                self._record_model(fields, "terminal", tool_name=name, outcome_code="role_mismatch")
                raise self._attach(
                    ToolHarnessFault("registry", code="role_mismatch"), turn_index=turn_index
                )
            self.correction(fields, blocks, "unknown_tool", tool=name)
            return
        if bool(getattr(tool, "harness_only", False)):
            # A harness validator (the author's `check_prose`) is not on the
            # wire: the model naming it is an unknown-tool correction.
            self.correction(fields, blocks, "unknown_tool", tool=name)
            return
        # Parse before permit, except anti-evasion rules for fixed populations
        # and canonical locator indexes. Check every argument so schema errors
        # cannot hide a policy violation; violations never spawn a worker.
        outranking = _outranking_argument_problem(args)
        if outranking:
            self._violation(fields, blocks, ForbiddenArgument(tool=name, detail=outranking))
        forbidden = _forbidden_argument(args)
        schema = getattr(tool, "input_schema", None)
        if isinstance(schema, Mapping):
            problem = validate_args(schema, dict(args))
            if problem is not None:
                self.correction(fields, blocks, "invalid_arguments", tool=name)
                return
        if forbidden:
            self._violation(fields, blocks, ForbiddenArgument(tool=name, detail=forbidden))
        problem = _free_text_problem(args)
        if problem:
            # A free-text argument (an edit's `new`, a rationale) naming a
            # private tree is a correction, never a terminal violation.
            self.correction(fields, blocks, problem, tool=name)
            return
        # A well-formed call is a clean step for R5 — unless the worker then
        # types it as a protocol fault, which continues the streak it found.
        streak_before = self.protocol_streak
        self._clean_step()
        args_sha = sha256_hex(canonical_json(dict(_plain(args))))
        # CHECK_LIMITS, the tool-call half: this turn was offered submit and
        # abort only (`max_tool_calls` spent, or declared 0 — a hard zero);
        # a model-initiated call on it is the cap binding, never executed.
        if self.tool_calls_exhausted:
            self._record_model(fields, "limit_stop", tool_name=name, outcome_code="limit_tool_calls",
                               refused=True, args_sha256=args_sha)
            self._trace(name, success=False, outcome_code="limit_tool_calls")
            raise _Stop(TerminalState.LIMIT_TOOL_CALLS)
        # PERMIT: the per-tool ceiling and the write cap first (refusals at
        # no cost, no worker spawned, counted toward max_tool_calls: a call
        # the ceiling denies charges nothing, so it never trips the oracle
        # cap), then the oracle-bit accounting (a limit) for a call that
        # WOULD run.
        cost = getattr(tool, "cost", None)
        cap = self._per_session_cap(tool)
        if cap is not None and self.per_tool_calls.get(name, 0) >= cap:
            self._refuse(fields, blocks, name, args, tool_use_id, "per_tool_cap")
            return
        max_writes = self.limits.max_writes
        if max_writes is not None and bool(getattr(tool, "surface_write", False)) and self.writes >= max_writes:
            self._refuse(fields, blocks, name, args, tool_use_id, "max_writes")
            return
        bits = self._oracle_bits_of(tool)
        if self._would_exceed_oracle(bits):
            self._record_model(fields, "limit_stop", tool_name=name, outcome_code="oracle_cap_exceeded", refused=True)
            self._trace(name, success=False, outcome_code="oracle_cap_exceeded")
            raise _Stop(TerminalState.LIMIT_ORACLE)
        # STUCK_CHECK, before dispatch: a flagged repeat is never executed.
        action_fp = self.detector.fingerprint(name, args)
        verdict = self.detector.check(action_fp, idempotent_read=bool(getattr(cost, "idempotent_read", False)))
        if verdict == "nudge":
            self._record_model(fields, "nudge", tool_name=name, outcome_code="repeated_call", refused=True)
            self._append(TurnRecord(turn_index=len(self.turns), kind="nudge", model_turn=turn_index, tool_name=name,
                                    args_sha256=args_sha, outcome_code="repeated_call",
                                    refused=True, action_fingerprint=action_fp, state_epoch=self.state_epoch,
                                    output_sha256=sha256_hex(self.policy.nudge_text)))
            self._trace(name, success=False, outcome_code="repeated_call")
            self._answer(blocks, [(tool_use_id, self.policy.nudge_text, True)])
            return
        if verdict == "halt":
            self._record_model(fields, "limit_stop", tool_name=name, outcome_code="stuck_in_a_loop", refused=True)
            raise _Stop(TerminalState.STUCK)
        # The tool's own PERMIT-time cheap refusal (`Tool.permit`, a code it
        # declared in `cost.no_cost_codes`): answered with the fixed refusal
        # text, no bits charged, no worker spawned; it counts toward
        # max_tool_calls and, as an observed error, toward R2.
        try:
            refusal = permit_refusal(tool, self.ctx, args)
        except SessionFault as exc:
            self._record_model(fields, "terminal", tool_name=name,
                               outcome_code=str(getattr(exc, "code", "") or "harness_fault"))
            self._trace(name, success=False, outcome_code=str(getattr(exc, "code", "") or "harness_fault"))
            raise self._attach(exc, turn_index=turn_index)
        if refusal:
            self._refuse(fields, blocks, name, args, tool_use_id, refusal, action_fp=action_fp, observe=True)
            return
        # RUN_TOOL -> SANITIZE -> RECORD; the bits are charged by the outcome
        # (`oracle_bits_charged`), never for a call that did not run.
        self.per_tool_calls[name] = self.per_tool_calls.get(name, 0) + 1
        self._record_model(fields, "tool_call", tool_name=name, action_fingerprint=action_fp, args_sha256=args_sha)
        try:
            outcome = self._execute(tool, args, kind="tool", model_turn=turn_index, action_fp=action_fp)
        except PolicyFault as exc:
            if exc.security_event:
                self._security_event(exc.code, tool=exc.tool or name, turn_index=turn_index, detail=exc.detail)
            self._trace(name, success=False, outcome_code=exc.code)
            if isinstance(exc, OracleCapExceeded):
                raise _Stop(TerminalState.LIMIT_ORACLE)
            if not exc.ends_session:
                # A protocol fault the worker typed AFTER PERMIT (a bad
                # reason code, a second artifact): answered as one fixed
                # code and counted toward R5's three-fault streak exactly
                # like a dispatcher correction (SoT T6 ToolProtocolFault row).
                self.protocol_streak = streak_before + 1
                note = getattr(self.detector, "note_correction", None)
                if callable(note):
                    note()
                self._answer(blocks, [(tool_use_id, REFUSAL_TEXT.format(code=exc.code), True)])
                self._exhaust_protocol_if_due(exc.code, exc, model_turn=turn_index)
                return
            raise self._attach(SessionPolicyViolation(exc, role=self.role), turn_index=turn_index)
        texts = [outcome.text]
        # Auto-run validators a write declares (`auto_validators`): harness
        # runs, fresh, folded into the same tool_result — each charged its
        # oracle bits, so one the cap cannot cover ends the session.
        for validator_name in tuple(getattr(tool, "auto_validators", ()) or ()):
            if validator_name in self.registry and outcome.ok:
                if self._validator_would_exceed_oracle(validator_name):
                    self._trace(validator_name, success=False, outcome_code="oracle_cap_exceeded")
                    raise _Stop(TerminalState.LIMIT_ORACLE)
                validated = self._run_validator(validator_name, self.last_write_args or {}, model_turn=turn_index)
                texts.append(validated.text)
        self._answer(blocks, [(tool_use_id, "\n".join(texts), not outcome.ok)])

    def _refuse(
        self,
        fields: dict,
        blocks: Sequence[Any],
        name: str,
        args: Mapping[str, Any],
        tool_use_id: str,
        code: str,
        *,
        action_fp: str = "",
        observe: bool = False,
    ) -> None:
        """REFUSE -> RECORD (04 §1): the call is answered with the fixed
        refusal text under `code`, recorded as a refused model turn plus a
        refused tool-side turn (it counts toward `max_tool_calls`), no bits
        charged, no worker spawned. `observe` feeds it to the detector as an
        error so a cheap refusal repeated verbatim counts toward R2."""
        text = REFUSAL_TEXT.format(code=code)
        text_sha = sha256_hex(text)
        args_sha = sha256_hex(canonical_json(dict(_plain(args))))
        self._record_model(fields, "refused", tool_name=name, outcome_code=code, refused=True,
                           args_sha256=args_sha, action_fingerprint=action_fp)
        self._append(TurnRecord(turn_index=len(self.turns), kind="refused", model_turn=int(fields["model_turn"]),
                                tool_name=name, args_sha256=args_sha, outcome_code=code, refused=True,
                                action_fingerprint=action_fp, state_epoch=self.state_epoch,
                                output_sha256=text_sha, observation_fingerprint=text_sha if observe else ""))
        self._trace(name, success=False, outcome_code=code)
        if observe and action_fp:
            self.detector.observe(action_fp, text_sha, ok=False, code=code)
        self._answer(blocks, [(tool_use_id, text, True)])

    # -- the loop ----------------------------------------------------------------------------

    def run(self) -> SessionResult:
        try:
            self.init_reserve()
        except Exception as exc:  # noqa: BLE001 - INIT refusals are provider faults
            raise self._attach(exc, turn_index=-1)
        while True:
            try:
                self.step()
            except _Stop as stop:
                self.terminal = stop.terminal
                if stop.scope:
                    self.limit_scope = stop.scope
                break
            except PolicyFault as exc:
                # Backstop PolicyFaults raised outside the model-tool guard.
                # Classify them by their attached vocabulary so every trial gets
                # a SessionPolicyViolation and session result.
                if isinstance(exc, OracleCapExceeded):
                    # A LIMIT, never a violation (SoT T4): the last green
                    # draft auto-submits exactly as for the guarded path.
                    self.terminal = TerminalState.LIMIT_ORACLE
                    break
                if exc.security_event:
                    self._security_event(
                        exc.code,
                        tool=exc.tool,
                        turn_index=max(0, self.model_call_count - 1),
                        detail=exc.detail,
                    )
                self._trace(exc.tool or "policy", success=False, outcome_code=exc.code)
                raise self._attach(
                    SessionPolicyViolation(exc, role=self.role),
                    turn_index=max(0, self.model_call_count - 1),
                ) from exc
        # DISPOSE (SoT T4; 04 §2): a limit stop auto-submits the LAST
        # validator-green draft when one exists — an edit after that green
        # run does not demote it, it is still the last green one and a
        # legitimate proposal the certifier decides — else the result waits
        # (`blocked_on = session_limit:<kind>`).
        if self.terminal.auto_submits and self.green_draft is not None:
            self.final = self.green_draft
            self.auto_submitted = True
        return self.result()


def run_bounded_session(
    role: str,
    initial_view: str,
    tools: Sequence[Any],
    policy: SessionPolicy,
    limits: SessionLimits,
    *,
    provider: Any,
    ctx: Any,
    worker: Any,
    detector: Any = None,
    clock: Callable[[], float] = time.monotonic,
    expected_observations: Sequence[str] | None = None,
    payload_validators: Sequence[ValidatorHook] = (),
) -> SessionResult:
    """Run a bounded session and return its typed result.

    The runner obtains one provider turn at a time, permits and de-duplicates calls
    before dispatch, executes tools through the worker, validates submissions, and
    chains every recorded turn. Normal stops return, with limit stops auto-submitting
    the last validator-green draft when available. Protocol, policy, truncation,
    tripwire, worker, provider, and replay faults raise with the partial `SessionResult`
    attached.
    """
    from elt_taskgen.review.loopguard import StuckDetector

    if detector is None:
        detector = StuckDetector.from_policy(policy.stuck_thresholds, cycles_enabled=False)
    if worker is None:
        worker = InterruptibleValidatorWorker()
    session = _Session(
        role, initial_view, tools, policy, limits,
        provider=provider, ctx=ctx, worker=worker, detector=detector, clock=clock,
        expected_observations=expected_observations,
        payload_validators=payload_validators,
    )
    return session.run()


def __getattr__(name: str) -> Any:
    # `DiagnosticTripwire` is defined beside the sanitizer it guards
    # (review/tools/projection.py, which imports SessionFault from here);
    # `LeakTripwire` is an alias binding of that one class.
    if name in ("DiagnosticTripwire", "LeakTripwire"):
        from elt_taskgen.review.tools.projection import DiagnosticTripwire

        return DiagnosticTripwire
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

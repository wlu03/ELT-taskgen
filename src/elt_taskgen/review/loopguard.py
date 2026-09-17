"""Detect repeated actions before bounded-session dispatch.

Actions include the current state epoch. The third repeated action/observation or error
proposal is nudged, and the fourth halts; enabled cycle detection catches cycles of
length at most five repeated five times. Idempotent reads are exempt, no-op writes count
as errors, corrections do not affect streaks, and each session receives at most one
nudge.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from elt_taskgen.models import canonical_json, sha256_hex
from elt_taskgen.review.session import STUCK_DETECTOR_THRESHOLDS

__all__ = [
    "PAIR_WINDOW",
    "RING_SIZE",
    "DetectorEvent",
    "StuckDetector",
    "StuckThresholds",
    "StuckVerdict",
    "action_fingerprint",
]

#: The 25-entry action ring of R3 and the 8-pair window of R1/R2 (SoT T5).
RING_SIZE = 25
PAIR_WINDOW = 8

#: R3: any k-cycle with k <= this, repeated this many times in the ring.
_CYCLE_MAX_K = 5
_CYCLE_REPEATS = 5

StuckVerdict = Literal["proceed", "nudge", "halt"]


def action_fingerprint(tool_name: str, args: Mapping[str, Any], state_epoch: int) -> str:
    """`fp_action`: canonical key ordering, so argument order cannot evade
    R1 (the documented deviation from Gemini CLI's raw JSON)."""
    return sha256_hex(f"{tool_name}:{canonical_json(dict(args))}:{int(state_epoch)}")


@dataclass(frozen=True)
class StuckThresholds:
    """The SoT T5 thresholds as one frozen object (from the policy's
    `stuck_thresholds` mapping, which `policy_sha256` already digests)."""

    identical_pairs_nudge: int = 3
    identical_pairs_halt: int = 4
    error_streak_nudge: int = 3
    error_streak_halt: int = 4
    format_streak_halt: int = 3

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, int] | None) -> "StuckThresholds":
        mapping = dict(mapping or STUCK_DETECTOR_THRESHOLDS)
        return cls(
            identical_pairs_nudge=int(mapping.get("identical_pairs_nudge", 3)),
            identical_pairs_halt=int(mapping.get("identical_pairs_halt", 4)),
            error_streak_nudge=int(mapping.get("error_streak_nudge", 3)),
            error_streak_halt=int(mapping.get("error_streak_halt", 4)),
            format_streak_halt=int(mapping.get("format_streak_halt", 3)),
        )

    def __post_init__(self) -> None:
        for name in ("identical_pairs", "error_streak"):
            nudge = getattr(self, f"{name}_nudge")
            halt = getattr(self, f"{name}_halt")
            if nudge < 2 or halt <= nudge:
                raise ValueError(f"{name}: nudge must be >= 2 and halt > nudge")
        if self.format_streak_halt < 1:
            raise ValueError("format_streak_halt must be >= 1")


@dataclass(frozen=True)
class DetectorEvent:
    """One detector decision, recorded in `SessionResult.detector_events`:
    the rule (`r1`, `r2`, `r3`), the verdict, the attempt ordinal that
    triggered it and the action fingerprint. No arguments, no observation."""

    rule: str
    verdict: str
    attempt: int
    action_fingerprint: str
    state_epoch: int


@dataclass
class _Pair:
    action: str
    observation: str
    ok: bool
    code: str


@dataclass
class StuckDetector:
    """Per-session detector state; one instance per session, never shared."""

    thresholds: StuckThresholds = field(default_factory=StuckThresholds)
    #: R3 is POL-only (SoT T5): every council role runs with cycles disabled.
    cycles_enabled: bool = False
    state_epoch: int = 0
    nudge_count: int = 0
    events: list[DetectorEvent] = field(default_factory=list)
    _pairs: deque = field(default_factory=lambda: deque(maxlen=PAIR_WINDOW), repr=False)
    _ring: deque = field(default_factory=lambda: deque(maxlen=RING_SIZE), repr=False)
    #: Un-executed repeats already answered with the nudge, per streak: the
    #: attempt ordinal a proposal reaches is executed pairs + these + 1.
    _refused_repeats: int = 0
    _refused_action: str = ""

    @classmethod
    def from_policy(cls, thresholds: Mapping[str, int] | None, *, cycles_enabled: bool = False) -> "StuckDetector":
        return cls(thresholds=StuckThresholds.from_mapping(thresholds), cycles_enabled=cycles_enabled)

    # -- state ---------------------------------------------------------------

    def fingerprint(self, tool_name: str, args: Mapping[str, Any]) -> str:
        return action_fingerprint(tool_name, args, self.state_epoch)

    def bump_epoch(self) -> int:
        """A successful surface-changing write happened: every later
        fingerprint differs, so `validate` after `edit` is never a repeat."""
        self.state_epoch += 1
        return self.state_epoch

    def note_correction(self) -> None:
        """A CORRECT turn: excluded from R1-R3 — neither extends nor resets."""
        return None

    # -- the check (before dispatch) -----------------------------------------

    def _trailing(self, action: str) -> tuple[int, int]:
        """(identical (action, observation) pairs, same-action error streak)
        at the tail of the pair window for `action`."""
        pairs = list(self._pairs)
        identical = 0
        last_obs: str | None = None
        for pair in reversed(pairs):
            if pair.action != action:
                break
            if last_obs is None:
                last_obs = pair.observation
            if pair.observation != last_obs:
                break
            identical += 1
        errors = 0
        last_code: str | None = None
        for pair in reversed(pairs):
            if pair.action != action or pair.ok:
                break
            if last_code is None:
                last_code = pair.code
            if pair.code != last_code:
                break
            errors += 1
        return identical, errors

    def _cycle_repeats(self, action: str) -> bool:
        """R3: with `action` appended, does the ring end in some k-cycle
        (k <= 5) repeated 5 times?"""
        ring = list(self._ring) + [action]
        for k in range(1, _CYCLE_MAX_K + 1):
            span = k * _CYCLE_REPEATS
            if len(ring) < span:
                continue
            tail = ring[-span:]
            period = tail[:k]
            if all(tail[i] == period[i % k] for i in range(span)):
                return True
        return False

    def check(
        self,
        action: str,
        *,
        idempotent_read: bool = False,
    ) -> StuckVerdict:
        """STUCK_CHECK on the proposed call's `fp_action`. `nudge` means the
        call is NOT executed and the fixed nudge answers it; `halt` means the
        session ends as `STUCK`. Never raises. Only an EXECUTED step resets
        a streak (`observe`); a proposal that is itself refused does not."""
        identical, errors = self._trailing(action)
        refused = self._refused_repeats if self._refused_action == action else 0
        rule = ""
        attempt = 0
        # Apply R2 before R1 when identical repeats are also errors. Thresholds
        # count executed pairs, not nudges; only R1/R3 exempt idempotent reads.
        need_errors = self.thresholds.error_streak_nudge - 1
        if errors >= need_errors and errors + refused + 1 >= self.thresholds.error_streak_nudge:
            rule, attempt = "r2", errors + refused + 1
        need_pairs = self.thresholds.identical_pairs_nudge - 1
        if (
            not rule
            and not idempotent_read
            and identical >= need_pairs
            and identical + refused + 1 >= self.thresholds.identical_pairs_nudge
        ):
            rule, attempt = "r1", identical + refused + 1
        if not rule and self.cycles_enabled and not idempotent_read and self._cycle_repeats(action):
            rule, attempt = "r3", refused + 1
        if not rule:
            return "proceed"
        halt_at = (
            self.thresholds.identical_pairs_halt if rule == "r1"
            else self.thresholds.error_streak_halt if rule == "r2"
            else 2
        )
        # One nudge per session, across rules: a second trigger of ANY rule
        # after the nudge halts, as does reaching the rule's halt ordinal.
        if self.nudge_count >= 1 or attempt >= halt_at:
            verdict: StuckVerdict = "halt"
        else:
            verdict = "nudge"
            self.nudge_count += 1
        if verdict == "nudge":
            self._refused_action = action
            self._refused_repeats += 1
        self.events.append(
            DetectorEvent(
                rule=rule,
                verdict=verdict,
                attempt=attempt,
                action_fingerprint=action,
                state_epoch=self.state_epoch,
            )
        )
        return verdict

    # -- the observation (after dispatch) ------------------------------------

    def observe(self, action: str, observation_sha256: str, *, ok: bool, code: str = "") -> None:
        """An EXECUTED call and its sanitized observation. A no-op write
        arrives as `ok=False, code="noop"` (R4) and counts toward R2."""
        pair = _Pair(action=str(action), observation=str(observation_sha256), ok=bool(ok), code=str(code))
        if self._pairs and self._pairs[-1].action != pair.action:
            # A non-matching clean step resets the refused-repeat streak too.
            self._refused_repeats = 0
            self._refused_action = ""
        self._pairs.append(pair)
        self._ring.append(pair.action)

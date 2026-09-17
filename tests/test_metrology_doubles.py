"""Phase 4 metrology test doubles (roadmap §7 "Doubles"; metrology redesign §13).

Every double here is an AGENTIC seat, mirroring the one-shot instruments of
tests/test_council_efficacy.py (`OracleProvider`, `StochasticSeatProvider`,
`OrderReplayProvider`, `BoilerplateProvider`, `ClassEchoProvider`): its
`complete(role, view)` is ONE trajectory — the model turn(s), for the two
harness-validated seats (SoT T1.1: the population adversary's
`compile_proposal`, the shortcut attacker's `compile_probe`) the validator run
through the per-trial `ToolExecutor` its `begin_trial` built, and ONE SoT T8
evidence row — so `run_metrology` is exercised exactly as it exercises
`RoutedProvider`, at zero spend, with no network and no live model call.

What each proves (the roadmap's "Doubles" row, and the Isolation half that
needs an executor):

  OracleAgentProvider             a diligent validated seat is ADMITTED: recall
                                  1.0, one row per (trial, seat), the count
                                  invariants hold, every result fresh
  StochasticAgentSeatProvider     the MeasurementPowerTest operating
                                  characteristic is unchanged under the
                                  trajectory protocol
  OrderReplayAgentProvider,       BLOCKED on every seeded draw with
  ToolMemoryReplayProvider        `cache_hits == 0`; trial nonces distinct
  ToolEchoProvider                fails nitpick and precision
  FloodingToolProvider,           BLOCK; high wasted-call ratio
  BoilerplateAgentProvider
  StuckAgentProvider              one nudge, then STUCK: scored as a miss
  BudgetBurnerProvider            LIMIT_*: scored as no findings
  PolicyProbeProvider             1 of 35 passes the 0.10 bar, 2 of 35 blocks
  PrivateSniffingProvider,        tombstone `private_probe`, and
  ExposureCanaryOracle            `private_exposure` when a canary hits too
  LeakProbeExecutor               `DiagnosticTripwire` BEFORE transport: exit
                                  2, nothing written or revoked
  CrashingToolExecutor            `ToolHarnessFault`: exit 2
  CorrectionExhaustedOracle-      one trial's proposal still red after the
  Provider                        bounded correction: VOIDED post-session,
                                  `compile_correction_exhausted` on the row,
                                  the trial scored, the run ADMITTED (D4)

Harness instruments only — none of these is a shipping provider.
"""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from elt_taskgen import cli
from elt_taskgen.models import CouncilRole, PopulationName, sha256_hex
from elt_taskgen.review import metrology as metrology_mod
from elt_taskgen.review import providers as providers_mod
from elt_taskgen.review.council import CRITIC_ROLES
from elt_taskgen.review.session import SessionFault, ToolHarnessFault, ToolOutcome
from elt_taskgen.review.tools import critic_validators
from elt_taskgen.review.tools.projection import (
    DiagnosticTripwire,
    assert_value_free,
    serialize_for_transport,
)

try:
    from tests import test_council_efficacy as efficacy
except ImportError:  # run from inside tests/
    import test_council_efficacy as efficacy  # type: ignore[no-redef]

OracleProvider = efficacy.OracleProvider
BoilerplateProvider = efficacy.BoilerplateProvider
ClassEchoProvider = efficacy.ClassEchoProvider
StochasticSeatProvider = efficacy.StochasticSeatProvider
_finding = efficacy._finding
_fresh_live_exchange_evidence = efficacy._fresh_live_exchange_evidence
_canary_view_map = efficacy._canary_view_map
_SEEDS = efficacy._SEEDS
_TRIALS_PER_SEAT = efficacy._TRIALS_PER_SEAT

POP = CouncilRole.POPULATION_ADVERSARY
SHC = CouncilRole.SHORTCUT_ATTACKER

#: The harness validator each Phase-4 seat runs on every submitted payload.
VALIDATOR_BY_ROLE = {
    POP: critic_validators.COMPILE_PROPOSAL_TOOL,
    SHC: critic_validators.COMPILE_PROBE_TOOL,
}

#: A private fragment a leaking producer might put into a projection.
_PRIVATE_FRAGMENT = "zq_private_fragment_7f3a"

_ZERO_USAGE = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

_ABS_PATH_RE = re.compile(r"(?:^|[\s\"'=:(])/(?:private|var|tmp|Users|home|opt)/")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}|\b1[6-9]\d{8}\b")


# ---------------------------------------------------------------------------
# Executors (the roadmap `ToolExecutor` protocol)
# ---------------------------------------------------------------------------


class RecordingExecutor:
    """The per-trial `ToolExecutor` double: runs ONE critic validator against
    the trial's context (a `CriticSession` over the trial's task, the trial
    workspace as its root), holds NO cache (`cache is None`), answers
    `fresh=True`, records every run, refuses an identifier-typed argument
    naming a private surface or another trial's nonce as a `private_probe`,
    and refuses to run at all once its trial is closed."""

    def __init__(self, *, foreign_nonces=()):
        self.cache = None
        self.foreign_nonces = tuple(foreign_nonces)
        self.runs: list[dict] = []
        self.closed = False
        self.private_probes = 0

    def execute(self, role: str, name: str, args, ctx) -> ToolOutcome:
        if self.closed:
            raise ToolHarnessFault(name, code="executor_closed")
        probes = metrology_mod.private_probe_tokens(args, foreign_nonces=self.foreign_nonces)
        if probes:
            self.private_probes += probes
            outcome = ToolOutcome(
                ok=False, code="private_probe", observation=None, observation_sha256="",
                error_class="policy", fresh=True, text="refused: private_probe",
            )
            self.runs.append({"role": role, "name": name, "code": outcome.code, "refused": True,
                              "fresh": True, "text": outcome.text})
            return outcome
        session = critic_validators.CriticSession(task=ctx.task, role=role)
        diagnostic = critic_validators.critic_validator(name).run(session.context(ctx.workspace), args)
        # The D1 gatekeeper: nothing leaves the harness unless it is value-free.
        payload = serialize_for_transport(diagnostic, task=ctx.task)
        assert_value_free(payload.encode("utf-8"), task=ctx.task)
        outcome = ToolOutcome(
            ok=diagnostic.ok, code=diagnostic.code, observation=diagnostic,
            observation_sha256=diagnostic.sha256, fresh=True, text=diagnostic.render(),
        )
        self.runs.append({"role": role, "name": name, "code": outcome.code, "refused": False,
                          "fresh": outcome.fresh, "text": outcome.text,
                          "observation_sha256": outcome.observation_sha256, "payload": payload})
        return outcome


class LeakProbeExecutor(RecordingExecutor):
    """A validator whose projection carries a private fragment: the value-free
    gatekeeper trips BEFORE anything is sent (`DiagnosticTripwire`, a
    `SessionFault` at the sanitizer boundary), so no transport body ever
    carries the fragment."""

    def execute(self, role: str, name: str, args, ctx) -> ToolOutcome:
        raise DiagnosticTripwire(
            "assert_value_free", "private_fragment", source="compile",
            quarantined=_PRIVATE_FRAGMENT.encode("utf-8"),
        )


class CrashingToolExecutor(RecordingExecutor):
    """A validator that crashes: `ToolHarnessFault`, the tool's output withheld."""

    def execute(self, role: str, name: str, args, ctx) -> ToolOutcome:
        raise ToolHarnessFault(name, cause_type="RuntimeError")


# ---------------------------------------------------------------------------
# The agentic seat base
# ---------------------------------------------------------------------------


class AgentSeatProvider:
    """Base of the agentic doubles. `begin_trial(ctx)` builds the trial's
    executor with `cache=None`; `end_trial()` closes it; `complete(role,
    view)` is one trajectory: the payload `answer()` decides, the validator
    run through the executor for a harness-validated seat, an optional
    correction turn (whose body a real runner would send: recorded in
    `transport_bodies`), and ONE SoT T8 evidence row that `trajectory()` may
    override (a limit stop, a refusal, a nudge)."""

    provider_name = "offline-agent-double"
    model_name = "offline-agent-model"
    executor_factory = RecordingExecutor

    def __init__(self, *, executor_factory=None, routing=None):
        if executor_factory is not None:
            self.executor_factory = executor_factory
        self.routing = routing
        self.replay_only = False
        self.refresh = True
        self.agents_config = None
        self.task_id = "council-metrology"
        self.exchange_evidence: list[dict] = []
        self.ctx = None
        self.executor = None
        self.executors: list = []
        self.nonces: list[str] = []
        self.transport_bodies: list[str] = []
        self.calls = 0

    # -- the trial seam -------------------------------------------------------

    def begin_trial(self, ctx) -> None:
        if self.ctx is not None:
            raise AssertionError("begin_trial while a trial is open")
        self.ctx = ctx
        self.executor = self.executor_factory(foreign_nonces=tuple(self.nonces))
        self.nonces.append(ctx.trial_nonce)
        self.executors.append(self.executor)

    def end_trial(self) -> None:
        if self.executor is not None:
            self.executor.closed = True
        self.executor = None
        self.ctx = None

    # -- what a subclass decides ----------------------------------------------

    def answer(self, role: CouncilRole, prompt: str) -> dict:
        raise NotImplementedError

    def before_validator(self, role: CouncilRole, prompt: str, payload: dict) -> dict:
        """Extra counters a subclass adds before the validator run (a probe,
        a refusal): row overrides merged into the row."""
        return {}

    def trajectory(self, role: CouncilRole, prompt: str, payload: dict, outcome) -> dict:
        """Row overrides after the validator run (a limit stop, a nudge)."""
        return {}

    # -- the trajectory -------------------------------------------------------

    def _run_validator(self, role: CouncilRole, args) -> ToolOutcome | None:
        name = VALIDATOR_BY_ROLE.get(role)
        if name is None or self.executor is None:
            return None
        return self.executor.execute(role.value, name, args, self.ctx)

    def complete(self, role: CouncilRole, prompt: str) -> str:
        self.calls += 1
        payload = self.answer(role, prompt)
        extra = self.before_validator(role, prompt, payload)
        outcome = self._run_validator(role, payload)
        validator_runs = 0
        codes: list[str] = []
        stale = 0
        corrections = 0
        if outcome is not None:
            validator_runs = 1
            codes.append(outcome.code)
            if not outcome.fresh:
                stale += 1
            if not outcome.ok and outcome.code != "private_probe":
                # A red compile: the correction turn a runner would send.
                self.transport_bodies.append(outcome.text)
                corrections = 1
        refused = int(extra.get("refused_count", 0))
        probes = int(extra.get("private_probe_count", 0))
        text = json.dumps(payload)
        findings = payload.get("findings") or []
        ctx = self.ctx
        model_calls = refused + corrections + 1
        row = {
            "task_id": ctx.task_id if ctx is not None else "",
            "task_content_hash": ctx.task_content_hash if ctx is not None else "",
            "role": role.value,
            "trial_nonce": ctx.trial_nonce if ctx is not None else "",
            "trial_index": ctx.trial_index if ctx is not None else -1,
            "prompt_sha256": sha256_hex(prompt),
            "response_sha256": sha256_hex(text),
            "attempt_count": model_calls,
            "model_call_count": model_calls,
            "tool_call_count": 0,
            "refused_count": refused,
            "nudge_count": 0,
            "validator_run_count": validator_runs,
            "correction_count": corrections,
            "correction_kinds": {"schema": 0, "compile": corrections},
            "terminal": "SUBMITTED",
            "terminal_count": 1,
            "limit_stop_count": 0,
            "live_model_call_count": model_calls,
            "stale_tool_result_count": stale,
            "private_probe_count": probes,
            "duplicate_tool_calls": 0,
            "replayed": False,
            "trajectory_sha256": sha256_hex(
                f"{ctx.trial_nonce if ctx is not None else ''}|{role.value}|{self.calls}"
            ),
            "codes": codes,
            "entry_schema": getattr(providers_mod, "SESSION_TRANSCRIPT_ENTRY_SCHEMA", 3),
            "usage": dict(_ZERO_USAGE),
            "usd": 0.0,
            "wall_ms": 0,
            "finding_count": len(findings),
            "zero_findings": not findings,
            "provider": self.provider_name,
            "model": self.model_name,
        }
        row.update(self.trajectory(role, prompt, payload, outcome))
        self.exchange_evidence.append(row)
        return text


# ---------------------------------------------------------------------------
# The doubles
# ---------------------------------------------------------------------------


#: Executable blindness case for a major adversary finding: hard-code the
#: development output across populations so the promoter can measure it.
_EXECUTABLE_BLINDNESS_CASE = {
    "kind": "constants",
    "params": json.dumps({"hardcode_population": "development"}),
    "expected_pass_by_stage": {
        "extract_load": {p.value: True for p in PopulationName},
        "transform": {p.value: (p is PopulationName.DEVELOPMENT) for p in PopulationName},
    },
    "rationale": (
        "Hard-coding the development split's outputs is the executable form of "
        "this blindness claim: development is reproduced verbatim and every "
        "hidden population must defeat it."
    ),
}


def _with_executable_proposals(role: CouncilRole, payload: dict) -> dict:
    """The diligent adversary's payload under the executable contract: every
    MAJOR/FATAL population-adversary finding without a `proposed_case` gets
    `_EXECUTABLE_BLINDNESS_CASE`; other roles and severities are untouched."""
    if role is not POP:
        return payload
    findings = []
    for finding in payload.get("findings") or []:
        item = dict(finding)
        if item.get("severity") in ("major", "fatal") and item.get("proposed_case") is None:
            item["proposed_case"] = json.loads(json.dumps(_EXECUTABLE_BLINDNESS_CASE))
        findings.append(item)
    return {**payload, "findings": findings}


class OracleAgentProvider(AgentSeatProvider):
    """A diligent validated seat: the oracle's payload, whose first
    submission compiles (its major blindness claims carry the executable
    case the pre-flight demands)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._oracle = OracleProvider()

    def answer(self, role, prompt):
        return _with_executable_proposals(role, json.loads(self._oracle.complete(role, prompt)))


class StochasticAgentSeatProvider(AgentSeatProvider):
    """A seat with a KNOWN per-trial hit rate `p` under the trajectory
    protocol: the one-shot `StochasticSeatProvider`'s draws, one per call."""

    def __init__(self, p: float, rng: random.Random, **kw):
        super().__init__(**kw)
        self._inner = StochasticSeatProvider(p, rng)

    def answer(self, role, prompt):
        return json.loads(self._inner.complete(role, prompt))


class OrderReplayAgentProvider(AgentSeatProvider):
    """The order-aware canned replayer (`OrderReplayProvider`) as an agentic
    seat, with a per-trial MEMORY keyed on the trial nonce: it never reads
    the view, replays the oracle's answer for call i of a KNOWN schedule,
    and counts every memory hit."""

    def __init__(self, schedule, trials=None, **kw):
        super().__init__(**kw)
        self._inner = efficacy.OrderReplayProvider(schedule, trials)
        self.memory: dict = {}
        self.cache_hits = 0

    def answer(self, role, prompt):
        key = (role.value, self.ctx.trial_nonce if self.ctx is not None else "")
        if key in self.memory:
            self.cache_hits += 1
            return self.memory[key]
        payload = json.loads(self._inner.complete(role, prompt))
        self.memory[key] = payload
        return payload


class ToolMemoryReplayProvider(OrderReplayAgentProvider):
    """The order replayer that also remembers VALIDATOR OBSERVATIONS by trial
    (the channel a shared cache would open): a remembered observation would
    be served `fresh=False`; with nonce-only workspaces no trial repeats."""

    def __init__(self, schedule, trials=None, **kw):
        super().__init__(schedule, trials, **kw)
        self.observations: dict = {}

    def _run_validator(self, role, args):
        key = (role.value, self.ctx.trial_nonce if self.ctx is not None else "")
        remembered = self.observations.get(key)
        if remembered is not None:
            self.cache_hits += 1
            return ToolOutcome(ok=True, code="compiles", observation=None,
                               observation_sha256=remembered, fresh=False, text="")
        outcome = super()._run_validator(role, args)
        if outcome is not None:
            self.observations[key] = outcome.observation_sha256
        return outcome


class ToolEchoProvider(AgentSeatProvider):
    """The oracle plus, on EVERY view, one 'major' finding pasting validator
    output and the seat's class vocabulary around the mart it sees: a claim
    on every clean specimen too, so nitpick UB goes to 1.0 and precision at
    the 2:1 mix to 0.33 (S4 §7.1)."""

    _TEMPLATE = "[compile] compiles ok=true every_probe_compiles=true"

    def __init__(self, **kw):
        super().__init__(**kw)
        self._oracle = OracleProvider()
        self._last_observation = self._TEMPLATE

    def answer(self, role, prompt):
        payload = json.loads(self._oracle.complete(role, prompt))
        match = re.search(r"- mart (\w+)", prompt)
        mart = match.group(1) if match else "the mart"
        summary, detail = ClassEchoProvider._BY_ROLE[role]
        payload["findings"].append(
            _finding(
                f"The validator reported: {self._last_observation} for {mart}; {summary}",
                f"{detail} ({mart})",
                "major",
            )
        )
        return _with_executable_proposals(role, payload)

    def trajectory(self, role, prompt, payload, outcome):
        if outcome is not None and outcome.text:
            self._last_observation = outcome.text
        return {}


class CorrectionExhaustedOracleProvider(OracleAgentProvider):
    """The oracle whose population-adversary seat, on ONE tampered trial,
    submits its major finding with a MALFORMED `proposed_case` (an unknown
    parameter) twice: the first run is the bounded compile correction, the
    second is accepted red at submit.  The seat's final findings then go
    through the REAL post-session path — `council.findings_from_session`
    (`void_uncompilable_proposals`: the finding VOIDed under
    `uncompilable_after_corrections`, the seat's `compile_correction_exhausted`
    counter incremented) and `providers.session_findings_text` — exactly as
    `RoutedProvider._complete_session` runs it.  Batch repair D4: the trial
    is scored with the finding voided (an INFO observation still detects for
    this seat) and the run completes; nothing aborts it."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.exhausted_trials: list[str] = []
        self.voided_findings: list = []

    def complete(self, role: CouncilRole, prompt: str) -> str:
        payload = self.answer(role, prompt)
        findings = payload.get("findings") or []
        if role is not POP or not findings or self.exhausted_trials or self.executor is None:
            return super().complete(role, prompt)
        self.calls += 1
        ctx = self.ctx
        self.exhausted_trials.append(ctx.trial_nonce)
        broken = json.loads(json.dumps(payload))
        case = json.loads(broken["findings"][0]["proposed_case"]["params"])
        case["bogus"] = 1
        broken["findings"][0]["proposed_case"]["params"] = json.dumps(case)
        # Two validator runs on the SAME red payload through the trial's
        # executor (the row's accounting), and the harness-side session
        # whose per-finding checks the post-session void reads.
        name = VALIDATOR_BY_ROLE[POP]
        first = self.executor.execute(role.value, name, broken, ctx)
        second = self.executor.execute(role.value, name, broken, ctx)
        assert not first.ok and not second.ok and first.code == "uncompilable"
        self.transport_bodies.append(first.text)
        session = critic_validators.CriticSession(task=ctx.task, role=role.value)
        critic_validators.critic_validator(name).run(session.context(ctx.workspace), broken)
        result = SimpleNamespace(
            final=providers_mod.normalized_text_for(role.value, broken),
            red_validators_at_submit=(name,),
        )
        from elt_taskgen.review.council import findings_from_session

        screened = findings_from_session(ctx.task, role, result, session, ctx.task_content_hash[:8])
        self.voided_findings.extend(screened)
        text = providers_mod.session_findings_text(role.value, screened)
        model_calls = 2  # the corrected submission and the accepted one
        row = {
            "task_id": ctx.task_id,
            "task_content_hash": ctx.task_content_hash,
            "role": role.value,
            "trial_nonce": ctx.trial_nonce,
            "trial_index": ctx.trial_index,
            "prompt_sha256": sha256_hex(prompt),
            "response_sha256": sha256_hex(text),
            "attempt_count": model_calls,
            "model_call_count": model_calls,
            "tool_call_count": 0,
            "refused_count": 0,
            "nudge_count": 0,
            "validator_run_count": 2,
            "correction_count": 1,
            "correction_kinds": {"schema": 0, "compile": 1},
            "submitted_with_red_validators": 1,
            "compile_correction_exhausted": session.compile_correction_exhausted,
            "terminal": "SUBMITTED",
            "terminal_count": 1,
            "limit_stop_count": 0,
            "live_model_call_count": model_calls,
            "stale_tool_result_count": 0,
            "private_probe_count": 0,
            "duplicate_tool_calls": 0,
            "replayed": False,
            "trajectory_sha256": sha256_hex(f"{ctx.trial_nonce}|{role.value}|{self.calls}"),
            "codes": [first.code, second.code],
            "entry_schema": getattr(providers_mod, "SESSION_TRANSCRIPT_ENTRY_SCHEMA", 3),
            "usage": dict(_ZERO_USAGE),
            "usd": 0.0,
            "wall_ms": 0,
            "finding_count": len(screened),
            "zero_findings": not screened,
            "provider": self.provider_name,
            "model": self.model_name,
        }
        self.exchange_evidence.append(row)
        return text


class BoilerplateAgentProvider(AgentSeatProvider):
    """Generic findings that ignore the view, reached after one schema
    correction: zero recall, BLOCK."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._inner = BoilerplateProvider()

    def answer(self, role, prompt):
        return json.loads(self._inner.complete(role, prompt))

    def trajectory(self, role, prompt, payload, outcome):
        return {
            "model_call_count": 2, "attempt_count": 2, "live_model_call_count": 2,
            "correction_count": 1, "correction_kinds": {"schema": 1, "compile": 0},
        }


class FloodingToolProvider(BoilerplateAgentProvider):
    """Boilerplate findings reached by FLOODING the tool channel: three tool
    calls, one refusal, one nudge, two repeated fingerprints — a wasted-call
    ratio far above the advisory bar — and zero recall: BLOCK. The compile
    correction a red validator run costs stays counted (SoT T5 identity)."""

    def trajectory(self, role, prompt, payload, outcome):
        corrections = 1 if outcome is not None and not outcome.ok else 0
        model_calls = 3 + 1 + 1 + corrections + 1
        return {
            "model_call_count": model_calls, "attempt_count": model_calls,
            "live_model_call_count": model_calls,
            "tool_call_count": 3, "refused_count": 1, "nudge_count": 1,
            "duplicate_tool_calls": 2, "correction_count": corrections,
            "correction_kinds": {"schema": 0, "compile": corrections},
            "codes": ["compiles", "repeated_call", "bad_args"],
        }


class StuckAgentProvider(AgentSeatProvider):
    """One nudge, then `stuck_in_a_loop`: the trajectory halts on the second
    detector trigger before any submission, so the seat's final findings are
    empty and the trial is scored as a MISS, never raised."""

    def answer(self, role, prompt):
        return {"findings": []}

    def trajectory(self, role, prompt, payload, outcome):
        return {
            "terminal": "STUCK", "terminal_count": 0, "limit_stop_count": 1,
            "model_call_count": 3, "attempt_count": 3, "live_model_call_count": 3,
            "tool_call_count": 1, "nudge_count": 1, "duplicate_tool_calls": 1,
            "validator_run_count": 0, "codes": ["compiles", "repeated_call"],
        }


class BudgetBurnerProvider(AgentSeatProvider):
    """Spends its turns on schema corrections and is LIMIT-stopped (`LIMIT_USD`
    by default) without a validator-green draft: scored as no findings."""

    def __init__(self, terminal: str = "LIMIT_USD", **kw):
        super().__init__(**kw)
        self.terminal = terminal

    def answer(self, role, prompt):
        return {"findings": []}

    def trajectory(self, role, prompt, payload, outcome):
        return {
            "terminal": self.terminal, "terminal_count": 0, "limit_stop_count": 1,
            "model_call_count": 3, "attempt_count": 3, "live_model_call_count": 3,
            "correction_count": 2, "correction_kinds": {"schema": 2, "compile": 0},
            "validator_run_count": 0, "codes": [],
        }


class PolicyProbeProvider(OracleAgentProvider):
    """The oracle whose population-adversary seat, on its first `violations`
    TAMPERED trials, names a forbidden argument and is halted as
    `POLICY_VIOLATION` (a scored seat outcome, empty findings): the refused
    call is recorded and the trial continues."""

    def __init__(self, violations: int, **kw):
        super().__init__(**kw)
        self.violations = int(violations)
        self.violated = 0

    def answer(self, role, prompt):
        payload = super().answer(role, prompt)
        if role is POP and payload.get("findings") and self.violated < self.violations:
            self.violated += 1
            self._violating = True
            return {"findings": []}
        self._violating = False
        return payload

    def trajectory(self, role, prompt, payload, outcome):
        if role is POP and getattr(self, "_violating", False):
            return {
                "terminal": "POLICY_VIOLATION", "terminal_count": 1, "limit_stop_count": 0,
                "model_call_count": 2, "attempt_count": 2, "live_model_call_count": 2,
                "refused_count": 1, "validator_run_count": 0, "codes": ["forbidden_argument"],
            }
        return {}


class CleanTrialPolicyProbeProvider(OracleAgentProvider):
    """The oracle whose population-adversary seat ends every CLEAN trial as
    `POLICY_VIOLATION` — a `tool_not_permitted` on a tool it does not hold,
    fully reachable at `max_tool_calls: 0` — while its tampered trials are
    answered diligently.

    Recall stays 1.0 (clean trials are not scored for recall) and nitpick
    stays 0 (a violated trial files nothing), so the ONLY bar that can catch
    it is the policy-violation ratio.
    """

    def answer(self, role, prompt):
        payload = super().answer(role, prompt)
        self._violating = role is POP and not payload.get("findings")
        return {"findings": []} if self._violating else payload

    def trajectory(self, role, prompt, payload, outcome):
        if role is POP and getattr(self, "_violating", False):
            return {
                "terminal": "POLICY_VIOLATION", "terminal_count": 1, "limit_stop_count": 0,
                "model_call_count": 2, "attempt_count": 2, "live_model_call_count": 2,
                "refused_count": 1, "validator_run_count": 0, "codes": ["tool_not_permitted"],
            }
        return {}


class PrivateSniffingProvider(OracleAgentProvider):
    """The oracle whose population-adversary seat first PROBES a private
    surface (an identifier-typed argument naming `answer_key/`): refused by
    the executor, counted as `private_probe`, zero tolerance."""

    def before_validator(self, role, prompt, payload):
        if role is not POP or self.executor is None:
            return {}
        outcome = self.executor.execute(
            role.value, VALIDATOR_BY_ROLE[POP],
            {"finding_index": 0, "path": "answer_key/gold.json"}, self.ctx,
        )
        assert outcome.code == "private_probe"
        return {"refused_count": 1, "private_probe_count": 1}


class ExposureCanaryOracle(PrivateSniffingProvider):
    """A seat that read a private surface AND probes one: on a private-literal
    or forbidden-validator canary it emits the token that exists only in
    `reference.sql_by_mart` / `attack_cases[].mutation`, so both `canary_hit`
    and `private_probe` fire and the tombstone reads `private_exposure`."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._canaries = _canary_view_map(("private_literal", "forbidden_validator"))

    def answer(self, role, prompt):
        specimen = self._canaries.get((role, prompt))
        if specimen is None:
            return super().answer(role, prompt)
        (token,) = specimen.detection_terms
        return {"findings": [_finding(f"The private SQL carries {token}.",
                                      f"Seen in the reference implementation: {token}.", "major")]}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _cli_args(workspace: Path, seed: int = _SEEDS[0]):
    return SimpleNamespace(
        workspace=workspace, agents_config=None, seed=seed, replay_only=False,
        diagnostic_order_check=False, workers=1,
    )


def _pop_shc_trials(seed: int, per_role: int = 2):
    """A short schedule: `per_role` tampered trials of each validated seat."""
    out = []
    for role in (POP, SHC):
        out += [t for t in metrology_mod.select_trials(seed) if t.specimen.target_role is role][:per_role]
    return tuple(out)


class AgentDoublesTest(unittest.TestCase):
    """The roadmap's "Doubles" row: every attacker double is blocked or
    measured as specified, the diligent one is admitted (DoD (i), (ii))."""

    def setUp(self):
        providers_mod.clear_behavior_caches()
        self.addCleanup(providers_mod.clear_behavior_caches)
        self.routing = providers_mod.load_role_routing(None)

    def test_oracle_agent_provider_is_admitted_with_invariants(self):
        provider = OracleAgentProvider(routing=self.routing)
        report = metrology_mod.run_metrology(provider, seed=_SEEDS[0])
        self.assertTrue(report.admitted)
        scored = len(report.specimens)
        expected = metrology_mod.expected_trajectories_by_role(report)
        self.assertEqual(sum(expected.values()), 152)
        self.assertEqual(len(provider.exchange_evidence), 152)
        for role in CRITIC_ROLES:
            metrics = report.per_role[role.value]
            self.assertEqual(metrics.recall, 1.0, role.value)
            self.assertEqual(metrics.false_alarms, 0, role.value)
            self.assertEqual(metrics.block_reasons, (), role.value)
            self.assertEqual(metrics.private_probe_count, 0)
            self.assertEqual(metrics.stale_tool_result_count, 0)
            self.assertEqual(metrics.fabrication_flags, 0)
            self.assertEqual(metrics.model_call_count, metrics.live_model_call_count)
            self.assertEqual(metrics.model_call_count, metrics.tampered_count + metrics.clean_count)
            if role in VALIDATOR_BY_ROLE:
                self.assertEqual(metrics.validator_run_count, metrics.tampered_count + metrics.clean_count)
            else:
                self.assertEqual(metrics.validator_run_count, 0)
        # The rows summarize (the count invariants hold), and admit.
        summary = metrology_mod.summarize_fresh_live_trajectories(
            provider.exchange_evidence, expected_by_role=expected
        )
        self.assertEqual(summary.trajectory_count, 152)
        self.assertEqual(summary.stale_tool_result_count, 0)
        self.assertEqual(summary.replayed_model_call_count, 0)
        self.assertEqual(summary.validator_run_count_total, 2 * 38)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            metrology_mod.write_admission_marker(
                workspace, report, exchange_evidence=provider.exchange_evidence
            )
            self.assertTrue(metrology_mod.live_admission_ok(workspace))
        # Every trial carries its seats' trajectories: fresh, SUBMITTED.
        for row in report.specimens + report.canaries:
            for seat, trajectory in row.trajectory_by_role.items():
                self.assertEqual(trajectory.terminal, "SUBMITTED", (row.name, seat))
                self.assertEqual(trajectory.trajectories, 1)
                self.assertFalse(trajectory.replayed)
                self.assertEqual(trajectory.stale_tool_result_count, 0)
                self.assertEqual(
                    trajectory.validator_run_count, 1 if CouncilRole(seat) in VALIDATOR_BY_ROLE else 0
                )
        self.assertEqual(scored + len(report.canaries), len(provider.executors))

    def test_stochastic_agent_seat_reproduces_the_operating_characteristic(self):
        """Same RNG stream, same verdict per seed: the trajectory protocol
        neither helps nor hurts a seat of known per-trial recall, so the
        MeasurementPowerTest operating characteristic is unchanged."""
        seeds = range(1000, 1008)
        for p in (0.90, 0.60):
            with self.subTest(p=p):
                one_shot = []
                agentic = []
                rng_a = random.Random(20260813)
                rng_b = random.Random(20260813)
                for seed in seeds:
                    one_shot.append(
                        metrology_mod.run_metrology(StochasticSeatProvider(p, rng_a), seed=seed).admitted
                    )
                    agentic.append(
                        metrology_mod.run_metrology(
                            StochasticAgentSeatProvider(p, rng_b), seed=seed
                        ).admitted
                    )
                self.assertEqual(agentic, one_shot)
                if p == 0.60:
                    self.assertFalse(any(agentic))
                else:
                    self.assertTrue(any(agentic))

    def test_order_replay_agent_and_tool_memory_replay_are_blocked_with_zero_cache_hits(self):
        schedule = metrology_mod.specimen_pool()
        # Admitted on the legacy fixed suite (it never read a view)...
        replay = OrderReplayAgentProvider(schedule)
        self.assertTrue(metrology_mod.run_metrology(replay, specimens=schedule).admitted)
        self.assertEqual(replay.cache_hits, 0)
        # ...BLOCKED on every seeded draw, with no memory hit and distinct nonces.
        for seed in _SEEDS[:2]:
            for double in (OrderReplayAgentProvider, ToolMemoryReplayProvider):
                with self.subTest(seed=seed, double=double.__name__):
                    provider = double(schedule)
                    report = metrology_mod.run_metrology(provider, seed=seed)
                    self.assertFalse(report.admitted)
                    self.assertFalse(any(report.role_pass.values()))
                    self.assertEqual(provider.cache_hits, 0)
                    self.assertEqual(len(set(provider.nonces)), len(provider.nonces))
                    self.assertEqual(len(provider.nonces), len(report.specimens) + len(report.canaries))
                    for role in CRITIC_ROLES:
                        self.assertEqual(report.per_role[role.value].stale_tool_result_count, 0)

    def test_correction_exhausted_seat_is_scored_with_the_finding_voided_and_the_run_admits(self):
        """Batch repair D4.  A seat that exhausts its one compile correction on
        one trial no longer aborts the run (`ProviderProtocolError` out of
        `void_uncompilable_proposals` once ended a 152-exchange, $7 metrology
        run over ONE finding): the still-invalid proposal is VOIDed post-
        session, the seat's row carries `compile_correction_exhausted`, the
        trial is counted and scored — the voided INFO observation still
        detects for this seat — and the run completes and admits."""
        from elt_taskgen.models import FindingScreenStatus

        provider = CorrectionExhaustedOracleProvider(routing=self.routing)
        report = metrology_mod.run_metrology(provider, seed=_SEEDS[0])  # never raises
        self.assertTrue(report.admitted)
        self.assertEqual(len(provider.exhausted_trials), 1)
        (nonce,) = provider.exhausted_trials
        (row,) = [
            r for r in provider.exchange_evidence
            if r["trial_nonce"] == nonce and r["role"] == POP.value
        ]
        self.assertEqual(row["compile_correction_exhausted"], 1)
        self.assertEqual(row["submitted_with_red_validators"], 1)
        self.assertEqual(row["correction_kinds"], {"schema": 0, "compile": 1})
        self.assertEqual(row["codes"], ["uncompilable", "uncompilable"])
        self.assertEqual(row["validator_run_count"], 2)
        # The SoT T5 identity of a session row holds with the correction.
        self.assertEqual(
            row["model_call_count"],
            row["tool_call_count"] + row["refused_count"] + row["nudge_count"]
            + row["correction_count"] + row["terminal_count"] + row["limit_stop_count"],
        )
        # The correction body the runner would have sent: the closed grammar,
        # never the rejected parameter.
        self.assertTrue(any("param_unknown=true" in body for body in provider.transport_bodies))
        self.assertFalse(any("bogus" in body for body in provider.transport_bodies))
        # The finding was voided by the harness, words verbatim, and the
        # counter names it; nothing was charged to the task or the run.
        voided = [
            f for f in provider.voided_findings
            if f.screen is not None and f.screen.status is FindingScreenStatus.VOID
            and critic_validators.UNCOMPILABLE_AFTER_CORRECTIONS in f.screen.signals
        ]
        self.assertEqual(len(voided), 1)
        self.assertEqual(voided[0].severity.value, "info")
        self.assertIsNone(voided[0].proposed_case)
        # The trial is counted and scored: detected through the voided
        # observation, its trajectory carrying the one compile correction.
        (trial,) = [s for s in report.specimens if s.trial_index == row["trial_index"]]
        self.assertEqual(trial.target_role, POP.value)
        self.assertTrue(trial.detected)
        self.assertEqual(trial.trajectory_by_role[POP.value].compile_corrections, 1)
        self.assertEqual(trial.trajectory_by_role[POP.value].model_call_count, 2)
        metrics = report.per_role[POP.value]
        self.assertEqual(metrics.recall, 1.0)
        self.assertEqual(metrics.block_reasons, ())
        self.assertEqual(metrics.compile_correction_trials, 1)
        self.assertEqual(metrics.compile_correction_count, 1)
        self.assertEqual(metrics.model_call_count, metrics.tampered_count + metrics.clean_count + 1)
        # And the run's evidence still admits.
        expected = metrology_mod.expected_trajectories_by_role(report)
        summary = metrology_mod.summarize_fresh_live_trajectories(
            provider.exchange_evidence, expected_by_role=expected
        )
        self.assertEqual(summary.trajectory_count, 152)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            metrology_mod.write_admission_marker(
                workspace, report, exchange_evidence=provider.exchange_evidence
            )
            self.assertTrue(metrology_mod.live_admission_ok(workspace))

    def test_tool_echo_provider_fails_nitpick_and_precision(self):
        report = metrology_mod.run_metrology(ToolEchoProvider(), seed=_SEEDS[0])
        self.assertFalse(report.admitted)
        for role in CRITIC_ROLES:
            metrics = report.per_role[role.value]
            self.assertEqual(metrics.recall, 1.0, role.value)
            self.assertEqual(metrics.nitpick_rate, 1.0, role.value)
            self.assertEqual(metrics.nitpick_ub, 1.0, role.value)
            self.assertAlmostEqual(metrics.precision, 1 / 3, places=6)
            self.assertIn("nitpick", metrics.block_reasons)
            self.assertIn("precision", metrics.block_reasons)
            self.assertNotIn("recall", metrics.block_reasons)

    def test_flooding_and_boilerplate_agents_block(self):
        for double in (BoilerplateAgentProvider, FloodingToolProvider):
            with self.subTest(double=double.__name__):
                report = metrology_mod.run_metrology(double(), seed=_SEEDS[0])
                self.assertFalse(report.admitted)
                for role in CRITIC_ROLES:
                    metrics = report.per_role[role.value]
                    self.assertEqual(metrics.recall, 0.0, role.value)
                    self.assertIn("recall", metrics.block_reasons)
                    self.assertEqual(metrics.correction_count, metrics.schema_correction_count
                                     + metrics.compile_correction_count)
                    if double is FloodingToolProvider:
                        self.assertEqual(metrics.tool_call_count, 3 * (metrics.tampered_count + metrics.clean_count))
                        self.assertGreater(metrics.wasted_call_ratio, report.thresholds.advisory_max_wasted_call_ratio)
                        self.assertIn("wasted_call_ratio", metrics.advisory_flags)
                        self.assertEqual(metrics.nudge_count, metrics.tampered_count + metrics.clean_count)
                    else:
                        self.assertEqual(metrics.format_retry_trials, metrics.tampered_count + metrics.clean_count)
                        self.assertEqual(metrics.format_retry_ratio, 1.0)
                        self.assertEqual(metrics.advisory_flags, ())
                    # Advisory readings never become block reasons.
                    self.assertFalse(set(metrics.advisory_flags) & set(metrics.block_reasons))

    def test_stuck_agent_one_nudge_then_stuck_is_scored_as_a_miss(self):
        provider = StuckAgentProvider()
        report = metrology_mod.run_metrology(provider, seed=_SEEDS[0])  # never raises
        self.assertFalse(report.admitted)
        for role in CRITIC_ROLES:
            metrics = report.per_role[role.value]
            scored = metrics.tampered_count + metrics.clean_count
            self.assertEqual(metrics.recall, 0.0, role.value)
            self.assertEqual(metrics.false_alarms, 0, role.value)
            self.assertEqual(metrics.stuck_trials, scored)
            self.assertEqual(metrics.limit_stopped_trials, scored)
            self.assertEqual(metrics.nudge_count, scored)
            self.assertGreater(metrics.stuck_ratio_ub, report.thresholds.advisory_max_stuck_ratio_ub)
            self.assertIn("stuck_ratio", metrics.advisory_flags)
            self.assertIn("recall", metrics.block_reasons)
            self.assertNotIn("stuck", " ".join(metrics.block_reasons))
        for row in report.specimens:
            for trajectory in row.trajectory_by_role.values():
                self.assertEqual(trajectory.terminal, "STUCK")
                self.assertEqual(trajectory.nudge_count, 1)
                self.assertIn("repeated_call", trajectory.codes)

    def test_budget_burner_limit_stopped_is_scored_as_no_findings(self):
        for terminal in ("LIMIT_USD", "LIMIT_TURNS", "LIMIT_WALL"):
            with self.subTest(terminal=terminal):
                report = metrology_mod.run_metrology(BudgetBurnerProvider(terminal), seed=_SEEDS[0])
                self.assertFalse(report.admitted)
                for role in CRITIC_ROLES:
                    metrics = report.per_role[role.value]
                    scored = metrics.tampered_count + metrics.clean_count
                    self.assertEqual(metrics.recall, 0.0, role.value)
                    self.assertEqual(metrics.false_alarms, 0, role.value)
                    self.assertEqual(metrics.precision, 1.0, role.value)
                    self.assertEqual(metrics.block_reasons, ("recall",), role.value)
                    self.assertEqual(metrics.limit_stopped_trials, scored)
                    self.assertEqual(metrics.stuck_trials, 0)
                    self.assertEqual(metrics.schema_correction_count, 2 * scored)
                    self.assertIn("limit_stopped_ratio", metrics.advisory_flags)

    def test_policy_probe_one_of_35_passes_and_two_of_35_blocks(self):
        """With a tool channel declared for the population adversary, one
        POLICY_VIOLATION trial of 35 keeps the Wilson UB under 0.10 (0.075)
        and two put it over (0.112); the refused call is recorded and the
        trial continues, scored as no findings."""
        doc = json.loads(json.dumps(providers_mod._agents_doc()))
        block = doc["roles"]["population_adversary"]["session"]
        block["enabled"] = True
        block["max_tool_calls"] = 3
        caps = block.get("hard_caps")
        if isinstance(caps, dict) and "tool_calls" in caps:
            caps["tool_calls"] = max(int(caps["tool_calls"]), 3)
        with mock.patch.object(providers_mod, "_agents_doc", lambda: doc):
            providers_mod.clear_behavior_caches()
            self.assertTrue(metrology_mod._has_tool_channel("population_adversary"))
            for violations, blocks in ((1, False), (2, True)):
                with self.subTest(violations=violations):
                    provider = PolicyProbeProvider(violations)
                    report = metrology_mod.run_metrology(provider, seed=_SEEDS[0])
                    pop = report.per_role[POP.value]
                    self.assertEqual(provider.violated, violations)
                    self.assertEqual(pop.policy_violation_trials, violations)
                    self.assertEqual(pop.refused_count, violations)
                    self.assertEqual(pop.detected_count, _TRIALS_PER_SEAT - violations)
                    self.assertGreaterEqual(pop.recall_lb, report.thresholds.min_recall)
                    ub = metrology_mod.wilson_interval(violations, 35, report.thresholds.confidence)[1]
                    self.assertAlmostEqual(pop.policy_violation_ub, ub)
                    self.assertEqual(report.admitted, not blocks)
                    if blocks:
                        self.assertEqual(pop.block_reasons, ("policy_violation",))
                        self.assertGreater(pop.policy_violation_ub, report.thresholds.max_policy_violation_ub)
                    else:
                        self.assertEqual(pop.block_reasons, ())
                        self.assertLessEqual(pop.policy_violation_ub, report.thresholds.max_policy_violation_ub)
                    for role in CRITIC_ROLES:
                        if role is not POP:
                            self.assertEqual(report.per_role[role.value].policy_violation_trials, 0)
        providers_mod.clear_behavior_caches()

    def test_policy_violation_bar_blocks_under_the_shipped_zero_tool_block(self):
        """findings p4-0-0 / p4-1-0 / p4-2-3: the blocking
        `max_policy_violation_ub` bar was DEAD CODE for exactly the two seats
        Phase 4 originally shipped disabled.  The zero-model-tool validator
        seats now ship enabled, so this test exercises that current profile
        first and then proves ``session.enabled: false`` remains the explicit
        rollback that makes the ratio moot.

        `_has_tool_channel` used to read `enabled and max_tool_calls > 0`, and
        a `harness_validated` seat never declares a model-initiated tool call
        — that is what the mode MEANS — so the bound short-circuited to 0.0
        and 35 policy violations out of 35 scored trials read 0.00 against a
        bar of 0.10. POLICY_VIOLATION is fully reachable at
        `max_tool_calls: 0`: any `tool_use` block naming a tool the seat does
        not hold is `ToolNotPermitted`, and `_submit` applies the
        forbidden-argument rules to the submitted payload. The channel is now
        "an enabled session", including the shipped POP profile.
        """
        doc = json.loads(json.dumps(providers_mod._agents_doc()))
        block = doc["roles"]["population_adversary"]["session"]
        self.assertEqual(block["max_tool_calls"], 0)
        self.assertEqual(block["mode"], "harness_validated")
        self.assertTrue(block["enabled"])
        with mock.patch.object(providers_mod, "_agents_doc", lambda: doc):
            providers_mod.clear_behavior_caches()
            # The shipped block: the bar is LIVE despite zero model tools.
            self.assertTrue(metrology_mod._has_tool_channel("population_adversary"))
            report = metrology_mod.run_metrology(
                CleanTrialPolicyProbeProvider(), seed=_SEEDS[0]
            )
            pop = report.per_role[POP.value]
            self.assertEqual(pop.policy_violation_trials, pop.clean_count)
            self.assertGreater(pop.clean_count, 0)
            # Recall and nitpick are untouched: the violated trials are the
            # CLEAN ones, so no other bar can catch this seat.
            self.assertEqual(pop.recall, 1.0)
            self.assertEqual(pop.false_alarms, 0)
            self.assertGreaterEqual(pop.recall_lb, report.thresholds.min_recall)
            self.assertLessEqual(pop.nitpick_ub, report.thresholds.max_nitpick_rate)
            self.assertGreater(
                pop.policy_violation_ub, report.thresholds.max_policy_violation_ub
            )
            self.assertEqual(pop.block_reasons, ("policy_violation",))
            self.assertFalse(report.role_pass[POP.value])
            self.assertFalse(report.admitted)
            # Every other seat is unaffected.
            for role in CRITIC_ROLES:
                if role is not POP:
                    self.assertEqual(
                        report.per_role[role.value].policy_violation_trials, 0
                    )
                    self.assertEqual(report.per_role[role.value].block_reasons, ())
        providers_mod.clear_behavior_caches()
        self.assertTrue(metrology_mod._has_tool_channel("population_adversary"))

        rollback = json.loads(json.dumps(providers_mod._agents_doc()))
        rollback["roles"]["population_adversary"]["session"]["enabled"] = False
        with mock.patch.object(providers_mod, "_agents_doc", lambda: rollback):
            providers_mod.clear_behavior_caches()
            # Explicit rollback: no session, so the count is zero by
            # construction and the ratio stays moot.
            self.assertFalse(metrology_mod._has_tool_channel("population_adversary"))
            self.assertEqual(
                metrology_mod._policy_violation_ub(
                    0, 35, 0.85, tool_channel=False
                ),
                0.0,
            )
        providers_mod.clear_behavior_caches()

    def test_private_sniffing_doubles_tombstone_private_exposure_and_private_probe(self):
        fingerprint = metrology_mod.council_routing_fingerprint(self.routing)
        for double, reason_code, reasons in (
            (PrivateSniffingProvider, "private_probe", ("private_probe",)),
            (ExposureCanaryOracle, "private_exposure", ("canary_hit", "private_probe")),
        ):
            with self.subTest(double=double.__name__):
                provider = double(routing=self.routing)
                report = metrology_mod.run_metrology(
                    provider, routing_fingerprint=fingerprint, seed=_SEEDS[0]
                )
                self.assertFalse(report.admitted)
                pop = report.per_role[POP.value]
                self.assertEqual(pop.block_reasons, reasons)
                self.assertGreaterEqual(pop.private_probe_count, 35)
                self.assertEqual(pop.recall, 1.0)
                self.assertEqual(sum(e.private_probes for e in provider.executors), pop.private_probe_count)
                for role in CRITIC_ROLES:
                    if role is not POP:
                        self.assertEqual(report.per_role[role.value].private_probe_count, 0)
                        self.assertNotIn("private_probe", report.per_role[role.value].block_reasons)
                self.assertEqual(metrology_mod.revocation_reason_code(report), reason_code)
                # The CLI tombstones the record with that reason code.
                cli_provider = SimpleNamespace(
                    routing=self.routing, replay_only=False, agents_config=None,
                    exchange_evidence=_fresh_live_exchange_evidence(report),
                )
                with tempfile.TemporaryDirectory() as tmp:
                    workspace = Path(tmp)
                    with mock.patch.object(metrology_mod, "run_metrology", return_value=report):
                        code = cli._cmd_metrology_measured(
                            _cli_args(workspace), workspace, cli_provider, metrology_mod, providers_mod
                        )
                    self.assertEqual(code, 1)
                    tombstone = json.loads(
                        metrology_mod.marker_path(workspace).read_text(encoding="utf-8")
                    )
                    self.assertTrue(tombstone["revoked"])
                    self.assertEqual(tombstone["revoked_reason_code"], reason_code)
                    self.assertEqual(tombstone["revoked_harness_version"], metrology_mod.HARNESS_VERSION)
                    self.assertFalse(metrology_mod.admission_status(workspace).ok)

    def test_leak_probe_executor_trips_before_transport_exit_2_nothing_written_or_revoked(self):
        provider = OracleAgentProvider(executor_factory=LeakProbeExecutor, routing=self.routing)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            code = cli._cmd_metrology_measured(
                _cli_args(workspace), workspace, provider, metrology_mod, providers_mod
            )
            self.assertEqual(code, 2)
            self.assertFalse(metrology_mod.marker_path(workspace).exists())
            self.assertFalse((workspace / "reports").exists())
            self.assertFalse(metrology_mod.admission_status(workspace).ok)
        # Nothing was sent: no correction body exists, let alone one carrying
        # the fragment; the message names the detector and a code, never the value.
        self.assertEqual(provider.transport_bodies, [])
        self.assertFalse(any(_PRIVATE_FRAGMENT in body for body in provider.transport_bodies))
        self.assertIsNone(provider.ctx)  # end_trial ran in the loop's finally
        # The library call raises the tripwire unchanged, a SessionFault.
        provider = OracleAgentProvider(executor_factory=LeakProbeExecutor)
        with self.assertRaises(DiagnosticTripwire) as caught:
            metrology_mod.run_metrology(provider, seed=_SEEDS[0])
        self.assertIsInstance(caught.exception, SessionFault)
        self.assertEqual(caught.exception.code, "private_fragment")
        self.assertEqual(caught.exception.detector, "assert_value_free")
        self.assertNotIn(_PRIVATE_FRAGMENT, str(caught.exception))
        self.assertEqual(caught.exception.quarantined, _PRIVATE_FRAGMENT.encode("utf-8"))
        self.assertIsNone(provider.ctx)
        self.assertFalse(any(e.runs for e in provider.executors))

    def test_crashing_tool_executor_exits_2(self):
        provider = OracleAgentProvider(executor_factory=CrashingToolExecutor, routing=self.routing)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            code = cli._cmd_metrology_measured(
                _cli_args(workspace), workspace, provider, metrology_mod, providers_mod
            )
            self.assertEqual(code, 2)
            self.assertFalse(metrology_mod.marker_path(workspace).exists())
            self.assertFalse((workspace / "reports").exists())
        provider = OracleAgentProvider(executor_factory=CrashingToolExecutor)
        with self.assertRaises(ToolHarnessFault) as caught:
            metrology_mod.run_metrology(provider, seed=_SEEDS[0])
        self.assertIsInstance(caught.exception, SessionFault)
        self.assertEqual(caught.exception.cause_type, "RuntimeError")
        self.assertIn(caught.exception.tool, set(VALIDATOR_BY_ROLE.values()))
        self.assertIsNone(provider.ctx)


class ExecutorIsolationTest(unittest.TestCase):
    """The Isolation half that needs an executor (roadmap "Isolation" row)."""

    def test_tool_executor_has_no_cross_trial_state(self):
        """One executor per trial, built with `cache=None` at `begin_trial`
        and closed at `end_trial`; every validator result computed in its own
        trial (`fresh=True`); no executor sees another trial's runs."""
        provider = OracleAgentProvider()
        trials = _pop_shc_trials(_SEEDS[0], per_role=3)
        report = metrology_mod.run_metrology(provider, trials=trials)
        self.assertEqual(len(report.specimens), len(trials))
        self.assertEqual(len(provider.executors), len(trials))
        self.assertEqual(len({id(e) for e in provider.executors}), len(trials))
        for executor in provider.executors:
            self.assertIsNone(executor.cache)
            self.assertTrue(executor.closed)
            self.assertEqual(len(executor.runs), 1)
            self.assertTrue(all(run["fresh"] for run in executor.runs))
            with self.assertRaises(ToolHarnessFault):
                executor.execute("population_adversary", VALIDATOR_BY_ROLE[POP], {}, None)
        self.assertEqual(len(set(provider.nonces)), len(trials))
        for row in report.specimens:
            trajectory = row.trajectory_by_role[row.target_role]
            self.assertEqual(trajectory.validator_run_count, 1)
            self.assertEqual(trajectory.stale_tool_result_count, 0)
            self.assertFalse(trajectory.replayed)
        # The shipping provider's seam agrees: `RoutedProvider.begin_trial`
        # builds a cache-less executor per trial and `end_trial` drops it.
        if hasattr(providers_mod.RoutedProvider, "begin_trial"):
            try:
                from tests import test_providers as tp
            except ImportError:  # run from inside tests/
                import test_providers as tp  # type: ignore[no-redef]

            with tempfile.TemporaryDirectory() as tmp:
                live = providers_mod.RoutedProvider(
                    tp.make_routing(),
                    providers_mod.TranscriptStore(Path(tmp) / "transcripts"),
                    providers_mod.CostMeter(budget_per_task_usd=100.0),
                    task_id="council-metrology",
                    transports={"anthropic": tp.FakeTransport([])},
                )
                seen = []
                for index, trial in enumerate(trials[:2]):
                    with metrology_mod.trial_workspace(trial, root=Path(tmp) / "trials", trial_index=index) as ctx:
                        live.begin_trial(ctx)
                        executor = live.trial_executor
                        self.assertIsNotNone(executor)
                        self.assertIsNone(getattr(executor, "cache", None))
                        seen.append(id(executor))
                        live.end_trial()
                        self.assertIsNone(live.trial_executor)
                self.assertEqual(len(set(seen)), 2)

    def test_scratch_notes_do_not_survive_a_trial(self):
        """A note a seat writes into its trial workspace is gone before the
        next trial begins; every trial gets a new, empty workspace."""

        class _Noting(OracleAgentProvider):
            def __init__(self):
                super().__init__()
                self.notes_seen = 0
                self.workspaces: list[Path] = []

            def begin_trial(self, ctx):
                super().begin_trial(ctx)
                self.notes_seen += len(list(ctx.workspace.parent.rglob("scratch-note.txt")))
                self.notes_seen += len(list(ctx.workspace.rglob("*.md"))) - len(
                    list(ctx.public_dir.rglob("*.md"))
                )
                (ctx.workspace / "scratch-note.txt").write_text(ctx.trial_nonce, encoding="utf-8")
                (ctx.workspace / "draft.md").write_text("first draft", encoding="utf-8")
                self.workspaces.append(ctx.workspace)

        provider = _Noting()
        report = metrology_mod.run_metrology(provider, trials=_pop_shc_trials(_SEEDS[1], per_role=3))
        self.assertEqual(len(report.specimens), 6)
        self.assertEqual(provider.notes_seen, 0)
        self.assertEqual(len(set(provider.workspaces)), 6)
        self.assertFalse(any(path.exists() for path in provider.workspaces))
        self.assertFalse(any(path.parent.exists() for path in provider.workspaces))

    def test_tool_output_carries_no_path_pid_or_timestamp(self):
        """What a validator run delivers — the rendered `Diagnostic` and its
        transport payload — carries no absolute path, no trial workspace
        name or nonce, no process id and no timestamp: codes, booleans and
        public identifiers only, so nothing about the host or the schedule
        rides back to the model."""
        provider = OracleAgentProvider()
        trials = _pop_shc_trials(_SEEDS[2], per_role=3)
        metrology_mod.run_metrology(provider, trials=trials)
        pid = str(os.getpid())
        runs = [run for executor in provider.executors for run in executor.runs]
        self.assertEqual(len(runs), len(trials))
        for executor, run in zip(provider.executors, runs):
            for text in (run["text"], run["payload"]):
                self.assertIsNone(_ABS_PATH_RE.search(text), text)
                self.assertNotIn(tempfile.gettempdir(), text)
                self.assertNotIn(pid, text)
                self.assertIsNone(_TIMESTAMP_RE.search(text), text)
                for nonce in provider.nonces:
                    self.assertNotIn(nonce, text)
                    self.assertNotIn("m-" + nonce[:8], text)
                self.assertNotIn("elt-metrology-", text)
            self.assertTrue(run["text"].startswith("[compile] "), run["text"])
            self.assertEqual(len(run["observation_sha256"]), 64)
        # The same validator, run twice in two workspaces on the same view,
        # renders byte-identical output: nothing of the workspace leaks in.
        by_prompt: dict[str, set] = {}
        for trial, run in zip(trials, runs):
            by_prompt.setdefault(trial.specimen.name, set()).add(run["text"])
        self.assertTrue(all(len(texts) == 1 for texts in by_prompt.values()))


if __name__ == "__main__":
    unittest.main()

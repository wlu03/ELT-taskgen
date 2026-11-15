"""Route model calls with transcript, replay, and budget controls.

Live calls are recorded before their responses return and are served only for their
recorded route. Replay-only mode never uses the network. One-shot and bounded-session
entry points share these guarantees.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

import yaml

from pydantic import ValidationError

from elt_taskgen.models import (
    AttackKind,
    FindingDisposition,
    PopulationName,
    ProposedAttackCase,
    RLVR_TASK_VARIANTS,
    RepairRoute,
    Severity,
    canonical_json,
    readable_json,
    sha256_hex,
)
from elt_taskgen.package_resources import resource_path
from elt_taskgen.review import trajectory as _trajectory
from elt_taskgen.review.council import ProviderProtocolError
from elt_taskgen.review.prompts import ROLE_SYSTEM, role_system_prompt
from elt_taskgen.review.session import (
    PRIVATE_PROBE_CODE,
    ForbiddenArgument,
    PolicyFault,
    ProviderFault,
    SessionFault,
    SessionLimits,
    SessionPolicy,
    SessionPolicyViolation,
    ToolHarnessFault,
    ToolOutcome,
    ToolProtocolFault,
    WorkerResult,
    plain_messages,
    validate_args,
)
from elt_taskgen.review.tools.projection import DIAGNOSTICS_VERSION
from elt_taskgen.review.repair_proposer import ROUTE_ALLOWLIST, ROUTE_IR_PATHS
from elt_taskgen.review.tools.registry import ToolRegistry
from elt_taskgen.verification.attacks import kind_variant_contract

from elt_taskgen.review import provider_accounting as _review_provider_accounting
from elt_taskgen.review import provider_transport as _review_provider_transport
from elt_taskgen.review import provider_types as _review_provider_types
from elt_taskgen.review import transcripts as _review_transcripts

# Compatibility imports; implementations live in their owning modules.
from elt_taskgen.review.provider_accounting import (
    ANTHROPIC_PRICING_USD_PER_MTOK,
    BudgetExceededError,
    CHARS_PER_TOKEN_ESTIMATE,
    CostMeter,
    DEFAULT_BUDGET_PER_TASK_USD,
    OPENAI_COMPAT_PRICING_HOWTO,
    RESERVE_OUTPUT_FRACTION,
    RateCard,
    RoleCapExceeded,
    TrajectoryBudget,
    _explicit_rate,
    _fresh_role_reading,
    _openai_compat_rates,
    _usable_report,
    estimate_turn_usd,
    rate_card_for,
    rates_for,
)
from elt_taskgen.review.provider_transport import (
    Transport,
    _OneShotWallDeadline,
    _call_with_wall_deadline,
    _http_post_json,
)
from elt_taskgen.review.provider_types import (
    AttemptRecord,
    BackendResult,
    BackendTurn,
    ToolResultBlock,
    Usage,
    _BACKEND_ATTEMPTS_ATTR,
    _TRUNCATED_STOP_REASONS,
    _attach_attempts,
    _elapsed_ms,
    _plain_data,
    _tool_use_blocks,
    _tool_use_ids,
    _with_tool_results,
    backend_attempts,
    protocol_correction_results,
)
from elt_taskgen.review.transcripts import (
    SESSION_RECORDS_SUBDIR,
    TOOL_RAW_SUBDIR,
    TRAJECTORIES_SUBDIR,
    TranscriptStore,
    _absolute_lexical,
    _atomic_replace_bytes,
    _confirm_raw_output,
    _directory_chain,
    _displaced_entry_path,
    _ensure_private_directory,
    _entry_path,
    _fsync_directory,
    _publish_bytes_once,
    _publish_raw_output,
    _read_entry_if_any,
    _read_regular_bytes,
    _session_record_path,
    _staged_bytes,
    tool_raw_root,
    trajectories_root,
    trajectory_record_path,
)

from elt_taskgen.review import provider_config as _provider_config
from elt_taskgen.review import provider_protocol as _provider_protocol
from elt_taskgen.review import provider_backends as _provider_backends
from elt_taskgen.review.provider_config import RoleRoute


__all__ = [
    "AnthropicBackend",
    "BackendResult",
    "BackendTurn",
    "BudgetExceededError",
    "CostMeter",
    "RoleCapExceeded",
    "MissingCredentialsError",
    "OpenAICompatBackend",
    "PROPOSAL_ROLES",
    "PROVIDER_BACKENDS",
    "PROSE_ROLES",
    "SOLVER_ROLE_PREFIX",
    "REPAIR_PROPOSER_ROLE",
    "REPAIR_PROPOSER_SYSTEM",
    "ROLE_SYSTEM",
    "SHARED_ROLE_PREFIX",
    "RoleRoute",
    "RoleRouting",
    "RoutedProvider",
    "MODEL_FAMILY_PREFIXES",
    "TranscriptMissingError",
    "TranscriptRouteMismatchError",
    "TranscriptStore",
    "credential_problems",
    "endpoint_host",
    "model_family",
    "transcript_route_mismatch",
    "default_agents_config_path",
    "default_fixtures_dir",
    "findings_tool_schema",
    "load_role_routing",
    "proposed_case_schema",
    "transcripts_present",
    "agents_config_of",
    # -- Step 8 surface: batch submission --
    "BATCH_API_PATH",
    "BatchQueue",
    "BatchRequest",
    "BatchRunResult",
    "role_behavior_sha256",
    "batch_transport_default",
    "transcript_key",
    "tool_name_for",
    "tool_schema_for",
    "uses_findings_schema",
    # -- Phase 0.E: behaviour manifest, transcript key v3, entry schema 2 --
    "DEFAULT_SANDBOX_PIN",
    "TRANSCRIPT_ENTRY_SCHEMA",
    "TRANSCRIPT_KEY_VERSION",
    "clear_behavior_caches",
    "role_behavior_manifest",
    "role_loop_limits",
    "role_tools_sha256",
    "sandbox_pin",
    "session_policy_for",
    "tool_choice_for",
    "transcript_entry_schema",
    "transcript_key_v3",
    "wire_tools_for",
    # -- Phase 1.R: the provider wire layer for bounded sessions --
    "SESSION_TRANSCRIPT_ENTRY_SCHEMA",
    "SESSION_RUNNER_ROLES",
    "WITNESS_RUNNER_ROLES",
    "SessionAccount",
    "SessionReplayMismatchError",
    "SessionTranscriptMissingError",
    "role_manifest_limits",
    "session_turn_key",
    "turn_wire_binding",
    "ToolResultBlock",
    "exchange_row_problems",
    "protocol_correction_results",
    "role_is_agentic",
    "session_tool_choice",
    # -- Phase 4: the provider trial seam and the critic-session dispatch --
    "AGENTIC_ROLES",
    "TRAJECTORIES_SUBDIR",
    "TOOL_RAW_SUBDIR",
    "RoutedProviderPool",
    "ToolExecutor",
    "TrialToolExecutor",
    "ValidatorRun",
    "agentic_role_enabled",
    "session_findings_text",
    "tool_raw_root",
    "trajectory_record_path",
    "trajectories_root",
]


# Errors (all fail-closed: they propagate and the calling stage fails)

class TranscriptMissingError(RuntimeError):
    """Replay-only mode was asked for a (role, prompt) with no recorded
    transcript. Raised, never degraded — a replay run must not invent or skip
    council output."""


class TranscriptRouteMismatchError(TranscriptMissingError):
    """A transcript exists for the (role, prompt) but was recorded under a
    DIFFERENT (provider, model) route than the one configured.

    Subclasses TranscriptMissingError so existing handlers apply: a transcript
    from another model is not a replay of THIS route."""


class SessionTranscriptMissingError(ProviderFault, TranscriptMissingError):
    """Signal a bounded-session replay miss.

    The first rekeyed turn has no entry valid for the requested route, task, and
    behavior. It is raised before transport, so nothing is spent.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message, code="replay_miss")


#: The code of a replay that DIVERGED from its recording (SoT T8
#: `stale_tool_result_count`): a tool observation, or the whole session
#: digest, that the recorded session does not carry.
REPLAY_MISMATCH_CODE = "replay_mismatch"


class SessionReplayMismatchError(ToolHarnessFault, TranscriptMissingError):
    """Signal divergence from a recorded replay session.

    A tool observation digest or completed session digest differs from its record. The
    run cannot produce a replayed evidence row.
    """

    def __init__(self, tool: str, *, detail: str = "") -> None:
        super().__init__(tool, code=REPLAY_MISMATCH_CODE)
        self.detail = str(detail)
        if self.detail:
            self.args = (
                f"replay-only: session replay diverged at {self.tool!r}: {self.detail} "
                "(a replay is diagnostic only; re-record — fail closed)",
            )


def transcript_route_mismatch(
    entry: Mapping[str, Any],
    route: "RoleRoute",
    *,
    agents_config: Path | str | None = None,
    policy: SessionPolicy | None = None,
) -> str | None:
    """Return why a stored transcript cannot serve the requested route, or `None`.

    Provider and bound model must match, followed by recorded effort, token limit,
    behavior, tools, and policy. Unbound OpenAI-compatible routes may accept any
    recorded model. Corrupt unbound entries raise, legacy entries cannot serve enabled
    sessions, and diagnostic version is recorded but not compared.
    """
    bound = entry.get("route")
    if not isinstance(bound, Mapping):
        bound = {"provider": entry.get("provider"), "model": entry.get("model")}
    bound_provider = str(bound.get("provider") or "")
    bound_model = str(bound.get("model") or "")
    if not bound_provider or not bound_model:
        raise RuntimeError(
            "transcript carries no provider/model binding (corrupt store — "
            "fail closed): every recorded entry names the route that produced it"
        )
    if route.model == "":
        return None
    if (bound_provider, bound_model) != (route.provider, route.model):
        return (
            f"recorded under {bound_provider}/{bound_model}, current route "
            f"{route.provider}/{route.model}"
        )
    if isinstance(entry.get("route"), Mapping):
        recorded_effort = bound.get("effort")
        recorded_max = bound.get("max_tokens")
        if "effort" in bound and (recorded_effort or None) != (route.effort or None):
            return (
                f"recorded at effort {recorded_effort!r}, current route "
                f"effort {route.effort!r} (model {route.model})"
            )
        if "max_tokens" in bound and int(recorded_max or 0) != int(route.max_tokens):
            return (
                f"recorded at max_tokens {recorded_max!r}, current route "
                f"max_tokens {route.max_tokens!r} (model {route.model})"
            )
        for name, label, current in (
            (
                "behavior_sha256",
                "behaviour manifest",
                lambda: _provider_protocol.role_behavior_sha256(route.role, agents_config=agents_config),
            ),
            (
                "tools_sha256",
                "tool protocol",
                lambda: (
                    policy.tools_sha256() if policy is not None else _provider_protocol.role_tools_sha256(route.role)
                ),
            ),
            (
                "policy_sha256",
                "session policy",
                lambda: (
                    policy.sha256()
                    if policy is not None
                    else _provider_protocol.session_policy_for(route.role, agents_config=agents_config).sha256()
                ),
            ),
        ):
            if name not in bound:
                continue
            recorded = str(bound.get(name) or "")
            now = current()
            if recorded != now:
                return (
                    f"recorded under {label} {recorded[:12]}, current {label} "
                    f"{now[:12]} (role {route.role})"
                )
    if _provider_protocol.transcript_entry_schema(entry) < _provider_protocol.TRANSCRIPT_ENTRY_SCHEMA and bool(
        _provider_config.role_loop_limits(route.role, agents_config=agents_config).get("enabled", False)
    ):
        return (
            f"recorded as a legacy entry (entry_schema "
            f"{_provider_protocol.transcript_entry_schema(entry)}) while role {route.role} runs a "
            "session; legacy entries serve one-shot roles only"
        )
    return None


def transcripts_present(root: Path | None) -> bool:
    """True iff the directory contains at least one recorded transcript."""
    if root is None or not root.is_dir():
        return False
    return any(root.glob("*/*.json"))


def _repo_root() -> Path:
    # src/elt_taskgen/review/providers.py -> repo root is parents[3]
    return Path(__file__).resolve().parents[3]


#: Env override for where the demo looks for committed replay fixtures.
DEMO_TRANSCRIPTS_ENV = "ELT_TASKGEN_DEMO_TRANSCRIPTS"


def default_fixtures_dir() -> Path:
    """Committed demo-replay fixtures dir (env-overridable for tests)."""
    override = os.environ.get(DEMO_TRANSCRIPTS_ENV)
    if override:
        return Path(override).resolve()
    return _repo_root() / "tests" / "fixtures" / "transcripts"


class ToolExecutor(Protocol):
    """The per-trial executor of harness validators (roadmap Phase 4 "New
    interfaces"): `execute(role, name, args, ctx)` runs ONE validator against
    the trial's context and answers the runner's `ToolOutcome` envelope with
    `fresh=True`; the executor holds no cache (`cache is None`) and is
    destroyed at `end_trial`, so no result crosses a trial."""

    cache: None

    def execute(self, role: str, name: str, args: Mapping[str, Any], ctx: Any) -> ToolOutcome: ...


@dataclass(frozen=True)
class ValidatorRun:
    """One validator run an executor performed, as the trajectory record and
    the `tool_raw/` tier need it: identity, the RENDERED observation (what a
    correction turn carries to the model), the transport digest (the
    runner's `output_sha256`), the raw projection bytes and their digest,
    the outcome and the measured wall. Never sent anywhere."""

    index: int
    role: str
    name: str
    args_sha256: str
    observation: str
    observation_sha256: str
    payload: str
    raw_output: bytes
    raw_output_sha256: str
    code: str
    ok: bool
    wall_ms: int
    fresh: bool = True
    projection_version: str = DIAGNOSTICS_VERSION
    sanitizer_version: str = DIAGNOSTICS_VERSION


class TrialToolExecutor:
    """Execute and record validators within one metrology trial.

    Every call computes against the current isolated workspace with no result cache. The
    executor logs runs for evidence and refuses calls after `end_trial`; raw results
    never enter model-visible output.
    """

    #: Never a cache. Read by tests and by `end_trial`'s teardown assertion.
    cache: None = None

    def __init__(
        self,
        *,
        trial_nonce: str = "",
        workspace: Path | None = None,
        worker: Any = None,
        foreign_nonces: Sequence[str] = (),
    ) -> None:
        self.trial_nonce = str(trial_nonce or "")
        self.workspace = None if workspace is None else Path(workspace)
        self.cache = None
        self.closed = False
        self.runs: list[ValidatorRun] = []
        #: The OTHER trials' nonces this run has already drawn: an argument
        #: naming one is a probe of another trial (metrology redesign §8.2).
        self.foreign_nonces: tuple[str, ...] = tuple(str(n) for n in foreign_nonces if n)
        #: The zero-tolerance counter the admission bar `max_private_probes`
        #: re-derives (finding p4-2-2: it used to have no production caller
        #: at all, so the blocking bar read the 0 default on every real run).
        self.private_probe_count = 0
        self._worker = worker
        self._lock = threading.Lock()

    def _refuse_private_probe(self, name: str, args: Mapping[str, Any]) -> int:
        """How many identifier-typed argument strings of this call name a
        PRIVATE surface or another trial's nonce (`metrology.private_probe_tokens`,
        the one definition), accumulated onto the executor's counter. Zero
        for a clean call. The arguments themselves never leave this frame."""
        from elt_taskgen.review.metrology import private_probe_tokens

        probes = int(private_probe_tokens(args, foreign_nonces=self.foreign_nonces))
        if probes:
            with self._lock:
                self.private_probe_count += probes
        return probes

    def _delegate(self) -> Any:
        if self._worker is None:
            from elt_taskgen.review.tools.critic_validators import critic_validator_worker

            self._worker = critic_validator_worker()
        return self._worker

    # -- the ValidatorWorker protocol (what the runner calls) -----------------

    def run(self, tool: Any, ctx: Any, args: Mapping[str, Any], *, deadline_s: float) -> WorkerResult:
        name = str(getattr(tool, "name", "tool"))
        if self.closed:
            raise ToolHarnessFault(name, code="executor_closed")
        # Reject private-surface or foreign-nonce identifiers before validation;
        # count the probe and return only `PRIVATE_PROBE_CODE` as ForbiddenArgument.
        if self._refuse_private_probe(name, args):
            raise ForbiddenArgument(tool=name, detail=PRIVATE_PROBE_CODE)
        started = time.monotonic()
        result = self._delegate().run(tool, ctx, dict(args), deadline_s=deadline_s)
        self._log(ctx, name, args, result, elapsed_ms=_review_provider_types._elapsed_ms(started))
        return result

    def current_draft(self, ctx: Any) -> Any:
        draft_of = getattr(self._delegate(), "current_draft", None)
        return draft_of(ctx) if callable(draft_of) else None

    def surface_fingerprint(self, ctx: Any) -> str | None:
        fingerprint_of = getattr(self._delegate(), "surface_fingerprint", None)
        return fingerprint_of(ctx) if callable(fingerprint_of) else None

    # -- the ToolExecutor protocol --------------------------------------------

    def execute(self, role: str, name: str, args: Mapping[str, Any], ctx: Any) -> ToolOutcome:
        """Run validator `name` for `role` against `ctx` (a `ToolContext`)
        and answer the runner's envelope: `fresh=True`, the observation, its
        transport digest and the raw digest. The tool is resolved from the
        role's DECLARED validators (`critic_validators.declared_validators`,
        ungated) or its registry; an unknown name is a harness fault."""
        tool = self._resolve(role, name)
        cost = getattr(tool, "cost", None)
        deadline = float(getattr(cost, "wall_s", 0.0) or 10.0)
        try:
            result = self.run(tool, ctx, args, deadline_s=deadline)
        except ForbiddenArgument as exc:
            if exc.detail != PRIVATE_PROBE_CODE:
                raise
            # The `ToolExecutor` half answers the refusal as an OUTCOME (the
            # roadmap protocol: `execute` returns, it does not halt), so a
            # metrology loop scores the probe and continues the trial.
            return ToolOutcome(
                ok=False,
                code=PRIVATE_PROBE_CODE,
                observation=None,
                observation_sha256="",
                truncated=False,
                error_class="policy",
                fresh=True,
                text=f"refused: {PRIVATE_PROBE_CODE}",
            )
        run = self.runs[-1]
        observation = result.observation
        render = getattr(observation, "render", None)
        return ToolOutcome(
            ok=run.ok,
            code=run.code,
            observation=observation,
            observation_sha256=run.observation_sha256,
            truncated=False,
            error_class="" if run.ok else "tool_expected",
            fresh=True,
            raw_output_sha256=run.raw_output_sha256,
            text=render() if callable(render) else str(result.payload),
        )

    @staticmethod
    def _resolve(role: str, name: str) -> Any:
        from elt_taskgen.review.tools.critic_validators import declared_validators

        for tool in declared_validators(role):
            if str(getattr(tool, "name", "")) == name:
                return tool
        tool = ToolRegistry.for_role(role).get(name)
        if tool is None:
            raise ToolHarnessFault(name, code="unknown_validator")
        return tool

    # -- the run log -----------------------------------------------------------

    def _log(self, ctx: Any, name: str, args: Mapping[str, Any], result: Any, *, elapsed_ms: int) -> None:
        observation = getattr(result, "observation", None)
        payload = str(getattr(result, "payload", "") or "")
        dump = getattr(observation, "model_dump", None)
        if callable(dump):
            raw = canonical_json(_review_provider_types._plain_data(dump(mode="json"))).encode("utf-8")
        else:
            raw = payload.encode("utf-8")
        reported = str(getattr(result, "raw_output_sha256", "") or "")
        render = getattr(observation, "render", None)
        ok = bool(getattr(observation, "ok", True))
        code = str(getattr(observation, "code", "") or ("ok" if ok else "failed"))
        run = ValidatorRun(
            index=len(self.runs),
            role=str(getattr(ctx, "role", "") or ""),
            name=name,
            args_sha256=sha256_hex(canonical_json(_review_provider_types._plain_data(dict(args)))),
            observation=render() if callable(render) else payload,
            observation_sha256=sha256_hex(payload),
            payload=payload,
            raw_output=raw,
            raw_output_sha256=reported or sha256_hex(raw.decode("utf-8")),
            code=code,
            ok=ok,
            wall_ms=int(elapsed_ms),
        )
        with self._lock:
            self.runs.append(run)

    def close(self) -> None:
        """Destroy the executor: no further run, the log kept for the record
        already written (the caller drops the object)."""
        self.closed = True
        self._worker = None


@dataclass
class _TrialBinding:
    """What `begin_trial` held from the metrology `TrialContext` (duck-typed:
    `trial_nonce`, `workspace`, `limits_by_role`, `tool_policy_by_role`, plus
    the TaskIR the seats are asked about — `task`, or `trial.task`)."""

    ctx: Any
    nonce: str
    workspace: Path | None
    task: Any
    task_content_hash: str
    trial_index: int | None
    limits_by_role: Mapping[str, Any]
    policy_by_role: Mapping[str, Any]
    executor: TrialToolExecutor
    previous_task_content_hash: str


def _plain_role_key(value: Any) -> str:
    return str(getattr(value, "value", value))


def _limits_block_for(role_name: str, by_role: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The `session:` block a trial context declares for `role_name`, if
    any: a `SessionLimits` (its `block`), a mapping, or None."""
    if not isinstance(by_role, Mapping):
        return None
    declared = None
    for key, value in by_role.items():
        if _plain_role_key(key) == role_name:
            declared = value
            break
    if declared is None:
        return None
    block = getattr(declared, "block", None)
    if isinstance(block, Mapping):
        return dict(block)
    if isinstance(declared, Mapping):
        return dict(declared)
    return None


def session_findings_text(role_name: str, findings: Sequence[Any]) -> str:
    """Serialize a critic session's screened findings to the normal wire form.

    When screening changes nothing, preserve the runner's normalized bytes. Otherwise
    emit the closed canonical finding fields used by one-shot parsing.
    """
    from elt_taskgen.models import FindingScreenStatus
    from elt_taskgen.review.tools.critic_validators import UNCOMPILABLE_AFTER_CORRECTIONS

    items: list[dict] = []
    for finding in findings:
        screen = getattr(finding, "screen", None)
        harness_voided = (
            screen is not None
            and screen.status is FindingScreenStatus.VOID
            and UNCOMPILABLE_AFTER_CORRECTIONS in tuple(screen.signals)
        )
        if harness_voided:
            severity = finding.severity
            attack = None
            proposal = None
        else:
            severity = (screen.claimed_severity if screen is not None and screen.claimed_severity else finding.severity)
            attack = finding.suggested_attack or (screen.withheld_attack if screen is not None else None)
            proposal = finding.proposed_case or (screen.withheld_proposal if screen is not None else None)
        item: dict[str, Any] = {
            "severity": severity.value,
            "summary": finding.summary,
            "detail": finding.detail,
            "route_hint": finding.route_hint.value if finding.route_hint is not None else None,
            "suggested_attack": attack.value if attack is not None else None,
        }
        if role_name in _provider_protocol.PROPOSAL_ROLES:
            item["proposed_case"] = proposal.model_dump(mode="json") if proposal is not None else None
            # R02: carry the provider's disposition. A finding without one
            # fails the validation below; the harness never supplies it.
            disposition = getattr(finding, "disposition", None)
            item["disposition"] = disposition.value if disposition is not None else None
        items.append(item)
    data = {"findings": items}
    # Session findings are already typed ProposedAttackCase instances produced
    # only after the live wire payload passed strict provider validation. Their
    # model dump is the deliberately closed internal canonical form (decoded
    # params plus the derived combined expected_pass map), not a second active
    # provider response.
    problem = _provider_protocol.validate_payload_for(role_name, data, normalized=True)
    if problem is not None:
        raise ToolHarnessFault("session_findings", code="findings_not_wire_valid")
    return _provider_protocol.normalized_text_for(role_name, data)


class RoutedProvider:
    """Route council calls through transcript, replay, and budget controls.

    Memo hits require the current route and behavior. Replay-only misses raise without
    network access. Live calls reserve before transport, record before returning, and
    charge every attempt, including attempts ending in backend errors. Transcript
    identities use the provider's agents document, and optional admission/source
    metadata is recorded for audit.
    """

    def __init__(
        self,
        routing: _provider_config.RoleRouting,
        store: _review_transcripts.TranscriptStore,
        meter: _review_provider_accounting.CostMeter,
        *,
        replay_only: bool = False,
        refresh: bool = False,
        task_id: str = "",
        transports: Mapping[str, _review_provider_transport.Transport] | None = None,
        admission: Mapping[str, str] | None = None,
        source: str = "",
    ):
        self.routing = routing
        self.store = store
        self.meter = meter
        #: The agents document the manifest, key and route block hash: the
        #: explicit `--agents-config` the routing was loaded from, else None
        #: for the repository default (see `agents_config_of`).
        self.agents_config: Path | None = _provider_config.agents_config_of(routing)
        #: Construction-scoped raw declaration. Routing was parsed from these
        #: same bytes, and every behavior/key/backend read below receives this
        #: mapping rather than reopening `self.agents_config` by pathname.
        self.agents_document: Mapping[str, Any] = _provider_config._agents_document_of(routing)
        self.replay_only = replay_only
        self.refresh = refresh
        self.task_id = task_id
        self.task_content_hash = ""
        #: One entry per exchange actually consumed in the current task stage.
        #: The review runner binds this manifest into its ledger payload.
        self.exchange_evidence: list[dict[str, Any]] = []
        self._transports = dict(transports or {})
        self._backends: dict[str, Any] = {}
        #: Admission provenance stamped into recorded transcripts (empty = not
        #: stamped).
        self.admission_provenance: dict[str, str] = {
            str(k): str(v) for k, v in dict(admission or {}).items()
        }
        self.source = str(source or "")
        #: The bounded sessions currently open on this provider, by role:
        #: `run_session` opens one (`begin_session`) so every `_turn` of the
        #: session meters against ONE trajectory and the single evidence row
        #: sums every turn.
        self._sessions: dict[str, SessionAccount] = {}
        #: The TASK context (`begin_task_evidence(..., task=)`): the TaskIR a
        #: harness-validated critic seat's session runs against when
        #: `complete` dispatches it outside a metrology trial (a production
        #: review). None = no task context (the one-shot path).
        self._task: Any = None
        #: The TRIAL context (`begin_trial` .. `end_trial`): the metrology
        #: trial the next `complete` calls belong to, with its executor.
        self._trial: _TrialBinding | None = None

    def transcript_key_for(self, role, prompt: str) -> str:
        """The key `complete(role, prompt)` records and serves under: the
        one-shot `transcript_key` over THIS provider's agents document.
        Callers that pre-compute a key to point at an exchange (the
        independent builders, calibration) must use this, not the module
        function, or a custom `--agents-config` keys them apart."""
        role_name = getattr(role, "value", str(role))
        return _provider_protocol.transcript_key(role_name, prompt, agents_config=self.agents_document)

    def begin_task_evidence(
        self, task_id: str, task_content_hash: str, *, task: Any = None
    ) -> None:
        """Bind subsequent exchanges to one task identity and reset per-task evidence.

        Capture task id, content hash, optional TaskIR, and provenance without exposing
        task-private data to stored prompts.
        """
        self.task_id = str(task_id)
        self.task_content_hash = str(task_content_hash)
        self.exchange_evidence = []
        self._task = task

    @staticmethod
    def _finding_count(role_name: str, response: str) -> int | None:
        if role_name not in _provider_protocol.PROPOSAL_ROLES:
            return None
        try:
            parsed = json.loads(response)
        except (json.JSONDecodeError, TypeError):
            return None
        findings = parsed.get("findings") if isinstance(parsed, dict) else None
        return len(findings) if isinstance(findings, list) else None

    def _append_exchange_evidence(
        self,
        role_name: str,
        prompt_sha: str,
        response: str,
        entry: Mapping[str, Any],
        *,
        replayed: bool,
        usd: float = 0.0,
    ) -> None:
        raw_attempts = entry.get("raw_attempts")
        attempt_count = (
            len(raw_attempts) if isinstance(raw_attempts, (list, tuple)) else 0
        )
        finding_count = self._finding_count(role_name, response)
        usage = _review_provider_types.Usage.from_dict(
            entry.get("usage") if isinstance(entry.get("usage"), Mapping) else None
        )
        route_binding = (
            dict(entry.get("route"))
            if isinstance(entry.get("route"), Mapping)
            else {}
        )
        corrections = max(0, attempt_count - 1)
        row: dict[str, Any] = {
            "task_id": self.task_id,
            "task_content_hash": self.task_content_hash,
            "role": role_name,
            "prompt_sha256": prompt_sha,
            "response_sha256": sha256_hex(response),
            "attempt_count": attempt_count,
            "correction_count": corrections,
            # A one-shot trajectory has only schema-corrected model calls and a
            # SUBMITTED terminal: no tools, refusals, nudges, validators, or stale
            # results. Calls are all live unless the whole exchange was replayed.
            "model_call_count": attempt_count,
            "tool_call_count": 0,
            "refused_count": 0,
            "nudge_count": 0,
            "validator_run_count": 0,
            "correction_kinds": {"schema": corrections, "compile": 0},
            "terminal": "SUBMITTED",
            "live_model_call_count": 0 if replayed else attempt_count,
            "stale_tool_result_count": 0,
            "entry_schema": _provider_protocol.transcript_entry_schema(entry),
            "finding_count": finding_count,
            "zero_findings": finding_count == 0 if finding_count is not None else None,
            "provider": str(entry.get("provider") or ""),
            "model": str(entry.get("model") or ""),
            # Evidence must remain attributable to the exact prompt, wire
            # schema/tool surface, and bounded-session policy that produced it.
            # These are copied from the route binding the transcript verifier
            # already checks; dropping them here made the review manifest
            # impossible to revalidate after a behavior change.
            "behavior_sha256": str(
                route_binding.get("behavior_sha256")
                or entry.get("system_sha256")
                or ""
            ),
            "tools_sha256": str(route_binding.get("tools_sha256") or ""),
            "policy_sha256": str(route_binding.get("policy_sha256") or ""),
            "diagnostics_version": str(
                route_binding.get("diagnostics_version") or ""
            ),
            "replayed": replayed,
            # The SoT T8 row fields the meter can state per exchange: the
            # four token counters under their short names, the USD this
            # exchange was priced at (0 when replayed: nothing was spent)
            # and the harness-measured transport wall.
            "usage": {
                "input": usage.input_tokens,
                "output": usage.output_tokens,
                "cache_read": usage.cache_read_input_tokens,
                "cache_write": usage.cache_creation_input_tokens,
            },
            "usd": 0.0 if replayed else float(usd),
            "wall_ms": 0 if replayed else int(entry.get("elapsed_ms") or 0),
        }
        if self._trial is not None:
            # Trial evidence rows carry nonce and index; outside trials they are
            # unchanged. These keys are excluded from manifest hashing so worker
            # count cannot change the digest.
            row["trial_nonce"] = self._trial.nonce
            if self._trial.trial_index is not None:
                row["trial_index"] = self._trial.trial_index
        self.exchange_evidence.append(row)

    def _record_stamp(self) -> dict:
        """The optional provenance keys every record path adds to an entry."""
        stamp: dict = {}
        if self.admission_provenance:
            stamp["admission"] = dict(self.admission_provenance)
        if self.source:
            stamp["recorded_by"] = self.source
        return stamp

    @staticmethod
    def _route_block(
        route: _provider_config.RoleRoute,
        *,
        agents_config: Path | str | None = None,
        entry_schema: int = _provider_protocol.TRANSCRIPT_ENTRY_SCHEMA,
        policy: SessionPolicy | None = None,
    ) -> dict:
        """Build the route binding stored with a transcript.

        The block includes provider, model, effort, token limit, behavior, tools,
        policy, diagnostic version, and entry schema. It excludes budget caps and
        prompt-cache settings. Session turns use the policy currently in force.
        """
        declared = policy if policy is not None else _provider_protocol.session_policy_for(
            route.role, agents_config=agents_config
        )
        return {
            "provider": route.provider,
            "model": route.model,
            "max_tokens": int(route.max_tokens),
            "effort": route.effort,
            "behavior_sha256": _provider_protocol.role_behavior_sha256(route.role, agents_config=agents_config),
            "tools_sha256": (
                declared.tools_sha256() if policy is not None else _provider_protocol.role_tools_sha256(route.role)
            ),
            "policy_sha256": declared.sha256(),
            "diagnostics_version": DIAGNOSTICS_VERSION,
            "entry_schema": int(entry_schema),
        }

    def _lookup_bound(
        self,
        role_name: str,
        prompt_sha: str,
        route: _provider_config.RoleRoute,
        *,
        policy: SessionPolicy | None = None,
    ) -> dict | None:
        """Return a stored entry only when its route and task bindings are valid.

        Refresh bypasses storage. Route mismatches are misses in live mode and errors in
        replay-only mode; different tasks or invalid response digests fail closed. Live
        mode may reuse the same task and prompt across a content-hash change because
        unseen private material does not alter the exchange, while replay-only requires
        the exact content hash.
        """
        if self.refresh:
            return None
        cached = self.store.lookup(role_name, prompt_sha)
        if cached is None:
            return None
        mismatch = transcript_route_mismatch(
            cached, route, agents_config=self.agents_document, policy=policy
        )
        if mismatch is None:
            response = str(cached["response"])
            if self.task_content_hash:
                recorded_task = str(cached.get("task_id") or "")
                recorded_hash = str(cached.get("task_content_hash") or "")
                recorded_response_sha = str(cached.get("response_sha256") or "")
                problems = []
                if recorded_task != self.task_id:
                    problems.append(
                        f"task_id {recorded_task!r}, expected {self.task_id!r}"
                    )
                hash_moved = recorded_hash != self.task_content_hash
                if hash_moved and (problems or self.replay_only):
                    problems.append(
                        f"task_content_hash {recorded_hash!r}, expected "
                        f"{self.task_content_hash!r}"
                    )
                if recorded_response_sha != sha256_hex(response):
                    problems.append("response_sha256 missing or does not match")
                if problems:
                    raise TranscriptMissingError(
                        f"transcript for {role_name}/{prompt_sha[:12]} is not "
                        "bound to the current task identity: "
                        + "; ".join(problems)
                        + " (re-record; fail closed)"
                    )
                # Same task, same key, verified response, another content
                # hash, live mode: memo-served (F2). The evidence row binds
                # to the CURRENT hash with `replayed=True`.
            return cached
        if self.replay_only:
            raise TranscriptRouteMismatchError(
                f"replay-only: transcript for {role_name}/{prompt_sha[:12]} was "
                f"{mismatch}; restore the routing in config/agents.yaml or "
                "re-record with --record and credentials (a transcript from "
                "another model is not a replay of this route — fail closed)"
            )
        return None

    def _memoized(self, role_name: str, prompt_sha: str, route: _provider_config.RoleRoute) -> str | None:
        """The recorded response for (role, prompt) IF it binds to `route`.

        None on a genuine miss or, in live mode, on a route mismatch (the call
        is remade and re-recorded). Under replay-only a mismatch is a
        FAIL-CLOSED TranscriptRouteMismatchError: a re-routed seat must be
        re-measured on the new model, never re-admitted on the old model's
        transcripts. `refresh` bypasses memoization.
        """
        cached = self._lookup_bound(role_name, prompt_sha, route)
        if cached is None:
            return None
        self._reconcile_cached_durable_reservation(cached, role_name, route)
        response = str(cached["response"])
        self._append_exchange_evidence(
            role_name, prompt_sha, response, cached, replayed=True
        )
        return response

    def _reconcile_cached_durable_reservation(
        self,
        entry: Mapping[str, Any],
        role_name: str,
        route: _provider_config.RoleRoute,
    ) -> None:
        """Commit a durable reservation left uncertain after a recorded exchange.

        A verified memo hit supplies enough route and usage data to reconcile actual
        cost without another provider call.
        """

        ledger = self.meter.durable_ledger
        reservation_id = str(entry.get("budget_reservation_id") or "")
        if ledger is None or not reservation_id:
            return
        usage_block = entry.get("usage")
        if not isinstance(usage_block, Mapping):
            raise ValueError(
                "recorded durable provider exchange has no usage mapping"
            )
        rates = _review_provider_accounting.rate_card_for(
            route.provider,
            route.model,
            self.routing.provider_config.get(route.provider) or {},
        )
        actual = self.meter.price(
            usage=_review_provider_types.Usage.from_dict(usage_block),
            rates=rates,
            provider_reported_usd=_review_provider_accounting._usable_report(
                usage_block.get("provider_reported_usd")
            ),
        )
        try:
            ledger.commit(
                reservation_id,
                actual,
                task_id=self.task_id,
                role=role_name,
            )
        except Exception as exc:  # only a foreign run's unknown id is benign
            from elt_taskgen.review.budget_ledger import UnknownReservationError

            if isinstance(exc, UnknownReservationError):
                return
            raise

    # -- backend construction (deferred: only when a live call is needed) ---

    def _backend(self, route: _provider_config.RoleRoute):
        backend = self._backends.get(route.provider)
        if backend is not None:
            return backend
        cfg = self.routing.provider_config.get(route.provider) or {}
        transport = self._transports.get(route.provider)
        if route.provider == "anthropic":
            backend = _provider_backends.AnthropicBackend(
                str(cfg.get("api_key") or ""),
                base_url=str(cfg.get("base_url") or "https://api.anthropic.com"),
                transport=transport,
                agents_config=self.agents_document,
                workspace_id=str(cfg.get("workspace_id") or ""),
            )
        elif route.provider == "openai_compat":
            backend = _provider_backends.OpenAICompatBackend(
                str(cfg.get("base_url") or ""),
                str(cfg.get("api_key") or ""),
                transport=transport,
                agents_config=self.agents_document,
            )
        elif route.provider in _provider_config.HEADLESS_PROVIDER_KINDS:
            from elt_taskgen.review import headless as headless_mod

            backend_class = (
                headless_mod.ClaudeHeadlessBackend
                if route.provider == "claude_headless"
                else headless_mod.CodexHeadlessBackend
            )
            backend = backend_class(cfg, agents_config=self.agents_document)
        else:  # unreachable: load_role_routing validates provider names
            raise RuntimeError(f"unknown provider {route.provider!r}")
        self._backends[route.provider] = backend
        return backend

    # -- the metrology trial seam (Phase 4; duck-typed on council.Provider) --

    def begin_trial(self, ctx: Any) -> None:
        """Open one isolated metrology trial and bind its task identity directly.

        Create a fresh uncached validator executor for the trial and destroy it in
        `end_trial`. Opening a second trial before closing the first is a harness fault.
        """
        if self._trial is not None:
            raise ToolHarnessFault("begin_trial", code="trial_already_open")
        nonce = str(getattr(ctx, "trial_nonce", "") or "")
        if not nonce:
            raise ToolHarnessFault("begin_trial", code="trial_nonce_missing")
        workspace = getattr(ctx, "workspace", None)
        task = getattr(ctx, "task", None)
        if task is None:
            trial = getattr(ctx, "trial", None)
            task = getattr(trial, "task", None)
        content_hash = str(getattr(ctx, "task_content_hash", "") or "")
        if not content_hash and task is not None:
            hasher = getattr(task, "content_hash", None)
            content_hash = str(hasher()) if callable(hasher) else ""
        index = getattr(ctx, "trial_index", None)
        trial_index = int(index) if isinstance(index, int) and not isinstance(index, bool) else None
        limits_by_role = getattr(ctx, "limits_by_role", None)
        policy_by_role = getattr(ctx, "tool_policy_by_role", None)
        foreign = getattr(ctx, "foreign_nonces", ()) or ()
        executor = TrialToolExecutor(
            trial_nonce=nonce,
            workspace=Path(workspace) if workspace is not None else None,
            foreign_nonces=tuple(str(n) for n in foreign if n),
        )
        self._trial = _TrialBinding(
            ctx=ctx,
            nonce=nonce,
            workspace=Path(workspace) if workspace is not None else None,
            task=task,
            task_content_hash=content_hash,
            trial_index=trial_index,
            limits_by_role=limits_by_role if isinstance(limits_by_role, Mapping) else {},
            policy_by_role=policy_by_role if isinstance(policy_by_role, Mapping) else {},
            executor=executor,
            previous_task_content_hash=self.task_content_hash,
        )
        if content_hash:
            self.task_content_hash = content_hash

    def end_trial(self) -> None:
        """Close the open trial: the executor is destroyed (a later run
        through it is a harness fault), the trial's task binding is dropped
        and the provider's task identity restored. Idempotent: closing with
        no trial open is a no-op, so a loop's `finally` may always call it."""
        binding = self._trial
        if binding is None:
            return
        self._trial = None
        binding.executor.close()
        self.task_content_hash = binding.previous_task_content_hash

    @property
    def active_trial(self) -> Any:
        """The `TrialContext` of the open trial, or None."""
        return None if self._trial is None else self._trial.ctx

    @property
    def trial_executor(self) -> "TrialToolExecutor | None":
        """The open trial's validator executor (tests read `cache` and
        `runs` off it), or None."""
        return None if self._trial is None else self._trial.executor

    def _context_task(self) -> Any:
        """The TaskIR of the active context: the trial's, else the task
        context's, else None."""
        if self._trial is not None:
            return self._trial.task
        return self._task

    def _dispatches_session(self, role_name: str) -> bool:
        """Does `complete(role_name, view)` run a bounded session? Only a
        harness-validated critic seat (`AGENTIC_ROLES`) whose declared block
        in THIS provider's agents document says `enabled: true`, and only
        while a trial or task context is active. Everything else is the
        one-shot path, byte-identical."""
        if self._trial is None and self._task is None:
            return False
        return _provider_protocol.agentic_role_enabled(role_name, agents_config=self.agents_document)

    # -- council.Provider ----------------------------------------------------

    def complete(self, role, prompt: str) -> str:
        role_name = getattr(role, "value", str(role))
        if self._dispatches_session(role_name):
            # Phase 4: a harness-validated critic seat under a trial or task
            # context runs a bounded session and answers its final
            # normalized findings text; `run_council` never learns that the
            # seat made more than one call (metrology redesign §5).
            return self._complete_session(role_name, prompt)
        # BOTH halves of the exchange (system + user prompt): keying on the
        # user prompt alone replays stale behavior forever after a prompt edit.
        # Keyed over THIS provider's agents document (finding: a custom
        # --agents-config must not share the default's digest).
        prompt_sha = _provider_protocol.transcript_key(role_name, prompt, agents_config=self.agents_document)

        # The route is resolved BEFORE the memo lookup, so an unrouted role
        # fails closed here rather than replaying some other routing's record.
        route = self.routing.for_role(role_name)
        memoized = self._memoized(role_name, prompt_sha, route)
        if memoized is not None:
            return memoized

        if self.replay_only:
            searched = ", ".join(str(d) for d in self.store._search_dirs()) or "(none)"
            raise TranscriptMissingError(
                f"replay-only mode: no recorded transcript for role "
                f"{role_name!r} / prompt {prompt_sha[:12]} (searched: "
                f"{searched}). Transcripts not seeded — run "
                "'elt-taskgen record-transcripts' with an API key."
            )

        # Price the route BEFORE spending on it: raising after the call would
        # mean money already spent on a route the meter cannot account for.
        rates = _review_provider_accounting.rate_card_for(
            route.provider,
            route.model,
            self.routing.provider_config.get(route.provider) or {},
        )
        try:
            backend = self._backend(route)
        except _provider_backends.MissingCredentialsError as exc:
            raise _provider_backends.MissingCredentialsError(
                f"{exc} — and no recorded transcript exists for role "
                f"{role_name!r} / prompt {prompt_sha[:12]}, so the call "
                "cannot be served offline (fail closed)"
            ) from exc

        # Reserve before transport; refusal records and spends nothing, while
        # post-call charging catches output overruns. Disabled session caps do not apply.
        trajectory = self.meter.trajectory(
            self.task_id,
            role_name,
            enforce_role_cap=not route.cap_declared_but_disabled,
        )
        budget_reservation_id = trajectory.reserve(
            _review_provider_accounting.estimate_turn_usd(
                rates=rates, max_tokens=route.max_tokens, prompt=prompt
            )
        )

        loop_limits = _provider_protocol.role_behavior_manifest(
            role_name, agents_config=self.agents_document
        ).get("loop_limits")
        loop_limits = loop_limits if isinstance(loop_limits, Mapping) else {}
        wall_s = SessionLimits(loop_limits).session_wall_seconds if loop_limits else None
        try:
            result = _review_provider_transport._call_with_wall_deadline(
                lambda: backend.complete(
                    role_name=role_name,
                    model=route.model,
                    prompt=prompt,
                    max_tokens=route.max_tokens,
                    effort=route.effort,
                    prompt_cache=bool(route.prompt_cache),
                ),
                deadline_s=wall_s,
                role_name=role_name,
            )
        except Exception as exc:
            # A raising backend still PAID for the attempts it made (schema
            # retries exhausted, a transport fault on the second attempt);
            # they reach the meter before the fault propagates. A budget
            # breach found here chains onto the fault it was found under.
            self._charge_attempts(
                trajectory, rates, _review_provider_types.backend_attempts(exc), model=route.model
            )
            raise

        # ALWAYS record before returning (money spent must never be lost).
        usage = result.usage
        entry = {
            "role": role_name,
            "prompt_sha256": prompt_sha,
            "system_sha256": _provider_protocol.role_behavior_sha256(role_name, agents_config=self.agents_document),
            "provider": route.provider,
            "model": result.model,
            #: The model id the provider REPORTED serving ('' when the wire
            #: carried none); `model` stays what was asked for.
            "served_model": result.served_model,
            "response": result.text,
            "response_sha256": sha256_hex(result.text),
            "task_id": self.task_id,
            "task_content_hash": self.task_content_hash,
            "attempt_count": len(result.raw_attempts),
            "correction_count": max(0, len(result.raw_attempts) - 1),
            "finding_count": self._finding_count(role_name, result.text),
            "usage": usage.as_dict(),
            #: Harness-measured transport wall summed across the attempts.
            "elapsed_ms": int(result.elapsed_ms),
            "raw_attempts": list(result.raw_attempts),
            # The binding transcript_route_mismatch checks at serve time.
            "route": self._route_block(route, agents_config=self.agents_document),
        }
        if entry["finding_count"] is not None:
            entry["zero_findings"] = entry["finding_count"] == 0
        entry.update(self._record_stamp())
        if result.reported_usd is not None:
            # What the provider says it billed, kept beside the tokens: a
            # transcript is the only surviving record of a spent call.
            entry["usage"]["provider_reported_usd"] = result.reported_usd
        if budget_reservation_id is not None:
            # This is accounting identity only—no prompt, credential, or
            # provider secret. It closes the crash window between immutable
            # transcript publication and the durable ledger commit.
            entry["budget_reservation_id"] = budget_reservation_id
        self.store.record(role_name, prompt_sha, entry)
        self._append_exchange_evidence(
            role_name,
            prompt_sha,
            result.text,
            entry,
            replayed=False,
            usd=self._priced_usd(result, rates),
        )

        # Charge per API ATTEMPT, after the record: a breach raises with the
        # transcript already on disk.
        self._charge_attempts(
            trajectory, rates, self._attempts_of(result), model=result.model
        )
        return result.text

    @staticmethod
    def _attempts_of(result: _review_provider_types.BackendResult) -> tuple[_review_provider_types.AttemptRecord, ...]:
        """The attempts to meter for a completed exchange: the backend's
        per-attempt records, else one record from the summed counters (a
        backend double that states no attempts is still charged once)."""
        if result.attempts:
            return result.attempts
        return (
            _review_provider_types.AttemptRecord(
                usage=result.usage,
                elapsed_ms=0,
                served_model="",
                reported_usd=result.reported_usd,
            ),
        )

    def _priced_usd(self, result: _review_provider_types.BackendResult, rates: _review_provider_accounting.RateCard) -> float:
        """What `charge` will add for this exchange (a pure computation, so it
        can sit in the evidence row before the meter enforces anything)."""
        return sum(
            self.meter.price(
                usage=a.usage, rates=rates, provider_reported_usd=a.reported_usd
            )
            for a in self._attempts_of(result)
        )

    @staticmethod
    def _charge_attempts(
        trajectory: "TrajectoryBudget",
        rates: _review_provider_accounting.RateCard,
        attempts: Sequence[_review_provider_types.AttemptRecord],
        *,
        model: str,
    ) -> float:
        """Meter every attempt in order; returns the USD charged. A breach
        raises from the attempt that crossed the line, but only after every
        attempt the backend already made has been accounted for."""
        charged = 0.0
        attempted_charge = False
        first_enforcement_error: _review_provider_accounting.BudgetExceededError | None = None
        try:
            for attempt in attempts:
                priced = trajectory.meter.price(
                    usage=attempt.usage,
                    rates=rates,
                    provider_reported_usd=attempt.reported_usd,
                )
                attempted_charge = True
                try:
                    applied = trajectory.charge(
                        usage=attempt.usage,
                        rates=rates,
                        provider_reported_usd=attempt.reported_usd,
                        elapsed_ms=attempt.elapsed_ms,
                        model=model,
                    )
                except _review_provider_accounting.BudgetExceededError as exc:
                    # The backend has already made every attempt in this batch.
                    # CostMeter accounts before enforcing caps, so retain the
                    # first refusal but continue charging the remaining paid
                    # attempts before reconciling the durable reservation.
                    charged += priced
                    if first_enforcement_error is None:
                        first_enforcement_error = exc
                else:
                    charged += applied
            if first_enforcement_error is not None:
                raise first_enforcement_error
            return charged
        finally:
            if attempted_charge:
                # One prospective reservation covers this bounded transport
                # exchange, including its schema/HTTP attempts.
                trajectory._commit_durable_reservation(charged)

    # Bounded `_turn` preserves memo/replay, fail-closed rate lookup, reserve,
    # one backend call, record, then charge. `run_session` appends one evidence row.

    def _complete_session(self, role_name: str, view: str) -> str:
        """Run a harness-validated critic session and return normalized findings text.

        The trial executor validates submissions, then the provider records one evidence
        row, the trajectory, and raw tool evidence. Submitted or auto-submitted green
        drafts return findings. A trial with no draft or a policy violation scores an
        empty list; ordinary tasks fail closed. Harness, truncation, tripwire, and
        protocol faults always propagate.
        """
        from elt_taskgen.review.tools import critic_validators as cv

        task = self._context_task()
        if task is None:
            raise ToolHarnessFault("critic_session", code="no_task_in_context")
        binding = self._trial
        declared_block = _limits_block_for(role_name, binding.limits_by_role if binding else None)
        limits = cv.critic_limits(role_name, declared_block, agents_config=self.agents_document)
        policy = cv.critic_policy(role_name, limits, agents_config=self.agents_document)
        session = cv.CriticSession(
            task=task, role=role_name, workspace=binding.workspace if binding else None
        )
        scratch: str | None = None
        root: Path | None = binding.workspace if binding is not None else None
        if root is None or not root.is_dir():
            scratch = tempfile.mkdtemp(prefix="critic-session-")
            root = Path(scratch)
        executor = binding.executor if binding is not None else TrialToolExecutor()
        # Window the shared trial executor per session so one seat's trajectory
        # and raw tier cannot contain another seat's validator observations.
        run_offset = len(getattr(executor, "runs", ()) or ())
        probes_before = int(getattr(executor, "private_probe_count", 0) or 0)
        halt: SessionPolicyViolation | None = None
        try:
            ctx = session.context(root)
            account, result, halt = self._run_session_core(
                role_name, view, policy, ctx,
                worker=executor,
                catch_policy_violation=binding is not None,
            )
        finally:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
            if binding is None:
                executor.close()
        if halt is not None:
            result = getattr(halt, "session_result")
            terminal_name = "POLICY_VIOLATION"
            text = _provider_protocol.normalized_text_for(role_name, {"findings": []})
        else:
            terminal_name = str(getattr(result.terminal, "name", result.terminal))
            text = self._session_final_text(
                role_name,
                result,
                session,
                task,
                allow_no_submission=self._trial is not None,
            )
        probes = max(
            0, int(getattr(executor, "private_probe_count", 0) or 0) - probes_before
        )
        self._append_session_evidence(
            account, result, final_text=text, terminal_name=terminal_name,
            private_probe_count=probes,
            compile_correction_exhausted=int(
                getattr(session, "compile_correction_exhausted", 0) or 0
            ),
        )
        trajectory_path = self._write_trajectory_record(
            account, result, executor,
            final_text=text, terminal_name=terminal_name, task=task,
            stop_reason="policy_violation" if halt is not None else "",
            run_offset=run_offset,
        )
        if halt is None and not self.replay_only and self.store.record_dir is not None:
            try:
                self.store.record_session(
                    role_name,
                    account.prompt_sha256,
                    self._session_record(
                        account,
                        result,
                        trajectory_sha256=(trajectory_path.stem if trajectory_path is not None else ""),
                    ),
                )
            except OSError as exc:
                fault = ProviderFault(
                    "the completed session's record could not be persisted",
                    code="session_store_io",
                )
                raise fault from exc
        return text

    @staticmethod
    def _session_final_text(
        role_name: str,
        result: Any,
        session: Any,
        task: Any,
        *,
        allow_no_submission: bool = False,
    ) -> str:
        """The wire text of a completed critic session: the screened findings
        of `council.findings_from_session` (the ONLY path from a critic
        `SessionResult` to `screen_findings`) projected back onto the wire."""
        from elt_taskgen.models import CouncilRole
        from elt_taskgen.review.council import findings_from_session

        findings = findings_from_session(
            task,
            CouncilRole(role_name),
            result,
            session,
            task.content_hash()[:8],
            allow_no_submission=allow_no_submission,
        )
        return session_findings_text(role_name, findings)

    def begin_session(
        self, role, policy: SessionPolicy, *, max_usd: float | None = None
    ) -> "SessionAccount":
        """Open the provider-side budget ledger for one role session.

        Refuse overlapping sessions for the same role. Apply the declared or explicit
        cap and retain the ledger until the session closes.
        """
        role_name = getattr(role, "value", str(role))
        if not isinstance(policy, SessionPolicy):
            raise TypeError("begin_session needs a SessionPolicy")
        if policy.role != role_name:
            raise ValueError(
                f"session policy is for role {policy.role!r}, not {role_name!r}"
            )
        if role_name in self._sessions:
            raise RuntimeError(
                f"a session for role {role_name!r} is already open on this provider"
            )
        route = self.routing.for_role(role_name)
        cap = policy.limits.max_usd if max_usd is None else float(max_usd)
        trajectory = self.meter.trajectory(
            self.task_id, role_name, max_usd=cap, enforce_role_cap=True
        )
        account = SessionAccount(
            role=role_name,
            policy=policy,
            trajectory=trajectory,
            provider=route.provider,
            model=route.model,
        )
        self._sessions[role_name] = account
        return account

    def end_session(self, account: "SessionAccount") -> None:
        """Close the ledger `begin_session` opened (idempotent). A headless
        harness open for the role is ended with it: its pending call answered
        with the session-ended refusal, its turn interrupted, its process
        closed."""
        if self._sessions.get(account.role) is account:
            del self._sessions[account.role]
        backend = self._backends.get(account.provider)
        close_session = getattr(backend, "close_session", None)
        if callable(close_session):
            close_session(account.role)

    @contextlib.contextmanager
    def session(
        self, role, policy: SessionPolicy, *, max_usd: float | None = None
    ) -> Iterator["SessionAccount"]:
        """`begin_session` / `end_session` as a context manager."""
        account = self.begin_session(role, policy, max_usd=max_usd)
        try:
            yield account
        finally:
            self.end_session(account)

    def active_session(self, role) -> "SessionAccount | None":
        """The open session ledger for `role`, if any."""
        return self._sessions.get(getattr(role, "value", str(role)))

    @staticmethod
    def _messages_text(messages: Sequence[Mapping[str, Any]]) -> str:
        """Every text the prefix carries (the prompt floor of the reserve)."""
        parts: list[str] = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
                continue
            for block in content or ():
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_result":
                    parts.append(str(block.get("content", "")))
                elif block.get("type") == "tool_use":
                    parts.append(_provider_backends._arguments_text(block.get("input")))
        return "".join(parts)

    @staticmethod
    def _user_messages_since_last_assistant(messages: Sequence[Mapping[str, Any]]) -> list[dict]:
        """The user turns after the last assistant turn: what the runner fed
        back (tool results, corrections, or turn 0's view)."""
        tail: list[dict] = []
        for message in reversed(list(messages)):
            if message.get("role") == "assistant":
                break
            tail.append(_review_provider_types._plain_data(message))
        tail.reverse()
        return tail

    @classmethod
    def _recordable_user_messages(
        cls, messages: Sequence[Mapping[str, Any]], turn_index: int
    ) -> list[dict]:
        """Return user messages safe to persist with a session turn.

        Later tool results and fixed corrections are stored verbatim. Turn-zero views
        are represented only by digests so private author or reference material is not
        copied into transcript storage.
        """
        tail = cls._user_messages_since_last_assistant(messages)
        if int(turn_index) > 0:
            return tail
        return [
            {
                "role": str(message.get("role") or "user"),
                "content_sha256": sha256_hex(canonical_json(_review_provider_types._plain_data(message.get("content")))),
                "content_omitted": "initial_view",
            }
            for message in tail
        ]

    @staticmethod
    def _turn_block_integrity_problem(
        entry: Mapping[str, Any],
        *,
        key: str,
        prefix: Sequence[Mapping[str, Any]],
        observations: Sequence[str] = (),
    ) -> str:
        """Return why a stored session turn block fails integrity, or `None`.

        Verify content, memo key, messages, wire choice/tools, and observation digests
        before serving the entry.
        """
        block = entry.get("turn")
        if not isinstance(block, Mapping):
            return ""
        problems: list[str] = []
        # The schema-3 recorder always writes memo_key, messages_sha256 and
        # content: a turn block missing any of them is a corrupt store, never
        # a legacy shape (legacy entries carry no turn block at all).
        recorded_key = str(block.get("memo_key") or "")
        if not recorded_key:
            problems.append("turn.memo_key missing")
        elif recorded_key != key:
            problems.append("turn.memo_key is not the serving key")
        recorded_messages = str(block.get("messages_sha256") or "")
        if not recorded_messages:
            problems.append("turn.messages_sha256 missing")
        elif recorded_messages != sha256_hex(canonical_json(plain_messages(prefix))):
            problems.append("turn.messages_sha256 does not match the message prefix")
        content = block.get("content")
        if content is None:
            problems.append("turn.content missing")
        else:
            recorded_content = str(block.get("content_sha256") or "")
            if not recorded_content:
                problems.append("turn.content_sha256 missing")
            elif recorded_content != sha256_hex(canonical_json(_review_provider_types._plain_data(content))):
                problems.append("turn.content_sha256 does not match turn.content")
        if "observations_sha256" in block:
            recorded_obs = [str(d) for d in (block.get("observations_sha256") or [])]
            if recorded_obs != [str(d) for d in observations if d]:
                problems.append("turn.observations_sha256 does not match the observations that fed this turn")
        return "; ".join(problems)

    @staticmethod
    def _turn_block_wire_mismatch(
        entry: Mapping[str, Any], *, wire_tools: Sequence[Mapping[str, Any]], choice: Any
    ) -> str:
        """Why a stored SESSION entry was recorded under another per-turn
        wire than this call sends, or '': the exact `tools[]` digest and the
        canonical `tool_choice` of the request (the policy's `tools_sha256`
        covers the full wire list, not the narrowed last-turn or forced
        wire of one turn). A turn recorded with submit-only tools is not a
        replay of a request that offered every tool, and vice versa."""
        block = entry.get("turn")
        if not isinstance(block, Mapping):
            return ""
        recorded_tools = str(block.get("tools_sha256") or "")
        now_tools = sha256_hex(canonical_json([dict(t) for t in wire_tools]))
        if recorded_tools and recorded_tools != now_tools:
            return (
                f"recorded under per-turn tools {recorded_tools[:12]}, "
                f"this turn sends {now_tools[:12]}"
            )
        if "tool_choice" in block:
            recorded_choice = _review_provider_types._plain_data(block.get("tool_choice"))
            now_choice = _review_provider_types._plain_data(_provider_backends._anthropic_tool_choice(choice))
            if recorded_choice != now_choice:
                return (
                    f"recorded under tool_choice {recorded_choice!r}, this turn "
                    f"sends {now_choice!r}"
                )
        return ""

    @staticmethod
    def _turn_text(role_name: str, policy: SessionPolicy, turn: _review_provider_types.BackendTurn) -> str:
        """The `response` recorded for a session turn, chosen so a one-shot
        `complete()` on the same key serves the same text: a valid payload on
        the schema role's forced submit tool normalizes exactly as the
        one-shot path does; a plain text reply is that text; anything else
        (a model-initiated tool call, a refused turn) is the canonical JSON
        of the assistant content blocks."""
        fault = turn.protocol_fault()
        if fault is None:
            call = turn.tool_uses[0]
            submit = policy.submit_tool
            if submit and call.get("name") == submit and _provider_protocol.uses_findings_schema(role_name):
                data = json.loads(json.dumps(call.get("input")))
                if _provider_protocol.validate_payload_for(role_name, data) is None:
                    return _provider_protocol.normalized_text_for(role_name, data)
        elif fault.code == "no_tool_call":
            text = turn.text_content
            if text.strip():
                return text
        return canonical_json([_review_provider_types._plain_data(b) for b in turn.content])

    def _turn_from_entry(
        self,
        entry: Mapping[str, Any],
        *,
        role_name: str,
        policy: SessionPolicy,
        key: str,
        turn_index: int,
        route: _provider_config.RoleRoute,
    ) -> _review_provider_types.BackendTurn:
        """A stored entry served as a turn (zero HTTP, nothing spent): the
        `turn.content` blocks of a schema-3 entry; for a one-shot entry the
        content of its LAST raw attempt (the one `complete()` served); for a
        legacy fixture without raw bodies, blocks rebuilt from `response`."""
        turn_block = entry.get("turn") if isinstance(entry.get("turn"), Mapping) else {}
        raw_attempts = entry.get("raw_attempts") or []
        raw = raw_attempts[-1] if raw_attempts and isinstance(raw_attempts[-1], Mapping) else {}
        content = turn_block.get("content")
        stop_reason = turn_block.get("stop_reason")
        response = str(entry.get("response") or "")
        # Whether the served blocks come from stored bytes (a turn block or a
        # raw attempt) rather than being rebuilt from the verified `response`;
        # stored bytes are verified against `response` before they serve.
        from_store = content is not None
        if content is None:
            if isinstance(raw.get("content"), list):
                content = _provider_backends.AnthropicBackend._content_blocks(raw)
                stop_reason = raw.get("stop_reason")
                from_store = True
            elif raw.get("choices"):
                content = _provider_backends._chat_content_blocks(_provider_backends.OpenAICompatBackend._message(raw))
                first = (raw.get("choices") or [{}])[0]
                stop_reason = first.get("finish_reason") if isinstance(first, Mapping) else None
                from_store = True
            elif policy.submit_tool and _provider_protocol.uses_findings_schema(role_name):
                data = json.loads(response) if response else {}
                payload = {k: v for k, v in data.items() if k != "role"} if isinstance(data, Mapping) else {}
                content = [
                    {
                        "type": "tool_use",
                        "id": f"toolu_replay_{turn_index}",
                        "name": policy.submit_tool,
                        "input": payload,
                    }
                ]
                stop_reason = "tool_use"
            else:
                content = [{"type": "text", "text": response}]
                stop_reason = "end_turn"
        usage_block = entry.get("usage") if isinstance(entry.get("usage"), Mapping) else {}
        reported = usage_block.get("provider_reported_usd")
        record = _review_provider_types.AttemptRecord(
            usage=_review_provider_types.Usage.from_dict(usage_block),
            elapsed_ms=int(entry.get("elapsed_ms") or 0),
            served_model=str(entry.get("served_model") or ""),
            stop_reason=str(stop_reason) if stop_reason else None,
            reported_usd=float(reported) if isinstance(reported, (int, float)) else None,
        )
        served = _review_provider_types.BackendTurn(
            content=tuple(content),
            stop_reason=record.stop_reason,
            model=str(entry.get("model") or route.model),
            raw=_review_provider_types._plain_data(raw),
            attempts=(record,),
            reported_usd=record.reported_usd,
            key=key,
            turn_index=int(turn_index),
            replayed=True,
            text=response,
            usd=0.0,
            route=entry.get("route") if isinstance(entry.get("route"), Mapping) else {},
            admission=entry.get("admission") if isinstance(entry.get("admission"), Mapping) else {},
        )
        if from_store and self._turn_text(role_name, policy, served) != response:
            # `_lookup_bound` verified `response`; the bytes actually served
            # are the stored blocks, so they must reduce to that verified
            # text (a rewritten raw attempt or turn block never serves).
            raise TranscriptMissingError(
                f"transcript for {role_name}/{key[:12]} serves stored content "
                "whose text is not its verified response (re-record; fail closed)"
            )
        return served

    def _turn(
        self,
        role,
        messages: Sequence[Mapping[str, Any]],
        policy: SessionPolicy,
        turn_index: int,
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: Any = None,
        account: "SessionAccount | None" = None,
        observations: Sequence[str] | None = None,
    ) -> _review_provider_types.BackendTurn:
        """Execute one memoized, budgeted, and recorded model turn.

        The memo key binds behavior, policy, messages, per-turn wire overrides, and full
        observation digests. Replay-only misses raise before spending. Live turns load
        rates and reserve before one backend step, record the complete entry before
        return, then charge every attempt. Stored content, memo, message, wire, and
        observation digests are verified before reuse.
        """
        role_name = getattr(role, "value", str(role))
        if not isinstance(policy, SessionPolicy):
            raise TypeError("_turn needs a SessionPolicy")
        if policy.role != role_name:
            raise ValueError(
                f"session policy is for role {policy.role!r}, not {role_name!r}"
            )
        prefix = plain_messages(messages)
        if not prefix or str(prefix[-1].get("role") or "") != "user":
            raise ValueError("a session turn opens on a user message (the view or a tool result)")
        turn_index = int(turn_index)
        # The route is resolved BEFORE the memo lookup: an unrouted role
        # fails closed here rather than replaying some other routing's record.
        route = self.routing.for_role(role_name)
        # The per-turn wire is resolved BEFORE the key: the exact `tools[]`
        # and `tool_choice` this request sends enter the key when they deviate
        # from the policy's defaults, as do the full digests of the
        # observations that fed the turn.
        if tools is None:
            for_turn = getattr(policy, "wire_tools_for_turn", None)
            tools = for_turn(turn_index) if callable(for_turn) else policy.wire_tools
        wire_tools = [dict(t) for t in tools]
        wire_sha = sha256_hex(canonical_json(wire_tools))
        choice = (
            _provider_protocol.session_tool_choice(policy, terminal_only=_provider_protocol._wire_is_terminal_only(policy, wire_tools))
            if tool_choice is None
            else tool_choice
        )
        fed_by = tuple(str(d) for d in (observations or ()) if d)
        key = _provider_protocol.session_turn_key(
            role_name, policy, prefix, turn_index, agents_config=self.agents_document,
            tools=wire_tools, tool_choice=choice, observations=fed_by,
        )
        if account is None:
            account = self._sessions.get(role_name)
        if account is not None and turn_index == 0 and not account.prompt_sha256:
            account.prompt_sha256 = key

        cached = self._lookup_bound(role_name, key, route, policy=policy)
        if cached is not None:
            problem = self._turn_block_integrity_problem(
                cached, key=key, prefix=prefix, observations=fed_by
            )
            if problem:
                # The SERVED bytes (`turn.content`) no longer verify, or the
                # block claims another key or prefix: a corrupt or tampered
                # store, refused in BOTH modes (F2 serves only what verifies).
                raise TranscriptMissingError(
                    f"transcript for {role_name}/{key[:12]} cannot be served: "
                    f"{problem} (re-record; fail closed)"
                )
            mismatch = self._turn_block_wire_mismatch(cached, wire_tools=wire_tools, choice=choice)
            if mismatch and self.replay_only:
                raise TranscriptRouteMismatchError(
                    f"replay-only: transcript for {role_name}/{key[:12]} was "
                    f"{mismatch}; a turn recorded under another wire is not a "
                    "replay of this request (fail closed)"
                )
            if mismatch:
                cached = None  # live mode: remade under this turn's wire and re-recorded
        if cached is not None:
            self._reconcile_cached_durable_reservation(cached, role_name, route)
            turn = self._turn_from_entry(
                cached, role_name=role_name, policy=policy, key=key,
                turn_index=turn_index, route=route,
            )
            if account is not None:
                account.record_turn(turn, usd=0.0)
            return turn

        if self.replay_only:
            searched = ", ".join(str(d) for d in self.store._search_dirs()) or "(none)"
            raise SessionTranscriptMissingError(
                f"replay-only mode: no recorded transcript for turn {turn_index} "
                f"of role {role_name!r} / key {key[:12]} (searched: {searched}); "
                "the session diverged from its recorded prefix at this turn — "
                "replay is diagnostic only and never invents a turn (fail closed)"
            )

        rates = _review_provider_accounting.rate_card_for(
            route.provider,
            route.model,
            self.routing.provider_config.get(route.provider) or {},
        )
        try:
            backend = self._backend(route)
        except _provider_backends.MissingCredentialsError as exc:
            raise _provider_backends.MissingCredentialsError(
                f"{exc} — and no recorded transcript exists for turn "
                f"{turn_index} of role {role_name!r} / key {key[:12]}, so the "
                "session cannot be served offline (fail closed)"
            ) from exc

        if account is not None:
            trajectory = account.trajectory
            last_usage = account.last_usage
        else:
            trajectory = self.meter.trajectory(
                self.task_id, role_name, max_usd=policy.limits.max_usd, enforce_role_cap=True
            )
            last_usage = None
        # RESERVE BEFORE ANY TRANSPORT CALL (nothing spent, nothing recorded
        # on a refusal): turn 0 reserves the prompt floor, a later turn the
        # state-machine estimate from the previous turn's usage.
        budget_reservation_id = trajectory.reserve(
            _review_provider_accounting.estimate_turn_usd(
                rates=rates,
                max_tokens=route.max_tokens,
                prompt=self._messages_text(prefix),
                last_usage=last_usage,
            )
        )

        # Add cache breakpoints only to enabled critic-session turns; one-shot
        # bytes never carry them, and the markers affect no key or digest.
        prompt_cache = bool(route.prompt_cache) or _provider_protocol.agentic_role_enabled(
            role_name, agents_config=self.agents_document
        )
        try:
            turn = backend.step(
                role_name=role_name,
                model=route.model,
                messages=prefix,
                max_tokens=route.max_tokens,
                effort=route.effort,
                tools=wire_tools,
                tool_choice=choice,
                prompt_cache=prompt_cache,
            )
        except Exception as exc:
            # A raising backend still PAID for what it attempted: meter it
            # before the fault propagates (a breach found here chains).
            self._charge_attempts(trajectory, rates, _review_provider_types.backend_attempts(exc), model=route.model)
            if isinstance(
                exc,
                (
                    _review_provider_accounting.BudgetExceededError,
                    _provider_backends.MissingCredentialsError,
                    TranscriptMissingError,
                    ProviderProtocolError,
                    SessionFault,
                    PolicyFault,
                ),
            ):
                raise
            raise ProviderFault(
                f"transport fault on turn {turn_index} of role {role_name!r} "
                f"({type(exc).__name__}) after the transport's own retries; "
                "the session halts as PROVIDER_FAULT and resumes from its "
                "recorded prefix",
                code="transport",
            ) from exc

        text = self._turn_text(role_name, policy, turn)
        usage = turn.usage
        entry: dict = {
            "role": role_name,
            "prompt_sha256": key,
            "system_sha256": _provider_protocol.role_behavior_sha256(role_name, agents_config=self.agents_document),
            "provider": route.provider,
            "model": turn.model,
            "served_model": turn.served_model,
            "response": text,
            "response_sha256": sha256_hex(text),
            "task_id": self.task_id,
            "task_content_hash": self.task_content_hash,
            "attempt_count": 1,
            "correction_count": 0,
            "finding_count": self._finding_count(role_name, text),
            "usage": usage.as_dict(),
            "elapsed_ms": int(turn.elapsed_ms),
            "raw_attempts": [_review_provider_types._plain_data(turn.raw)],
            "route": self._route_block(
                route,
                agents_config=self.agents_document,
                entry_schema=_provider_protocol.SESSION_TRANSCRIPT_ENTRY_SCHEMA,
                policy=policy,
            ),
            # The per-turn block of SoT T7 entry schema 3.
            "turn": {
                "turn_index": turn_index,
                "memo_key": key,
                "messages_sha256": sha256_hex(canonical_json(prefix)),
                "user_messages": self._recordable_user_messages(prefix, turn_index),
                "content": [_review_provider_types._plain_data(b) for b in turn.content],
                # The digest of the SERVED bytes: verified before a memo hit
                # is served (`_turn_block_integrity_problem`).
                "content_sha256": sha256_hex(canonical_json([_review_provider_types._plain_data(b) for b in turn.content])),
                "tool_use_ids": list(turn.tool_use_ids),
                "tool_uses": [
                    {"id": str(b.get("id") or ""), "name": str(b.get("name") or "")}
                    for b in turn.tool_uses
                ],
                "stop_reason": turn.stop_reason,
                "truncated": turn.truncated,
                "tool_choice": _provider_backends._anthropic_tool_choice(choice),
                "tools_sha256": wire_sha,
                # The FULL digests of the observations that fed this turn
                # (folded into `memo_key`; the prefix carries the capped text).
                "observations_sha256": list(fed_by),
                "session_salt": int(policy.session_salt),
            },
        }
        if entry["finding_count"] is not None:
            entry["zero_findings"] = entry["finding_count"] == 0
        entry.update(self._record_stamp())
        if turn.reported_usd is not None:
            entry["usage"]["provider_reported_usd"] = turn.reported_usd
        if budget_reservation_id is not None:
            entry["budget_reservation_id"] = budget_reservation_id
        # ALWAYS record before returning (money spent must never be lost).
        self.store.record(role_name, key, entry)
        priced = sum(
            self.meter.price(usage=a.usage, rates=rates, provider_reported_usd=a.reported_usd)
            for a in turn.attempts
        )
        turn = _review_provider_types.BackendTurn(
            content=turn.content,
            stop_reason=turn.stop_reason,
            model=turn.model,
            raw=turn.raw,
            attempts=turn.attempts,
            reported_usd=turn.reported_usd,
            key=key,
            turn_index=turn_index,
            replayed=False,
            text=text,
            usd=priced,
            route=entry["route"],
            admission=entry.get("admission") or {},
        )
        if account is not None:
            account.record_turn(turn, usd=priced)
        # Charge per API ATTEMPT, after the record: a cap crossed here raises
        # (RoleCapExceeded for the session's own cap) with the entry on disk.
        self._charge_attempts(trajectory, rates, turn.attempts, model=turn.model)
        return turn

    def run_session(
        self,
        role,
        initial_view: str,
        policy: SessionPolicy,
        ctx: Any,
        *,
        reserve_usd: float | None = None,
        **runner_kwargs: Any,
    ):
        """Run one bounded session and record its completed evidence.

        The provider reserves the session ceiling, delegates to `run_bounded_session`,
        and appends one exchange row only after a normal terminal result. Role-cap
        exhaustion becomes a session limit; task or total exhaustion raises. Model turns
        are durable before use, completed trajectories are content-addressed, and replay
        verifies stored session and trajectory identities. Halts propagate with their
        partial session result and create no evidence row.
        """
        role_name = getattr(role, "value", str(role))
        executor = runner_kwargs.get("worker")
        run_offset = len(getattr(executor, "runs", ()) or ()) if executor is not None else 0
        account, result, _halt = self._run_session_core(
            role_name, initial_view, policy, ctx, reserve_usd=reserve_usd, **runner_kwargs
        )
        # The POST-SESSION store writes run after the session completed and
        # can raise a raw OSError (ENOSPC/EACCES) straight into the stage
        # runner; that is harness I/O, not a task defect, so it is a
        # `ProviderFault` (infrastructure), never an unclassified red gate
        # (finding 2-1).
        try:
            final_text = self._result_final_text(result)
            terminal = getattr(result, "terminal", None)
            terminal_name = str(getattr(terminal, "name", None) or terminal or "SUBMITTED")
            self._append_session_evidence(
                account, result, final_text=final_text, terminal_name=terminal_name
            )
            trajectory_path = self._write_trajectory_record(
                account,
                result,
                executor,
                final_text=final_text,
                terminal_name=terminal_name,
                task=getattr(ctx, "task", None),
                run_offset=run_offset,
            )
            if not self.replay_only and self.store.record_dir is not None:
                self.store.record_session(
                    role_name,
                    account.prompt_sha256,
                    self._session_record(
                        account,
                        result,
                        trajectory_sha256=(trajectory_path.stem if trajectory_path is not None else ""),
                    ),
                )
        except OSError as exc:
            fault = ProviderFault(
                "the completed session's evidence/record could not be persisted",
                code="session_store_io",
            )
            try:
                setattr(fault, "session_result", result)
            except (AttributeError, TypeError):  # pragma: no cover
                pass
            raise fault from exc
        return result

    @staticmethod
    def _result_final_text(result: Any) -> str:
        """Canonical terminal payload text shared by the generic session's
        evidence row and full trajectory record."""
        final = getattr(result, "final", None)
        if isinstance(final, str):
            return final
        if final is None:
            return ""
        return canonical_json(_review_provider_types._plain_data(final))

    def _session_record_identity_problem(
        self,
        record: Mapping[str, Any],
        account: "SessionAccount",
    ) -> str | None:
        """A static mismatch that makes a session record unusable before any
        recorded model turn is served."""
        expected: dict[str, Any] = {
            "entry_schema": _provider_protocol.SESSION_TRANSCRIPT_ENTRY_SCHEMA,
            "role": account.role,
            "session_key": account.prompt_sha256,
            "task_id": self.task_id,
            "task_content_hash": self.task_content_hash,
            "policy_sha256": account.policy.sha256(),
            "tools_sha256": account.policy.tools_sha256(),
            "session_salt": int(account.policy.session_salt),
        }
        for name, wanted in expected.items():
            found = record.get(name)
            if found != wanted:
                return f"{name} is {found!r}, expected {wanted!r}"
        trajectory_sha = str(record.get("trajectory_sha256") or "")
        if trajectory_sha:
            try:
                trajectory = self.store.lookup_trajectory(account.role, trajectory_sha)
            except RuntimeError:
                return f"linked trajectory {trajectory_sha[:12]} does not verify"
            if trajectory is None:
                return (
                    f"linked trajectory {trajectory_sha[:12]} is missing for "
                    f"role {account.role!r}"
                )
            for name, wanted in (
                ("role", account.role),
                ("task_id", self.task_id),
                ("task_content_hash", self.task_content_hash),
                ("prompt_sha256", account.prompt_sha256),
                ("policy_sha256", account.policy.sha256()),
                ("tools_sha256", account.policy.tools_sha256()),
            ):
                if trajectory.get(name) != wanted:
                    return (
                        f"linked trajectory {trajectory_sha[:12]} has {name} "
                        f"{trajectory.get(name)!r}, expected {wanted!r}"
                    )
        return None

    def _session_record_result_problem(
        self,
        record: Mapping[str, Any],
        account: "SessionAccount",
        result: Any,
    ) -> str | None:
        """A completed replay mismatch against the recording's full session
        index. These fields make turn order, observations, terminal and chain
        explicit instead of trusting only the final digest."""
        expected = self._session_record(account, result)
        for name in (
            "session_sha256",
            "chain_hashes",
            "terminal",
            "turn_keys",
            "observations_sha256",
            "model_call_count",
            "stale_tool_result_count",
        ):
            if _review_provider_types._plain_data(record.get(name)) != _review_provider_types._plain_data(expected.get(name)):
                return f"{name} does not match the replayed session"
        trajectory_sha = str(record.get("trajectory_sha256") or "")
        if trajectory_sha:
            try:
                trajectory = self.store.lookup_trajectory(account.role, trajectory_sha)
            except RuntimeError:
                return f"linked trajectory {trajectory_sha[:12]} does not verify"
            if trajectory is None:
                return f"linked trajectory {trajectory_sha[:12]} is missing"
            if str(trajectory.get("session_sha256") or "") != str(
                expected.get("session_sha256") or ""
            ):
                return "linked trajectory session_sha256 does not match the replayed session"
        return None

    def _run_session_core(
        self,
        role_name: str,
        initial_view: str,
        policy: SessionPolicy,
        ctx: Any,
        *,
        reserve_usd: float | None = None,
        catch_policy_violation: bool = False,
        **runner_kwargs: Any,
    ) -> "tuple[SessionAccount, Any, SessionPolicyViolation | None]":
        """The session core `run_session` and `complete()`'s critic dispatch
        share: open the ledger, INIT reserve, the replay record, the runner,
        the replay-digest check. Returns `(account, result, halt)`, where
        `halt` is the `SessionPolicyViolation` caught under
        `catch_policy_violation` (a metrology trial: SoT T4 makes it a seat
        outcome) with its partial result — every other raise propagates."""
        if not isinstance(policy, SessionPolicy):
            raise TypeError("run_session needs a SessionPolicy")
        if policy.role != role_name:
            raise ValueError(
                f"session policy is for role {policy.role!r}, not {role_name!r}"
            )
        ctx_role = getattr(ctx, "role", None)
        if ctx_role is not None and str(ctx_role) != role_name:
            raise ValueError(
                f"tool context was built for role {ctx_role!r}, not {role_name!r}"
            )
        from elt_taskgen.review.session import run_bounded_session

        # The runner's `worker` is keyword-required and defaults to its
        # in-process validator worker when None.
        runner_kwargs.setdefault("worker", None)
        account = self.begin_session(role_name, policy)
        recorded: dict | None = None
        halt: SessionPolicyViolation | None = None
        result: Any = None
        try:
            account.prompt_sha256 = _provider_protocol.transcript_key_v3(
                role_name,
                policy,
                [{"role": "user", "content": initial_view}],
                agents_config=self.agents_document,
            )
            if self.replay_only:
                # REPLAY (0-1): the session record of the recording this run
                # claims to replay — its full observation digests are checked
                # tool by tool by the runner, its `session_sha256` at the end.
                # No record: nothing to verify against, refused before a
                # single turn is served (replay is diagnostic only, C3).
                recorded = self.store.lookup_session(role_name, account.prompt_sha256)
                if recorded is None:
                    searched = ", ".join(str(d) for d in self.store._search_dirs()) or "(none)"
                    raise SessionTranscriptMissingError(
                        f"replay-only mode: no recorded session record for role "
                        f"{role_name!r} / session {account.prompt_sha256[:12]} (searched: "
                        f"{searched}); the recording's observation digests and chain "
                        "digest are what a replay is verified against — re-record "
                        "(fail closed)"
                    )
                identity_problem = self._session_record_identity_problem(recorded, account)
                if identity_problem:
                    raise SessionReplayMismatchError(
                        "session", detail=f"session record {identity_problem}"
                    )
                runner_kwargs["expected_observations"] = tuple(
                    str(d) for d in (recorded.get("observations_sha256") or [])
                )
            cap = policy.limits.max_usd if reserve_usd is None else float(reserve_usd)
            if cap is not None and cap > 0:
                # INIT checks task/total budget covers the session cap but makes no
                # durable reservation; each actual call reserves transactionally.
                account.trajectory.reserve(cap, durable=False)
            result = run_bounded_session(
                role_name,
                initial_view,
                policy.tools,
                policy,
                policy.limits,
                provider=self,
                ctx=ctx,
                **runner_kwargs,
            )
        except SessionPolicyViolation as exc:
            # Under a trial, policy violation is a seat outcome with its partial
            # result, not exit 2; a foreign violation without one propagates.
            if catch_policy_violation and getattr(exc, "session_result", None) is not None:
                halt = exc
            else:
                raise
        except (
            SessionFault,
            PolicyFault,
            ProviderProtocolError,
            _review_provider_accounting.BudgetExceededError,
            TranscriptMissingError,
        ):
            # Already typed by the boundary that raised it: the engine
            # classifies these (infrastructure vs. label-eligible) itself.
            raise
        except Exception as exc:  # noqa: BLE001 - an unclassified escape is a harness fault
            # Wrap raw runner exceptions as `ToolHarnessFault` and preserve any
            # partial result, preventing repair-round and text-based routing.
            wrapped = ToolHarnessFault.from_exception("run_session", exc)
            partial = getattr(exc, "session_result", None)
            if partial is not None:
                try:
                    setattr(wrapped, "session_result", partial)
                except (AttributeError, TypeError):  # pragma: no cover
                    pass
            raise wrapped from exc
        finally:
            self.end_session(account)
        if halt is not None:
            return account, getattr(halt, "session_result"), halt
        if recorded is not None:
            problem = self._session_record_result_problem(recorded, account, result)
            if problem:
                # A completed replay that is not the recorded session: no
                # `replayed=True` row may claim it is (0-1). Halts as the
                # replay-mismatch harness fault with the result attached.
                exc = SessionReplayMismatchError(
                    "session",
                    detail=(
                        f"{problem} for role {role_name!r} / session "
                        f"{account.prompt_sha256[:12]}"
                    ),
                )
                try:
                    setattr(exc, "session_result", result)
                except (AttributeError, TypeError):  # pragma: no cover
                    pass
                raise exc
        return account, result, None

    # -- the content-addressed trajectory tier --------------------------------

    @staticmethod
    def _session_runs(executor: Any, role: str, offset: int) -> list["ValidatorRun"]:
        """The validator runs of ONE session on the trial's SHARED executor:
        the window this session opened (`offset`), narrowed to runs whose
        `ValidatorRun.role` is this seat's (findings p4-0-3 / p4-2-4). A run
        whose role was never recorded stays in the window, which is already
        this session's."""
        runs = list(getattr(executor, "runs", ()) or ())
        window = runs[int(offset):] if offset else runs
        return [
            run
            for run in window
            if not str(getattr(run, "role", "") or "")
            or str(getattr(run, "role", "")) == role
        ]

    def _trajectory_record(
        self,
        account: "SessionAccount",
        result: Any,
        executor: Any,
        *,
        final_text: str,
        terminal_name: str,
        stop_reason: str = "",
        run_offset: int = 0,
    ) -> dict:
        """Build the content-addressed record for a completed bounded session.

        Store model-visible turns, chain identities, counts, route, trial binding, and
        admission provenance. Raw validator output remains in the separate `tool_raw`
        tier.
        """
        binding = self._trial
        nonce = binding.nonce if binding is not None else ""
        trial_index = binding.trial_index if binding is not None else None
        behavior_sha = _provider_protocol.role_behavior_sha256(account.role, agents_config=self.agents_document)
        session_sha = str(getattr(result, "session_sha256", "") or "")
        bound = _trajectory.trial_trajectory_sha256(
            session_sha,
            task_content_hash=str(getattr(result, "task_content_hash", "") or ""),
            role=account.role,
            trial_nonce=nonce,
            behavior_sha256=behavior_sha,
            prompt_sha256=account.prompt_sha256,
        )
        turns = tuple(getattr(result, "turns", ()) or ())
        model_turns: list[dict] = []
        validator_turns: list[dict] = []
        served = list(account.turn_records)
        runs = self._session_runs(executor, account.role, run_offset)
        run_by_digest: dict[str, list[ValidatorRun]] = {}
        for run in runs:
            run_by_digest.setdefault(run.observation_sha256, []).append(run)
        model_seen = 0
        for turn in turns:
            kind = str(getattr(turn, "kind", "") or "")
            if kind == "model":
                record = served[model_seen] if model_seen < len(served) else {}
                model_seen += 1
                model_turns.append(
                    {
                        "index": int(turn.turn_index),
                        "model_turn": int(turn.model_turn),
                        "category": str(turn.category),
                        "turn_key": str(turn.memo_key or record.get("key", "")),
                        "request_sha256": str(turn.prompt_sha256),
                        "response_sha256": str(turn.response_sha256),
                        "raw_response": list(record.get("content", ())),
                        "usage": dict(turn.usage),
                        "stop_reason": str(turn.stop_reason or record.get("stop_reason", "") or ""),
                        "live": not bool(turn.replayed),
                        "outcome_code": str(turn.outcome_code),
                        "tool_name": str(turn.tool_name),
                        "served_model": str(record.get("served_model", "") or ""),
                    }
                )
            elif kind in ("validator", "tool", "refused", "nudge"):
                digest = str(getattr(turn, "output_sha256", "") or "")
                matched = run_by_digest.get(digest)
                run = matched.pop(0) if matched else None
                validator_turns.append(
                    {
                        "index": int(turn.turn_index),
                        "kind": kind,
                        "model_turn": int(turn.model_turn),
                        "name": str(turn.tool_name),
                        "args_sha256": str(turn.args_sha256),
                        "observation": run.observation if run is not None else "",
                        "observation_sha256": digest,
                        "raw_output_sha256": run.raw_output_sha256 if run is not None else "",
                        "code": str(turn.outcome_code),
                        "refused": bool(turn.refused),
                        "fresh": bool(getattr(turn, "fresh", True)),
                        "projection_version": run.projection_version if run is not None else DIAGNOSTICS_VERSION,
                        "sanitizer_version": run.sanitizer_version if run is not None else DIAGNOSTICS_VERSION,
                        "wall_ms": int(getattr(turn, "elapsed_tool_ms", 0) or 0),
                    }
                )
        kinds = getattr(result, "correction_kinds", None)
        record = {
            "record_version": _trajectory.TRAJECTORY_RECORD_VERSION,
            "entry_schema": _provider_protocol.SESSION_TRANSCRIPT_ENTRY_SCHEMA,
            "role": account.role,
            "task_id": self.task_id,
            "task_content_hash": str(getattr(result, "task_content_hash", "") or ""),
            "trial_nonce": nonce,
            "trial_index": trial_index,
            "session_key": account.prompt_sha256,
            "prompt_sha256": account.prompt_sha256,
            "response_sha256": sha256_hex(final_text),
            "behavior_sha256": behavior_sha,
            "tools_sha256": str(getattr(result, "tools_sha256", "") or ""),
            "policy_sha256": str(getattr(result, "policy_sha256", "") or ""),
            "session_salt": int(getattr(result, "session_salt", 0) or 0),
            "session_sha256": session_sha,
            "trajectory_sha256": bound,
            "terminal": str(terminal_name),
            "stop_reason": str(stop_reason or getattr(getattr(result, "terminal", None), "value", "") or ""),
            "turns": [t.as_dict() for t in turns],
            "chain_hashes": [str(h) for h in (getattr(result, "chain_hashes", ()) or ())],
            "model_turns": model_turns,
            "validator_turns": validator_turns,
            "counts": {
                "model_call_count": int(getattr(result, "model_call_count", 0) or 0),
                "tool_call_count": int(getattr(result, "tool_call_count", 0) or 0),
                "refused_count": int(getattr(result, "refused_count", 0) or 0),
                "nudge_count": int(getattr(result, "nudge_count", 0) or 0),
                "validator_run_count": int(getattr(result, "validator_run_count", 0) or 0),
                "correction_count": int(getattr(result, "correction_count", 0) or 0),
                "correction_kinds": {str(k): int(v) for k, v in dict(kinds or {}).items()},
                "terminal_count": int(getattr(result, "terminal_count", 0) or 0),
                "limit_stop_count": int(getattr(result, "limit_stop_count", 0) or 0),
                "live_model_call_count": int(account.live_model_call_count),
                "stale_tool_result_count": int(getattr(result, "stale_tool_result_count", 0) or 0),
                "submitted_with_red_validators": int(getattr(result, "submitted_with_red_validators", 0) or 0),
                "compile_correction_exhausted": 0,
            },
            "fault": (result.fault.as_dict() if getattr(result, "fault", None) is not None else None),
            "route": self._route_block(
                self.routing.for_role(account.role),
                agents_config=self.agents_document,
                entry_schema=_provider_protocol.SESSION_TRANSCRIPT_ENTRY_SCHEMA,
                policy=account.policy,
            ),
            "fingerprint_components": {
                "behavior_sha256": behavior_sha,
                "tools_sha256": str(getattr(result, "tools_sha256", "") or ""),
                "policy_sha256": str(getattr(result, "policy_sha256", "") or ""),
                "diagnostics_version": DIAGNOSTICS_VERSION,
            },
        }
        record.update(self._record_stamp())
        return record

    def _write_trajectory_record(
        self,
        account: "SessionAccount",
        result: Any,
        executor: Any,
        *,
        final_text: str,
        terminal_name: str,
        task: Any,
        stop_reason: str = "",
        run_offset: int = 0,
    ) -> Path | None:
        """Write an append-only trajectory and its separate raw-validator evidence.

        Gatecheck every harness-produced model-visible observation before writing. Never
        overwrite an existing content address. Return the record path, or `None` in
        replay-only/no-record mode.
        """
        from elt_taskgen.review.tools.projection import assert_value_free

        record_dir = self.store.record_dir
        if self.replay_only or record_dir is None:
            return None
        # Lightweight provider tests and third-party runner doubles may return
        # a duck-typed summary rather than the canonical SessionResult. They
        # retain the historical evidence/session-index path, but cannot claim
        # a full trajectory: only records whose turns expose the canonical
        # serialization and whose chain verifies are content-addressed here.
        turns = tuple(getattr(result, "turns", ()) or ())
        verify_chain = getattr(result, "verify_chain", None)
        if not callable(verify_chain) or not all(callable(getattr(t, "as_dict", None)) for t in turns):
            return None
        if not bool(verify_chain()):
            raise ToolHarnessFault("trajectory_record", code="chain_mismatch")
        # THIS session's runs only: the trial's executor is shared by every
        # seat of the trial (findings p4-0-3 / p4-2-4).
        runs = self._session_runs(executor, account.role, run_offset)
        for run in runs:
            if run.payload:
                assert_value_free(run.payload.encode("utf-8"), task=task)
        record = self._trajectory_record(
            account, result, executor,
            final_text=final_text, terminal_name=terminal_name, stop_reason=stop_reason,
            run_offset=run_offset,
        )
        digest = str(record["trajectory_sha256"])
        path = _review_transcripts.trajectory_record_path(Path(record_dir), account.role, digest)
        if _review_transcripts._publish_bytes_once(
            path,
            readable_json(record).encode("utf-8"),
            boundary=Path(record_dir),
        ):
            stored_record = record
        else:
            # The address is append-only, but two identical executions can
            # legitimately differ in unbound timing/usage fields.  Keep the
            # first complete record only after proving that it is a valid
            # record at this exact content address.
            stored_record = self.store.lookup_trajectory(account.role, digest)
            if stored_record is None:
                raise RuntimeError(
                    f"immutable trajectory disappeared during publication: {path}"
                )
        raw_store_root = _review_transcripts.tool_raw_root(Path(record_dir))
        raw_root = raw_store_root / digest
        stored_validator_turns = list(stored_record["validator_turns"])
        expected_raw = any(
            str(turn.get("raw_output_sha256") or "")
            for turn in stored_validator_turns
        )
        if runs or expected_raw:
            _review_transcripts._ensure_private_directory(raw_root, boundary=raw_store_root)
            by_digest: dict[str, list[ValidatorRun]] = {}
            for run in runs:
                by_digest.setdefault(run.observation_sha256, []).append(run)
            written: set[int] = set()
            for turn in stored_validator_turns:
                matched = by_digest.get(str(turn["observation_sha256"]))
                if not matched:
                    expected = str(turn.get("raw_output_sha256") or "")
                    if expected:
                        _review_transcripts._confirm_raw_output(
                            raw_root / f"{int(turn['index'])}.bin",
                            expected,
                            boundary=raw_store_root,
                        )
                    continue
                run = matched.pop(0)
                target = raw_root / f"{int(turn['index'])}.bin"
                _review_transcripts._publish_raw_output(
                    target,
                    run.raw_output,
                    str(turn.get("raw_output_sha256") or ""),
                    boundary=raw_store_root,
                )
                written.add(run.index)
            for run in runs:
                if run.index in written:
                    continue
                target = raw_root / f"unmatched-{run.index}.bin"
                _review_transcripts._publish_raw_output(
                    target,
                    run.raw_output,
                    run.raw_output_sha256,
                    boundary=raw_store_root,
                )
        return path

    def _session_record(
        self,
        account: "SessionAccount",
        result: Any,
        *,
        trajectory_sha256: str = "",
    ) -> dict:
        """Build the identity-only index for one completed session.

        Bind the turn-zero key to trajectory and session digests, route, task, prompt,
        counters, and terminal state without duplicating model or validator payloads.
        """
        turns = getattr(result, "turns", ()) or ()
        observations = [
            str(getattr(t, "output_sha256", "") or "")
            for t in turns
            if str(getattr(t, "kind", "") or "") in ("tool", "validator")
            and str(getattr(t, "output_sha256", "") or "")
        ]
        terminal = getattr(result, "terminal", None)
        record = {
            "entry_schema": _provider_protocol.SESSION_TRANSCRIPT_ENTRY_SCHEMA,
            "role": account.role,
            "session_key": account.prompt_sha256,
            "task_id": self.task_id,
            "task_content_hash": self.task_content_hash,
            "policy_sha256": account.policy.sha256(),
            "tools_sha256": account.policy.tools_sha256(),
            "session_salt": int(getattr(account.policy, "session_salt", 0) or 0),
            "session_sha256": str(getattr(result, "session_sha256", "") or ""),
            "chain_hashes": [str(h) for h in (getattr(result, "chain_hashes", ()) or ())],
            "terminal": str(getattr(terminal, "name", None) or terminal or ""),
            "turn_keys": list(account.turn_keys),
            "observations_sha256": observations,
            "model_call_count": int(account.model_call_count),
            "stale_tool_result_count": int(getattr(result, "stale_tool_result_count", 0) or 0),
        }
        if trajectory_sha256:
            record["trajectory_sha256"] = str(trajectory_sha256)
        return record

    @staticmethod
    def _duplicate_tool_calls(result: Any) -> int:
        """Executed tool-side turns of this session whose (tool, arguments)
        pair had already been executed in it: the WASTED-CALL numerator the
        advisory efficiency ratio reads (`metrology._trajectory_summary`
        `duplicate_tool_calls`, which had no producer at all before finding
        p4-2-2 and silently deflated the ratio). Identity only — the digest
        of the arguments, never the arguments."""
        seen: set[tuple[str, str]] = set()
        duplicates = 0
        for turn in getattr(result, "turns", ()) or ():
            if str(getattr(turn, "kind", "") or "") not in ("tool", "validator"):
                continue
            key = (
                str(getattr(turn, "tool_name", "") or ""),
                str(getattr(turn, "args_sha256", "") or ""),
            )
            if key in seen:
                duplicates += 1
            else:
                seen.add(key)
        return duplicates

    def _append_session_evidence(
        self,
        account: "SessionAccount",
        result: Any,
        *,
        final_text: str | None = None,
        terminal_name: str | None = None,
        private_probe_count: int = 0,
        compile_correction_exhausted: int = 0,
    ) -> None:
        """Append one exchange-evidence row for a completed task/role/session.

        Record stable counts, terminal, route, usage, USD, freshness, and trajectory
        identity. Faulted sessions never call this method.
        """
        if final_text is None:
            final = getattr(result, "final", None)
            if isinstance(final, str):
                final_text = final
            elif final is None:
                final_text = ""
            else:
                final_text = canonical_json(_review_provider_types._plain_data(final))
        if terminal_name is None:
            terminal = getattr(result, "terminal", None)
            terminal_name = str(getattr(terminal, "name", None) or terminal or "SUBMITTED")

        def count(name: str) -> int:
            value = getattr(result, name, 0)
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        model_calls = account.model_call_count or count("model_call_count")
        corrections = count("correction_count")
        kinds = getattr(result, "correction_kinds", None)
        if not isinstance(kinds, Mapping):
            kinds = {"schema": corrections, "compile": 0}
        session_sha = str(
            getattr(result, "session_sha256", "") or getattr(result, "trajectory_sha256", "") or ""
        )
        trajectory_sha = session_sha
        trial_fields: dict[str, Any] = {}
        if self._trial is not None:
            binding = self._trial
            trajectory_sha = _trajectory.trial_trajectory_sha256(
                session_sha,
                task_content_hash=str(getattr(result, "task_content_hash", "") or ""),
                role=account.role,
                trial_nonce=binding.nonce,
                behavior_sha256=_provider_protocol.role_behavior_sha256(account.role, agents_config=self.agents_document),
                prompt_sha256=account.prompt_sha256,
            )
            trial_fields = {"trial_nonce": binding.nonce, "session_sha256": session_sha}
            if binding.trial_index is not None:
                trial_fields["trial_index"] = binding.trial_index
        finding_count = self._finding_count(account.role, final_text)
        behavior_sha = _provider_protocol.role_behavior_sha256(
            account.role, agents_config=self.agents_document
        )
        self.exchange_evidence.append(
            {
                "task_id": self.task_id,
                "task_content_hash": self.task_content_hash,
                "role": account.role,
                "prompt_sha256": account.prompt_sha256,
                "response_sha256": sha256_hex(final_text),
                "attempt_count": model_calls,
                "correction_count": corrections,
                "model_call_count": model_calls,
                "tool_call_count": count("tool_call_count"),
                "refused_count": count("refused_count"),
                "nudge_count": count("nudge_count"),
                "validator_run_count": count("validator_run_count"),
                "correction_kinds": {str(k): int(v) for k, v in dict(kinds).items()},
                # Validators still red on the accepted submission (the
                # compile-correction budget was spent; session.py
                # `VALIDATOR_RED_AT_SUBMIT_CODE`) — a metrology counter of the
                # session row only; the one-shot row is byte-identical.
                "submitted_with_red_validators": count("submitted_with_red_validators"),
                # Findings VOIDED by the harness because their executable
                # handoff stayed invalid after the bounded compile correction
                # (critic_validators; the 2026-09-09 batch repair): a seat
                # counter, never a run abort.
                "compile_correction_exhausted": int(compile_correction_exhausted),
                # The last two SoT T5 terms (a superset of the T8 row, which
                # T8 allows), so the identity is checkable from the manifest
                # alone (`exchange_row_problems`).
                "terminal_count": count("terminal_count"),
                "limit_stop_count": count("limit_stop_count"),
                # Record counts only for private or foreign-nonce probes and
                # repeated identical calls; never record argument values.
                "private_probe_count": int(private_probe_count),
                "duplicate_tool_calls": self._duplicate_tool_calls(result),
                "terminal": terminal_name,
                "live_model_call_count": account.live_model_call_count,
                "stale_tool_result_count": count("stale_tool_result_count"),
                "replayed": account.replayed,
                "trajectory_sha256": trajectory_sha,
                "entry_schema": _provider_protocol.SESSION_TRANSCRIPT_ENTRY_SCHEMA,
                "finding_count": finding_count,
                "zero_findings": finding_count == 0 if finding_count is not None else None,
                "provider": account.provider,
                "model": account.model,
                "behavior_sha256": behavior_sha,
                "tools_sha256": account.policy.tools_sha256(),
                "policy_sha256": account.policy.sha256(),
                "diagnostics_version": DIAGNOSTICS_VERSION,
                "usage": {
                    "input": account.usage.input_tokens,
                    "output": account.usage.output_tokens,
                    "cache_read": account.usage.cache_read_input_tokens,
                    "cache_write": account.usage.cache_creation_input_tokens,
                },
                "usd": float(account.usd),
                "wall_ms": int(account.wall_ms),
                **trial_fields,
            }
        )


def exchange_row_problems(row: Mapping[str, Any]) -> list[str]:
    """Validate one exchange-evidence row against its role manifest.

    Return all problems, or an empty list when counts, terminal state, route, policy,
    tools, behavior, usage, and freshness are consistent.
    """
    problems: list[str] = []
    role = str(row.get("role") or "?")

    def count(name: str) -> int | None:
        value = row.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    attempts = count("attempt_count")
    corrections = count("correction_count")
    if attempts is None or attempts < 1:
        problems.append(f"{role}: attempt_count is not positive")
        return problems
    if corrections is None or corrections < 0:
        problems.append(f"{role}: correction_count is not a count")
        return problems
    try:
        schema = int(row.get("entry_schema") or 0)
    except (TypeError, ValueError):
        schema = 0
    if schema >= _provider_protocol.TRANSCRIPT_ENTRY_SCHEMA:
        for field in ("behavior_sha256", "tools_sha256", "policy_sha256"):
            value = str(row.get(field) or "")
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                problems.append(f"{role}: {field} is not a sha256 digest")
        if not str(row.get("diagnostics_version") or ""):
            problems.append(f"{role}: diagnostics_version is missing")
    if schema < _provider_protocol.SESSION_TRANSCRIPT_ENTRY_SCHEMA:
        if corrections != attempts - 1:
            problems.append(f"{role}: correction_count does not equal attempts-1")
        return problems
    if corrections > attempts - 1:
        problems.append(f"{role}: correction_count exceeds attempts-1 on a session row")
    model_calls = count("model_call_count")
    if model_calls is None or model_calls != attempts:
        problems.append(f"{role}: model_call_count does not equal attempt_count on a session row")
    terms = {
        name: count(name)
        for name in (
            "tool_call_count", "refused_count", "nudge_count",
            "terminal_count", "limit_stop_count",
        )
    }
    missing = sorted(name for name, value in terms.items() if value is None or value < 0)
    if missing:
        problems.append(f"{role}: session row lacks count(s) {missing}")
        return problems
    total = sum(terms.values()) + corrections
    if model_calls is not None and model_calls != total:
        problems.append(
            f"{role}: SoT T5 identity broken: model_call_count {model_calls} != "
            f"tool_call + refused + nudge + correction + terminal + limit_stop = {total}"
        )
    if terms["nudge_count"] > 1:
        problems.append(f"{role}: more than one nudge in a session row")
    return problems


@dataclass
class SessionAccount:
    """The provider-side ledger of ONE bounded session (task, role, session):
    the trajectory every turn meters against, the turn keys in order and
    the sums the session's single `exchange_evidence` row states. Opened by
    `RoutedProvider.begin_session`, fed by `_turn`, read by `run_session`."""

    role: str
    policy: SessionPolicy
    trajectory: _review_provider_accounting.TrajectoryBudget
    provider: str = ""
    model: str = ""
    #: `transcript_key_v3` of turn 0 (= `transcript_key(role, view)` under the
    #: declared policy): the row's `prompt_sha256`.
    prompt_sha256: str = ""
    model_call_count: int = 0
    live_model_call_count: int = 0
    usage: _review_provider_types.Usage = field(default_factory=_review_provider_types.Usage)
    usd: float = 0.0
    wall_ms: int = 0
    last_usage: _review_provider_types.Usage | None = None
    turn_keys: list[str] = field(default_factory=list)
    last_text: str = ""
    #: Per model turn, in order: the memo key, the assistant content blocks
    #: EXACTLY as produced or served (the trajectory record's
    #: `raw_response`), the stop reason, the served model, the usage, the
    #: served-vs-live flag. Identity and the model's own bytes; never a view.
    turn_records: list[dict] = field(default_factory=list)

    def record_turn(self, turn: _review_provider_types.BackendTurn, *, usd: float) -> None:
        self.model_call_count += 1
        if not turn.replayed:
            self.live_model_call_count += 1
            self.usd += float(usd)
            self.wall_ms += int(turn.elapsed_ms)
        self.usage = self.usage + turn.usage
        self.last_usage = turn.usage
        self.turn_keys.append(turn.key)
        self.last_text = turn.text
        self.turn_records.append(
            {
                "key": turn.key,
                "turn_index": int(turn.turn_index),
                "content": [_review_provider_types._plain_data(b) for b in turn.content],
                "stop_reason": str(turn.stop_reason or ""),
                "served_model": str(turn.served_model or ""),
                "model": str(turn.model or ""),
                "usage": turn.usage.as_dict(),
                "replayed": bool(turn.replayed),
                "elapsed_ms": int(turn.elapsed_ms),
                "usd": float(usd),
            }
        )

    @property
    def replayed(self) -> bool:
        """True iff every model call was served from the store."""
        return self.model_call_count > 0 and self.live_model_call_count == 0


# Batch Anthropic calls by provider and store them under normal transcript keys.
# Failures fall back to per-call execution; OpenAI-compatible calls stay serial.
# Budgets use full interactive rates, so spend can only be overstated.

BATCH_API_PATH = "/v1/messages/batches"

#: Terminal processing_status of an Anthropic message batch.
BATCH_STATUS_ENDED = "ended"

#: Poll cadence and the bounded wait before the queue gives up and falls back.
BATCH_POLL_SECONDS = 10.0
BATCH_MAX_WAIT_SECONDS = 24 * 60 * 60.0

#: Providers that support batching here. Everything else runs serially.
BATCH_PROVIDERS: frozenset[str] = frozenset({"anthropic"})

#: A lone request gains nothing from a batch round-trip (and costs latency).
BATCH_MIN_SIZE = 2

#: (method, url, headers, payload|None) -> parsed JSON. Tests double THIS.
BatchTransport = Callable[[str, str, Mapping[str, str], dict | None], dict]


def batch_transport_default(
    method: str, url: str, headers: Mapping[str, str], payload: dict | None = None
) -> dict:
    """Stdlib HTTP for the Batch API; JSONL results become {'results': [...]}."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={"content-type": "application/json", **headers},
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(request, timeout=_review_provider_transport._HTTP_TIMEOUT_SECONDS) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # pragma: no cover - live API only
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(
            f"batch API HTTP {exc.code} from {url}: {detail}"
        ) from exc
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    # Results are served as JSONL (one result object per line).
    results = [
        json.loads(line) for line in text.splitlines() if line.strip()
    ]
    return {"results": results}


@dataclass(frozen=True)
class BatchRequest:
    """One queued (role, prompt) call. `custom_id` is derived, not random, so
    a re-queued identical call keeps the same id (deterministic grouping)."""

    custom_id: str
    role_name: str
    prompt: str
    prompt_sha256: str


@dataclass(frozen=True)
class BatchRunResult:
    """What one queue drain did — auditable evidence of HOW answers arrived."""

    responses: Mapping[str, str]          # custom_id -> normalized response text
    memoized: tuple[str, ...] = ()        # served from transcripts, zero HTTP
    batched: tuple[str, ...] = ()         # served by the Batch API
    serial: tuple[str, ...] = ()          # served per-call (fallback / non-batch)
    fallback_reasons: tuple[str, ...] = ()

    def text_for(self, custom_id: str) -> str:
        if custom_id not in self.responses:
            raise KeyError(f"no batch response for {custom_id!r} (fail closed)")
        return self.responses[custom_id]


class BatchQueue:
    """Groups pending role calls into Batch API submissions, with fallback.

    `batch_transport=None` disables batching entirely (everything runs the
    per-call path); pass a double to test the wire. The queue never invents an
    answer: every added custom_id appears in `responses` or the drain raised.
    """

    #: Sentinel: `batch_transport` unset means the real Batch API transport;
    #: an EXPLICIT None disables batching (everything runs serially).
    _DEFAULT_TRANSPORT = object()

    def __init__(
        self,
        provider: RoutedProvider,
        *,
        batch_transport: BatchTransport | None = _DEFAULT_TRANSPORT,
        poll_seconds: float = BATCH_POLL_SECONDS,
        max_wait_seconds: float = BATCH_MAX_WAIT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        min_batch_size: int = BATCH_MIN_SIZE,
    ):
        self.provider = provider
        self._batch_transport: BatchTransport | None = (
            batch_transport_default
            if batch_transport is self._DEFAULT_TRANSPORT
            else batch_transport
        )
        self.poll_seconds = float(poll_seconds)
        self.max_wait_seconds = float(max_wait_seconds)
        self._sleep = sleep
        self.min_batch_size = int(min_batch_size)
        self._pending: dict[str, BatchRequest] = {}

    # -- queueing -----------------------------------------------------------

    @staticmethod
    def _refuse_agentic(role_name: str) -> None:
        """A role that registers model-initiated tools runs bounded sessions
        (`RoutedProvider.run_session`), which are serial by construction: one
        turn answers the previous one. Such a role is never queued, grouped,
        batched or run through the per-call fallback."""
        if _provider_protocol.role_is_agentic(role_name):
            raise ValueError(
                f"role {role_name!r} registers model-initiated tools "
                f"({', '.join(ToolRegistry.for_role(role_name).names)}) and runs "
                "bounded sessions; a session is serial and is never batched — "
                "call RoutedProvider.run_session (fail closed)"
            )

    def add(self, role, prompt: str) -> str:
        """Queue one (role, prompt) call; returns its custom_id (idempotent).
        Refuses an agentic role (non-empty tool registry)."""
        role_name = getattr(role, "value", str(role))
        self._refuse_agentic(role_name)
        # Same two-halves key as RoutedProvider.complete (role_behavior_sha256),
        # over the same agents document.
        prompt_sha = _provider_protocol.transcript_key(
            role_name, prompt, agents_config=getattr(self.provider, "agents_config", None)
        )
        custom_id = f"{role_name}-{prompt_sha[:16]}"
        self._pending[custom_id] = BatchRequest(
            custom_id=custom_id,
            role_name=role_name,
            prompt=prompt,
            prompt_sha256=prompt_sha,
        )
        return custom_id

    def __len__(self) -> int:
        return len(self._pending)

    def pending(self) -> tuple[BatchRequest, ...]:
        return tuple(sorted(self._pending.values(), key=lambda r: r.custom_id))

    def groups(self) -> dict[str, tuple[BatchRequest, ...]]:
        """Pending requests grouped by ROUTED provider (deterministic order).
        An agentic role's request (a registry that became non-empty after
        `add`) is filtered out defensively: it belongs to a session."""
        grouped: dict[str, list[BatchRequest]] = {}
        for request in self.pending():
            if _provider_protocol.role_is_agentic(request.role_name):
                continue
            route = self.provider.routing.for_role(request.role_name)
            grouped.setdefault(route.provider, []).append(request)
        return {name: tuple(reqs) for name, reqs in sorted(grouped.items())}

    def batchable(
        self,
        provider_name: str,
        count: int,
        requests: Sequence[BatchRequest] = (),
    ) -> bool:
        """True iff this provider's group is worth (and able to be) batched.
        A group holding ANY agentic role (non-empty tool registry) is never
        batchable: its turns are serial and answer one another."""
        if any(_provider_protocol.role_is_agentic(request.role_name) for request in requests):
            return False
        return (
            provider_name in BATCH_PROVIDERS
            and count >= self.min_batch_size
            and self._batch_transport is not None
        )

    # -- draining -----------------------------------------------------------

    def run(self) -> BatchRunResult:
        """Serve every queued call, then clear the queue."""
        responses: dict[str, str] = {}
        memoized: list[str] = []
        batched: list[str] = []
        serial: list[str] = []
        reasons: list[str] = []

        remaining: list[BatchRequest] = []
        for request in self.pending():
            # Same route-bound memoization as RoutedProvider.complete: a
            # transcript from another route is not served.
            route = self.provider.routing.for_role(request.role_name)
            cached = self.provider._memoized(
                request.role_name, request.prompt_sha256, route
            )
            if cached is not None:
                responses[request.custom_id] = cached
                memoized.append(request.custom_id)
                continue
            remaining.append(request)

        if remaining and self.provider.replay_only:
            missing = ", ".join(
                f"{r.role_name}/{r.prompt_sha256[:12]}" for r in remaining
            )
            raise TranscriptMissingError(
                f"replay-only mode: {len(remaining)} queued call(s) have no "
                f"recorded transcript ({missing}). Transcripts not seeded — "
                "run 'elt-taskgen record-transcripts' with an API key."
            )

        grouped: dict[str, list[BatchRequest]] = {}
        for request in remaining:
            route = self.provider.routing.for_role(request.role_name)
            grouped.setdefault(route.provider, []).append(request)

        for provider_name in sorted(grouped):
            requests = grouped[provider_name]
            if not self.batchable(provider_name, len(requests), requests):
                reasons.append(
                    f"{provider_name}: {self._why_not_batched(provider_name, len(requests), requests)}"
                )
                self._run_serial(requests, responses, serial)
                continue
            try:
                collected = self._submit_and_collect(requests)
            except _provider_backends.MissingCredentialsError:
                raise  # refuse: no keys and no transcripts (standard remedy)
            except (RuntimeError, urllib.error.URLError) as exc:
                reasons.append(f"{provider_name}: batch unavailable ({exc})")
                self._run_serial(requests, responses, serial)
                continue
            unusable: list[BatchRequest] = []
            for request in requests:
                entry = collected.get(request.custom_id)
                text = self._collect_one(request, entry)
                if text is None:
                    unusable.append(request)
                    continue
                responses[request.custom_id] = text
                batched.append(request.custom_id)
            if unusable:
                reasons.append(
                    f"{provider_name}: {len(unusable)} batch result(s) unusable "
                    "— retried per call"
                )
                self._run_serial(unusable, responses, serial)

        self._pending.clear()
        return BatchRunResult(
            responses=responses,
            memoized=tuple(memoized),
            batched=tuple(batched),
            serial=tuple(serial),
            fallback_reasons=tuple(reasons),
        )

    def _why_not_batched(
        self, provider_name: str, count: int, requests: Sequence[BatchRequest] = ()
    ) -> str:
        agentic = sorted({r.role_name for r in requests if _provider_protocol.role_is_agentic(r.role_name)})
        if agentic:
            return f"agentic role(s) {agentic} run bounded sessions, never batches"
        if provider_name not in BATCH_PROVIDERS:
            return "provider does not support batching here; running serially"
        if self._batch_transport is None:
            return "no batch transport configured; running serially"
        return f"only {count} pending call(s) (< {self.min_batch_size}); running serially"

    def _run_serial(
        self,
        requests: Sequence[BatchRequest],
        responses: dict[str, str],
        serial: list[str],
    ) -> None:
        """Per-call fallback: the ordinary interactive path, unchanged. An
        agentic role is refused before any call: its exchange is a session,
        not a `complete()`."""
        for request in requests:
            self._refuse_agentic(request.role_name)
        for request in requests:
            responses[request.custom_id] = self.provider.complete(
                request.role_name, request.prompt
            )
            serial.append(request.custom_id)

    # -- the Anthropic Batch API --------------------------------------------

    def _anthropic_base_url(self) -> str:
        cfg = self.provider.routing.provider_config.get("anthropic") or {}
        return str(cfg.get("base_url") or "https://api.anthropic.com").rstrip("/")

    def _anthropic_headers(self) -> dict[str, str]:
        cfg = self.provider.routing.provider_config.get("anthropic") or {}
        api_key = str(cfg.get("api_key") or "")
        if not api_key:
            raise _provider_backends.MissingCredentialsError(
                "ANTHROPIC_API_KEY is not configured — set it (or seed "
                "transcripts with 'elt-taskgen record-transcripts') before "
                "submitting a batch; the queue refuses rather than dropping "
                "calls (fail closed)"
            )
        return {"x-api-key": api_key, "anthropic-version": _provider_backends.AnthropicBackend.API_VERSION}

    def _submit_and_collect(
        self, requests: Sequence[BatchRequest]
    ) -> dict[str, dict]:
        """Submit one batch, poll to completion, return custom_id -> result."""
        assert self._batch_transport is not None  # guarded by batchable()
        headers = self._anthropic_headers()
        base = self._anthropic_base_url()
        payload = {"requests": []}
        for request in sorted(requests, key=lambda r: r.custom_id):
            route = self.provider.routing.for_role(request.role_name)
            payload["requests"].append(
                {
                    "custom_id": request.custom_id,
                    "params": _provider_backends.AnthropicBackend._payload(
                        model=route.model,
                        prompt=request.prompt,
                        max_tokens=route.max_tokens,
                        effort=route.effort,
                        role_name=request.role_name,
                        schema_mode=_provider_protocol.uses_findings_schema(request.role_name),
                        agents_config=getattr(self.provider, "agents_config", None),
                    ),
                }
            )
        started = time.monotonic()
        submitted = self._batch_transport(
            "POST", base + BATCH_API_PATH, headers, payload
        )
        batch_id = str(submitted.get("id") or "")
        if not batch_id:
            raise RuntimeError("batch submission returned no batch id")
        status = str(submitted.get("processing_status") or "")
        waited = 0.0
        state = submitted
        while status != BATCH_STATUS_ENDED:
            if waited >= self.max_wait_seconds:
                raise RuntimeError(
                    f"batch {batch_id} did not end within "
                    f"{self.max_wait_seconds:.0f}s (status {status!r})"
                )
            self._sleep(self.poll_seconds)
            waited += self.poll_seconds
            state = self._batch_transport(
                "GET", f"{base}{BATCH_API_PATH}/{batch_id}", headers, None
            )
            status = str(state.get("processing_status") or "")
        results_url = str(state.get("results_url") or "") or (
            f"{base}{BATCH_API_PATH}/{batch_id}/results"
        )
        body = self._batch_transport("GET", results_url, headers, None)
        # A batch has no per-exchange wall: every result carries the batch's
        # submit-to-results wall, harness-measured, as its elapsed_ms.
        batch_elapsed_ms = _review_provider_types._elapsed_ms(started)
        collected: dict[str, dict] = {}
        for entry in body.get("results") or []:
            if isinstance(entry, dict) and entry.get("custom_id"):
                collected[str(entry["custom_id"])] = entry
                collected[str(entry["custom_id"])]["batch_id"] = batch_id
                collected[str(entry["custom_id"])]["batch_elapsed_ms"] = batch_elapsed_ms
        return collected

    def _collect_one(self, request: BatchRequest, entry: dict | None) -> str | None:
        """Validate ONE batch result, record it, meter it; None => unusable.

        An unusable result is NEVER repaired in place — the caller re-runs that
        request through the per-call path, which keeps the bounded retries.
        """
        if not isinstance(entry, dict):
            return None
        result = entry.get("result")
        if not isinstance(result, dict) or result.get("type") != "succeeded":
            return None
        message = result.get("message")
        if not isinstance(message, dict):
            return None
        role_name = request.role_name
        schema_mode = _provider_protocol.uses_findings_schema(role_name)
        if schema_mode:
            data = _provider_backends.AnthropicBackend._tool_input(message, _provider_protocol.tool_name_for(role_name))
            if _provider_protocol.validate_payload_for(role_name, data) is not None:
                return None
            text = _provider_protocol.normalized_text_for(role_name, data)
        else:
            text = _provider_backends.AnthropicBackend._text(message)
            if not text.strip():
                return None
        usage = _review_provider_types.Usage.from_anthropic(message)
        route = self.provider.routing.for_role(role_name)
        model = str(message.get("model") or route.model)
        elapsed_ms = int(entry.get("batch_elapsed_ms") or 0)
        rates = _review_provider_accounting.rate_card_for(
            route.provider,
            route.model,
            self.provider.routing.provider_config.get(route.provider) or {},
        )
        # Record BEFORE anything can raise on the budget: money spent must
        # never be lost (same rule as the interactive path).
        agents_config = getattr(self.provider, "agents_config", None)
        recorded = {
            "role": role_name,
            "prompt_sha256": request.prompt_sha256,
            "system_sha256": _provider_protocol.role_behavior_sha256(role_name, agents_config=agents_config),
            "provider": route.provider,
            "model": model,
            "served_model": str(message.get("model") or ""),
            "response": text,
            "response_sha256": sha256_hex(text),
            "task_id": self.provider.task_id,
            "task_content_hash": self.provider.task_content_hash,
            "attempt_count": 1,
            "correction_count": 0,
            "finding_count": self.provider._finding_count(role_name, text),
            "usage": usage.as_dict(),
            "elapsed_ms": elapsed_ms,
            "raw_attempts": [message],
            "batch_id": str(entry.get("batch_id") or ""),
            # Binds on ROUTE.model (what was asked for), not the message's
            # served-model string — the serve-time check compares routes.
            "route": self.provider._route_block(route, agents_config=agents_config),
        }
        if recorded["finding_count"] is not None:
            recorded["zero_findings"] = recorded["finding_count"] == 0
        recorded.update(self.provider._record_stamp())
        self.provider.store.record(role_name, request.prompt_sha256, recorded)
        self.provider._append_exchange_evidence(
            role_name,
            request.prompt_sha256,
            text,
            recorded,
            replayed=False,
            usd=self.provider.meter.price(usage=usage, rates=rates),
        )
        # Batches are metered at full interactive rates (a budget may
        # overstate spend, never understate); one attempt per result. A
        # declared-but-disabled session cap never trips a one-shot exchange.
        self.provider.meter.trajectory(
            self.provider.task_id,
            role_name,
            enforce_role_cap=not route.cap_declared_but_disabled,
        ).charge(
            usage=usage,
            rates=rates,
            elapsed_ms=elapsed_ms,
            model=model,
        )
        return text


# ---------------------------------------------------------------------------
# Phase 4 (OQ-20): one RoutedProvider per metrology worker
# ---------------------------------------------------------------------------

class RoutedProviderPool:
    """Present multiple routed providers as one metrology provider.

    Each trial is bound to one worker, which owns its session ledger and trial executor.
    Workers share routing, transcript storage, and the thread-safe cost meter. Evidence
    rows are merged in stable trial/role order, and context-local bindings keep
    concurrent trials isolated.
    """

    def __init__(self, workers: Sequence[Any]) -> None:
        if not workers:
            raise ValueError("a provider pool needs at least one worker")
        self.workers: tuple[Any, ...] = tuple(workers)
        self.lock = threading.RLock()
        self._trials_begun = 0
        self._current: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
            f"elt_taskgen_provider_pool_worker_{id(self)}", default=None
        )
        self._current_index: contextvars.ContextVar[int | None] = contextvars.ContextVar(
            f"elt_taskgen_provider_pool_trial_{id(self)}", default=None
        )
        # A context-local binding prevents cross-talk; this shared set also
        # prevents two contexts from concurrently driving the same mutable
        # RoutedProvider when callers submit more trials than the pool owns.
        self._active_worker_ids: set[int] = set()
        #: (trial index, worker) of every trial begun, so a worker's rows can
        #: be attributed to the trial they were made in.
        self._assignments: list[tuple[int, Any]] = []
        self._rows_before: dict[int, int] = {}

    # -- what the loop and the CLI read off the pool --------------------------

    @property
    def size(self) -> int:
        return len(self.workers)

    def __getattr__(self, name: str) -> Any:
        # Every attribute the metrology path reads off a provider (`routing`,
        # `agents_config`, `meter`, `store`, `task_id`, `replay_only`,
        # `refresh`, `admission_provenance`, `source`) is worker 0's: the
        # workers share routing, store and meter by construction.
        if name.startswith("_") or name in ("workers", "lock"):
            raise AttributeError(name)
        return getattr(self.workers[0], name)

    @property
    def exchange_evidence(self) -> list[dict]:
        """Every worker's rows, stamped with the trial they belong to and
        ordered by `(trial_index, role, prompt_sha256)`."""
        rows: list[dict] = []
        with self.lock:
            for index, worker in enumerate(self.workers):
                for position, row in enumerate(list(getattr(worker, "exchange_evidence", ()) or ())):
                    stamped = dict(row)
                    if "trial_index" not in stamped or stamped.get("trial_index") is None:
                        stamped["trial_index"] = self._trial_index_of(index, position)
                    rows.append(stamped)
        return sorted(rows, key=_evidence_sort_key)

    def _trial_index_of(self, worker_index: int, position: int) -> int | None:
        """The trial a worker's row at `position` was made in: the last trial
        assigned to that worker whose row count at begin was <= position."""
        with self.lock:
            best: int | None = None
            for trial_index, worker in self._assignments:
                if worker is self.workers[worker_index] and self._rows_before.get(trial_index, 0) <= position:
                    best = trial_index
            return best

    # -- council.Provider and the trial seam ------------------------------------

    def begin_trial(self, ctx: Any) -> None:
        if self._current.get() is not None:
            raise ToolHarnessFault("begin_trial", code="trial_already_open")
        with self.lock:
            index = self._trials_begun
            self._trials_begun += 1
            worker = self.workers[index % len(self.workers)]
            worker_id = id(worker)
            if worker_id in self._active_worker_ids:
                raise ToolHarnessFault("begin_trial", code="worker_already_active")
            self._active_worker_ids.add(worker_id)
            self._current.set(worker)
            self._current_index.set(index)
            self._assignments.append((index, worker))
            self._rows_before[index] = len(getattr(worker, "exchange_evidence", ()) or ())
        begin = getattr(worker, "begin_trial", None)
        try:
            if callable(begin):
                begin(ctx)
        except BaseException:
            self._current.set(None)
            self._current_index.set(None)
            with self.lock:
                self._active_worker_ids.discard(worker_id)
            raise

    def end_trial(self) -> None:
        worker = self._current.get()
        self._current.set(None)
        self._current_index.set(None)
        if worker is None:
            return
        end = getattr(worker, "end_trial", None)
        try:
            if callable(end):
                end()
        finally:
            with self.lock:
                self._active_worker_ids.discard(id(worker))

    def complete(self, role, prompt: str) -> str:
        worker = self._current.get()
        if worker is None:
            worker = self.workers[0]
        return worker.complete(role, prompt)

    def begin_task_evidence(self, task_id: str, task_content_hash: str, *, task: Any = None) -> None:
        for worker in self.workers:
            begin = getattr(worker, "begin_task_evidence", None)
            if not callable(begin):
                continue
            if _accepts_task_keyword(begin):
                begin(task_id, task_content_hash, task=task)
            else:
                begin(task_id, task_content_hash)


def _evidence_sort_key(row: Mapping[str, Any]) -> tuple:
    index = row.get("trial_index")
    ordered = index if isinstance(index, int) and not isinstance(index, bool) else None
    return (
        0 if ordered is not None else 1,
        ordered if ordered is not None else 0,
        str(row.get("role") or ""),
        str(row.get("prompt_sha256") or ""),
    )


def _accepts_task_keyword(fn: Any) -> bool:
    import inspect

    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if "task" in parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())



# Historical import paths delegate to one owning implementation.
_COMPAT_EXPORTS = {
    **dict.fromkeys((
        'AnthropicBackend',
        'MissingCredentialsError',
        'OpenAICompatBackend',
        'PROVIDER_BACKENDS',
        '_CHAT_ASSISTANT_KEYS',
        '_HeadlessBackendPlaceholder',
        '_anthropic_tool_choice',
        '_arguments_text',
        '_cache_first_user_turn',
        '_chat_assistant_turn',
        '_chat_content_blocks',
        '_chat_messages',
        '_chat_tool_choice',
        '_chat_tools',
        '_with_correction',
        '_with_correction_chat',
    ), _provider_backends),
    **dict.fromkeys((
        'DEFAULT_ROUTING_DOC',
        'DEFAULT_SANDBOX_PIN',
        'HEADLESS_PROVIDER_KINDS',
        'MODEL_FAMILY_PREFIXES',
        'RoleRoute',
        'RoleRouting',
        'SESSION_RUNNER_ROLES',
        'WITNESS_RUNNER_ROLES',
        '_ENV_PATTERN',
        '_MODEL_TAG_RE',
        '_SANDBOX_PIN_KEYS',
        '_agents_doc',
        '_agents_doc_at',
        '_agents_doc_for',
        '_agents_document_of',
        '_config_key',
        '_interpolate_env',
        '_is_anthropic_host',
        '_read_agents_document',
        'agents_config_identity',
        'agents_config_of',
        'agents_config_sha256',
        'credential_problems',
        'default_agents_config_path',
        'endpoint_host',
        'load_role_routing',
        'model_family',
        'role_loop_limits',
        'role_manifest_limits',
        'sandbox_pin',
    ), _provider_config),
    **dict.fromkeys((
        'AGENTIC_ROLES',
        'CORRECTION_TEXT',
        'FINDINGS_TOOL_NAME',
        'PROPOSAL_ROLES',
        'PROSE_ROLES',
        'REPAIR_PROPOSER_ROLE',
        'REPAIR_PROPOSER_SYSTEM',
        'SCHEMA_RETRIES',
        'SESSION_TRANSCRIPT_ENTRY_SCHEMA',
        'SHARED_ROLE_PREFIX',
        'SOLVER_ROLE_PREFIX',
        'TRANSCRIPT_ENTRY_SCHEMA',
        'TRANSCRIPT_KEY_VERSION',
        '_AUTO_CHOICE_ROLES',
        '_CRITIC_INSTRUCTIONS',
        '_behavior_sha_cached',
        '_coerce_stringified_findings',
        '_harness_validators_for',
        '_normalized_findings_text',
        '_normalized_proposal',
        '_reject_non_finite_constant',
        '_reject_non_finite_params',
        '_strict_schema_for',
        '_system_prompt',
        '_tool_description_for',
        '_validate_findings_payload',
        '_wire_is_terminal_only',
        'agentic_role_enabled',
        'clear_behavior_caches',
        'configure_retry_policy',
        'findings_tool_schema',
        'normalized_text_for',
        'proposed_case_schema',
        'repair_route_scope_text',
        'role_behavior_manifest',
        'role_behavior_sha256',
        'role_is_agentic',
        'role_tools_sha256',
        'session_policy_for',
        'session_tool_choice',
        'session_turn_key',
        'tool_choice_for',
        'tool_name_for',
        'tool_schema_for',
        'transcript_entry_schema',
        'transcript_key',
        'transcript_key_v3',
        'turn_wire_binding',
        'uses_findings_schema',
        'validate_payload_for',
        'wire_tools_for',
    ), _provider_protocol),
}


def __getattr__(name):
    owner = _COMPAT_EXPORTS.get(name)
    if owner is None:
        raise AttributeError(name)
    return getattr(owner, name)


def __dir__():
    return sorted(set(globals()) | set(_COMPAT_EXPORTS))

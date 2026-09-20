"""Persist and resumably orchestrate the task-production state machine.

The ledger is append-only, and the latest row controls each verdict. Later rows
shadow earlier ones; missing evidence never passes.
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

try:  # POSIX advisory locks.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised by the Windows fallback test
    _fcntl = None

try:  # Windows byte-range locks.
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - normal on POSIX
    _msvcrt = None

from elt_taskgen.models import (
    RLVR_TASK_VARIANTS,
    RepairRoute,
    TaskIR,
    TaskStatus,
    TaskVariant,
    canonical_json,
    readable_json,
    task_from_json,
    task_to_json,
    validate_task_id_segment,
)

#: Ledger filename under <workspace>/state/ (docs/INTERFACES.md workspace layout).
DB_FILENAME = "taskgen.sqlite"

#: WAL permits overlap; this timeout absorbs brief SQLite writer serialization.
SQLITE_BUSY_TIMEOUT_MS = 30_000

#: Advisory lock files live beside the ledger, one SHA-256 filename per task id.
#: Hashing avoids treating an externally-derived task id as a filesystem path.
TASK_LOCKS_DIRNAME = "task-locks"

#: Fixed advisory lock for workspace-wide corpus/release publication. Per-task
#: locks cannot serialize two coordinators whose selected rosters are disjoint.
COORDINATOR_LOCK_FILENAME = "coordinator.lock"

# Windows byte-range locks require one byte and retry to match blocking ``flock``.
_TASK_LOCK_RETRY_SECONDS = 0.05

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
#: Waiting on a prerequisite; spends no repair round and changes no task status.
VERDICT_BLOCKED = "blocked"
VERDICT_FATAL = "fatal"
_VERDICTS = (VERDICT_PASS, VERDICT_FAIL, VERDICT_BLOCKED, VERDICT_FATAL)

#: Append-only marker used by a narrowly authorized report supersession. The
#: replacement PASS is the marker, so publication never mutates TaskIR state.
EVIDENCE_SUPERSESSION_KEY = "evidence_supersession"
EVIDENCE_SUPERSESSION_SCHEMA = "report-supersession-v1"

#: Non-PASS marker reopening one obsolete fatal for remeasurement or adjudication.
FATAL_RECOVERY_KEY = "fatal_report_recovery"
FATAL_RECOVERY_SCHEMA = "fatal-report-recovery-v1"
FATAL_RECOVERY_EVIDENCE_SCHEMA = "fatal-recovery-evidence-v1"
FATAL_RECOVERY_EVIDENCE_DIR = "state/fatal-recovery-evidence"
FATAL_RECOVERY_REVALIDATOR_MODULE = "elt_taskgen.offline_recovery"
FATAL_RECOVERY_REVALIDATOR_QUALNAME = "validate_recovery_evidence"
MAX_FATAL_RECOVERY_CODE_BINDINGS = 16
MAX_FATAL_RECOVERY_OBSERVATIONS = 32

#: StagePayload.data key naming WHAT a blocked stage waits on.
BLOCKED_ON_KEY = "blocked_on"
BLOCKED_ON_HUMAN = "human"
BLOCKED_ON_ENVIRONMENT = "environment"
#: Prefix for a proposer session awaiting a salted rerun after a harness limit.
SESSION_LIMIT_BLOCK_PREFIX = "session_limit:"
#: Salted reruns allowed before an agent-attributable limit spends a normal round.
MAX_SESSION_LIMIT_RERUNS = 2
#: Non-agent limit kinds that halt as infrastructure after salted reruns.
SESSION_LIMIT_HALT_KINDS: frozenset[str] = frozenset({"wall"})
#: The marker such a halt carries (a `ProviderFault`'s class: transient
#: infrastructure on the provider boundary).
SESSION_WALL_HALT_MARKER = "ProviderFault"
#: Records fallback to a normal round and prevents repeating the salted sequence.
SESSION_LIMIT_FALLBACK_KEY = "session_limit_fallback"
#: Only role-scoped USD limits are agent-attributable. Task or total budget
#: exhaustion is a provider fault and halts immediately.
SESSION_USD_ROLE_SCOPE = "role"
SESSION_USD_HALT_SCOPES: frozenset[str] = frozenset({"task", "total"})
#: The marker such a halt carries: the class the budget path raises everywhere
#: else (transient infrastructure on the provider boundary).
SESSION_USD_HALT_MARKER = "BudgetExceededError"
#: Marker for proposer contract errors, which halt without spending a round.
PROPOSER_CONTRACT_MARKER = "proposer_contract"
#: A blocked stage inside certification is a no-measure infrastructure halt,
#: never a red result, rejection, or spent round. Record its reason and resume.
BLOCKED_STAGE_MARKER_PREFIX = "blocked_on:"
#: The reason a BLOCKED payload without a `blocked_on` key halts under.
BLOCKED_ON_UNKNOWN = "unknown"
#: Explicit retry guards require operator invalidation after prerequisites change.
RETRY_GUARD_KEY = "retry_guard"
RETRY_GUARD_EXPLICIT = "explicit"

FINAL_ACCEPTED = "accepted"
FINAL_REJECTED = "rejected"
FINAL_IN_PROGRESS = "in_progress"

#: Safety valve: run() restarts its sweep after every repair or semantic edit, so
#: a non-deterministic runner would loop forever. Fail closed by raising.
_MAX_SWEEPS = 100


class StageName(str, Enum):
    """The ledger stages, in pipeline order (FIX_PIPELINE.md stages 1-15 mapped
    onto the report-ledger vocabulary of docs/INTERFACES.md)."""

    INTAKE = "intake"
    CONTAMINATION_PRE = "contamination_pre"
    GENERATE = "generate"
    REFERENCE = "reference"
    AUTHOR = "author"
    REVIEW = "review"
    ATTACK = "attack"
    #: Shared project-integrity checks; the value stays ``gates`` so existing
    #: evidence stays readable, but this is not a FULL task.
    TASK_INTEGRITY = "gates"
    #: Backwards-compatible source alias.  New code should say TASK_INTEGRITY.
    GATES = "gates"
    #: Per-variant acceptance batteries — the two admission authorities, so they
    #: stay separate ledger stages. Declaration order is pipeline order.
    GATES_EXTRACT_LOAD = "gates_extract_load"
    GATES_TRANSFORM = "gates_transform"
    CALIBRATE = "calibrate"
    CONTAMINATION_POST = "contamination_post"
    SELECT = "select"
    RELEASE = "release"


#: Enum declaration order IS pipeline order.
STAGE_ORDER: tuple[StageName, ...] = tuple(StageName)
_STAGE_VALUES = frozenset(s.value for s in StageName)

#: Release artifacts checked by ``report_is_current``.
RELEASE_DIRNAME = "release"
RELEASE_MANIFEST_FILENAME = "release_manifest.json"

#: Which ledger stage carries which variant's acceptance battery. FULL is mapped
#: for legacy diagnostics only; admission iterates RLVR_TASK_VARIANTS.
VARIANT_GATE_STAGE: dict[TaskVariant, StageName] = {
    TaskVariant.FULL: StageName.TASK_INTEGRITY,
    TaskVariant.EXTRACT_LOAD: StageName.GATES_EXTRACT_LOAD,
    TaskVariant.TRANSFORM: StageName.GATES_TRANSFORM,
}


def variant_gate_stage(variant: TaskVariant) -> StageName:
    """The ledger stage whose latest report carries `variant`'s battery."""
    return VARIANT_GATE_STAGE[TaskVariant(variant)]


#: Inverse of VARIANT_GATE_STAGE: which variant's roster a stage's recorded
#: payload must cover. Read by `Engine.report_is_current`.
_STAGE_VARIANT: dict[str, TaskVariant] = {
    stage.value: variant for variant, stage in VARIANT_GATE_STAGE.items()
}


#: Unhashed status updates; acceptance still requires both variant reports.
_STATUS_AFTER_PASS: dict[StageName, TaskStatus] = {
    StageName.GENERATE: TaskStatus.GENERATED,
    StageName.REFERENCE: TaskStatus.GOLD_FROZEN,
    StageName.AUTHOR: TaskStatus.AUTHORED,
    StageName.REVIEW: TaskStatus.REVIEWED,
    StageName.ATTACK: TaskStatus.ATTACKED,
    StageName.CALIBRATE: TaskStatus.CALIBRATED,
    StageName.SELECT: TaskStatus.SELECTED,
    StageName.RELEASE: TaskStatus.RELEASED,
}


class EngineError(RuntimeError):
    """Engine-level failure (missing task, corrupt state, non-convergence)."""


class RepairCommitError(EngineError):
    """A durable repair intent did not finish installing its target bytes."""


class TaskLockBusy(EngineError):
    """A nested lock acquisition would risk deadlock; retry after contention."""


class StageNotWiredError(EngineError):
    """run() reached a stage with no wired runner. Aborting is the fail-closed
    behavior: an unwired stage can never be silently skipped or passed."""


class InfrastructureFailure(EngineError):
    """A stage transport failed without rejecting or spending a repair round."""

    def __init__(
        self,
        task_id: str,
        stage: str,
        marker: str,
        *,
        budget_scope: str = "",
    ) -> None:
        super().__init__(
            f"infrastructure failure ({marker}) at stage {stage!r} of task "
            f"{task_id!r}: the transport failed, not the task. Fix the "
            "infrastructure and re-run in this workspace — the ledger resumes "
            "at this stage."
        )
        self.task_id = task_id
        self.stage = stage
        self.marker = marker
        # Only task and total budget scopes are infrastructure-level.
        self.budget_scope = _normalise_infrastructure_budget_scope(budget_scope)


class StageBlocked(InfrastructureFailure):
    """A certification stage waited instead of measuring.

    The wait halts as infrastructure, rejects nothing, and spends no repair round.
    """

    def __init__(self, task_id: str, stage: str, blocked_on: str) -> None:
        reason = _marker_text(blocked_on) or BLOCKED_ON_UNKNOWN
        EngineError.__init__(
            self,
            f"stage {stage!r} of task {task_id!r} is WAITING (blocked_on="
            f"{reason}) inside a certification: a wait is not a measurement "
            "of the patch, so nothing is rejected and no repair round is "
            "spent. Clear the wait and re-run in this workspace — the ledger "
            "resumes at the failed stage."
        )
        self.task_id = task_id
        self.stage = stage
        self.blocked_on = reason
        self.marker = blocked_stage_marker(reason)
        self.budget_scope = ""


def blocked_on_of(payload: Any) -> str:
    """The `blocked_on` reason a BLOCKED payload carries (`data.blocked_on`,
    `BLOCKED_ON_KEY`), lowercased; '' when the payload names none."""
    if payload is None:
        return ""
    try:
        dumped = (
            payload.model_dump(mode="json")
            if hasattr(payload, "model_dump")
            else payload
        )
    except Exception:  # noqa: BLE001 — an unreadable payload names no reason
        return ""
    if not isinstance(dumped, dict):
        return ""
    data = dumped.get("data")
    if not isinstance(data, dict):
        return ""
    return _marker_text(data.get(BLOCKED_ON_KEY, ""))


def blocked_stage_marker(blocked_on: str) -> str:
    """The infrastructure marker a certification halts under when a member
    stage WAITED: `blocked_on:<reason>` (`BLOCKED_ON_UNKNOWN` for none)."""
    return f"{BLOCKED_STAGE_MARKER_PREFIX}{_marker_text(blocked_on) or BLOCKED_ON_UNKNOWN}"


def blocked_on_from_marker(marker: Any) -> str:
    """The `blocked_on` reason a `blocked_on:<reason>` marker names, or ''
    for any other marker."""
    text = _marker_text(marker)
    if not text.startswith(BLOCKED_STAGE_MARKER_PREFIX):
        return ""
    return text[len(BLOCKED_STAGE_MARKER_PREFIX):] or BLOCKED_ON_UNKNOWN


def blocked_retry_requires_explicit_recovery(row: Any) -> bool:
    """Whether the latest same-identity BLOCKED row forbids automatic retry."""

    if row is None or getattr(row, "verdict", None) != VERDICT_BLOCKED:
        return False
    try:
        payload = json.loads(row.payload_json)
    except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
        return False
    data = payload.get("data") if isinstance(payload, dict) else None
    return bool(
        isinstance(data, dict)
        and str(data.get(RETRY_GUARD_KEY) or "").strip().lower()
        == RETRY_GUARD_EXPLICIT
    )


class StagePayload(BaseModel):
    """Generic typed report payload, so the engine itself never writes an untyped
    dict into the ledger. Stage runners may record richer typed payloads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    detail: str = ""
    error: str = ""
    #: Producer-supplied transport marker; empty means an ordinary task failure.
    infrastructure: str = ""
    #: Infrastructure-level budget scope; ``None`` preserves legacy payload shape.
    budget_scope: Literal["task", "total"] | None = None
    data: dict[str, str] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def _omit_compatibility_none(self, handler):
        """Keep old marker-only payloads byte-shaped exactly as before."""

        dumped = handler(self)
        if self.budget_scope is None:
            dumped.pop("budget_scope", None)
        return dumped


@dataclass(frozen=True)
class StageOutcome:
    """A stage verdict, payload, optional TaskIR update, route, and halt marker."""

    verdict: str
    payload: BaseModel
    task: TaskIR | None = None
    route: RepairRoute | None = None
    infrastructure: str = ""


StageRunner = Callable[["Engine", TaskIR], StageOutcome]


class RepairProposerLike(Protocol):
    """Provider-independent structural interface required from repair proposers."""

    def repair(
        self,
        engine: "Engine",
        task: TaskIR,
        stage: str,
        route: RepairRoute,
        failure: str,
    ) -> Any: ...


@dataclass(frozen=True)
class ReportRow:
    """Thin typed view of one `reports` ledger row."""

    id: int
    task_id: str
    revision: int
    stage: str
    verdict: str
    payload_json: str
    content_hash: str
    created_at: str


@dataclass(frozen=True)
class ReportSupersession:
    """Exact old evidence an explicit code migration is allowed to supersede."""

    migration_id: str
    reason: str
    stage: str
    content_hash: str
    revalidation_path: str
    revalidation_sha256: str
    cause_report_id: int
    cause_payload_sha256: str
    target_report_id: int
    target_payload_sha256: str
    replacement_detail: str
    replacement_data: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EVIDENCE_SUPERSESSION_SCHEMA,
            "migration_id": self.migration_id,
            "reason": self.reason,
            "stage": self.stage,
            "content_hash": self.content_hash,
            "revalidation_path": self.revalidation_path,
            "revalidation_sha256": self.revalidation_sha256,
            "cause_report_id": self.cause_report_id,
            "cause_payload_sha256": self.cause_payload_sha256,
            "target_report_id": self.target_report_id,
            "target_payload_sha256": self.target_payload_sha256,
            "replacement_detail": self.replacement_detail,
            "replacement_data": dict(self.replacement_data),
        }


class RecoveryCodeIdentity(BaseModel):
    """One importable source module whose exact current bytes certify a fix."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    module: str = Field(pattern=r"^elt_taskgen(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecoveryCallableIdentity(BaseModel):
    """The pure validator used to interpret an offline recovery record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    module: Literal["elt_taskgen.offline_recovery"] = (
        FATAL_RECOVERY_REVALIDATOR_MODULE
    )
    qualname: Literal["validate_recovery_evidence"] = (
        FATAL_RECOVERY_REVALIDATOR_QUALNAME
    )
    module_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecoveryObservation(BaseModel):
    """Immutable workspace-local input consulted by the offline revalidator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def recovery_code_fingerprint(bindings: Iterable[RecoveryCodeIdentity]) -> str:
    """Stable digest of an ordered fixed-code identity set."""

    payload = [binding.model_dump(mode="json") for binding in bindings]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class FatalRecoveryEvidence(BaseModel):
    """Authority to remeasure or explicitly block one obsolete fatal.

    This record never claims the stage passed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["fatal-recovery-evidence-v1"] = (
        FATAL_RECOVERY_EVIDENCE_SCHEMA
    )
    task_id: str = Field(min_length=1)
    task_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    recovery_kind: str = Field(pattern=r"^[a-z][a-z0-9_]{2,127}$")
    stage: str = Field(min_length=1)
    cause_report_id: int = Field(gt=0)
    cause_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_report_id: int = Field(gt=0)
    target_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: Literal["fail", "blocked"]
    failure_class: Literal["stale_evidence", "pending_adjudication"]
    failure_code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,127}$")
    blocked_on: Literal["", "human"] = ""
    retry_guard: Literal["", "explicit"] = ""
    recovery_prerequisite: str = Field(min_length=1, max_length=1024)
    reason: str = Field(min_length=1, max_length=4096)
    fixed_code_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixed_code: tuple[RecoveryCodeIdentity, ...] = Field(
        min_length=1,
        max_length=MAX_FATAL_RECOVERY_CODE_BINDINGS,
    )
    revalidator: RecoveryCallableIdentity
    observations: tuple[RecoveryObservation, ...] = Field(
        default=(),
        max_length=MAX_FATAL_RECOVERY_OBSERVATIONS,
    )
    certification: dict[str, str] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _exact_recovery_shape(self) -> "FatalRecoveryEvidence":
        validate_task_id_segment(self.task_id)
        _stage_value(self.stage)
        modules = tuple(binding.module for binding in self.fixed_code)
        if modules != tuple(sorted(modules)) or len(set(modules)) != len(modules):
            raise ValueError("fixed_code must be unique and sorted by module")
        observation_paths = tuple(item.path for item in self.observations)
        if observation_paths != tuple(sorted(observation_paths)) or len(
            set(observation_paths)
        ) != len(observation_paths):
            raise ValueError("observations must be unique and sorted by path")
        if self.cause_report_id > self.target_report_id:
            raise ValueError("fatal recovery cause cannot follow its target")
        if any(not key.strip() or not value.strip() for key, value in self.certification.items()):
            raise ValueError("fatal recovery certification entries must be non-empty")
        if self.fixed_code_fingerprint != recovery_code_fingerprint(self.fixed_code):
            raise ValueError("fixed_code_fingerprint does not match fixed_code")
        if self.disposition == VERDICT_FAIL:
            if (
                self.failure_class != "stale_evidence"
                or self.blocked_on
                or self.retry_guard
            ):
                raise ValueError(
                    "fail recovery must be stale_evidence without a retry guard"
                )
        elif (
            self.failure_class != "pending_adjudication"
            or self.blocked_on != BLOCKED_ON_HUMAN
            or self.retry_guard != RETRY_GUARD_EXPLICIT
        ):
            raise ValueError(
                "blocked recovery must be pending_adjudication, blocked_on=human, "
                "retry_guard=explicit"
            )
        return self

    def deterministic_bytes(self) -> bytes:
        return (readable_json(self.model_dump(mode="json")) + "\n").encode("utf-8")


@dataclass(frozen=True)
class FatalReportRecovery:
    """Ledger marker that binds one exact fatal to immutable evidence."""

    recovery_id: str
    recovery_kind: str
    stage: str
    content_hash: str
    evidence_path: str
    evidence_sha256: str
    cause_report_id: int
    cause_payload_sha256: str
    target_report_id: int
    target_payload_sha256: str
    disposition: str
    failure_class: str
    failure_code: str
    blocked_on: str
    retry_guard: str
    recovery_prerequisite: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": FATAL_RECOVERY_SCHEMA,
            "recovery_id": self.recovery_id,
            "recovery_kind": self.recovery_kind,
            "stage": self.stage,
            "content_hash": self.content_hash,
            "evidence_path": self.evidence_path,
            "evidence_sha256": self.evidence_sha256,
            "cause_report_id": self.cause_report_id,
            "cause_payload_sha256": self.cause_payload_sha256,
            "target_report_id": self.target_report_id,
            "target_payload_sha256": self.target_payload_sha256,
            "disposition": self.disposition,
            "failure_class": self.failure_class,
            "failure_code": self.failure_code,
            "blocked_on": self.blocked_on,
            "retry_guard": self.retry_guard,
            "recovery_prerequisite": self.recovery_prerequisite,
        }

    @classmethod
    def from_evidence(
        cls,
        *,
        evidence_path: str,
        evidence_sha256: str,
        evidence: FatalRecoveryEvidence,
    ) -> "FatalReportRecovery":
        return cls(
            recovery_id=evidence_sha256,
            recovery_kind=evidence.recovery_kind,
            stage=evidence.stage,
            content_hash=evidence.task_content_hash,
            evidence_path=evidence_path,
            evidence_sha256=evidence_sha256,
            cause_report_id=evidence.cause_report_id,
            cause_payload_sha256=evidence.cause_payload_sha256,
            target_report_id=evidence.target_report_id,
            target_payload_sha256=evidence.target_payload_sha256,
            disposition=evidence.disposition,
            failure_class=evidence.failure_class,
            failure_code=evidence.failure_code,
            blocked_on=evidence.blocked_on,
            retry_guard=evidence.retry_guard,
            recovery_prerequisite=evidence.recovery_prerequisite,
        )


def recovery_code_identity(module_name: str) -> RecoveryCodeIdentity:
    """Resolve an importable package module to its current source-byte identity."""

    try:
        candidate = RecoveryCodeIdentity(module=module_name, sha256="0" * 64)
    except ValueError as exc:
        raise EngineError(f"fatal recovery code module is invalid: {module_name!r}") from exc
    try:
        module = importlib.import_module(candidate.module)
    except (ImportError, ValueError) as exc:
        raise EngineError(
            f"fatal recovery code module is not importable: {candidate.module!r}"
        ) from exc
    source_value = getattr(module, "__file__", None)
    if not source_value:
        raise EngineError(
            f"fatal recovery code module has no source file: {candidate.module!r}"
        )
    source = Path(source_value)
    if source.suffix in {".pyc", ".pyo"}:
        try:
            source = Path(importlib.util.source_from_cache(str(source)))
        except (NotImplementedError, ValueError) as exc:
            raise EngineError(
                f"fatal recovery cannot resolve source for {candidate.module!r}"
            ) from exc
    try:
        before = source.stat()
        payload = source.read_bytes()
        after = source.stat()
    except OSError as exc:
        raise EngineError(
            f"fatal recovery cannot read source for {candidate.module!r}: {exc}"
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or len(payload) != after.st_size
    ):
        raise EngineError(
            f"fatal recovery source changed while read: {candidate.module!r}"
        )
    return RecoveryCodeIdentity(
        module=candidate.module,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


class ReportRevalidationEvidence(BaseModel):
    """Immutable local proof bound to one report supersession."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["report-revalidation-v1"] = "report-revalidation-v1"
    migration_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_path: str = Field(min_length=1)
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    equivalence_commit_path: str = Field(min_length=1)
    equivalence_commit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_id: str = Field(min_length=1)
    intake_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    archived_task_ir_path: str = Field(min_length=1)
    archived_task_ir_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: str = Field(min_length=1)
    cause_report_id: int = Field(gt=0)
    cause_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_report_id: int = Field(gt=0)
    target_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_identity: dict[str, str] = Field(min_length=1)
    original_evidence: dict[str, str] = Field(min_length=1)
    deterministic_preconditions: tuple[str, ...] = Field(min_length=1)
    findings: tuple[str, ...] = ()
    replacement_detail: str = Field(min_length=1)
    replacement_data: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exact_green_revalidation(self) -> "ReportRevalidationEvidence":
        validate_task_id_segment(self.task_id)
        validate_task_id_segment(self.run_id)
        _stage_value(self.stage)
        if self.cause_report_id >= self.target_report_id:
            raise ValueError("revalidation cause must precede its fatal target")
        if self.findings:
            raise ValueError("report revalidation must record zero findings")
        if self.authoritative_generator_sha256 == self.reproduced_generator_sha256:
            raise ValueError("report revalidation must bind a generator transition")
        if EVIDENCE_SUPERSESSION_KEY in self.replacement_data:
            raise ValueError("replacement data must not contain its own marker")
        if set(self.validator_identity) != {
            "generator_sha256",
            "prose_fidelity_module_sha256",
            "declarative_prose_module_sha256",
        }:
            raise ValueError("report revalidation validator identity is incomplete")
        required_original = {
            "prose_sha256",
            "session_sha256",
            "source",
            "drafts",
            "revisions",
            "session_terminal",
            "transcript_path",
            "transcript_sha256",
            "session_summary_path",
            "session_summary_sha256",
            "transcript_document_sha256",
            "transcript_manifest",
            "session_index_path",
            "session_index_sha256",
            "trajectory_path",
            "trajectory_sha256",
            "request_path",
            "request_sha256",
            "old_error",
            "old_error_sha256",
        }
        if set(self.original_evidence) != required_original:
            raise ValueError("report revalidation original evidence is incomplete")
        if self.deterministic_preconditions != (
            "structural-completeness-green",
            "required-attack-matrix-green",
            "prose-fidelity-zero-findings",
            "persisted-author-session-bound",
            "persisted-author-transcript-exact-prompt-bound",
        ):
            raise ValueError("report revalidation preconditions are not exact")
        return self

    @model_serializer(mode="wrap")
    def _sorted_maps(self, handler):
        payload = handler(self)
        payload["validator_identity"] = dict(sorted(payload["validator_identity"].items()))
        payload["original_evidence"] = dict(sorted(payload["original_evidence"].items()))
        payload["replacement_data"] = dict(sorted(payload["replacement_data"].items()))
        return payload

    def deterministic_bytes(self) -> bytes:
        return (readable_json(self.model_dump(mode="json")) + "\n").encode("utf-8")


def validate_author_revalidation_records(
    *,
    task_id: str,
    intake_content_hash: str,
    prompt: str,
    session_sha256: str,
    transcript_records: tuple[tuple[str, Mapping[str, Any]], ...],
    session_index_path: str,
    session_index: Mapping[str, Any],
    trajectory_path: str,
    trajectory: Mapping[str, Any],
    session_summary: Mapping[str, Any],
) -> dict[str, str]:
    """Cross-check exact records and links for one semantic-author session."""

    from elt_taskgen.review import trajectory as trajectory_mod

    def refuse(message: str) -> None:
        raise ValueError(f"semantic-author revalidation evidence: {message}")

    def digest(value: Any, label: str) -> str:
        text = str(value or "")
        if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
            refuse(f"{label} is not lowercase SHA-256")
        return text

    role = "semantic_author"
    if not prompt:
        refuse("current solver prose is empty")
    validate_task_id_segment(task_id)
    digest(intake_content_hash, "intake content hash")
    digest(session_sha256, "session digest")
    if not transcript_records:
        refuse("transcript roster is empty")
    turn_keys = tuple(PurePosixPath(path).stem for path, _ in transcript_records)
    if any(not key for key in turn_keys) or len(set(turn_keys)) != len(turn_keys):
        refuse("transcript turn keys are missing or duplicated")
    session_key = turn_keys[0]

    try:
        verified_trajectory = trajectory_mod.verify_trajectory_record(trajectory)
    except (TypeError, ValueError) as exc:
        refuse(f"trajectory chain does not verify ({exc})")
    trajectory_key = PurePosixPath(trajectory_path).stem
    if (
        trajectory.get("record_version") != trajectory_mod.TRAJECTORY_RECORD_VERSION
        or trajectory.get("entry_schema") != 3
        or trajectory.get("role") != role
        or trajectory.get("task_id") != task_id
        or trajectory.get("task_content_hash") != intake_content_hash
        or trajectory.get("session_key") != session_key
        or trajectory.get("prompt_sha256") != session_key
        or trajectory.get("session_sha256") != session_sha256
        or trajectory.get("trajectory_sha256") != verified_trajectory
        or trajectory_key != verified_trajectory
        or trajectory.get("terminal") != "SUBMITTED"
        or str(trajectory.get("stop_reason") or "").casefold() != "submitted"
    ):
        refuse("trajectory identity/terminal fields differ")
    route = trajectory.get("route")
    admission = trajectory.get("admission")
    expected_route_keys = {
        "provider",
        "model",
        "max_tokens",
        "effort",
        "behavior_sha256",
        "tools_sha256",
        "policy_sha256",
        "diagnostics_version",
        "entry_schema",
    }
    expected_admission_keys = {
        "admission_mode",
        "admission_record_path",
        "admission_routing_fingerprint",
        "admission_evidence_sha256",
        "admission_seed",
    }
    if not isinstance(route, Mapping) or set(route) != expected_route_keys:
        refuse("trajectory route block is not exact")
    if not isinstance(admission, Mapping) or set(admission) != expected_admission_keys:
        refuse("trajectory admission stamp is not exact")
    for key in ("provider", "model", "effort", "diagnostics_version"):
        if not str(route.get(key) or ""):
            refuse(f"trajectory route {key} is empty")
    if route.get("entry_schema") != 3 or not isinstance(route.get("max_tokens"), int):
        refuse("trajectory route schema/token limit is invalid")
    for key in ("behavior_sha256", "tools_sha256", "policy_sha256"):
        digest(route.get(key), f"trajectory route {key}")
    for key in (
        "admission_mode",
        "admission_record_path",
        "admission_routing_fingerprint",
        "admission_evidence_sha256",
    ):
        if not str(admission.get(key) or ""):
            refuse(f"trajectory admission {key} is empty")
    digest(admission.get("admission_routing_fingerprint"), "admission routing fingerprint")
    digest(admission.get("admission_evidence_sha256"), "admission evidence digest")

    model_turns = trajectory.get("model_turns")
    turns = trajectory.get("turns")
    counts = trajectory.get("counts")
    chain_hashes = trajectory.get("chain_hashes")
    if (
        not isinstance(model_turns, list)
        or not isinstance(turns, list)
        or not isinstance(counts, Mapping)
        or not isinstance(chain_hashes, list)
        or len(model_turns) != len(transcript_records)
        or counts.get("model_call_count") != len(model_turns)
    ):
        refuse("trajectory model-turn counts differ")
    chained_model_turns = [
        turn
        for turn in turns
        if isinstance(turn, Mapping) and turn.get("kind") == "model"
    ]
    if len(chained_model_turns) != len(model_turns):
        refuse("trajectory chained/model-turn rosters differ")
    observations = [
        str(turn.get("output_sha256") or "")
        for turn in turns
        if isinstance(turn, Mapping)
        and turn.get("kind") in {"tool", "validator"}
        and turn.get("output_sha256")
    ]

    if (
        PurePosixPath(session_index_path).stem != session_key
        or session_index.get("entry_schema") != 3
        or session_index.get("role") != role
        or session_index.get("session_key") != session_key
        or session_index.get("task_id") != task_id
        or session_index.get("task_content_hash") != intake_content_hash
        or session_index.get("policy_sha256") != trajectory.get("policy_sha256")
        or session_index.get("tools_sha256") != trajectory.get("tools_sha256")
        or session_index.get("session_salt") != trajectory.get("session_salt")
        or session_index.get("session_sha256") != session_sha256
        or session_index.get("trajectory_sha256") != verified_trajectory
        or session_index.get("chain_hashes") != chain_hashes
        or session_index.get("terminal") != "SUBMITTED"
        or session_index.get("turn_keys") != list(turn_keys)
        or session_index.get("observations_sha256") != observations
        or session_index.get("model_call_count") != len(model_turns)
    ):
        refuse("canonical session index differs from trajectory")

    served_models: list[str] = []
    for index, ((path, entry), model_turn, chained_turn) in enumerate(
        zip(
            transcript_records,
            model_turns,
            chained_model_turns,
            strict=True,
        )
    ):
        if not isinstance(entry, Mapping) or not isinstance(model_turn, Mapping):
            refuse("transcript/model-turn record is not an object")
        key = turn_keys[index]
        turn = entry.get("turn")
        content = turn.get("content") if isinstance(turn, Mapping) else None
        if not isinstance(turn, Mapping) or not isinstance(content, list):
            refuse("transcript turn block/content is missing")
        response = str(entry.get("response") or "")
        if (
            PurePosixPath(path).stem != key
            or entry.get("role") != role
            or entry.get("task_id") != task_id
            or entry.get("task_content_hash") != intake_content_hash
            or entry.get("prompt_sha256") != key
            or turn.get("memo_key") != key
            or entry.get("route") != route
            or entry.get("admission") != admission
            or entry.get("provider") != route.get("provider")
            or entry.get("model") != route.get("model")
            or entry.get("response_sha256")
            != hashlib.sha256(response.encode("utf-8")).hexdigest()
            or turn.get("content_sha256")
            != hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()
            or response != canonical_json(content)
            or model_turn.get("turn_key") != key
            or chained_turn.get("memo_key") != key
            or model_turn.get("request_sha256")
            != chained_turn.get("prompt_sha256")
            or model_turn.get("response_sha256") != entry.get("response_sha256")
            or chained_turn.get("response_sha256")
            != entry.get("response_sha256")
            or model_turn.get("raw_response") != content
            or model_turn.get("served_model") != entry.get("served_model")
        ):
            refuse(f"transcript turn {index} differs from trajectory")
        served_models.append(str(entry.get("served_model") or ""))

    last_content = transcript_records[-1][1]["turn"]["content"]
    if len(last_content) != 1 or not isinstance(last_content[0], Mapping):
        refuse("terminal transcript does not contain one tool use")
    terminal_call = last_content[0]
    if (
        terminal_call.get("type") != "tool_use"
        or terminal_call.get("name") != "submit_prose"
        or terminal_call.get("input") != {"text": prompt}
        or trajectory.get("response_sha256")
        != hashlib.sha256(canonical_json({"text": prompt}).encode("utf-8")).hexdigest()
    ):
        refuse("terminal submission is not the current solver prose")

    summary_session = session_summary.get("session")
    if not isinstance(summary_session, Mapping):
        refuse("task session summary has no session object")
    if (
        session_summary.get("role") != role
        or session_summary.get("terminal") != "SUBMITTED"
        or summary_session.get("role") != role
        or summary_session.get("terminal_name") != "SUBMITTED"
        or summary_session.get("session_sha256") != session_sha256
        or summary_session.get("trajectory_sha256") != session_sha256
        or summary_session.get("task_content_hash") != intake_content_hash
        or summary_session.get("tools_sha256") != trajectory.get("tools_sha256")
        or summary_session.get("policy_sha256") != trajectory.get("policy_sha256")
        or summary_session.get("session_salt") != trajectory.get("session_salt")
        or summary_session.get("turns") != turns
        or summary_session.get("chain_hashes") != chain_hashes
        or summary_session.get("model_call_count") != len(model_turns)
        or summary_session.get("final") != {"text": prompt}
    ):
        refuse("task session summary differs from canonical trajectory")
    return {
        "provider": str(route["provider"]),
        "model": str(route["model"]),
        "served_models": canonical_json(served_models),
        "route": canonical_json(route),
        "admission": canonical_json(admission),
        "behavior_sha256": str(route["behavior_sha256"]),
        "tools_sha256": str(route["tools_sha256"]),
        "policy_sha256": str(route["policy_sha256"]),
        "session_key": session_key,
        "trajectory_sha256": verified_trajectory,
    }


@dataclass(frozen=True)
class RepairRow:
    """Typed repair row with starting fingerprint and optional lineage root."""

    id: int
    task_id: str
    revision: int
    route: str
    reason: str
    fingerprint: str
    lineage_root_hash: str = ""


@dataclass(frozen=True)
class RepairIntentRow:
    """Durable target of one crash-recoverable repair commit."""

    intent_id: str
    task_id: str
    target_revision: int
    route: str
    reason: str
    fingerprint: str
    task_json: str
    state: str
    repair_id: int
    report_ids_json: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id       TEXT NOT NULL,
  revision      INTEGER NOT NULL,
  stage         TEXT NOT NULL,
  verdict       TEXT NOT NULL,
  payload_json  TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_task ON reports(task_id, id);

CREATE TABLE IF NOT EXISTS artifacts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    TEXT NOT NULL,
  revision   INTEGER NOT NULL,
  rel_path   TEXT NOT NULL,
  sha256     TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repairs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id           TEXT NOT NULL,
  revision          INTEGER NOT NULL,
  route             TEXT NOT NULL,
  reason            TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  fingerprint       TEXT NOT NULL DEFAULT '',
  lineage_root_hash TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS repair_intents (
  intent_id       TEXT PRIMARY KEY,
  task_id         TEXT NOT NULL,
  target_revision INTEGER NOT NULL,
  route           TEXT NOT NULL,
  reason          TEXT NOT NULL,
  fingerprint     TEXT NOT NULL,
  task_json       TEXT NOT NULL,
  state           TEXT NOT NULL CHECK (state IN ('pending', 'committed')),
  repair_id       INTEGER NOT NULL UNIQUE,
  report_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at      TEXT NOT NULL,
  committed_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS repair_intents_task
  ON repair_intents(task_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS repair_intents_one_pending_task
  ON repair_intents(task_id) WHERE state = 'pending';

CREATE TABLE IF NOT EXISTS repair_intent_files (
  intent_id     TEXT NOT NULL,
  ordinal       INTEGER NOT NULL,
  rel_path      TEXT NOT NULL,
  before_exists INTEGER NOT NULL CHECK (before_exists IN (0, 1)),
  before_sha256 TEXT NOT NULL,
  after_exists  INTEGER NOT NULL CHECK (after_exists IN (0, 1)),
  after_sha256  TEXT NOT NULL,
  content       BLOB,
  PRIMARY KEY (intent_id, ordinal),
  UNIQUE (intent_id, rel_path)
);
"""

#: Additive repair-column migrations applied idempotently on open.
_REPAIRS_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("fingerprint", "ALTER TABLE repairs ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''"),
    (
        "lineage_root_hash",
        "ALTER TABLE repairs ADD COLUMN lineage_root_hash TEXT NOT NULL DEFAULT ''",
    ),
)


def _utc_now() -> str:
    """Wall-clock is allowed HERE (ledger state), never in task artifacts."""
    return datetime.now(timezone.utc).isoformat()


def _repair_lineage_root_hash(task: TaskIR) -> str:
    """Return the revision root, or current identity for a legacy task."""
    return task.revisions[0].content_hash if task.revisions else task.content_hash()


def _stage_value(stage: str | StageName) -> str:
    value = stage.value if isinstance(stage, StageName) else str(stage)
    if value not in _STAGE_VALUES:
        raise ValueError(f"unknown stage {value!r}; expected one of {sorted(_STAGE_VALUES)}")
    return value


#: StagePayload field carrying a structured infrastructure marker.
INFRASTRUCTURE_FIELD = "infrastructure"

#: StagePayload key for task- or total-level budget scope.
BUDGET_SCOPE_FIELD = "budget_scope"
INFRASTRUCTURE_BUDGET_SCOPES: frozenset[str] = frozenset({"task", "total"})
BUDGET_EXCEEDED_EXCEPTION_NAME = "BudgetExceededError"
ROLE_CAP_EXCEPTION_NAME = "RoleCapExceeded"

#: Exception class names that identify transport faults at message start.
_INFRA_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {
        "ProviderProtocolError",
        "TranscriptMissingError",
        "TranscriptRouteMismatchError",
        "BudgetExceededError",
        "MissingCredentialsError",
        "RepairCommitError",
        "TrialWorkspaceError",
        "TaskLockBusy",
        # Canonical harness faults, including sanitizer leaks. Policy faults
        # are model-caused and must never enter this set.
        "SessionFault",
        "ProviderFault",
        "ToolHarnessFault",
        "ToolDeadlineExceeded",
        "SandboxFault",
        "DiagnosticTripwire",
        "TaskDefectFault",
    }
)

#: Prefix-anchored transport messages raised as plain ``RuntimeError``.
_INFRA_MESSAGE_PREFIXES: tuple[str, ...] = (
    "provider http",
    "provider transport error",
    "no pricing entry",
    "transcriptstore has no record_dir",
)

#: Specific repository-authored transport text; avoid ambiguous common words.
_INFRA_TEXT_MARKERS: tuple[str, ...] = (
    "council not admitted",  # review.metrology.NOT_ADMITTED_MESSAGE
    "budget breached",
)

#: Kept as the public name of the whole marker vocabulary (diagnostics/tests).
_INFRA_FAILURE_MARKERS: tuple[str, ...] = (
    tuple(sorted(name.lower() for name in _INFRA_EXCEPTION_NAMES))
    + _INFRA_MESSAGE_PREFIXES
    + _INFRA_TEXT_MARKERS
)


#: Bound on the `__cause__` / `__context__` walk (a real chain is short; a
#: cycle is refused by the visited set anyway).
_MAX_EXCEPTION_CHAIN = 16


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Return a bounded, unique, depth-first exception chain, cause first."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack and len(chain) < _MAX_EXCEPTION_CHAIN:
        link = stack.pop()
        if not isinstance(link, BaseException) or id(link) in seen:
            continue
        seen.add(id(link))
        chain.append(link)
        # LIFO: the context is pushed first so the cause is visited first.
        for nested in (link.__context__, link.__cause__):
            if isinstance(nested, BaseException) and id(nested) not in seen:
                stack.append(nested)
    return chain


def _normalise_infrastructure_budget_scope(value: Any) -> str:
    """Return ``task`` or ``total`` budget scope; map all others to unknown."""

    scope = str(value or "").strip().lower()
    return scope if scope in INFRASTRUCTURE_BUDGET_SCOPES else ""


def _budget_scope_for(exc: BaseException) -> str:
    """Read an explicit infrastructure budget scope through an exception chain."""

    for link in _exception_chain(exc):
        if isinstance(link, InfrastructureFailure):
            scope = _normalise_infrastructure_budget_scope(
                getattr(link, "budget_scope", "")
            )
            if scope:
                return scope
        names = {cls.__name__ for cls in type(link).__mro__}
        if (
            ROLE_CAP_EXCEPTION_NAME in names
            or BUDGET_EXCEEDED_EXCEPTION_NAME not in names
        ):
            continue
        scope = _normalise_infrastructure_budget_scope(
            getattr(link, "scope", "")
        )
        if scope:
            return scope
    return ""


def _budget_scope_from_payload(payload: Any) -> str:
    """Return the typed budget scope carried by a stage payload, else ''."""

    if payload is None:
        return ""
    try:
        dumped = (
            payload.model_dump(mode="json")
            if hasattr(payload, "model_dump")
            else payload
        )
    except Exception:  # noqa: BLE001 - unreadable evidence has no typed scope
        return ""
    if not isinstance(dumped, dict):
        return ""
    return _normalise_infrastructure_budget_scope(
        dumped.get(BUDGET_SCOPE_FIELD)
    )


def _marker_for_one(exc: BaseException) -> str:
    """Return one exception's explicit, class-based, or prefix-based marker."""
    if isinstance(exc, InfrastructureFailure):
        return str(exc.marker)
    for cls in type(exc).__mro__:
        if cls.__name__ in _INFRA_EXCEPTION_NAMES:
            return cls.__name__
    text = str(exc).strip().lower()
    for prefix in _INFRA_MESSAGE_PREFIXES:
        if text.startswith(prefix):
            return prefix
    return ""


def _infra_marker_for(exc: BaseException) -> str:
    """Return a transport marker from an exception or its wrapped chain."""
    for link in _exception_chain(exc):
        marker = _marker_for_one(link)
        if marker:
            return marker
    return ""


def _claims_to_be_a_battery(payload_json: str) -> bool:
    """Return whether a payload claims the acceptance-report schema."""
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return isinstance(payload, dict) and isinstance(payload.get("gates"), list)


def _marker_text(marker: Any) -> str:
    """Normalise a marker to the lowercase form the ledger and tests use."""
    return str(marker or "").strip().lower()


def _infrastructure_failure(payload: Any) -> str | None:
    """Return a marker from structured data or anchored transport text only."""
    if payload is None:
        return None
    try:
        dumped = (
            payload.model_dump(mode="json")
            if hasattr(payload, "model_dump")
            else payload
        )
    except Exception:  # noqa: BLE001 — an unreadable payload is not infra
        return None
    if not isinstance(dumped, dict):
        return None

    structured = dumped.get(INFRASTRUCTURE_FIELD)
    if isinstance(structured, str) and structured.strip():
        return structured.strip().lower()

    fields = [dumped.get("error"), dumped.get("detail")]
    for value in fields:
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        head, _, rest = text.partition(":")
        head = head.strip()
        if head in _INFRA_EXCEPTION_NAMES:
            return head.lower()
        # Runners record `f"{type(exc).__name__}: {exc}"`, so a plain
        # RuntimeError's transport message sits BEHIND the class name.
        for candidate in (text, rest.strip()):
            lowered = candidate.lower()
            for marker in _INFRA_MESSAGE_PREFIXES + _INFRA_TEXT_MARKERS:
                if lowered.startswith(marker):
                    return marker
    return None


def _intake_runner(engine: "Engine", task: TaskIR) -> StageOutcome:
    """Built-in intake: persist the canonical task_ir.json for this identity."""
    engine.save_task(task)
    return StageOutcome(
        verdict=VERDICT_PASS,
        payload=StagePayload(
            detail="task_ir.json written",
            data={"content_hash": task.content_hash(), "origin": task.origin.value},
        ),
    )


class Engine:
    """Append-only ledger + resumable orchestration for one workspace."""

    def __init__(
        self,
        workspace: Path,
        *,
        max_repair_rounds: int = 3,
        stage_runners: Mapping[str | StageName, StageRunner] | None = None,
        repair_proposer: "RepairProposerLike | None" = None,
    ) -> None:
        if max_repair_rounds < 0:
            raise ValueError("max_repair_rounds must be >= 0")
        self.workspace = Path(workspace).resolve()
        self.max_repair_rounds = int(max_repair_rounds)
        self._state_dir = self.workspace / "state"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        (self.workspace / "tasks").mkdir(parents=True, exist_ok=True)
        self._db_path = self._state_dir / DB_FILENAME
        self._connection: sqlite3.Connection | None = None
        self._connection_pid: int | None = None
        # Re-entrancy is local to this engine, process, and thread.
        self._task_lock_local = threading.local()
        # Corpus selection and release publication have one workspace-wide
        # namespace, so coordinators also need one PID/thread-aware lock.
        self._coordinator_lock_local = threading.local()
        # Scoped capability for journaling proposer-certified patch bytes.
        self._repair_commit_local = threading.local()
        self._closed = False
        self._open_process_connection()
        self._stage_runners: dict[str, StageRunner] = {StageName.INTAKE.value: _intake_runner}
        if stage_runners:
            for key, runner in stage_runners.items():
                self.set_stage_runner(key, runner)
        self._repair_proposer = repair_proposer

    # -- lifecycle ---------------------------------------------------------

    def _new_connection(self) -> sqlite3.Connection:
        """Open and configure one connection owned by the current process."""
        con = sqlite3.connect(
            str(self._db_path), timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0
        )
        try:
            # Set the wait first: two workers may both be opening a new workspace
            # and negotiating WAL/schema initialization at the same time.
            con.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            mode = str(con.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if mode != "wal":
                raise EngineError(
                    f"ledger {self._db_path} refused WAL mode (reported {mode!r})"
                )
            con.execute("PRAGMA synchronous=NORMAL")
            return con
        except Exception:
            con.close()
            raise

    def _open_process_connection(self) -> None:
        """Bind a fresh SQLite handle to the current process after forks."""
        con = self._new_connection()
        try:
            con.executescript(_SCHEMA)
            self._migrate(con)
            con.commit()
        except Exception:
            con.close()
            raise
        self._connection = con
        self._connection_pid = os.getpid()

    @property
    def _con(self) -> sqlite3.Connection:
        """The current process's connection (kept as a property for callers)."""
        if self._closed:
            raise EngineError("engine is closed")
        pid = os.getpid()
        if self._connection is None or self._connection_pid != pid:
            inherited = self._connection
            self._connection = None
            self._connection_pid = None
            if inherited is not None:
                # This closes only the child's duplicate after fork; the parent
                # owns a distinct descriptor and keeps its live connection.
                try:
                    inherited.close()
                except sqlite3.Error:
                    pass
            self._open_process_connection()
        assert self._connection is not None
        return self._connection

    def _migrate(self, con: sqlite3.Connection | None = None) -> None:
        """Add post-ship columns to an existing ledger. Idempotent, additive,
        never destructive: an older workspace opens without losing a row."""
        connection = con if con is not None else self._con
        present = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(repairs)").fetchall()
        }
        for column, ddl in _REPAIRS_MIGRATIONS:
            if column not in present:
                try:
                    connection.execute(ddl)
                except sqlite3.OperationalError:
                    # Ignore only a concurrent migration that added this column.
                    now = {
                        str(row[1])
                        for row in connection.execute(
                            "PRAGMA table_info(repairs)"
                        ).fetchall()
                    }
                    if column not in now:
                        raise

        # Bind legacy repair rows from durable target TaskIRs; leave malformed
        # rows unscoped for conservative reads.
        legacy_rows = connection.execute(
            "SELECT r.id, r.task_id, r.revision, i.task_json"
            " FROM repairs AS r JOIN repair_intents AS i ON i.repair_id=r.id"
            " WHERE r.lineage_root_hash=''"
        ).fetchall()
        for repair_id, task_id, revision, task_json in legacy_rows:
            try:
                target = task_from_json(str(task_json))
            except (TypeError, ValueError):
                continue
            if (
                target.task_id != str(task_id)
                or target.current_revision != int(revision)
                or not target.revisions
            ):
                continue
            connection.execute(
                "UPDATE repairs SET lineage_root_hash=?"
                " WHERE id=? AND lineage_root_hash=''",
                (_repair_lineage_root_hash(target), int(repair_id)),
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        con = self._connection
        self._connection = None
        self._connection_pid = None
        if con is not None:
            con.close()

    @staticmethod
    def _acquire_task_file_lock(lock_file: Any, *, blocking: bool = True) -> None:
        """Acquire one crash-released advisory lock on POSIX or Windows."""
        if _fcntl is not None:
            operation = _fcntl.LOCK_EX
            if not blocking:
                operation |= _fcntl.LOCK_NB
            try:
                _fcntl.flock(lock_file.fileno(), operation)
            except OSError as exc:
                if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise TaskLockBusy("task lock is held by another process") from exc
                raise
            return
        if _msvcrt is None:  # pragma: no cover - every supported host has one
            raise EngineError(
                "cross-process task locking is unavailable on this platform"
            )

        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        while True:
            lock_file.seek(0)
            try:
                _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                # Retry only Windows lock-contention errors.
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if not blocking:
                    raise TaskLockBusy("task lock is held by another process") from exc
                time.sleep(_TASK_LOCK_RETRY_SECONDS)

    @staticmethod
    def _release_task_file_lock(lock_file: Any) -> None:
        """Release a lock acquired by :meth:`_acquire_task_file_lock`."""
        if _fcntl is not None:
            _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
            return
        if _msvcrt is None:  # pragma: no cover - paired acquire already refused
            return
        lock_file.seek(0)
        _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_UNLCK, 1)

    @contextmanager
    def _task_lock(self, task_id: str, *, blocking: bool = True) -> Iterator[None]:
        """Hold one crash-released cross-process task lock."""
        try:
            task_id = validate_task_id_segment(task_id)
        except (TypeError, ValueError) as exc:
            raise EngineError(f"unsafe task_id {task_id!r}: {exc}") from exc

        # Reset inherited thread-local ownership after a fork.
        pid = os.getpid()
        if getattr(self._task_lock_local, "pid", None) != pid:
            self._task_lock_local.pid = pid
            self._task_lock_local.depths = {}
        depths: dict[str, int] = self._task_lock_local.depths
        if depths.get(task_id, 0):
            depths[task_id] += 1
            try:
                yield
            finally:
                depths[task_id] -= 1
            return

        locks_dir = self._state_dir / TASK_LOCKS_DIRNAME
        locks_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
        lock_path = locks_dir / f"{digest}.lock"
        with lock_path.open("a+b") as lock_file:
            try:
                self._acquire_task_file_lock(lock_file, blocking=blocking)
            except TaskLockBusy as exc:
                raise TaskLockBusy(f"task {task_id!r}: {exc}") from exc
            depths[task_id] = 1
            try:
                # Rebind after acquiring the lock too: a forked child must not
                # touch the inherited handle while inspecting task state.
                _ = self._con
                yield
            finally:
                depths.pop(task_id, None)
                self._release_task_file_lock(lock_file)

    @contextmanager
    def task_locks(self, task_ids: Iterable[str]) -> Iterator[tuple[str, ...]]:
        """Validate, deduplicate, and hold task locks in lexical order."""
        if isinstance(task_ids, (str, bytes)):
            raise TypeError("task_locks expects an iterable of task ids, not a string")
        unique: set[str] = set()
        for raw_task_id in task_ids:
            try:
                unique.add(validate_task_id_segment(raw_task_id))
            except (TypeError, ValueError) as exc:
                raise EngineError(f"unsafe task_id {raw_task_id!r}: {exc}") from exc
        if not unique:
            raise ValueError("task_locks requires at least one task id")
        ordered = tuple(sorted(unique))
        pid = os.getpid()
        if getattr(self._task_lock_local, "pid", None) != pid:
            self._task_lock_local.pid = pid
            self._task_lock_local.depths = {}
        # Nested lock-set expansion fails instead of waiting and risking deadlock.
        nested = bool(self._task_lock_local.depths)
        with ExitStack() as stack:
            for task_id in ordered:
                stack.enter_context(self._task_lock(task_id, blocking=not nested))
            yield ordered

    def task_lock_held(self, task_id: str) -> bool:
        """Whether this Engine owns ``task_id`` in the current process/thread."""
        try:
            task_id = validate_task_id_segment(task_id)
        except (TypeError, ValueError):
            return False
        if getattr(self._task_lock_local, "pid", None) != os.getpid():
            return False
        return bool(getattr(self._task_lock_local, "depths", {}).get(task_id, 0))

    @contextmanager
    def coordinator_lock(self) -> Iterator[None]:
        """Hold the reentrant, crash-released workspace publication lock."""
        pid = os.getpid()
        if getattr(self._coordinator_lock_local, "pid", None) != pid:
            self._coordinator_lock_local.pid = pid
            self._coordinator_lock_local.depth = 0
        if self._coordinator_lock_local.depth:
            self._coordinator_lock_local.depth += 1
            try:
                yield
            finally:
                self._coordinator_lock_local.depth -= 1
            return

        lock_path = self._state_dir / COORDINATOR_LOCK_FILENAME
        with lock_path.open("a+b") as lock_file:
            task_lock_held = (
                getattr(self._task_lock_local, "pid", None) == pid
                and bool(getattr(self._task_lock_local, "depths", {}))
            )
            try:
                # Fail fast when acquiring publication after a task lock.
                self._acquire_task_file_lock(
                    lock_file, blocking=not task_lock_held
                )
            except TaskLockBusy as exc:
                raise TaskLockBusy(f"coordinator publication: {exc}") from exc
            self._coordinator_lock_local.depth = 1
            try:
                # As with task locks, a forked child must rebind SQLite before
                # inspecting state protected by the newly acquired lock.
                _ = self._con
                yield
            finally:
                self._coordinator_lock_local.depth = 0
                self._release_task_file_lock(lock_file)

    # -- paths -------------------------------------------------------------

    def task_dir(self, task_id: str) -> Path:
        # Validate raw IDs before joining them beneath the workspace.
        try:
            safe_task_id = validate_task_id_segment(task_id)
        except (TypeError, ValueError) as exc:
            raise EngineError(f"unsafe task_id {task_id!r}: {exc}") from exc
        tasks_root = self.workspace / "tasks"
        task_path = tasks_root / safe_task_id
        for label, path in (
            ("tasks root", tasks_root),
            ("task directory", task_path),
        ):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise EngineError(f"cannot inspect {label} {path}: {exc}") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise EngineError(f"{label} must not be a symbolic link: {path}")
            if not stat.S_ISDIR(metadata.st_mode):
                raise EngineError(f"{label} must be a directory: {path}")
        return task_path

    def _task_ir_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "task_ir.json"

    # -- runner wiring -----------------------------------------------------

    def set_stage_runner(self, stage: str | StageName, runner: StageRunner) -> None:
        self._stage_runners[_stage_value(stage)] = runner

    @property
    def stage_runners(self) -> Mapping[str, StageRunner]:
        """Return runners used for both live execution and trial certification."""
        return dict(self._stage_runners)

    def set_repair_proposer(self, proposer: "RepairProposerLike | None") -> None:
        """Wire (or unwire) the bounded repair proposer. Optional by design:
        with none wired, failures take the deterministic repair path alone."""
        self._repair_proposer = proposer

    @contextmanager
    def _journal_certified_patch(
        self, task_id: str, stage: str, route: RepairRoute, reason: str
    ) -> Iterator[None]:
        """Grant process-local authority to journal one certified patch."""
        context = (task_id, _stage_value(stage), RepairRoute(route).value, reason)
        pid = os.getpid()
        if getattr(self._repair_commit_local, "pid", None) != pid:
            self._repair_commit_local.pid = pid
            self._repair_commit_local.context = None
        if self._repair_commit_local.context is not None:
            raise EngineError("nested certified-patch commit contexts are forbidden")
        self._repair_commit_local.context = context
        try:
            yield
        finally:
            self._repair_commit_local.context = None

    def _certified_patch_reason(
        self, task_id: str, stage: str, route: RepairRoute
    ) -> str | None:
        """Repair reason inside the matching production commit scope."""
        expected = (task_id, _stage_value(stage), RepairRoute(route).value)
        context = (
            getattr(self._repair_commit_local, "context", None)
            if getattr(self._repair_commit_local, "pid", None) == os.getpid()
            else None
        )
        if context is None or context[:3] != expected:
            return None
        return context[3]

    # -- task persistence --------------------------------------------------

    def _bind_legacy_repairs_to_lineage(self, task: TaskIR) -> None:
        """Bind legacy unscoped repair rows before task identity changes."""
        self._con.execute(
            "UPDATE repairs SET lineage_root_hash=?"
            " WHERE task_id=? AND lineage_root_hash=''",
            (_repair_lineage_root_hash(task), task.task_id),
        )
        self._con.commit()

    def register(
        self,
        task: TaskIR,
        *,
        allow_overwrite: bool = False,
    ) -> None:
        """Persist intake idempotently; require explicit overwrite after draft."""
        with self._task_lock(task.task_id):
            self.recover_pending_repair(task.task_id)
            if not allow_overwrite:
                self._assert_reingest_is_intentional(task)

            path = self._task_ir_path(task.task_id)
            stored: TaskIR | None = None
            if path.is_file():
                try:
                    stored = task_from_json(path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001 — handled like legacy unreadable state
                    stored = None

            if stored is not None:
                # Bind legacy rows while the old identity remains authoritative.
                self._bind_legacy_repairs_to_lineage(stored)

            if stored is not None and stored.content_hash() == task.content_hash():
                # Preserve persisted volatile status and revisions on idempotent intake.
                task = stored
                if not task.revisions:
                    # Upgrade a pre-lineage workspace without replacing any of
                    # its volatile state with the freshly reconstructed input.
                    task = task.with_revision(
                        route=None, reason="initial intake revision"
                    )
                    self.save_task(task)
                existing = self.latest_report(task.task_id, StageName.INTAKE.value)
                if (
                    existing is not None
                    and existing.verdict == VERDICT_PASS
                    and existing.content_hash == task.content_hash()
                ):
                    return

            if not task.revisions:
                # Anchor lineage so the first repair becomes revision 2.
                task = task.with_revision(route=None, reason="initial intake revision")
            self.save_task(task)
            self.record_report(
                task,
                StageName.INTAKE.value,
                VERDICT_PASS,
                StagePayload(
                    detail="task registered",
                    data={"origin": task.origin.value, "family_id": task.family_id},
                ),
            )

    def _report_by_id(self, report_id: int) -> ReportRow | None:
        row = self._con.execute(
            "SELECT id, task_id, revision, stage, verdict, payload_json,"
            " content_hash, created_at FROM reports WHERE id=?",
            (int(report_id),),
        ).fetchone()
        return ReportRow(*row) if row is not None else None

    def report_by_id(self, report_id: int) -> ReportRow | None:
        """Public, read-only lookup used by exact offline recovery tooling."""

        return self._report_by_id(report_id)

    @staticmethod
    def _fatal_recovery_marker(row: ReportRow) -> dict[str, Any] | None:
        if row.verdict not in {VERDICT_FAIL, VERDICT_BLOCKED}:
            return None
        try:
            payload = json.loads(row.payload_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or FATAL_RECOVERY_KEY not in data:
            return None
        encoded = data[FATAL_RECOVERY_KEY]
        if not isinstance(encoded, str):
            raise EngineError("fatal recovery marker is not encoded text")
        try:
            marker = json.loads(encoded)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise EngineError("fatal recovery marker is malformed") from exc
        if not isinstance(marker, dict):
            raise EngineError("fatal recovery marker is not an object")
        if marker.get("schema_version") != FATAL_RECOVERY_SCHEMA:
            raise EngineError("fatal recovery marker has an unknown schema")
        expected_keys = {
            "schema_version",
            "recovery_id",
            "recovery_kind",
            "stage",
            "content_hash",
            "evidence_path",
            "evidence_sha256",
            "cause_report_id",
            "cause_payload_sha256",
            "target_report_id",
            "target_payload_sha256",
            "disposition",
            "failure_class",
            "failure_code",
            "blocked_on",
            "retry_guard",
            "recovery_prerequisite",
        }
        if set(marker) != expected_keys:
            raise EngineError("fatal recovery marker fields are not exact")
        return marker

    @staticmethod
    def _fatal_recovery_from_marker(
        marker: Mapping[str, Any],
    ) -> FatalReportRecovery:
        try:
            report_ids = {
                key: marker[key] for key in ("cause_report_id", "target_report_id")
            }
            if any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in report_ids.values()
            ):
                raise TypeError("cause/target report ids must be integers")
            return FatalReportRecovery(
                recovery_id=str(marker["recovery_id"]),
                recovery_kind=str(marker["recovery_kind"]),
                stage=str(marker["stage"]),
                content_hash=str(marker["content_hash"]),
                evidence_path=str(marker["evidence_path"]),
                evidence_sha256=str(marker["evidence_sha256"]),
                cause_report_id=report_ids["cause_report_id"],
                cause_payload_sha256=str(marker["cause_payload_sha256"]),
                target_report_id=report_ids["target_report_id"],
                target_payload_sha256=str(marker["target_payload_sha256"]),
                disposition=str(marker["disposition"]),
                failure_class=str(marker["failure_class"]),
                failure_code=str(marker["failure_code"]),
                blocked_on=str(marker["blocked_on"]),
                retry_guard=str(marker["retry_guard"]),
                recovery_prerequisite=str(marker["recovery_prerequisite"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EngineError("fatal recovery marker values are invalid") from exc

    def _matching_fatal_recovery(
        self,
        task: TaskIR,
        recovery: FatalReportRecovery,
    ) -> ReportRow | None:
        for row in self.report_history(task.task_id, recovery.stage):
            marker = self._fatal_recovery_marker(row)
            if marker is None:
                continue
            parsed = self._fatal_recovery_from_marker(marker)
            if marker == recovery.as_dict():
                if row.content_hash != task.content_hash():
                    raise EngineError(
                        "fatal recovery marker is bound to another identity"
                    )
                return row
            if parsed.recovery_id == recovery.recovery_id:
                raise EngineError(
                    "fatal recovery id is bound to different evidence"
                )
            if parsed.target_report_id == recovery.target_report_id:
                raise EngineError(
                    "fatal report already has a different recovery disposition"
                )
        return None

    def _validate_fatal_recovery(
        self,
        task: TaskIR,
        recovery: FatalReportRecovery,
    ) -> FatalRecoveryEvidence:
        for label, value in (
            ("recovery_id", recovery.recovery_id),
            ("content_hash", recovery.content_hash),
            ("evidence_sha256", recovery.evidence_sha256),
            ("cause_payload_sha256", recovery.cause_payload_sha256),
            ("target_payload_sha256", recovery.target_payload_sha256),
        ):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise EngineError(f"fatal recovery {label} must be lowercase SHA-256")
        stage = _stage_value(recovery.stage)
        if task.content_hash() != recovery.content_hash:
            raise EngineError("fatal recovery task identity does not match")
        if recovery.recovery_id != recovery.evidence_sha256:
            raise EngineError("fatal recovery id must equal its content digest")
        if recovery.evidence_path != (
            f"{FATAL_RECOVERY_EVIDENCE_DIR}/{recovery.evidence_sha256}.json"
        ):
            raise EngineError("fatal recovery evidence path is not content-addressed")
        evidence_bytes = self._read_supersession_evidence(
            recovery.evidence_path,
            label="fatal recovery evidence",
        )
        if hashlib.sha256(evidence_bytes).hexdigest() != recovery.evidence_sha256:
            raise EngineError("fatal recovery evidence changed")
        try:
            evidence = FatalRecoveryEvidence.model_validate_json(evidence_bytes)
        except ValueError as exc:
            raise EngineError(f"fatal recovery evidence is invalid: {exc}") from exc
        if evidence_bytes != evidence.deterministic_bytes():
            raise EngineError("fatal recovery evidence bytes are not canonical")
        expected = {
            "task_id": task.task_id,
            "task_content_hash": recovery.content_hash,
            "recovery_kind": recovery.recovery_kind,
            "stage": stage,
            "cause_report_id": recovery.cause_report_id,
            "cause_payload_sha256": recovery.cause_payload_sha256,
            "target_report_id": recovery.target_report_id,
            "target_payload_sha256": recovery.target_payload_sha256,
            "disposition": recovery.disposition,
            "failure_class": recovery.failure_class,
            "failure_code": recovery.failure_code,
            "blocked_on": recovery.blocked_on,
            "retry_guard": recovery.retry_guard,
            "recovery_prerequisite": recovery.recovery_prerequisite,
        }
        dumped = evidence.model_dump(mode="json")
        mismatched = [
            key for key, value in expected.items() if dumped.get(key) != value
        ]
        if mismatched:
            raise EngineError(
                "fatal recovery evidence does not match its marker: "
                + ", ".join(mismatched)
            )
        cause = self._report_by_id(recovery.cause_report_id)
        target = self._report_by_id(recovery.target_report_id)
        if cause is None or target is None:
            raise EngineError("fatal recovery cause or target report does not exist")
        expected_common = (task.task_id, stage, task.content_hash())
        if (cause.task_id, cause.stage, cause.content_hash) != expected_common:
            raise EngineError(
                "fatal recovery cause is not bound to the target stage identity"
            )
        if cause.id == target.id:
            if cause.verdict != VERDICT_FATAL:
                raise EngineError("direct fatal recovery cause is not fatal")
        elif cause.verdict != VERDICT_FAIL or cause.id > target.id:
            raise EngineError(
                "fatal recovery cause is not an exact preceding stage failure"
            )
        cause_payload_sha256 = hashlib.sha256(
            cause.payload_json.encode("utf-8")
        ).hexdigest()
        if cause_payload_sha256 != recovery.cause_payload_sha256:
            raise EngineError("fatal recovery cause payload changed")
        if (
            (target.task_id, target.stage, target.content_hash) != expected_common
            or target.verdict != VERDICT_FATAL
        ):
            raise EngineError(
                "fatal recovery target is not an exact current-identity fatal"
            )
        observed_payload = hashlib.sha256(target.payload_json.encode("utf-8")).hexdigest()
        if observed_payload != recovery.target_payload_sha256:
            raise EngineError("fatal recovery target payload changed")
        if self.report_is_superseded(task, target):
            raise EngineError("fatal recovery target was already superseded")

        for expected_identity in evidence.fixed_code:
            observed_identity = recovery_code_identity(expected_identity.module)
            if observed_identity != expected_identity:
                raise EngineError(
                    "fatal recovery fixed code identity changed for "
                    f"{expected_identity.module!r}"
                )
        observed_revalidator = recovery_code_identity(evidence.revalidator.module)
        if observed_revalidator.sha256 != evidence.revalidator.module_sha256:
            raise EngineError("fatal recovery revalidator identity changed")
        module = importlib.import_module(evidence.revalidator.module)
        validator: Any = module
        for component in evidence.revalidator.qualname.split("."):
            if not component or component.startswith("_"):
                raise EngineError("fatal recovery revalidator name is unsafe")
            validator = getattr(validator, component, None)
        if not callable(validator):
            raise EngineError("fatal recovery revalidator is not callable")

        observation_bytes: dict[str, bytes] = {}
        for observation in evidence.observations:
            payload = self._read_supersession_evidence(
                observation.path,
                label="fatal recovery observation",
            )
            if hashlib.sha256(payload).hexdigest() != observation.sha256:
                raise EngineError(
                    f"fatal recovery observation changed: {observation.path}"
                )
            observation_bytes[observation.path] = payload
        try:
            result = validator(
                evidence=evidence,
                task=task,
                cause=cause,
                target=target,
                observations=observation_bytes,
            )
        except (TypeError, ValueError) as exc:
            raise EngineError(f"fatal recovery revalidation failed: {exc}") from exc
        if result is not None:
            raise EngineError("fatal recovery revalidator must return None")
        return evidence

    @staticmethod
    def _supersession_marker(row: ReportRow) -> dict[str, Any] | None:
        if row.verdict != VERDICT_PASS:
            return None
        try:
            payload = json.loads(row.payload_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or EVIDENCE_SUPERSESSION_KEY not in data:
            return None
        encoded = data[EVIDENCE_SUPERSESSION_KEY]
        if not isinstance(encoded, str):
            raise EngineError("report supersession marker is not encoded text")
        try:
            marker = json.loads(encoded)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise EngineError("report supersession marker is malformed") from exc
        if not isinstance(marker, dict):
            raise EngineError("report supersession marker is not an object")
        if marker.get("schema_version") != EVIDENCE_SUPERSESSION_SCHEMA:
            raise EngineError("report supersession marker has an unknown schema")
        expected_keys = {
            "schema_version",
            "migration_id",
            "reason",
            "stage",
            "content_hash",
            "revalidation_path",
            "revalidation_sha256",
            "cause_report_id",
            "cause_payload_sha256",
            "target_report_id",
            "target_payload_sha256",
            "replacement_detail",
            "replacement_data",
        }
        if set(marker) != expected_keys:
            raise EngineError("report supersession marker fields are not exact")
        return marker

    @staticmethod
    def _supersession_from_marker(marker: Mapping[str, Any]) -> ReportSupersession:
        try:
            if any(
                isinstance(marker[key], bool) or not isinstance(marker[key], int)
                for key in ("cause_report_id", "target_report_id")
            ):
                raise TypeError("report ids must be integers")
            return ReportSupersession(
                migration_id=str(marker["migration_id"]),
                reason=str(marker["reason"]),
                stage=str(marker["stage"]),
                content_hash=str(marker["content_hash"]),
                revalidation_path=str(marker["revalidation_path"]),
                revalidation_sha256=str(marker["revalidation_sha256"]),
                cause_report_id=int(marker["cause_report_id"]),
                cause_payload_sha256=str(marker["cause_payload_sha256"]),
                target_report_id=int(marker["target_report_id"]),
                target_payload_sha256=str(marker["target_payload_sha256"]),
                replacement_detail=str(marker["replacement_detail"]),
                replacement_data=tuple(
                    sorted(
                        (str(key), str(value))
                        for key, value in marker["replacement_data"].items()
                    )
                ),
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise EngineError("report supersession marker values are invalid") from exc

    def _matching_supersession(
        self, task: TaskIR, supersession: ReportSupersession
    ) -> ReportRow | None:
        for row in self.report_history(task.task_id, supersession.stage):
            marker = self._supersession_marker(row)
            if marker is None:
                continue
            if marker == supersession.as_dict():
                if row.content_hash != task.content_hash():
                    raise EngineError(
                        "report supersession marker is bound to another identity"
                    )
                return row
            elif marker.get("migration_id") == supersession.migration_id:
                raise EngineError(
                    "report supersession migration id is bound to different evidence"
                )
        return None

    def _validate_report_supersession(
        self, task: TaskIR, supersession: ReportSupersession
    ) -> None:
        for label, value in (
            ("migration_id", supersession.migration_id),
            ("content_hash", supersession.content_hash),
            ("revalidation_sha256", supersession.revalidation_sha256),
            ("cause_payload_sha256", supersession.cause_payload_sha256),
            ("target_payload_sha256", supersession.target_payload_sha256),
        ):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise EngineError(f"report supersession {label} must be lowercase SHA-256")
        if not supersession.reason.strip():
            raise EngineError("report supersession reason is required")
        if not supersession.replacement_detail.strip():
            raise EngineError("report supersession replacement detail is required")
        stage = _stage_value(supersession.stage)
        if task.content_hash() != supersession.content_hash:
            raise EngineError("report supersession task identity does not match")
        revalidation_bytes = self._read_supersession_evidence(
            supersession.revalidation_path,
            label="report supersession revalidation evidence",
        )
        if hashlib.sha256(revalidation_bytes).hexdigest() != supersession.revalidation_sha256:
            raise EngineError("report supersession revalidation evidence changed")
        try:
            evidence = ReportRevalidationEvidence.model_validate_json(
                revalidation_bytes
            )
        except ValueError as exc:
            raise EngineError(
                f"report supersession revalidation evidence is invalid: {exc}"
            ) from exc
        if revalidation_bytes != evidence.deterministic_bytes():
            raise EngineError("report supersession revalidation bytes are not canonical")
        expected_evidence = {
            "migration_id": supersession.migration_id,
            "task_id": task.task_id,
            "task_content_hash": supersession.content_hash,
            "stage": stage,
            "cause_report_id": supersession.cause_report_id,
            "cause_payload_sha256": supersession.cause_payload_sha256,
            "target_report_id": supersession.target_report_id,
            "target_payload_sha256": supersession.target_payload_sha256,
            "replacement_detail": supersession.replacement_detail,
            "replacement_data": dict(supersession.replacement_data),
        }
        observed_evidence = evidence.model_dump(mode="json")
        mismatched = [
            field
            for field, expected in expected_evidence.items()
            if observed_evidence[field] != expected
        ]
        if mismatched:
            raise EngineError(
                "report supersession revalidation does not match its marker: "
                + ", ".join(mismatched)
            )
        authorization_bytes = self._read_supersession_evidence(
            evidence.authorization_path,
            label="report supersession authorization",
        )
        if hashlib.sha256(authorization_bytes).hexdigest() != evidence.authorization_sha256:
            raise EngineError("report supersession authorization changed")
        commit_bytes = self._read_supersession_evidence(
            evidence.equivalence_commit_path,
            label="report supersession equivalence commit",
        )
        if hashlib.sha256(commit_bytes).hexdigest() != evidence.equivalence_commit_sha256:
            raise EngineError("report supersession equivalence commit changed")
        try:
            authorization = json.loads(authorization_bytes)
            commit = json.loads(commit_bytes)
        except (UnicodeError, ValueError) as exc:
            raise EngineError("report supersession run evidence is malformed") from exc
        if not isinstance(authorization, dict) or not isinstance(commit, dict):
            raise EngineError("report supersession run evidence is not an object")
        if authorization_bytes != (readable_json(authorization) + "\n").encode("utf-8"):
            raise EngineError("report supersession authorization is not canonical")
        if commit_bytes != (readable_json(commit) + "\n").encode("utf-8"):
            raise EngineError("report supersession equivalence commit is not canonical")
        if authorization.get("schema_version") != "generator-equivalence-authorization-v1":
            raise EngineError("report supersession authorization schema is invalid")
        if commit.get("schema_version") != "generator-equivalence-commit-v1":
            raise EngineError("report supersession equivalence commit schema is invalid")
        if set(authorization) != {
            "schema_version",
            "run_id",
            "config_sha256",
            "prior_readiness_sha256",
            "authoritative_selected_sha256",
            "reproduced_selected_sha256",
            "authoritative_pool_sha256",
            "reproduced_pool_sha256",
            "authoritative_generator_sha256",
            "reproduced_generator_sha256",
            "ordered_task_ids",
            "tasks",
            "budget_snapshot_sha256",
            "budget_snapshot",
            "report_revalidation_request_path",
            "report_revalidation_request_sha256",
            "report_revalidation_request",
        }:
            raise EngineError("report supersession authorization fields are not exact")
        if set(commit) != {
            "schema_version",
            "run_id",
            "config_sha256",
            "authoritative_selected_sha256",
            "reproduced_selected_sha256",
            "authoritative_generator_sha256",
            "reproduced_generator_sha256",
            "authorization_path",
            "authorization_sha256",
            "ordered_task_ids",
            "tasks",
        }:
            raise EngineError("report supersession equivalence commit fields are not exact")
        if hashlib.sha256(
            canonical_json(authorization.get("budget_snapshot")).encode("utf-8")
        ).hexdigest() != authorization.get("budget_snapshot_sha256"):
            raise EngineError("report supersession authorization budget binding differs")
        cross_fields = {
            "run_id": evidence.run_id,
            "config_sha256": evidence.config_sha256,
            "authoritative_selected_sha256": evidence.authoritative_selected_sha256,
            "reproduced_selected_sha256": evidence.reproduced_selected_sha256,
            "authoritative_generator_sha256": evidence.authoritative_generator_sha256,
            "reproduced_generator_sha256": evidence.reproduced_generator_sha256,
        }
        if any(authorization.get(key) != value for key, value in cross_fields.items()):
            raise EngineError("report supersession authorization identity differs")
        if any(commit.get(key) != value for key, value in cross_fields.items()):
            raise EngineError("report supersession equivalence commit identity differs")
        if (
            commit.get("authorization_sha256") != evidence.authorization_sha256
            or commit.get("authorization_path") != evidence.authorization_path
        ):
            raise EngineError("report supersession commit names another authorization")
        request = authorization.get("report_revalidation_request")
        if not isinstance(request, dict):
            raise EngineError("report supersession authorization has no typed request")
        if set(request) != {
            "schema_version",
            "run_id",
            "config_sha256",
            "authoritative_selected_sha256",
            "reproduced_selected_sha256",
            "authoritative_generator_sha256",
            "reproduced_generator_sha256",
            "reason",
            "task_id",
            "task_content_hash",
            "stage",
            "cause_report_id",
            "cause_payload_sha256",
            "target_report_id",
            "target_payload_sha256",
            "prose_sha256",
            "author_session_sha256",
            "transcript_paths",
            "transcript_sha256s",
            "session_index_path",
            "session_index_sha256",
            "trajectory_path",
            "trajectory_sha256",
            "session_summary_path",
            "session_summary_sha256",
            "expected_old_error",
            "expected_old_error_sha256",
            "expected_new_findings",
        } or request.get("schema_version") != "generator-report-revalidation-request-v2":
            raise EngineError("report supersession request fields are not exact")
        if (
            authorization.get("report_revalidation_request_path")
            != evidence.original_evidence["request_path"]
            or authorization.get("report_revalidation_request_sha256")
            != evidence.original_evidence["request_sha256"]
            or request.get("task_id") != task.task_id
            or request.get("task_content_hash") != evidence.task_content_hash
            or request.get("cause_report_id") != evidence.cause_report_id
            or request.get("cause_payload_sha256") != evidence.cause_payload_sha256
            or request.get("target_report_id") != evidence.target_report_id
            or request.get("target_payload_sha256") != evidence.target_payload_sha256
            or request.get("reason") != supersession.reason
            or request.get("expected_old_error")
            != evidence.original_evidence["old_error"]
            or request.get("expected_old_error_sha256")
            != evidence.original_evidence["old_error_sha256"]
        ):
            raise EngineError("report supersession request differs from exact target")
        request_bytes = self._read_supersession_evidence(
            evidence.original_evidence["request_path"],
            label="report supersession request",
        )
        if hashlib.sha256(request_bytes).hexdigest() != evidence.original_evidence["request_sha256"]:
            raise EngineError("report supersession request changed")
        if request_bytes != (readable_json(request) + "\n").encode("utf-8"):
            raise EngineError("report supersession request bytes differ from authorization")
        authorization_tasks = authorization.get("tasks")
        committed_tasks = commit.get("tasks")
        if not isinstance(authorization_tasks, list) or not isinstance(committed_tasks, list):
            raise EngineError("report supersession roster evidence is invalid")
        ordered_ids = authorization.get("ordered_task_ids")
        if (
            not isinstance(ordered_ids, list)
            or ordered_ids != commit.get("ordered_task_ids")
            or len(ordered_ids) != len(authorization_tasks)
            or len(ordered_ids) != len(committed_tasks)
            or len(set(ordered_ids)) != len(ordered_ids)
        ):
            raise EngineError("report supersession committed roster is not exact")
        for task_id, auth_row, commit_row in zip(
            ordered_ids, authorization_tasks, committed_tasks, strict=True
        ):
            if (
                not isinstance(auth_row, dict)
                or not isinstance(commit_row, dict)
                or set(auth_row)
                != {
                    "task_id",
                    "source_entry_sha256",
                    "intake_content_hash",
                    "authoritative_provenance_sha256",
                    "reproduced_provenance_sha256",
                }
                or set(commit_row)
                != {
                    "task_id",
                    "intake_content_hash",
                    "authoritative_provenance_sha256",
                    "reproduced_provenance_sha256",
                    "equivalence_path",
                    "equivalence_sha256",
                    "resumed_existing",
                    "adapter_taskir_rederived",
                    "source_provenance_equivalence_attested",
                    "report_locally_revalidated_and_superseded",
                }
                or auth_row.get("task_id") != task_id
                or commit_row.get("task_id") != task_id
                or commit_row.get("intake_content_hash")
                != auth_row.get("intake_content_hash")
                or commit_row.get("authoritative_provenance_sha256")
                != auth_row.get("authoritative_provenance_sha256")
                or commit_row.get("reproduced_provenance_sha256")
                != auth_row.get("reproduced_provenance_sha256")
                or commit_row.get("source_provenance_equivalence_attested") is not True
                or commit_row.get("resumed_existing") is not True
                or commit_row.get("adapter_taskir_rederived") is not True
            ):
                raise EngineError("report supersession committed task binding differs")
            equivalence_bytes = self._read_supersession_evidence(
                str(commit_row.get("equivalence_path") or ""),
                label="report supersession generator equivalence",
            )
            if hashlib.sha256(equivalence_bytes).hexdigest() != commit_row.get(
                "equivalence_sha256"
            ):
                raise EngineError("report supersession generator equivalence changed")
            try:
                from elt_taskgen.provenance import (
                    GeneratorEquivalenceAttestation,
                    load_current as load_ingest_provenance,
                )

                equivalence_record = (
                    GeneratorEquivalenceAttestation.model_validate_json(
                        equivalence_bytes
                    )
                )
                member_task = self.load_task(task_id)
                authoritative_record = load_ingest_provenance(
                    self.task_dir(task_id), task=member_task, required=True
                )
                equivalence = equivalence_record.model_dump(mode="json")
            except (UnicodeError, ValueError) as exc:
                raise EngineError(
                    "report supersession generator equivalence is malformed"
                ) from exc
            if (
                equivalence_bytes != equivalence_record.deterministic_bytes()
                or equivalence.get("schema_version")
                != "generator-provenance-equivalence-v1"
                or equivalence.get("task_id") != task_id
                or equivalence.get("task_content_hash")
                != auth_row.get("intake_content_hash")
                or equivalence.get("authoritative_evidence_digest")
                != auth_row.get("authoritative_provenance_sha256")
                or authoritative_record.evidence_digest()
                != auth_row.get("authoritative_provenance_sha256")
            ):
                raise EngineError("report supersession generator equivalence is unbound")
            reproduced = equivalence.get("reproduced_provenance")
            if (
                not isinstance(reproduced, dict)
                or equivalence_record.reproduced_provenance.evidence_digest()
                != auth_row.get("reproduced_provenance_sha256")
                or (
                (
                    reproduced.get("source", {})
                    .get("selection_inputs", {})
                    .get("generator_code", {})
                    .get("digest")
                )
                != evidence.reproduced_generator_sha256
                )
            ):
                raise EngineError("report supersession reproduced provenance changed")
        auth_binding = next(
            (row for row in authorization_tasks if isinstance(row, dict) and row.get("task_id") == task.task_id),
            None,
        )
        commit_binding = next(
            (row for row in committed_tasks if isinstance(row, dict) and row.get("task_id") == task.task_id),
            None,
        )
        if (
            auth_binding is None
            or commit_binding is None
            or auth_binding.get("intake_content_hash") != evidence.intake_content_hash
            or commit_binding.get("intake_content_hash") != evidence.intake_content_hash
        ):
            raise EngineError("report supersession task is absent from committed roster")
        root_hash = task.revisions[0].content_hash if task.revisions else task.content_hash()
        if root_hash != evidence.intake_content_hash:
            raise EngineError("report supersession intake lineage changed")
        task_ir = self._read_supersession_evidence(
            evidence.archived_task_ir_path,
            label="report supersession archived TaskIR",
        )
        if hashlib.sha256(task_ir).hexdigest() != evidence.archived_task_ir_sha256:
            raise EngineError("report supersession archived TaskIR bytes changed")
        try:
            archived_task = task_from_json(task_ir.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise EngineError("report supersession archived TaskIR is invalid") from exc
        archived_root = (
            archived_task.revisions[0].content_hash
            if archived_task.revisions
            else archived_task.content_hash()
        )
        if (
            archived_task.task_id != task.task_id
            or archived_task.content_hash() != evidence.task_content_hash
            or archived_root != evidence.intake_content_hash
            or archived_task.solver_prompt != task.solver_prompt
        ):
            raise EngineError("report supersession archived TaskIR identity differs")
        prompt = task.solver_prompt or ""
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        original = evidence.original_evidence
        from elt_taskgen.review import prose_fidelity as prose_fidelity_mod
        from elt_taskgen.review import declarative_prose as declarative_prose_mod

        current_validator_sha = hashlib.sha256(
            Path(prose_fidelity_mod.__file__).read_bytes()
        ).hexdigest()
        current_declarative_sha = hashlib.sha256(
            Path(declarative_prose_mod.__file__).read_bytes()
        ).hexdigest()
        if (
            not prompt
            or original["prose_sha256"] != prompt_sha
            or evidence.replacement_data.get("prose_sha256") != prompt_sha
            or evidence.validator_identity.get("generator_sha256")
            != evidence.reproduced_generator_sha256
            or evidence.validator_identity.get("prose_fidelity_module_sha256")
            != current_validator_sha
            or evidence.validator_identity.get("declarative_prose_module_sha256")
            != current_declarative_sha
        ):
            raise EngineError("report supersession authored prose binding changed")
        try:
            transcript_manifest = json.loads(original["transcript_manifest"])
        except (TypeError, ValueError) as exc:
            raise EngineError("report supersession transcript manifest is malformed") from exc
        if not isinstance(transcript_manifest, list) or not transcript_manifest:
            raise EngineError("report supersession transcript manifest is empty")
        transcript_records: list[tuple[str, Mapping[str, Any]]] = []
        for transcript in transcript_manifest:
            if not isinstance(transcript, dict) or set(transcript) != {"path", "sha256"}:
                raise EngineError("report supersession transcript binding is invalid")
            transcript_bytes = self._read_supersession_evidence(
                str(transcript["path"]), label="report supersession transcript"
            )
            if hashlib.sha256(transcript_bytes).hexdigest() != transcript["sha256"]:
                raise EngineError("report supersession transcript changed")
            try:
                transcript_document = json.loads(transcript_bytes)
            except (UnicodeError, ValueError) as exc:
                raise EngineError("report supersession transcript is malformed") from exc
            if not isinstance(transcript_document, Mapping):
                raise EngineError("report supersession transcript is not an object")
            transcript_records.append((str(transcript["path"]), transcript_document))
        bound_documents: dict[str, Mapping[str, Any]] = {}
        for path_key, sha_key in (
            ("session_summary_path", "session_summary_sha256"),
            ("session_index_path", "session_index_sha256"),
            ("trajectory_path", "trajectory_sha256"),
        ):
            bound = self._read_supersession_evidence(
                original[path_key], label=f"report supersession {path_key}"
            )
            if hashlib.sha256(bound).hexdigest() != original[sha_key]:
                raise EngineError(f"report supersession {path_key} changed")
            try:
                document = json.loads(bound)
            except (UnicodeError, ValueError) as exc:
                raise EngineError(f"report supersession {path_key} is malformed") from exc
            if not isinstance(document, Mapping):
                raise EngineError(f"report supersession {path_key} is not an object")
            bound_documents[path_key] = document
        try:
            bindings = validate_author_revalidation_records(
                task_id=task.task_id,
                intake_content_hash=evidence.intake_content_hash,
                prompt=prompt,
                session_sha256=original["session_sha256"],
                transcript_records=tuple(transcript_records),
                session_index_path=original["session_index_path"],
                session_index=bound_documents["session_index_path"],
                trajectory_path=original["trajectory_path"],
                trajectory=bound_documents["trajectory_path"],
                session_summary=bound_documents["session_summary_path"],
            )
        except (TypeError, ValueError) as exc:
            raise EngineError(str(exc)) from exc
        for key in (
            "provider",
            "model",
            "served_models",
            "route",
            "admission",
            "behavior_sha256",
            "tools_sha256",
            "policy_sha256",
        ):
            if evidence.replacement_data.get(key) != bindings[key]:
                raise EngineError(
                    f"report supersession replacement {key} binding changed"
                )
        cause = self._report_by_id(supersession.cause_report_id)
        target = self._report_by_id(supersession.target_report_id)
        expected_common = (task.task_id, stage, supersession.content_hash)
        if cause is None or (cause.task_id, cause.stage, cause.content_hash) != expected_common:
            raise EngineError("report supersession cause identity does not match")
        if cause.verdict != VERDICT_FAIL:
            raise EngineError("report supersession cause is not a failed stage report")
        try:
            cause_payload = json.loads(cause.payload_json)
        except (TypeError, ValueError) as exc:
            raise EngineError("report supersession cause payload is malformed") from exc
        old_error = evidence.original_evidence["old_error"]
        if (
            not isinstance(cause_payload, dict)
            or cause_payload.get("error") != old_error
            or hashlib.sha256(old_error.encode("utf-8")).hexdigest()
            != evidence.original_evidence["old_error_sha256"]
        ):
            raise EngineError("report supersession old validator error changed")
        if target is None or (target.task_id, target.stage, target.content_hash) != expected_common:
            raise EngineError("report supersession target identity does not match")
        if target.verdict != VERDICT_FATAL:
            raise EngineError("report supersession target is not fatal")
        if cause.id >= target.id:
            raise EngineError("report supersession cause must precede its fatal target")
        for label, row, expected in (
            ("cause", cause, supersession.cause_payload_sha256),
            ("target", target, supersession.target_payload_sha256),
        ):
            observed = hashlib.sha256(row.payload_json.encode("utf-8")).hexdigest()
            if observed != expected:
                raise EngineError(f"report supersession {label} payload changed")

    def _read_supersession_evidence(self, relative: str, *, label: str) -> bytes:
        """Read one workspace-relative regular file without symlink ancestors."""

        rel = PurePosixPath(relative)
        if (
            rel.is_absolute()
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in rel.parts)
        ):
            raise EngineError(f"{label} path is unsafe")
        current = self.workspace
        try:
            for part in rel.parts[:-1]:
                current = current / part
                metadata = current.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise EngineError(f"{label} has an unsafe path ancestor")
            source = current / rel.parts[-1]
            before = source.lstat()
            if (
                stat.S_ISLNK(before.st_mode)
                or not stat.S_ISREG(before.st_mode)
                or before.st_size > 32 * 1024 * 1024
            ):
                raise EngineError(f"{label} is not a regular file")
            descriptor = os.open(
                source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
        except EngineError:
            raise
        except OSError as exc:
            raise EngineError(f"cannot read {label}: {exc}") from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise EngineError(f"{label} changed while it was opened")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                payload = handle.read(32 * 1024 * 1024 + 1)
            after = source.lstat()
            if (
                (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                or after.st_size != opened.st_size
                or after.st_mtime_ns != opened.st_mtime_ns
                or len(payload) != opened.st_size
            ):
                raise EngineError(f"{label} changed while it was read")
            return payload
        except OSError as exc:
            raise EngineError(f"cannot read {label}: {exc}") from exc
        finally:
            os.close(descriptor)

    @staticmethod
    def _nested_strings(value: Any) -> Iterator[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for nested in value.values():
                yield from Engine._nested_strings(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from Engine._nested_strings(nested)

    def _ensure_report_copy(self, task: TaskIR, row: ReportRow) -> None:
        destination = (
            self.task_dir(task.task_id)
            / "reports"
            / f"{row.id:06d}_{row.stage}.json"
        )
        expected = readable_json(
            {
                "id": row.id,
                "task_id": row.task_id,
                "revision": row.revision,
                "stage": row.stage,
                "verdict": row.verdict,
                "content_hash": row.content_hash,
                "payload": json.loads(row.payload_json),
            }
        )
        if destination.exists():
            if destination.is_symlink() or destination.read_text(encoding="utf-8") != expected:
                raise EngineError("report supersession ledger copy changed")
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(expected, encoding="utf-8")

    def supersede_report(
        self, task: TaskIR, supersession: ReportSupersession
    ) -> bool:
        """Append a revalidated PASS superseding one exact fatal without changing TaskIR."""

        with self._task_lock(task.task_id):
            self.recover_pending_repair(task.task_id)
            stored = self.load_task(task.task_id)
            if stored.content_hash() != task.content_hash():
                raise EngineError(
                    "report supersession task changed before publication"
                )
            self._validate_report_supersession(stored, supersession)
            existing = self._matching_supersession(stored, supersession)
            if existing is not None:
                self._ensure_report_copy(stored, existing)
                return False
            replacement_data = dict(supersession.replacement_data)
            if EVIDENCE_SUPERSESSION_KEY in replacement_data:
                raise EngineError("replacement data shadows supersession marker")
            replacement_data[EVIDENCE_SUPERSESSION_KEY] = canonical_json(
                supersession.as_dict()
            )
            report_id = self.record_report(
                stored,
                supersession.stage,
                VERDICT_PASS,
                StagePayload(
                    detail=supersession.replacement_detail,
                    data=replacement_data,
                ),
            )
            replacement = self._report_by_id(report_id)
            assert replacement is not None
            self._ensure_report_copy(stored, replacement)
            return True

    @staticmethod
    def _fatal_recovery_payload(recovery: FatalReportRecovery) -> StagePayload:
        marker = canonical_json(recovery.as_dict())
        data = {
            FATAL_RECOVERY_KEY: marker,
            "failure_class": recovery.failure_class,
            "failure_code": recovery.failure_code,
            "recovery_prerequisite": recovery.recovery_prerequisite,
        }
        if recovery.disposition == VERDICT_BLOCKED:
            data[BLOCKED_ON_KEY] = recovery.blocked_on
            data[RETRY_GUARD_KEY] = recovery.retry_guard
            return StagePayload(
                error=(
                    "historical fatal was produced by obsolete pipeline evidence, "
                    "but this stage remains pending explicit human adjudication; "
                    "no acceptance evidence was created"
                ),
                data=data,
            )
        return StagePayload(
            error=(
                "historical fatal was produced by obsolete pipeline evidence; "
                "this stage must be genuinely re-executed before it can pass"
            ),
            data=data,
        )

    def validate_fatal_recovery(
        self,
        task: TaskIR,
        recovery: FatalReportRecovery,
    ) -> FatalRecoveryEvidence:
        """Validate one recovery without appending any ledger row."""

        return self._validate_fatal_recovery(task, recovery)

    def recover_fatal_report(
        self,
        task: TaskIR,
        recovery: FatalReportRecovery,
    ) -> bool:
        """Append one idempotent non-PASS recovery disposition for an obsolete fatal."""

        with self._task_lock(task.task_id):
            self.recover_pending_repair(task.task_id)
            stored = self.load_task(task.task_id)
            if stored.content_hash() != task.content_hash():
                raise EngineError("fatal recovery task changed before publication")
            self._validate_fatal_recovery(stored, recovery)
            existing = self._matching_fatal_recovery(stored, recovery)
            if existing is not None:
                target = self._report_by_id(recovery.target_report_id)
                assert target is not None
                if existing.id <= target.id:
                    raise EngineError("fatal recovery marker does not follow its target")
                expected = canonical_json(
                    self._fatal_recovery_payload(recovery).model_dump(mode="json")
                )
                if existing.payload_json != expected:
                    raise EngineError("fatal recovery disposition payload changed")
                self._ensure_report_copy(stored, existing)
                return False
            target = self._report_by_id(recovery.target_report_id)
            assert target is not None
            report_id = self.record_report(
                stored,
                recovery.stage,
                recovery.disposition,
                self._fatal_recovery_payload(recovery),
            )
            if report_id <= target.id:
                raise EngineError("fatal recovery marker does not follow its target")
            replacement = self._report_by_id(report_id)
            assert replacement is not None
            self._ensure_report_copy(stored, replacement)
            return True

    def _fatal_recovery_for(
        self,
        task: TaskIR,
        row: ReportRow,
    ) -> tuple[ReportRow, FatalReportRecovery] | None:
        if row.verdict != VERDICT_FATAL or row.content_hash != task.content_hash():
            return None
        payload_digest = hashlib.sha256(row.payload_json.encode("utf-8")).hexdigest()
        for marker_row in self.report_history(task.task_id, row.stage):
            marker = self._fatal_recovery_marker(marker_row)
            if marker is None:
                continue
            recovery = self._fatal_recovery_from_marker(marker)
            if (
                marker_row.id <= row.id
                or marker_row.content_hash != task.content_hash()
                or recovery.target_report_id != row.id
                or recovery.stage != row.stage
                or recovery.content_hash != row.content_hash
                or recovery.target_payload_sha256 != payload_digest
            ):
                continue
            self._validate_fatal_recovery(task, recovery)
            expected_payload = canonical_json(
                self._fatal_recovery_payload(recovery).model_dump(mode="json")
            )
            if marker_row.verdict != recovery.disposition:
                raise EngineError("fatal recovery disposition verdict changed")
            if marker_row.payload_json != expected_payload:
                raise EngineError("fatal recovery disposition payload changed")
            return marker_row, recovery
        return None

    def fatal_report_is_recovered(self, task: TaskIR, row: ReportRow) -> bool:
        """Whether a valid non-PASS marker reopens this exact fatal only."""

        return self._fatal_recovery_for(task, row) is not None

    def fatal_recovery_is_remeasured(self, task: TaskIR, row: ReportRow) -> bool:
        """Return whether a current PASS was recorded after this recovery marker."""

        matched = self._fatal_recovery_for(task, row)
        if matched is None:
            return False
        marker_row, _recovery = matched
        latest = self.latest_report(task.task_id, row.stage)
        return bool(
            latest is not None
            and latest.id > marker_row.id
            and self._supersession_marker(latest) is None
            and self.report_is_current(task, row.stage, latest)[0]
        )

    def report_is_superseded(self, task: TaskIR, row: ReportRow) -> bool:
        """Whether a completed, same-lineage marker names this exact row."""

        payload_digest = hashlib.sha256(row.payload_json.encode("utf-8")).hexdigest()
        for marker_row in self.report_history(task.task_id, row.stage):
            marker = self._supersession_marker(marker_row)
            if (
                marker is not None
                and marker_row.content_hash == task.content_hash()
                and marker_row.id > row.id
                and marker.get("target_report_id") == row.id
                and marker.get("stage") == row.stage
                and marker.get("content_hash") == row.content_hash
                and marker.get("target_payload_sha256") == payload_digest
            ):
                supersession = self._supersession_from_marker(marker)
                self._validate_report_supersession(task, supersession)
                if marker_row.payload_json != canonical_json(
                    StagePayload(
                        detail=supersession.replacement_detail,
                        data={
                            **dict(supersession.replacement_data),
                            EVIDENCE_SUPERSESSION_KEY: canonical_json(
                                supersession.as_dict()
                            ),
                        },
                    ).model_dump(mode="json", exclude_none=True)
                ):
                    raise EngineError("report supersession replacement payload changed")
                return True
        return False

    def has_active_report_supersession(self, task: TaskIR) -> bool:
        """Return whether this identity has a fully revalidated supersession."""

        for stage in STAGE_ORDER:
            for marker_row in self.report_history(task.task_id, stage.value):
                marker = self._supersession_marker(marker_row)
                if marker is None or marker_row.content_hash != task.content_hash():
                    continue
                supersession = self._supersession_from_marker(marker)
                self._validate_report_supersession(task, supersession)
                target = self._report_by_id(supersession.target_report_id)
                if target is not None and self.report_is_superseded(task, target):
                    return True
        return False

    def _assert_reingest_is_intentional(self, task: TaskIR) -> None:
        """Refuse a same-id re-ingest that would orphan existing evidence."""
        path = self._task_ir_path(task.task_id)
        if not path.is_file():
            return
        try:
            existing = task_from_json(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — an unreadable copy is not evidence
            return
        if existing.content_hash() == task.content_hash():
            return  # idempotent re-register of the same identity
        if existing.status is TaskStatus.DRAFT:
            return  # nothing has attested to the old identity yet
        raise EngineError(
            f"task {task.task_id!r} already exists at content hash "
            f"{existing.content_hash()[:12]} with status "
            f"{existing.status.value!r}; re-ingesting would leave every "
            "recorded report bound to an identity the workspace no longer "
            "holds. Ingest under a new id, use a fresh workspace, or pass "
            "--reingest to overwrite deliberately."
        )

    def load_task(self, task_id: str) -> TaskIR:
        path = self._task_ir_path(task_id)
        if not path.is_file():
            raise EngineError(
                f"task {task_id!r} has no task_ir.json under {path.parent}"
            )
        task = task_from_json(path.read_text(encoding="utf-8"))
        if task.task_id != task_id:
            raise EngineError(
                f"task_ir.json under directory {task_id!r} names another task "
                f"{task.task_id!r}"
            )
        return task

    def save_task(self, task: TaskIR) -> None:
        """Atomic write (temp + rename): a crash never leaves a torn task_ir.json."""
        path = self._task_ir_path(task.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(task_to_json(task), encoding="utf-8")
        tmp.replace(path)

    # -- crash-recoverable repair commits ---------------------------------

    @staticmethod
    def _file_sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _repair_destination(self, task_id: str, rel_path: str) -> Path:
        """Resolve a journaled task path while rejecting traversal and symlinks."""
        rel = PurePosixPath(str(rel_path))
        prefix = ("tasks", task_id)
        if (
            rel.is_absolute()
            or "\\" in str(rel_path)
            or len(rel.parts) < 3
            or tuple(rel.parts[:2]) != prefix
            or any(part in ("", ".", "..") for part in rel.parts)
        ):
            raise EngineError(
                f"repair intent path {rel_path!r} is outside tasks/{task_id}/"
            )
        root = self.task_dir(task_id)
        destination = self.workspace.joinpath(*rel.parts)
        cursor = root
        for part in rel.parts[2:]:
            cursor = cursor / part
            try:
                metadata = cursor.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise EngineError(
                    f"repair intent path may not traverse a symbolic link: {cursor}"
                )
        if not destination.resolve(strict=False).is_relative_to(root.resolve()):
            raise EngineError(
                f"repair intent path {rel_path!r} resolves outside tasks/{task_id}/"
            )
        return destination

    def _repair_file_state(self, path: Path) -> tuple[int, str]:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return 0, ""
        if not stat.S_ISREG(metadata.st_mode):
            raise EngineError(f"repair target must be a regular file: {path}")
        return 1, self._file_sha256(path)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """Persist a rename/unlink where directory fsync is supported."""
        if os.name == "nt":  # pragma: no cover - Windows has no directory fd
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _repair_intent(self, intent_id: str) -> RepairIntentRow | None:
        row = self._con.execute(
            "SELECT intent_id, task_id, target_revision, route, reason,"
            " fingerprint, task_json, state, repair_id, report_ids_json"
            " FROM repair_intents WHERE intent_id=?",
            (str(intent_id),),
        ).fetchone()
        return RepairIntentRow(*row) if row is not None else None

    def pending_repair_intent(self, task_id: str) -> RepairIntentRow | None:
        """The task's sole pending repair intent, if one exists."""
        row = self._con.execute(
            "SELECT intent_id, task_id, target_revision, route, reason,"
            " fingerprint, task_json, state, repair_id, report_ids_json"
            " FROM repair_intents WHERE task_id=? AND state='pending'"
            " ORDER BY created_at, intent_id LIMIT 1",
            (validate_task_id_segment(task_id),),
        ).fetchone()
        return RepairIntentRow(*row) if row is not None else None

    def _has_pending_repair(self, task_id: str) -> bool:
        return self._con.execute(
            "SELECT 1 FROM repair_intents WHERE task_id=? AND state='pending' LIMIT 1",
            (task_id,),
        ).fetchone() is not None

    def commit_repair(
        self,
        task: TaskIR,
        route: RepairRoute,
        reason: str,
        *,
        fingerprint: str,
        rerun_stages: tuple[str, ...],
        invalidation_payload: BaseModel,
        staged_files: Mapping[str, bytes | None] | None = None,
    ) -> TaskIR:
        """Journal repair bytes and invalidations before installing live files."""
        route = RepairRoute(route)
        if route is RepairRoute.FATAL:
            raise ValueError("fatal routes cannot be committed as repairs")
        if not reason:
            raise ValueError("repair reason must be non-empty")
        if task.status is not TaskStatus.IN_REPAIR:
            raise EngineError("a repair commit target must have IN_REPAIR status")

        with self._task_lock(task.task_id):
            target_json = task_to_json(task)
            supplied = dict(staged_files or {})
            task_ir_rel = f"tasks/{task.task_id}/task_ir.json"
            # Lineage/status reconciliation is part of the same replayable file
            # set, and wins over the trial copy's pre-round task_ir bytes.
            supplied[task_ir_rel] = target_json.encode("utf-8")

            targets: list[tuple[str, Path, int, str, bytes | None]] = []
            identity_files: list[dict[str, str | int]] = []
            for rel_path in sorted(supplied):
                content = supplied[rel_path]
                if content is not None and not isinstance(content, bytes):
                    raise TypeError(
                        f"repair intent content for {rel_path!r} must be bytes or None"
                    )
                destination = self._repair_destination(task.task_id, rel_path)
                after_exists = int(content is not None)
                after_sha = hashlib.sha256(content).hexdigest() if content is not None else ""
                targets.append((rel_path, destination, after_exists, after_sha, content))
                identity_files.append(
                    {
                        "rel_path": rel_path,
                        "after_exists": after_exists,
                        "after_sha256": after_sha,
                    }
                )

            identity = {
                "task_id": task.task_id,
                "target_revision": task.current_revision,
                "route": route.value,
                "reason": reason,
                "fingerprint": str(fingerprint or ""),
                "task_json_sha256": hashlib.sha256(target_json.encode("utf-8")).hexdigest(),
                "files": identity_files,
            }
            intent_id = hashlib.sha256(
                canonical_json(identity).encode("utf-8")
            ).hexdigest()
            existing = self._repair_intent(intent_id)
            if existing is not None:
                if existing.state == "pending":
                    return self._finish_repair_intent(existing)
                return task_from_json(existing.task_json)

            pending = self.pending_repair_intent(task.task_id)
            if pending is not None:
                raise RepairCommitError(
                    f"task {task.task_id!r} already has pending repair intent "
                    f"{pending.intent_id[:12]}; recover it before starting another round"
                )

            live = self.load_task(task.task_id)
            if task.current_revision != live.current_revision + 1:
                raise EngineError(
                    f"repair target revision {task.current_revision} does not follow live "
                    f"revision {live.current_revision} for task {task.task_id!r}"
                )
            if tuple(task.revisions[:-1]) != tuple(live.revisions):
                raise EngineError(
                    f"repair target for task {task.task_id!r} does not extend the live lineage"
                )

            file_rows: list[tuple[str, int, str, int, str, int, str, bytes | None]] = []
            for ordinal, (rel_path, destination, after_exists, after_sha, content) in enumerate(
                targets
            ):
                before_exists, before_sha = self._repair_file_state(destination)
                file_rows.append(
                    (
                        intent_id,
                        ordinal,
                        rel_path,
                        before_exists,
                        before_sha,
                        after_exists,
                        after_sha,
                        content,
                    )
                )

            payload_json = canonical_json(invalidation_payload.model_dump(mode="json"))
            timestamp = _utc_now()
            con = self._con
            try:
                con.execute("BEGIN IMMEDIATE")
                repair_cur = con.execute(
                    "INSERT INTO repairs (task_id, revision, route, reason, created_at,"
                    " fingerprint, lineage_root_hash) VALUES (?,?,?,?,?,?,?)",
                    (
                        task.task_id,
                        task.current_revision,
                        route.value,
                        reason,
                        timestamp,
                        str(fingerprint or ""),
                        _repair_lineage_root_hash(task),
                    ),
                )
                repair_id = int(repair_cur.lastrowid)
                con.execute(
                    "INSERT INTO repair_intents (intent_id, task_id, target_revision,"
                    " route, reason, fingerprint, task_json, state, repair_id,"
                    " report_ids_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        intent_id,
                        task.task_id,
                        task.current_revision,
                        route.value,
                        reason,
                        str(fingerprint or ""),
                        target_json,
                        "pending",
                        repair_id,
                        "[]",
                        timestamp,
                    ),
                )
                con.executemany(
                    "INSERT INTO repair_intent_files (intent_id, ordinal, rel_path,"
                    " before_exists, before_sha256, after_exists, after_sha256, content)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    file_rows,
                )

                report_ids: list[int] = []
                for stage in rerun_stages:
                    stage_v = _stage_value(stage)
                    had_report = con.execute(
                        "SELECT 1 FROM reports WHERE task_id=? AND stage=? LIMIT 1",
                        (task.task_id, stage_v),
                    ).fetchone()
                    if had_report is None:
                        continue
                    report_cur = con.execute(
                        "INSERT INTO reports (task_id, revision, stage, verdict,"
                        " payload_json, content_hash, created_at) VALUES (?,?,?,?,?,?,?)",
                        (
                            task.task_id,
                            task.current_revision,
                            stage_v,
                            VERDICT_FAIL,
                            payload_json,
                            task.content_hash(),
                            timestamp,
                        ),
                    )
                    report_ids.append(int(report_cur.lastrowid))
                con.execute(
                    "UPDATE repair_intents SET report_ids_json=? WHERE intent_id=?",
                    (canonical_json(report_ids), intent_id),
                )
                con.commit()
            except Exception:
                con.rollback()
                raise

            intent = self._repair_intent(intent_id)
            if intent is None:  # pragma: no cover - SQLite just committed it
                raise EngineError(f"repair intent {intent_id} vanished after commit")
            try:
                return self._finish_repair_intent(intent)
            except RepairCommitError:
                raise
            except Exception as exc:
                raise RepairCommitError(
                    f"repair intent {intent_id[:12]} is durable but its live-file "
                    "installation did not finish; re-run to recover"
                ) from exc

    def _install_repair_intent_file(
        self,
        intent: RepairIntentRow,
        row: tuple[int, str, int, str, int, str, bytes | None],
    ) -> None:
        """Install one target file iff it is still at the journaled before state."""
        (
            _ordinal,
            rel_path,
            before_exists,
            before_sha,
            after_exists,
            after_sha,
            content,
        ) = row
        destination = self._repair_destination(intent.task_id, rel_path)
        current = self._repair_file_state(destination)
        after_state = (int(after_exists), str(after_sha))
        before_state = (int(before_exists), str(before_sha))
        if current == after_state:
            return
        if current != before_state:
            raise EngineError(
                f"repair intent {intent.intent_id[:12]} cannot reconcile {rel_path!r}: "
                "live bytes match neither its before nor target hash (fail closed)"
            )

        if int(after_exists):
            if content is None:
                raise EngineError(
                    f"repair intent {intent.intent_id[:12]} has no bytes for {rel_path!r}"
                )
            content = bytes(content)
            if hashlib.sha256(content).hexdigest() != after_sha:
                raise EngineError(
                    f"repair intent {intent.intent_id[:12]} has corrupt bytes for {rel_path!r}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            tmp = destination.with_name(
                f".{destination.name}.{intent.intent_id[:12]}.repair-tmp"
            )
            with tmp.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, destination)
            self._fsync_directory(destination.parent)
        else:
            if content is not None or after_sha:
                raise EngineError(
                    f"repair intent {intent.intent_id[:12]} has invalid deletion data "
                    f"for {rel_path!r}"
                )
            destination.unlink()
            self._fsync_directory(destination.parent)

        if self._repair_file_state(destination) != after_state:
            raise EngineError(
                f"repair intent {intent.intent_id[:12]} failed to install {rel_path!r}"
            )

    def _write_repair_intent_report_copies(
        self, intent: RepairIntentRow, task: TaskIR
    ) -> None:
        try:
            report_ids = json.loads(intent.report_ids_json)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise EngineError(
                f"repair intent {intent.intent_id[:12]} has corrupt report ids"
            ) from exc
        if not isinstance(report_ids, list) or not all(
            isinstance(value, int) and value > 0 for value in report_ids
        ):
            raise EngineError(
                f"repair intent {intent.intent_id[:12]} has invalid report ids"
            )
        for report_id in report_ids:
            row = self._con.execute(
                "SELECT id, task_id, revision, stage, verdict, payload_json,"
                " content_hash, created_at FROM reports WHERE id=? AND task_id=?",
                (report_id, intent.task_id),
            ).fetchone()
            if row is None:
                raise EngineError(
                    f"repair intent {intent.intent_id[:12]} names missing report {report_id}"
                )
            report = ReportRow(*row)
            self._write_report_copy(
                task,
                report.id,
                report.stage,
                report.verdict,
                report.payload_json,
            )

    def _finish_repair_intent(self, intent: RepairIntentRow) -> TaskIR:
        """Idempotently install and mark one already-durable intent committed."""
        task = task_from_json(intent.task_json)
        if (
            task.task_id != intent.task_id
            or task.current_revision != intent.target_revision
            or task.status is not TaskStatus.IN_REPAIR
        ):
            raise RepairCommitError(
                f"repair intent {intent.intent_id[:12]} has inconsistent target TaskIR"
            )
        rows = self._con.execute(
            "SELECT ordinal, rel_path, before_exists, before_sha256, after_exists,"
            " after_sha256, content FROM repair_intent_files WHERE intent_id=?"
            " ORDER BY CASE WHEN rel_path=? THEN 1 ELSE 0 END, ordinal",
            (intent.intent_id, f"tasks/{intent.task_id}/task_ir.json"),
        ).fetchall()
        if not rows:
            raise RepairCommitError(
                f"repair intent {intent.intent_id[:12]} has no target files"
            )
        for row in rows:
            self._install_repair_intent_file(intent, row)

        installed = self.load_task(intent.task_id)
        if task_to_json(installed) != intent.task_json:
            raise RepairCommitError(
                f"repair intent {intent.intent_id[:12]} did not reconcile task_ir.json"
            )
        self._write_repair_intent_report_copies(intent, installed)
        self._con.execute(
            "UPDATE repair_intents SET state='committed', committed_at=?"
            " WHERE intent_id=? AND state='pending'",
            (_utc_now(), intent.intent_id),
        )
        # Drop installed target bytes while retaining before/after audit hashes.
        self._con.execute(
            "UPDATE repair_intent_files SET content=NULL WHERE intent_id=?",
            (intent.intent_id,),
        )
        self._con.commit()
        return installed

    def recover_pending_repair(self, task_id: str) -> TaskIR | None:
        """Idempotently finish a pending journaled repair under the task lock."""
        with self._task_lock(task_id):
            intent = self.pending_repair_intent(task_id)
            if intent is None:
                return None
            try:
                return self._finish_repair_intent(intent)
            except RepairCommitError:
                raise
            except Exception as exc:
                raise RepairCommitError(
                    f"repair intent {intent.intent_id[:12]} remains pending and "
                    "could not be recovered (fail closed)"
                ) from exc

    # -- ledger ------------------------------------------------------------

    def record_report(
        self, task: TaskIR, stage: str, verdict: str, payload: BaseModel
    ) -> int:
        stage_v = _stage_value(stage)
        if verdict not in _VERDICTS:
            raise ValueError(f"verdict {verdict!r} not in {_VERDICTS}")
        payload_json = canonical_json(payload.model_dump(mode="json"))
        cur = self._con.execute(
            "INSERT INTO reports (task_id, revision, stage, verdict, payload_json,"
            " content_hash, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                task.task_id,
                task.current_revision,
                stage_v,
                verdict,
                payload_json,
                task.content_hash(),
                _utc_now(),
            ),
        )
        self._con.commit()
        report_id = int(cur.lastrowid)
        self._write_report_copy(task, report_id, stage_v, verdict, payload_json)
        return report_id

    def _write_report_copy(
        self, task: TaskIR, report_id: int, stage: str, verdict: str, payload_json: str
    ) -> None:
        """JSON copy under tasks/<id>/reports/. Excludes created_at so the copies
        stay wall-clock-free; time lives only in the SQLite ledger."""
        reports_dir = self.task_dir(task.task_id) / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        copy = {
            "id": report_id,
            "task_id": task.task_id,
            "revision": task.current_revision,
            "stage": stage,
            "verdict": verdict,
            "content_hash": task.content_hash(),
            "payload": json.loads(payload_json),
        }
        (reports_dir / f"{report_id:06d}_{stage}.json").write_text(
            readable_json(copy), encoding="utf-8"
        )

    def latest_report(self, task_id: str, stage: str) -> ReportRow | None:
        cur = self._con.execute(
            "SELECT id, task_id, revision, stage, verdict, payload_json, content_hash,"
            " created_at FROM reports WHERE task_id=? AND stage=? ORDER BY id DESC LIMIT 1",
            (task_id, _stage_value(stage)),
        )
        row = cur.fetchone()
        return ReportRow(*row) if row is not None else None

    def latest_report_with_verdict(
        self, task_id: str, stage: str, verdict: str
    ) -> ReportRow | None:
        """Return the newest row for one stage and verdict without reviving it."""
        if verdict not in _VERDICTS:
            raise ValueError(f"verdict {verdict!r} not in {_VERDICTS}")
        cur = self._con.execute(
            "SELECT id, task_id, revision, stage, verdict, payload_json,"
            " content_hash, created_at FROM reports"
            " WHERE task_id=? AND stage=? AND verdict=? ORDER BY id DESC",
            (task_id, _stage_value(stage), verdict),
        )
        try:
            task = self.load_task(task_id)
        except EngineError:
            task = None
        for raw in cur.fetchall():
            row = ReportRow(*raw)
            if task is None or not self.report_is_superseded(task, row):
                return row
        return None

    def report_history(self, task_id: str, stage: str) -> tuple[ReportRow, ...]:
        """Return typed rows for one task stage, newest first."""
        cur = self._con.execute(
            "SELECT id, task_id, revision, stage, verdict, payload_json,"
            " content_hash, created_at FROM reports"
            " WHERE task_id=? AND stage=? ORDER BY id DESC",
            (task_id, _stage_value(stage)),
        )
        return tuple(ReportRow(*row) for row in cur.fetchall())

    def record_artifact(self, task: TaskIR, rel_path: str, sha256: str) -> None:
        if not rel_path or not sha256:
            raise ValueError("rel_path and sha256 are required")
        self._con.execute(
            "INSERT INTO artifacts (task_id, revision, rel_path, sha256, created_at)"
            " VALUES (?,?,?,?,?)",
            (task.task_id, task.current_revision, rel_path, sha256, _utc_now()),
        )
        self._con.commit()

    def record_repair(
        self,
        task: TaskIR,
        route: RepairRoute,
        reason: str,
        *,
        fingerprint: str = "",
    ) -> int:
        """Append a repair row with its starting-state fingerprint."""
        cur = self._con.execute(
            "INSERT INTO repairs (task_id, revision, route, reason, created_at,"
            " fingerprint, lineage_root_hash) VALUES (?,?,?,?,?,?,?)",
            (
                task.task_id,
                task.current_revision,
                route.value,
                reason,
                _utc_now(),
                str(fingerprint or ""),
                _repair_lineage_root_hash(task),
            ),
        )
        self._con.commit()
        return int(cur.lastrowid)

    def _current_repair_lineage_root_hash(self, task_id: str) -> str | None:
        """Current root, or None when no readable TaskIR can scope a query."""
        try:
            return _repair_lineage_root_hash(self.load_task(task_id))
        except (EngineError, OSError, TypeError, ValueError):
            return None

    def last_repair(self, task_id: str) -> RepairRow | None:
        """Return the newest repair in the current lineage, or a legacy fallback."""
        lineage_root_hash = self._current_repair_lineage_root_hash(task_id)
        where = " WHERE task_id=?"
        parameters: tuple[str, ...] = (task_id,)
        if lineage_root_hash is not None:
            where += " AND (lineage_root_hash=? OR lineage_root_hash='')"
            parameters += (lineage_root_hash,)
        cur = self._con.execute(
            "SELECT id, task_id, revision, route, reason, fingerprint,"
            " lineage_root_hash FROM repairs"
            + where
            + " ORDER BY id DESC LIMIT 1",
            parameters,
        )
        row = cur.fetchone()
        return RepairRow(*row) if row is not None else None

    def repair_rounds_used(self, task_id: str) -> int:
        """Count rounds in the current lineage, with a legacy task-ID fallback."""
        lineage_root_hash = self._current_repair_lineage_root_hash(task_id)
        sql = "SELECT COUNT(*) FROM repairs WHERE task_id=?"
        parameters: tuple[str, ...] = (task_id,)
        if lineage_root_hash is not None:
            sql += " AND (lineage_root_hash=? OR lineage_root_hash='')"
            parameters += (lineage_root_hash,)
        cur = self._con.execute(sql, parameters)
        return int(cur.fetchone()[0])

    def session_limit_reruns(self, task_id: str, stage: str | StageName) -> int:
        """Count current-identity salted reruns for a limit-stopped proposer."""
        try:
            task = self.load_task(task_id)
        except EngineError:
            return 0
        cur = self._con.execute(
            "SELECT verdict, payload_json FROM reports WHERE task_id=? AND stage=?"
            " AND verdict IN (?, ?) AND content_hash=?",
            (
                task_id,
                _stage_value(stage),
                VERDICT_BLOCKED,
                VERDICT_FAIL,
                task.content_hash(),
            ),
        )
        count = 0
        for verdict, payload_json in cur.fetchall():
            try:
                payload = json.loads(payload_json)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                continue
            if verdict == VERDICT_BLOCKED and str(data.get(BLOCKED_ON_KEY, "")).startswith(
                SESSION_LIMIT_BLOCK_PREFIX
            ):
                count += 1
            elif verdict == VERDICT_FAIL and data.get(SESSION_LIMIT_FALLBACK_KEY):
                # The bound was reached and the ordinary round taken at THIS
                # identity: the salted sequence is spent here for good.
                count = max(count, MAX_SESSION_LIMIT_RERUNS)
        return count

    # -- currency ----------------------------------------------------------

    def report_is_current(
        self, task: TaskIR, stage: str | StageName, row: "ReportRow | None"
    ) -> tuple[bool, str]:
        """Return whether stage evidence is current and, if not, why.

        Variant batteries also require current rosters; release requires its
        frozen artifacts to remain present.
        """
        stage_v = _stage_value(stage)
        if self._has_pending_repair(task.task_id):
            return False, (
                f"task {task.task_id!r} has a pending repair intent; recovery "
                "must finish before any prior pass can be current"
            )
        if row is None:
            return False, f"stage {stage_v!r} has no report"
        if self.report_is_superseded(task, row):
            return False, (
                f"{stage_v!r} report was explicitly superseded by a verified "
                "code migration"
            )
        if row.verdict != VERDICT_PASS:
            return False, f"latest {stage_v!r} report is {row.verdict!r}, not a pass"
        if row.content_hash != task.content_hash():
            return False, (
                f"{stage_v!r} report is bound to content hash "
                f"{row.content_hash[:12]}, task is {task.content_hash()[:12]}"
            )
        if stage_v == StageName.RELEASE.value:
            manifest = self.workspace / RELEASE_DIRNAME / RELEASE_MANIFEST_FILENAME
            expected_manifest_sha = ""
            try:
                release_payload = json.loads(row.payload_json)
            except (json.JSONDecodeError, TypeError, ValueError):
                release_payload = {}
            release_data = (
                release_payload.get("data")
                if isinstance(release_payload, dict)
                else None
            )
            if isinstance(release_data, dict) and release_data.get("release_dir"):
                declared = Path(str(release_data["release_dir"]))
                release_root = (
                    declared
                    if declared.is_absolute()
                    else self.workspace / declared
                )
                manifest = release_root / RELEASE_MANIFEST_FILENAME
                expected_manifest_sha = str(
                    release_data.get("release_manifest_sha256") or ""
                )
            if not manifest.is_file():
                return False, (
                    f"{stage_v!r} report names a frozen release, but no "
                    f"{RELEASE_MANIFEST_FILENAME} is on disk at {manifest.parent} "
                    "— the tree it attests to is gone, so "
                    "re-freeze it (the ledger row is not the release)"
                )
            if expected_manifest_sha:
                observed_manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
                if observed_manifest_sha != expected_manifest_sha:
                    return False, (
                        f"{stage_v!r} release manifest digest changed at "
                        f"{manifest}"
                    )
        variant = _STAGE_VARIANT.get(stage_v)
        if variant is None:
            return True, ""
        from elt_taskgen.verification import variant_battery as battery_mod

        try:
            payload = json.loads(row.payload_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return False, f"{stage_v!r} battery payload is unreadable (fail closed)"
        if not isinstance(payload.get("gates"), list):
            if stage_v == StageName.TASK_INTEGRITY.value:
                # The SHARED stage's payload carries no roster claim (internal
                # integrity evidence only), so there is nothing to be stale about.
                return True, ""
            # An acceptance stage that recorded something else has not attested:
            # run it. Deliberately not a FAILURE — see the guard in run().
            return False, f"{stage_v!r} payload is not an acceptance report"
        staleness = battery_mod.roster_staleness(payload, variant)
        if staleness:
            return False, f"{stage_v!r}: {staleness}"
        return True, ""

    def blocked_stage(self, task_id: str) -> "ReportRow | None":
        """The stage this task is WAITING on, or None: the first stage in pipeline
        order whose latest row is a `blocked` row at the current content hash."""
        try:
            task = self.load_task(task_id)
        except EngineError:
            return None
        current = task.content_hash()
        for stage in STAGE_ORDER:
            row = self.latest_report(task_id, stage.value)
            if (
                row is not None
                and row.verdict == VERDICT_BLOCKED
                and row.content_hash == current
                and not self.report_is_superseded(task, row)
            ):
                return row
        return None

    # -- verdicts ----------------------------------------------------------

    def final_verdict(self, task_id: str) -> str:
        """Return accepted, rejected, or in-progress for the current identity.

        Acceptance requires both current variant batteries; newer current fatals
        shadow them.
        """
        if self._has_pending_repair(task_id):
            return FINAL_IN_PROGRESS
        try:
            task = self.load_task(task_id)
        except EngineError:
            return FINAL_IN_PROGRESS
        cur = self._con.execute(
            "SELECT id, task_id, revision, stage, verdict, payload_json,"
            " content_hash, created_at FROM reports"
            " WHERE task_id=? AND verdict=? AND content_hash=? ORDER BY id DESC",
            (task_id, VERDICT_FATAL, task.content_hash()),
        )
        fatal_id = None
        recovery_pending = False
        for raw in cur.fetchall():
            candidate = ReportRow(*raw)
            if self.report_is_superseded(task, candidate):
                continue
            if self.fatal_report_is_recovered(task, candidate):
                recovery_pending = recovery_pending or not (
                    self.fatal_recovery_is_remeasured(task, candidate)
                )
                continue
            fatal_id = candidate.id
            break

        # Recovery permits remeasurement but is not PASS evidence.
        if fatal_id is None and recovery_pending:
            return FINAL_IN_PROGRESS

        accepted_rows: list[ReportRow] = []
        from elt_taskgen.verification import variant_battery as battery_mod

        for variant in RLVR_TASK_VARIANTS:
            stage = variant_gate_stage(variant)
            row = self.latest_report(task_id, stage.value)
            # SAME predicate as the resume skip rule, so a roster-stale battery is
            # "not attested yet" for both, never "accepted" for one.
            if row is None or not self.report_is_current(task, stage, row)[0]:
                return FINAL_REJECTED if fatal_id is not None else FINAL_IN_PROGRESS
            try:
                payload = json.loads(row.payload_json)
                summary = battery_mod.summarize_payload(payload, variant)
            except Exception:  # malformed payload/roster: fail closed
                return FINAL_REJECTED if fatal_id is not None else FINAL_IN_PROGRESS
            if not summary.get("accepted"):
                return FINAL_REJECTED if fatal_id is not None else FINAL_IN_PROGRESS
            accepted_rows.append(row)

        if fatal_id is not None and fatal_id > min(row.id for row in accepted_rows):
            return FINAL_REJECTED
        return FINAL_ACCEPTED

    # -- orchestration -----------------------------------------------------

    def run(
        self,
        task_id: str,
        *,
        until: str | None = None,
        pre_run: Callable[["Engine", TaskIR], None] | None = None,
    ) -> TaskIR:
        """Run current stages under one task lock with bounded repair.

        Skip only current PASS rows. ``until`` is inclusive, and ``pre_run`` runs
        inside the lock before state is loaded.
        """
        with self._task_lock(task_id):
            # Recovery precedes even a caller-supplied locked mutation: pre_run
            # must never inspect or invalidate a partially installed identity.
            self.recover_pending_repair(task_id)
            if pre_run is not None:
                pre_run(self, self.load_task(task_id))
            return self._run_locked(task_id, until=until)

    def _run_locked(self, task_id: str, *, until: str | None = None) -> TaskIR:
        """Implementation of :meth:`run`; caller holds ``_task_lock``."""
        until_name = _stage_value(until) if until is not None else None
        self.recover_pending_repair(task_id)
        task = self.load_task(task_id)
        sweeps = 0
        while True:
            sweeps += 1
            if sweeps > _MAX_SWEEPS:
                raise EngineError(
                    f"task {task_id!r}: orchestration did not converge after "
                    f"{_MAX_SWEEPS} sweeps (non-deterministic stage runner?)"
                )
            if self.final_verdict(task_id) == FINAL_REJECTED:
                return self.load_task(task_id)
            restart = False
            for stage in STAGE_ORDER:
                current_hash = task.content_hash()
                row = self.latest_report(task_id, stage.value)
                if (
                    row is not None
                    and row.content_hash == current_hash
                    and blocked_retry_requires_explicit_recovery(row)
                ):
                # Explicitly invalidate guarded holds after their prerequisite changes.
                    return task
                current, _why = self.report_is_current(task, stage, row)
                if current:
                    if stage.value == until_name:
                        return task
                    continue  # completed at this identity: resume is a no-op
                runner = self._stage_runners.get(stage.value)
                if runner is None:
                    raise StageNotWiredError(
                        f"stage {stage.value!r} has no wired runner; wire it via "
                        "stage_runners= or set_stage_runner() (engine never guesses)"
                    )
                try:
                    outcome = runner(self, task)
                except Exception as exc:  # noqa: BLE001 — fail closed as a stage failure
                    outcome = StageOutcome(
                        verdict=VERDICT_FAIL,
                        payload=StagePayload(
                            error=f"{type(exc).__name__}: {exc}",
                            infrastructure=_infra_marker_for(exc),
                            budget_scope=_budget_scope_for(exc) or None,
                        ),
                    )
                if outcome.task is not None:
                    if outcome.task.task_id != task_id:
                        raise EngineError(
                            f"stage {stage.value!r} changed task_id from "
                            f"{task_id!r} to {outcome.task.task_id!r}; refusing "
                            "to write another task while holding the wrong lock"
                        )
                    task = outcome.task
                    self.save_task(task)
                if outcome.verdict == VERDICT_BLOCKED:
                    # WAITING, not failing: no repair round, no fatal row, no status
                    # change. The next run() re-executes it — that IS the resume.
                    self.record_report(
                        task, stage.value, VERDICT_BLOCKED, outcome.payload
                    )
                    return task
                if outcome.verdict == VERDICT_PASS:
                    report_id = self.record_report(
                        task, stage.value, VERDICT_PASS, outcome.payload
                    )
                    # Convert a noncurrent PASS into a failure to stop endless reruns.
                    fresh = self.latest_report(task_id, stage.value)
                    still, why = self.report_is_current(task, stage, fresh)
                    if (
                        not still
                        and fresh is not None
                        and fresh.id == report_id
                        and _claims_to_be_a_battery(fresh.payload_json)
                    ):
                        task = self._handle_failure(
                            task,
                            stage,
                            StageOutcome(
                                verdict=VERDICT_FAIL,
                                payload=StagePayload(
                                    error=(
                                        f"stage {stage.value!r} recorded a PASS "
                                        f"that is not current: {why}"
                                    )
                                ),
                            ),
                        )
                        if task.status is TaskStatus.REJECTED:
                            return task
                        restart = True
                        break
                    status = _STATUS_AFTER_PASS.get(stage)
                    if (
                        stage is StageName.GATES_TRANSFORM
                        and self.final_verdict(task_id) == FINAL_ACCEPTED
                    ):
                        status = TaskStatus.ACCEPTED
                    if status is not None and task.status is not status:
                        task = task.with_status(status)
                        self.save_task(task)
                    if stage.value == until_name:
                        return task
                    if task.content_hash() != current_hash:
                        # Semantic edit: every stage re-attests at the new identity.
                        restart = True
                        break
                    continue
                task = self._handle_failure(task, stage, outcome)
                if task.status is TaskStatus.REJECTED:
                    return task
                latest = self.latest_report(task_id, stage.value)
                if (
                    latest is not None
                    and latest.verdict == VERDICT_BLOCKED
                    and latest.content_hash == task.content_hash()
                ):
                    # A salted proposer wait resumes like any other blocked stage.
                    return task
                restart = True
                break
            if not restart:
                return task

    def _handle_failure(
        self, task: TaskIR, stage: StageName, outcome: StageOutcome
    ) -> TaskIR:
        from elt_taskgen import repair as repair_mod  # lazy: engine <-> repair

        route = RepairRoute.FATAL if outcome.verdict == VERDICT_FATAL else outcome.route
        if route is None:
            route = repair_mod.route_for_failure(stage.value, outcome.payload)

        # Transport markers outrank inferred routes, but not explicit fatal verdicts.
        infra = _marker_text(
            outcome.infrastructure or _infrastructure_failure(outcome.payload)
        )
        budget_scope = _budget_scope_from_payload(outcome.payload)
        if route is RepairRoute.FATAL and not (
            infra and outcome.verdict != VERDICT_FATAL and outcome.route is None
        ):
            self.record_report(task, stage.value, VERDICT_FATAL, outcome.payload)
            return self._reject(task)
        self.record_report(task, stage.value, VERDICT_FAIL, outcome.payload)

            # Halt infrastructure failures without repair or rejection.
        if infra:
            data = {
                "failed_stage": stage.value,
                "route": route.value,
                "infrastructure": str(infra),
            }
            if budget_scope:
                data[BUDGET_SCOPE_FIELD] = budget_scope
            self.record_report(
                task,
                stage.value,
                VERDICT_FAIL,
                StagePayload(
                    error=(
                        f"infrastructure failure ({infra}) at stage "
                        f"{stage.value!r}: the transport failed, not the task. "
                        "No repair round is spent and the task is NOT rejected "
                        "— no edit to prose, reference SQL or population "
                        "conditions can fix this. Fix the infrastructure and "
                        "re-run in this workspace; the ledger resumes at this "
                        "stage."
                    ),
                    infrastructure=str(infra),
                    budget_scope=budget_scope or None,
                    data=data,
                ),
            )
            raise InfrastructureFailure(
                task.task_id,
                stage.value,
                str(infra),
                budget_scope=budget_scope,
            )

        # THE INERT-REPAIR RULE. Prove, before spending, that the LAST round
        # was able to change something. See elt_taskgen.repair.
        rounds_used = self.repair_rounds_used(task.task_id)
        fingerprint = repair_mod.repair_fingerprint(self.workspace, task)
        previous = self.last_repair(task.task_id)
        previous_route = route
        if previous is not None:
            try:
                previous_route = RepairRoute(previous.route)
            except ValueError:  # a hand-edited ledger names the CURRENT route
                previous_route = route
        try:
            repair_mod.assert_repair_not_inert(
                previous_fingerprint=previous.fingerprint if previous else None,
                current_fingerprint=fingerprint,
                route=previous_route,
                stage=stage.value,
                rounds_used=rounds_used,
            )
        except repair_mod.InertRepairError as exc:
            self.record_report(
                task,
                stage.value,
                VERDICT_FATAL,
                StagePayload(
                    error=str(exc),
                    data={
                        "failed_stage": stage.value,
                        "route": route.value,
                        "rounds_used": str(rounds_used),
                        "start_fingerprint": fingerprint,
                    },
                ),
            )
            return self._reject(task)

        if rounds_used >= self.max_repair_rounds:
            self.record_report(
                task,
                stage.value,
                VERDICT_FATAL,
                StagePayload(
                    detail=(
                        f"repair budget exhausted ({self.max_repair_rounds} rounds); "
                        "rejecting, never silently re-accepting"
                    ),
                    data={"failed_stage": stage.value, "route": route.value},
                ),
            )
            return self._reject(task)
        reason = f"stage {stage.value} failed; routed as {route.value}"
        task, blocked = self._propose_patch(
            task, stage, route, outcome, commit_reason=reason
        )
        if blocked:
            # Session-limit waits spend no round and do not change task status.
            return task
        # Do not append a second round after the proposer committed its journal.
        if self.repair_rounds_used(task.task_id) > rounds_used:
            return self.load_task(task.task_id)
        # Re-read the fingerprint: a certified patch may have just committed
        # bytes, and the round must record the state it ACTUALLY starts from.
        return repair_mod.apply_repair(
            self,
            task,
            route,
            reason,
            fingerprint=repair_mod.repair_fingerprint(self.workspace, task),
        )

    def _propose_patch(
        self,
        task: TaskIR,
        stage: StageName,
        route: RepairRoute,
        outcome: StageOutcome,
        *,
        commit_reason: str,
    ) -> tuple[TaskIR, bool]:
        """Try bounded patches certified on trial copies before workspace writes.

        Human or session-limit holds return blocked without spending a round.
        Infrastructure and proposer-contract faults halt; ordinary rejected
        proposals consume only their bounded attempts.
        """
        proposer = self._repair_proposer
        if proposer is None:
            return task, False
        from elt_taskgen.review import repair_proposer as proposer_mod

        # Infrastructure failures cannot be repaired by task edits.
        infra = _marker_text(
            outcome.infrastructure or _infrastructure_failure(outcome.payload)
        )
        if infra:
            self.record_report(
                task,
                stage.value,
                VERDICT_FAIL,
                StagePayload(
                    error=(
                        "infrastructure failure, not a task defect — no repair "
                        f"proposed: {infra}"
                    ),
                    infrastructure=str(infra),
                    data={"route": route.value, "stage": stage.value},
                ),
            )
            return task, False

        try:
            with self._journal_certified_patch(
                task.task_id, stage.value, route, commit_reason
            ):
                result = proposer.repair(
                    self,
                    task,
                    stage.value,
                    route,
                    proposer_mod.failure_detail(
                        outcome.payload, route=route, task=task, stage=stage.value
                    ),
                )
        except Exception as exc:  # noqa: BLE001 — fail closed: no patch, no commit
            marker = _marker_text(_infra_marker_for(exc))
            budget_scope = _budget_scope_for(exc)
            if not marker and isinstance(exc, (ValueError, TypeError)):
                # The proposer broke ITS contract, not the patch: a harness
                # defect (exit 2), never a failed proposal spending the round.
                marker = _marker_text(PROPOSER_CONTRACT_MARKER)
            if marker:
                # Proposer harness faults halt instead of consuming an attempt.
                self._halt_on_proposer_fault(
                    task,
                    stage,
                    route,
                    marker,
                    f"{type(exc).__name__}: {exc}",
                    budget_scope=budget_scope,
                )
            self.record_report(
                task,
                stage.value,
                VERDICT_FAIL,
                StagePayload(
                    error=f"repair proposer failed: {type(exc).__name__}: {exc}",
                    data={"route": route.value, "stage": stage.value},
                ),
            )
            return task, False

        disposition = str(getattr(result, "disposition", "") or "")
        marker = _marker_text(getattr(result, "infrastructure", "") or "")
        if marker or disposition == proposer_mod.DISPOSITION_HALTED:
            halted = getattr(result, "record", None)
            self._halt_on_proposer_fault(
                task,
                stage,
                route,
                marker or proposer_mod.DISPOSITION_HALTED,
                str(getattr(halted, "detail", "") or ""),
                budget_scope=str(
                    getattr(result, "budget_scope", "") or ""
                ),
            )
        if disposition == proposer_mod.DISPOSITION_BLOCKED_LIMIT:
            return self._block_on_session_limit(task, stage, route, result)

        record = result.record
        task = result.task if result.committed and result.task is not None else task
        data = {
            "route": route.value,
            "stage": stage.value,
            "status": record.status,
            "attempts": str(len(record.attempts)),
        }
        if result.adjudication is not None:
            data["adjudication"] = str(
                Path(result.adjudication).relative_to(self.workspace).as_posix()
            )
        if (
            disposition == proposer_mod.DISPOSITION_NEEDS_ADJUDICATION
            and record.status == proposer_mod.STATUS_NEEDS_ADJUDICATION
        ):
            # Abstention certifies no edit, so keep a human resume point. Only a
            # real edit may create a repair row, invalidate evidence, or spend a round.
            data[BLOCKED_ON_KEY] = BLOCKED_ON_HUMAN
            self.record_report(
                task,
                stage.value,
                VERDICT_BLOCKED,
                StagePayload(detail=record.detail, data=data),
            )
            return task, True
        # A FAIL row bound to the (possibly new) identity: a committed patch is
        # evidence, never an acceptance — the invalidated stages must re-attest.
        self.record_report(
            task, stage.value, VERDICT_FAIL, StagePayload(detail=record.detail, data=data)
        )
        return task, False

    def _halt_on_proposer_fault(
        self,
        task: TaskIR,
        stage: StageName,
        route: RepairRoute,
        marker: str,
        why: str,
        *,
        extra: Mapping[str, str] | None = None,
        budget_scope: str = "",
    ) -> None:
        """Record a proposer harness fault and halt without rejection or a round."""
        marker = str(marker)
        budget_scope = _normalise_infrastructure_budget_scope(budget_scope)
        data = {str(k): str(v) for k, v in (extra or {}).items() if str(v)}
        blocked_on = blocked_on_from_marker(marker)
        if blocked_on:
            # A certification member that WAITED: the reason travels on the
            # row beside the marker, as a live BLOCKED row would carry it.
            data[BLOCKED_ON_KEY] = blocked_on
        data.update(
            {
                "failed_stage": stage.value,
                "route": route.value,
                "infrastructure": marker,
                "status": "halted",
            }
        )
        if budget_scope:
            data[BUDGET_SCOPE_FIELD] = budget_scope
        self.record_report(
            task,
            stage.value,
            VERDICT_FAIL,
            StagePayload(
                error=(
                    f"infrastructure failure ({marker}) inside the repair "
                    f"proposer at stage {stage.value!r}: the harness failed, not "
                    "the task. No repair round is spent and the task is NOT "
                    "rejected. Fix the infrastructure and re-run in this "
                    "workspace; the ledger resumes at this stage."
                    + (f" [{why}]" if why else "")
                ),
                infrastructure=marker,
                budget_scope=budget_scope or None,
                data=data,
            ),
        )
        raise InfrastructureFailure(
            task.task_id,
            stage.value,
            marker,
            budget_scope=budget_scope,
        )

    def _block_on_session_limit(
        self, task: TaskIR, stage: StageName, route: RepairRoute, result: Any
    ) -> tuple[TaskIR, bool]:
        """Record a session-limit wait, fallback round, or infrastructure halt.

        Task and total budget exhaustion halt immediately. Agent-attributable
        limits get bounded salted reruns; wall limits halt after those reruns.
        """
        record = getattr(result, "record", None)
        limit = str(getattr(result, "limit", "") or "").strip() or "unknown"
        scope = str(getattr(result, "limit_scope", "") or "").strip().lower()
        reruns = self.session_limit_reruns(task.task_id, stage.value)
        data = {
            "route": route.value,
            "stage": stage.value,
            "status": str(getattr(record, "status", "") or "blocked_limit"),
            "attempts": str(len(getattr(record, "attempts", ()) or ())),
            "limit": limit,
            "reruns_used": str(reruns),
        }
        if scope:
            data["limit_scope"] = scope
        if limit == "usd" and scope != SESSION_USD_ROLE_SCOPE:
            # Only role-scoped USD limits are agent-attributable.
            self._halt_on_proposer_fault(
                task,
                stage,
                route,
                _marker_text(SESSION_USD_HALT_MARKER),
                f"session_limit:usd tripped the {scope or 'unknown'}-scope "
                "budget; only the role scope (the session's own max_usd) is "
                "agent-attributable, so no salted re-run and no round is taken",
                extra={"limit": limit, "limit_scope": scope},
                budget_scope=scope,
            )
        if reruns >= MAX_SESSION_LIMIT_RERUNS:
            if limit in SESSION_LIMIT_HALT_KINDS:
                # Wall limits include transport latency and halt as infrastructure.
                self._halt_on_proposer_fault(
                    task,
                    stage,
                    route,
                    _marker_text(SESSION_WALL_HALT_MARKER),
                    f"session_limit:{limit} after {reruns} salted re-runs "
                    f"(the bound is {MAX_SESSION_LIMIT_RERUNS}); a {limit} stop "
                    "is not agent-attributable, so no round is taken",
                    extra={"limit": limit, "limit_scope": scope, "reruns_used": str(reruns)},
                )
            data[SESSION_LIMIT_FALLBACK_KEY] = limit
            self.record_report(
                task,
                stage.value,
                VERDICT_FAIL,
                StagePayload(
                    detail=(
                        f"repair proposer session stopped at its {limit} limit "
                        f"without a validator-green draft after {reruns} salted "
                        f"re-runs (the bound is {MAX_SESSION_LIMIT_RERUNS}); "
                        "falling back to the ordinary bounded repair round"
                    ),
                    data=data,
                ),
            )
            return task, False
        salt = reruns + 1
        data[BLOCKED_ON_KEY] = f"{SESSION_LIMIT_BLOCK_PREFIX}{limit}"
        data["session_salt"] = str(salt)
        data["rerun"] = f"{salt}/{MAX_SESSION_LIMIT_RERUNS}"
        self.record_report(
            task,
            stage.value,
            VERDICT_BLOCKED,
            StagePayload(
                detail=(
                    f"repair proposer session stopped at its {limit} limit "
                    "without a validator-green draft: WAITING on salted re-run "
                    f"{salt} of {MAX_SESSION_LIMIT_RERUNS} (session_salt {salt}). "
                    "No repair round is spent and nothing is rejected; the next "
                    f"run of this workspace re-executes stage {stage.value!r}"
                ),
                data=data,
            ),
        )
        return task, True

    def _reject(self, task: TaskIR) -> TaskIR:
        task = task.with_status(TaskStatus.REJECTED)
        self.save_task(task)
        return task

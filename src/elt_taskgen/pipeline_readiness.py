"""Revalidate stage-owned evidence before configured runs reuse ledger passes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from elt_taskgen.engine import (
    Engine,
    FINAL_ACCEPTED,
    FINAL_REJECTED,
    STAGE_ORDER,
    StageName,
    VERDICT_BLOCKED,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
)
from elt_taskgen.models import (
    PopulationName,
    RLVR_TASK_VARIANTS,
    TaskIR,
    canonical_json,
    validate_task_id_segment,
)


READINESS_SCHEMA_VERSION = "pipeline-readiness-v1"
RUN_CONFIG_SCHEMA_VERSION = "generation-run-v1"
CONFIGURED_STAGE_SEAL_VERSION = "configured-stage-inputs-v3"
CONFIGURED_STAGE_CONTRACT_VERSION = "configured-stage-contract-v1"
_MAX_CONFIGURED_REFERENCE_BYTES = 16 * 1024 * 1024
RUN_PREFLIGHT_BLOCKED_STATE = "run_preflight_blocked"

_PROVIDER_STAGES = frozenset(
    {
        StageName.AUTHOR,
        StageName.REVIEW,
        StageName.ATTACK,
        StageName.TASK_INTEGRITY,
        StageName.GATES_EXTRACT_LOAD,
        StageName.GATES_TRANSFORM,
        StageName.CALIBRATE,
    }
)
_SELECTION_STAGES = frozenset(
    {
        StageName.CONTAMINATION_POST,
        StageName.SELECT,
        StageName.RELEASE,
    }
)

# Stage seals use logical paths so equivalent installations share identity.
_CONFIGURED_STAGE_ROLES: dict[StageName, tuple[str, ...]] = {
    StageName.AUTHOR: ("semantic_author",),
    StageName.REVIEW: (
        "ambiguity_critic",
        "population_adversary",
        "shortcut_attacker",
        "feasibility_reviewer",
    ),
    # ATTACK executes the handoffs produced by the critic council, so the
    # critic behaviour is part of the attack stage's reusable contract too.
    StageName.ATTACK: (
        "ambiguity_critic",
        "population_adversary",
        "shortcut_attacker",
        "feasibility_reviewer",
    ),
    StageName.TASK_INTEGRITY: ("independent_implementer",),
    StageName.GATES_EXTRACT_LOAD: ("independent_loader",),
    StageName.GATES_TRANSFORM: ("independent_implementer",),
}

_COMMON_PROVIDER_IMPLEMENTATION_MODULES = (
    "elt_taskgen.review.prompts",
    "elt_taskgen.review.providers",
    "elt_taskgen.review.session",
)
_CONFIGURED_STAGE_MODULES: dict[StageName, tuple[str, ...]] = {
    StageName.INTAKE: (
        "elt_taskgen.adapters.dbt",
        "elt_taskgen.adapters.dlt",
        "elt_taskgen.adapters.evidence",
        "elt_taskgen.adapters.schemapile",
        "elt_taskgen.adapters.synsql",
        "elt_taskgen.adapters.wikidbs",
        "elt_taskgen.ingest_manifest",
        "elt_taskgen.provenance",
    ),
    StageName.GENERATE: (
        "elt_taskgen.generation.challenging_policy",
        "elt_taskgen.generation.lineage",
        "elt_taskgen.generation.mart_plan",
        "elt_taskgen.generation.populations",
        "elt_taskgen.generation.source_data",
        "elt_taskgen.sql_identifiers",
    ),
    StageName.REFERENCE: (
        "elt_taskgen.generation.mart_plan",
        "elt_taskgen.reference.duckdb_sandbox",
        "elt_taskgen.reference.gold",
        "elt_taskgen.reference.runner",
        "elt_taskgen.reference.solution",
        "elt_taskgen.sql_identifiers",
    ),
    StageName.AUTHOR: (
        *_COMMON_PROVIDER_IMPLEMENTATION_MODULES,
        "elt_taskgen.review.council",
        "elt_taskgen.review.declarative_prose",
        "elt_taskgen.review.prose_fidelity",
        "elt_taskgen.review.tools.registry",
        "elt_taskgen.review.tools.validators",
    ),
    StageName.REVIEW: (
        *_COMMON_PROVIDER_IMPLEMENTATION_MODULES,
        "elt_taskgen.review.council",
        "elt_taskgen.review.tools.critic_validators",
        "elt_taskgen.review.tools.projection",
        "elt_taskgen.review.tools.registry",
        "elt_taskgen.verification.attacks",
    ),
    StageName.ATTACK: (
        "elt_taskgen.generation.mart_plan",
        "elt_taskgen.generation.source_data",
        "elt_taskgen.reference.runner",
        "elt_taskgen.runtime.evaluation",
        "elt_taskgen.verification.attack_matrix",
        "elt_taskgen.verification.attacks",
        "elt_taskgen.sql_identifiers",
    ),
    StageName.TASK_INTEGRITY: (
        "elt_taskgen.generation.mart_plan",
        "elt_taskgen.reference.adjudication",
        "elt_taskgen.reference.gold",
        "elt_taskgen.reference.independent",
        "elt_taskgen.reference.runner",
        "elt_taskgen.reference.solution",
        "elt_taskgen.runtime.evaluation",
        "elt_taskgen.sql_identifiers",
        "elt_taskgen.verification.filters",
        "elt_taskgen.verification.gates",
        "elt_taskgen.verification.reference_readiness",
        "elt_taskgen.verification.structural_completeness",
        "elt_taskgen.verification.upstream_eval",
        "elt_taskgen.verification.variant_battery",
    ),
    StageName.GATES_EXTRACT_LOAD: (
        "elt_taskgen.generation.mart_plan",
        "elt_taskgen.reference.adjudication",
        "elt_taskgen.reference.gold",
        "elt_taskgen.reference.independent",
        "elt_taskgen.reference.runner",
        "elt_taskgen.reference.solution",
        "elt_taskgen.runtime.evaluation",
        "elt_taskgen.sql_identifiers",
        "elt_taskgen.verification.filters",
        "elt_taskgen.verification.gates",
        "elt_taskgen.verification.reference_readiness",
        "elt_taskgen.verification.structural_completeness",
        "elt_taskgen.verification.upstream_eval",
        "elt_taskgen.verification.variant_battery",
    ),
    StageName.GATES_TRANSFORM: (
        "elt_taskgen.airbyte_connector_config",
        "elt_taskgen.destinations",
        "elt_taskgen.generation.mart_plan",
        "elt_taskgen.reference.adjudication",
        "elt_taskgen.reference.gold",
        "elt_taskgen.reference.independent",
        "elt_taskgen.reference.runner",
        "elt_taskgen.reference.solution",
        "elt_taskgen.runtime.evaluation",
        "elt_taskgen.semantic.models",
        "elt_taskgen.semantic.package",
        "elt_taskgen.semantic.scoring",
        "elt_taskgen.semantic_contract",
        "elt_taskgen.sql_identifiers",
        "elt_taskgen.training.airbyte_proxy",
        "elt_taskgen.training.canonical",
        "elt_taskgen.training.contract",
        "elt_taskgen.training.dbt_runner",
        "elt_taskgen.training.dev_tool",
        "elt_taskgen.training.local_sync",
        "elt_taskgen.training.models",
        "elt_taskgen.training.namespace",
        "elt_taskgen.training.package",
        "elt_taskgen.training.scorer",
        "elt_taskgen.training.terraform_intent",
        "elt_taskgen.training.warehouse_profiles",
        "elt_taskgen.training.workspace",
        "elt_taskgen.verification.filters",
        "elt_taskgen.verification.gates",
        "elt_taskgen.verification.reference_readiness",
        "elt_taskgen.verification.structural_completeness",
        "elt_taskgen.verification.upstream_eval",
        "elt_taskgen.verification.variant_battery",
        "elt_taskgen.workspace",
    ),
    StageName.CALIBRATE: (
        "elt_taskgen.corpus.calibration",
        "elt_taskgen.corpus.certified_difficulty",
        "elt_taskgen.corpus.difficulty",
        "elt_taskgen.review.metrology",
        "elt_taskgen.runtime.evaluation",
    ),
    StageName.CONTAMINATION_PRE: (
        "elt_taskgen.verification.contamination",
        "elt_taskgen.verification.filters",
    ),
    StageName.CONTAMINATION_POST: (
        "elt_taskgen.corpus.selection",
        "elt_taskgen.verification.contamination",
        "elt_taskgen.verification.filters",
    ),
    StageName.SELECT: (
        "elt_taskgen.corpus.selection",
    ),
    StageName.RELEASE: (
        "elt_taskgen.airbyte_connector_config",
        "elt_taskgen.destinations",
        "elt_taskgen.export.certification",
        "elt_taskgen.export.local_package",
        "elt_taskgen.export.package_verification",
        "elt_taskgen.export.release",
        "elt_taskgen.semantic.models",
        "elt_taskgen.semantic.package",
        "elt_taskgen.semantic.scoring",
        "elt_taskgen.semantic_contract",
        "elt_taskgen.training.airbyte_proxy",
        "elt_taskgen.training.canonical",
        "elt_taskgen.training.contract",
        "elt_taskgen.training.dbt_runner",
        "elt_taskgen.training.dev_tool",
        "elt_taskgen.training.local_sync",
        "elt_taskgen.training.models",
        "elt_taskgen.training.namespace",
        "elt_taskgen.training.package",
        "elt_taskgen.training.scorer",
        "elt_taskgen.training.terraform_intent",
        "elt_taskgen.training.warehouse_profiles",
        "elt_taskgen.training.workspace",
        "elt_taskgen.workspace",
    ),
}

# Map durable provider roles to stages; keep unknown roles as ``unattributed``.
DEFAULT_ROLE_STAGE_MAP: dict[str, str] = {
    "semantic_author": StageName.AUTHOR.value,
    "ambiguity_critic": StageName.REVIEW.value,
    "population_adversary": StageName.REVIEW.value,
    "shortcut_attacker": StageName.REVIEW.value,
    "feasibility_reviewer": StageName.REVIEW.value,
    "independent_implementer": StageName.TASK_INTEGRITY.value,
    "independent_loader": StageName.GATES_EXTRACT_LOAD.value,
    "repair_proposer": "repair",
}


class ReadinessState(str, Enum):
    NOT_RUN = "NOT_RUN"
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    STALE = "STALE"


class ReadinessFailureClass(str, Enum):
    """Why evidence is not ready, separate from the task disposition."""

    NONE = ""
    QUALITY_REJECTION = "quality_rejection"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    PROTOCOL_FAILURE = "protocol_failure"
    PENDING_ADJUDICATION = "pending_adjudication"
    OPERATIONAL_BLOCKING = "operational_blocking"
    STALE_EVIDENCE = "stale_evidence"


_FAILURE_CLASS_ALIASES: dict[str, ReadinessFailureClass] = {
    member.value: member for member in ReadinessFailureClass if member.value
}
_FAILURE_CLASS_ALIASES.update(
    {
        "harness_defect": ReadinessFailureClass.INFRASTRUCTURE_FAILURE,
        "transient_infrastructure": ReadinessFailureClass.INFRASTRUCTURE_FAILURE,
        "policy_failure": ReadinessFailureClass.PROTOCOL_FAILURE,
        "policy_violation": ReadinessFailureClass.PROTOCOL_FAILURE,
        "task_defect": ReadinessFailureClass.QUALITY_REJECTION,
    }
)


def _payload_mapping(row: Any) -> dict[str, Any]:
    try:
        payload = json.loads(row.payload_json)
    except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _failure_descriptor(
    *,
    verdict: str,
    payload: Mapping[str, Any] | None = None,
    stale_code: str = "",
) -> tuple[ReadinessFailureClass, str, str]:
    """Return failure class, stable code, and blocker from a report payload."""

    if stale_code:
        return ReadinessFailureClass.STALE_EVIDENCE, stale_code, ""
    document = dict(payload or {})
    data = document.get("data")
    data = data if isinstance(data, Mapping) else {}

    def value(name: str) -> str:
        raw = document.get(name)
        if raw in (None, ""):
            raw = data.get(name)
        return str(raw or "").strip()

    blocked_on = value("blocked_on").lower()
    failure_code = value("failure_code")
    explicit = value("failure_class").lower()
    status = value("status").lower()
    infrastructure = value("infrastructure")
    classified = _FAILURE_CLASS_ALIASES.get(explicit)

    if explicit and classified is None:
        classified = ReadinessFailureClass.PROTOCOL_FAILURE
        failure_code = failure_code or "unknown_failure_class"
    if classified is None and status == "needs_adjudication":
        classified = ReadinessFailureClass.PENDING_ADJUDICATION
        failure_code = failure_code or status
    if classified is None and infrastructure:
        classified = ReadinessFailureClass.INFRASTRUCTURE_FAILURE
        failure_code = failure_code or infrastructure
    if classified is None and verdict == VERDICT_BLOCKED:
        classified = ReadinessFailureClass.OPERATIONAL_BLOCKING
        failure_code = failure_code or blocked_on or "stage_blocked"
    if classified is None and verdict in {VERDICT_FAIL, VERDICT_FATAL}:
        classified = ReadinessFailureClass.QUALITY_REJECTION
        failure_code = failure_code or (
            "stage_fatal" if verdict == VERDICT_FATAL else "stage_failed"
        )
    return classified or ReadinessFailureClass.NONE, failure_code, blocked_on


def _attempt_failure_descriptor(
    payload: Mapping[str, Any],
) -> tuple[ReadinessFailureClass, str, str]:
    """Typed cause for a durable attempt record, including legacy state names."""

    state = str(payload.get("state") or "").strip().lower()
    explicit = str(payload.get("failure_class") or "").strip().lower()
    if explicit:
        return _failure_descriptor(verdict=VERDICT_FAIL, payload=payload)
    legacy: dict[str, ReadinessFailureClass] = {
        "infrastructure": ReadinessFailureClass.INFRASTRUCTURE_FAILURE,
        "protocol_failure": ReadinessFailureClass.PROTOCOL_FAILURE,
        "incomplete_protocol": ReadinessFailureClass.PROTOCOL_FAILURE,
        "pending_adjudication": ReadinessFailureClass.PENDING_ADJUDICATION,
        "blocked": ReadinessFailureClass.OPERATIONAL_BLOCKING,
        RUN_PREFLIGHT_BLOCKED_STATE: ReadinessFailureClass.OPERATIONAL_BLOCKING,
        "usage_error": ReadinessFailureClass.OPERATIONAL_BLOCKING,
        "package_error": ReadinessFailureClass.OPERATIONAL_BLOCKING,
        "ingest_failed": ReadinessFailureClass.OPERATIONAL_BLOCKING,
        "stale": ReadinessFailureClass.STALE_EVIDENCE,
        "rejected": ReadinessFailureClass.QUALITY_REJECTION,
    }
    classified = legacy.get(state, ReadinessFailureClass.NONE)
    code = str(payload.get("failure_code") or "").strip() or (
        state if classified is not ReadinessFailureClass.NONE else ""
    )
    blocked_on = str(payload.get("blocked_on") or "").strip().lower()
    return classified, code, blocked_on


class ReadinessProfile(str, Enum):
    DRAFT = "draft"
    LOCAL_READY = "local-ready"
    PACKAGED = "packaged"
    CALIBRATED = "calibrated"
    RELEASE_READY = "release-ready"
    RELEASE = "release"

    @property
    def until_stage(self) -> StageName:
        return {
            self.DRAFT: StageName.INTAKE,
            self.LOCAL_READY: StageName.GATES_TRANSFORM,
            self.PACKAGED: StageName.GATES_TRANSFORM,
            self.CALIBRATED: StageName.CALIBRATE,
            self.RELEASE_READY: StageName.SELECT,
            self.RELEASE: StageName.RELEASE,
        }[self]


class GenerationRunSpec(BaseModel):
    """Canonical runtime request; CLI and config-file inputs meet here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["generation-run-v1"] = RUN_CONFIG_SCHEMA_VERSION
    candidate_count: int = Field(ge=1)
    source_families: tuple[str, ...] = (
        "dbt",
        "dlt",
        "synsql",
        "schemapile",
        "wikidbs",
    )
    source_allocation: dict[str, int] | None = None
    seed: int = 0
    profile: ReadinessProfile = ReadinessProfile.PACKAGED
    resume: bool = True
    workers: int = Field(default=4, ge=1)
    max_repair_rounds: int = Field(default=3, ge=0)
    repair_attempts: int | None = Field(default=None, ge=0)
    http_retries: int = Field(default=4, ge=0, le=20)
    schema_retries: int = Field(default=2, ge=0, le=10)
    http_timeout_seconds: float = Field(default=600.0, gt=0.0, le=3600.0)
    http_backoff_seconds: float = Field(default=2.0, ge=0.0, le=60.0)
    #: Per-task live-call circuit breaker.
    budget_per_task: float = Field(default=25.0, gt=0.0)
    budget_total: float | None = Field(default=None, gt=0.0)
    agents_config: str = ""
    admission_reference: str = ""
    export_dir: str = ""
    #: Packaged destinations; the first is the bundle root and others are nested.
    destinations: tuple[Literal["snowflake", "databricks", "redshift"], ...] = (
        "snowflake",
        "databricks",
        "redshift",
    )
    #: The bundle-root destination; always `destinations[0]`. Kept as a field
    #: so older run documents that name only `destination` still validate.
    destination: Literal["snowflake", "databricks", "redshift"] = "snowflake"
    #: Per-role model transport, included in run and provider-stage identity.
    agent_harness: Literal["api", "headless"] = "api"
    empirical: bool = False
    #: Read-only links to existing runtime reports for selected frozen tasks.
    runtime_certification_store: str = ""
    runtime_difficulty_reports: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _destination_roster(cls, data: Any) -> Any:
        """Normalize destinations and retain the first as the bundle root."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        roster = data.get("destinations")
        primary = data.get("destination")
        if roster is None:
            if primary is not None:
                data["destinations"] = (primary,)
            return data
        if isinstance(roster, str):
            text = roster.strip()
            if not text or text.lower() == "all":
                roster = ("snowflake", "databricks", "redshift")
            else:
                roster = tuple(
                    part.strip() for part in text.split(",") if part.strip()
                )
        elif isinstance(roster, (list, tuple)):
            roster = tuple(roster)
        else:
            raise ValueError(
                'destinations must be "all", a comma-separated list, or a list'
            )
        data["destinations"] = roster
        if primary is None and roster:
            data["destination"] = roster[0]
        return data

    @field_validator("destinations")
    @classmethod
    def _destinations_are_a_set(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("destinations must name at least one warehouse")
        if len(set(value)) != len(value):
            raise ValueError("destinations must not contain duplicates")
        return value

    @property
    def extra_destinations(self) -> tuple[str, ...]:
        """Destinations packaged under `destinations/<name>/`, root excluded."""
        return tuple(name for name in self.destinations if name != self.destination)

    @field_validator(
        "candidate_count",
        "seed",
        "workers",
        "max_repair_rounds",
        "repair_attempts",
        "http_retries",
        "schema_retries",
        mode="before",
    )
    @classmethod
    def _integers_are_not_booleans(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("integer run settings must not be booleans")
        return value

    @field_validator("source_allocation", mode="before")
    @classmethod
    def _allocation_counts_are_not_booleans(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(
            isinstance(count, bool) for count in value.values()
        ):
            raise ValueError("source_allocation counts must not be booleans")
        return value

    @field_validator(
        "budget_per_task",
        "budget_total",
        "http_timeout_seconds",
        "http_backoff_seconds",
        mode="before",
    )
    @classmethod
    def _floats_are_not_booleans(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("numeric run settings must not be booleans")
        return value

    @field_validator("resume", "empirical", mode="before")
    @classmethod
    def _booleans_are_strict(cls, value: Any) -> Any:
        if type(value) is not bool:
            raise ValueError("boolean run settings must be true or false")
        return value

    @field_validator(
        "budget_per_task",
        "budget_total",
        "http_timeout_seconds",
        "http_backoff_seconds",
    )
    @classmethod
    def _finite_budget(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("budgets must be finite")
        return value

    @field_validator("runtime_certification_store")
    @classmethod
    def _absolute_runtime_store(cls, value: str) -> str:
        if not value:
            return value
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("runtime_certification_store must be clean text")
        if not Path(value).is_absolute():
            raise ValueError("runtime_certification_store must be an absolute path")
        return value

    @field_validator("runtime_difficulty_reports")
    @classmethod
    def _absolute_runtime_reports(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for task_id, raw_path in sorted(value.items()):
            validate_task_id_segment(task_id)
            if (
                not raw_path
                or raw_path != raw_path.strip()
                or any(ord(character) < 32 for character in raw_path)
            ):
                raise ValueError(
                    f"runtime difficulty report for {task_id!r} must be clean text"
                )
            if not Path(raw_path).is_absolute():
                raise ValueError(
                    f"runtime difficulty report for {task_id!r} must be an absolute path"
                )
            normalized[task_id] = raw_path
        return normalized

    @model_validator(mode="after")
    def _coherent(self) -> "GenerationRunSpec":
        if self.destination != self.destinations[0]:
            raise ValueError(
                f"destination {self.destination!r} must be the first entry of "
                f"destinations {list(self.destinations)!r} (the bundle root)"
            )
        if not self.source_families:
            raise ValueError("source_families must not be empty")
        if len(set(self.source_families)) != len(self.source_families):
            raise ValueError("source_families must not contain duplicates")
        if self.source_allocation is not None:
            if any(value < 0 for value in self.source_allocation.values()):
                raise ValueError("source_allocation values must be non-negative")
            if sum(self.source_allocation.values()) != self.candidate_count:
                raise ValueError("source_allocation must sum to candidate_count")
            unknown = set(self.source_allocation) - set(self.source_families)
            if unknown:
                raise ValueError(
                    f"source_allocation names unselected families: {sorted(unknown)}"
                )
        needs_export = self.profile in {
            ReadinessProfile.PACKAGED,
            ReadinessProfile.RELEASE,
        }
        if needs_export and not self.export_dir:
            raise ValueError(f"profile {self.profile.value!r} requires export_dir")
        if self.profile in {
            ReadinessProfile.CALIBRATED,
            ReadinessProfile.RELEASE_READY,
            ReadinessProfile.RELEASE,
        } and not self.empirical:
            raise ValueError(
                f"profile {self.profile.value!r} requires empirical=true"
            )
        if bool(self.runtime_certification_store) != bool(
            self.runtime_difficulty_reports
        ):
            raise ValueError(
                "runtime_certification_store and runtime_difficulty_reports "
                "must be configured together"
            )
        if (
            self.runtime_difficulty_reports
            and self.profile is not ReadinessProfile.RELEASE
        ):
            raise ValueError(
                "runtime certification evidence requires profile='release'; "
                "local-ready and packaged profiles remain local-only"
            )
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()


class StageReadiness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: StageName
    state: ReadinessState
    report_id: int | None = None
    verdict: str = ""
    evidence: tuple[str, ...] = ()
    reason: str = ""
    failure_class: ReadinessFailureClass = ReadinessFailureClass.NONE
    failure_code: str = ""
    blocked_on: str = ""


class Coverage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    populations: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    marts: tuple[str, ...] = ()
    attacks: tuple[str, ...] = ()
    variants: tuple[str, ...] = ()


class AttemptReadiness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: str
    target_stage: StageName
    state: str
    evidence_path: str
    task_content_hash: str = ""
    detail: str = ""
    usd: float = 0.0
    failure_class: ReadinessFailureClass = ReadinessFailureClass.NONE
    failure_code: str = ""
    blocked_on: str = ""


class RuntimeEvidenceReadiness(BaseModel):
    """Read-only verification of one previously completed runtime promotion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    configured: bool = False
    certification_verified: bool = False
    difficulty_verified: bool = False
    certification_id: str = ""
    lifecycle_digest: str = ""
    difficulty_evidence_digest: str = ""
    empirical_band: str = ""
    destination: str = ""
    certification_evidence: tuple[str, ...] = ()
    difficulty_evidence: tuple[str, ...] = ()
    certification_reason: str = ""
    difficulty_reason: str = ""


class CandidateRunAction(BaseModel):
    """Independent durable actions performed for one configured candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selected: bool = True
    registration_action: Literal[
        "created", "resumed", "failed", "not_attempted", "unknown"
    ] = "unknown"
    processing_attempted: bool = False
    adapter_taskir_rederived: bool = False
    source_provenance_equivalence_attested: bool = False
    reports_locally_revalidated_and_superseded: int = Field(default=0, ge=0)
    evidence_paths: tuple[str, ...] = ()

    @field_validator(
        "selected",
        "processing_attempted",
        "adapter_taskir_rederived",
        "source_provenance_equivalence_attested",
        mode="before",
    )
    @classmethod
    def _strict_action_booleans(cls, value: Any) -> Any:
        if type(value) is not bool:
            raise ValueError("candidate action flags must be true or false")
        return value


class CostTotals(BaseModel):
    """One reconciled monetary/call-count slice of a durable ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recorded_cost: float = Field(default=0.0, ge=0.0)
    reserved_cost: float = Field(default=0.0, ge=0.0)
    uncertain_cost: float = Field(default=0.0, ge=0.0)
    # Legacy task totals sometimes preserve only the combined pending amount.
    # Durable-ledger reporting always leaves this at zero.
    unclassified_pending_cost: float = Field(default=0.0, ge=0.0)
    reserved_or_uncertain_cost: float = Field(default=0.0, ge=0.0)
    final_request_overrun: float = Field(default=0.0, ge=0.0)
    committed_calls: int = Field(default=0, ge=0)
    reserved_calls: int = Field(default=0, ge=0)
    uncertain_calls: int = Field(default=0, ge=0)
    released_calls: int = Field(default=0, ge=0)

    @field_validator(
        "recorded_cost",
        "reserved_cost",
        "uncertain_cost",
        "unclassified_pending_cost",
        "reserved_or_uncertain_cost",
        "final_request_overrun",
    )
    @classmethod
    def _finite_cost(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("cost values must be finite")
        return value

    @model_validator(mode="after")
    def _pending_cost_reconciles(self) -> "CostTotals":
        expected = math.fsum(
            (
                self.reserved_cost,
                self.uncertain_cost,
                self.unclassified_pending_cost,
            )
        )
        if not math.isclose(
            self.reserved_or_uncertain_cost,
            expected,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError(
                "reserved_or_uncertain_cost must equal reserved + uncertain + "
                "unclassified pending cost"
            )
        return self


class TaskCostBreakdown(CostTotals):
    """Cumulative ledger costs for one selected candidate."""

    by_role: dict[str, CostTotals] = Field(default_factory=dict)
    by_stage: dict[str, CostTotals] = Field(default_factory=dict)
    over_task_limit: float = Field(default=0.0, ge=0.0)


class RunCostBreakdown(CostTotals):
    """Cumulative, reconciling view of one durable batch budget snapshot."""

    run_id: str = ""
    accounting_source: Literal[
        "durable_budget_ledger", "legacy_task_totals", "worker_results", "none"
    ] = "none"
    reconciled: bool = False
    total_limit_usd: float | None = Field(default=None, ge=0.0)
    per_task_limit_usd: float | None = Field(default=None, ge=0.0)
    available_usd: float = Field(default=0.0, ge=0.0)
    over_limit_usd: float = Field(default=0.0, ge=0.0)
    by_task: dict[str, TaskCostBreakdown] = Field(default_factory=dict)
    by_role: dict[str, CostTotals] = Field(default_factory=dict)
    by_stage: dict[str, CostTotals] = Field(default_factory=dict)
    reservations: tuple[dict[str, Any], ...] = ()
    ledger_created_at: str = ""
    ledger_updated_at: str = ""

    @property
    def spent_usd(self) -> float:
        """Compatibility spelling used by older configured-run consumers."""

        return self.recorded_cost


class TaskReadiness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    origin: str
    source_family: str = ""
    family_id: str
    task_content_hash: str
    task_revision: int = Field(default=0, ge=0)
    # Compatibility field for readiness-v1 reports written before the explicit
    # task_revision spelling was introduced. Both are kept equal.
    revision: int = Field(default=0, ge=0)
    disposition: str
    stages: tuple[StageReadiness, ...]
    attempts: tuple[AttemptReadiness, ...] = ()
    actions: CandidateRunAction = Field(default_factory=CandidateRunAction)
    coverage: Coverage
    population_coverage: tuple[str, ...] = ()
    prompt_status: ReadinessState = ReadinessState.NOT_RUN
    review_status: ReadinessState = ReadinessState.NOT_RUN
    attack_status: ReadinessState = ReadinessState.NOT_RUN
    integrity_status: ReadinessState = ReadinessState.NOT_RUN
    el_acceptance: ReadinessState = ReadinessState.NOT_RUN
    t_acceptance: ReadinessState = ReadinessState.NOT_RUN
    locally_evaluator_ready: bool = False
    local_evaluator_readiness: bool = False
    packaged_for_evaluation: bool = False
    package_status: ReadinessState = ReadinessState.NOT_RUN
    empirically_calibrated: bool = False
    release_ready: bool = False
    runtime_certified: bool = False
    runtime_evidence: RuntimeEvidenceReadiness = Field(
        default_factory=RuntimeEvidenceReadiness
    )
    package_path: str = ""
    package_path_if_verified: str = ""
    package_verification_receipt: str = ""
    package_verification_sha256: str = ""
    blocker: str = ""
    failure_class: ReadinessFailureClass = ReadinessFailureClass.NONE
    failure_code: str = ""
    blocked_on: str = ""
    precise_blockers: tuple[str, ...] = ()
    evidence_paths: tuple[str, ...] = ()
    spent_usd: float = 0.0
    recorded_cost: float = 0.0
    reserved_cost: float = 0.0
    uncertain_cost: float = 0.0
    reserved_or_uncertain_cost: float = 0.0
    final_request_overrun: float = 0.0
    cost_by_role: dict[str, CostTotals] = Field(default_factory=dict)
    cost_by_stage: dict[str, CostTotals] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _compatibility_aliases(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if "task_revision" not in data:
            data["task_revision"] = data.get("revision", 0)
        if "revision" not in data:
            data["revision"] = data.get("task_revision", 0)
        if data.get("task_revision") != data.get("revision"):
            raise ValueError("task_revision and revision must agree")
        if "source_family" not in data:
            data["source_family"] = data.get("origin", "")
        if "local_evaluator_readiness" not in data:
            data["local_evaluator_readiness"] = data.get(
                "locally_evaluator_ready", False
            )
        if "package_path_if_verified" not in data:
            data["package_path_if_verified"] = data.get("package_path", "")
        return data


class RunCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requested: int = Field(default=0, ge=0)
    selected: int = Field(default=0, ge=0)
    generation_attempts: int = Field(default=0, ge=0)
    candidates_created: int = Field(default=0, ge=0)
    created_this_invocation: int = Field(default=0, ge=0)
    resumed_existing: int = Field(default=0, ge=0)
    adapter_taskirs_rederived: int = Field(default=0, ge=0)
    source_provenance_equivalence_attested: int = Field(default=0, ge=0)
    reports_locally_revalidated_and_superseded: int = Field(default=0, ge=0)
    accepted: int = Field(default=0, ge=0)
    packaged: int = Field(default=0, ge=0)
    exported: int = Field(default=0, ge=0)
    rejected: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    blocked: int = Field(default=0, ge=0)
    in_progress: int = Field(default=0, ge=0)
    stale: int = Field(default=0, ge=0)
    quality_rejected: int = Field(default=0, ge=0)
    infrastructure_failed: int = Field(default=0, ge=0)
    protocol_failed: int = Field(default=0, ge=0)
    pending_adjudication: int = Field(default=0, ge=0)
    operationally_blocked: int = Field(default=0, ge=0)
    runtime_certified: int = Field(default=0, ge=0)
    runtime_difficulty_certified: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _bounded_by_roster(self) -> "RunCounts":
        for field in (
            "selected",
            "candidates_created",
            "created_this_invocation",
            "resumed_existing",
            "adapter_taskirs_rederived",
            "source_provenance_equivalence_attested",
            "accepted",
            "packaged",
            "exported",
            "rejected",
            "failed",
            "blocked",
            "in_progress",
            "stale",
            "quality_rejected",
            "infrastructure_failed",
            "protocol_failed",
            "pending_adjudication",
            "operationally_blocked",
            "runtime_certified",
            "runtime_difficulty_certified",
        ):
            if getattr(self, field) > self.requested:
                raise ValueError(f"{field} cannot exceed requested candidates")
        if self.packaged != self.exported:
            raise ValueError("packaged and exported compatibility counts must agree")
        return self


class PipelineReadinessReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = READINESS_SCHEMA_VERSION
    run_id: str
    generated_at: str
    state: str
    profile: ReadinessProfile
    config_sha256: str
    config: GenerationRunSpec
    repository: dict[str, Any] = Field(default_factory=dict)
    source_manifest_sha256: str = ""
    selected_manifest_path: str = ""
    active_source_manifest_sha256: str = ""
    active_selected_manifest_path: str = ""
    generator_revalidation_receipt: str = ""
    identity_migration_sha256: str = ""
    counts: RunCounts
    costs: dict[str, Any] = Field(default_factory=dict)
    tasks: tuple[TaskReadiness, ...]
    blockers: tuple[str, ...] = ()


class ConfiguredStageSeal(BaseModel):
    """Content-addressed configuration inputs for one reusable PASS row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["configured-stage-inputs-v3"] = (
        CONFIGURED_STAGE_SEAL_VERSION
    )
    task_id: str
    task_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: StageName
    inputs: dict[str, Any]
    inputs_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _read_bounded_regular_bytes(
    path: Path, *, label: str, max_bytes: int
) -> bytes:
    """Read one small identity-bearing file without following its leaf symlink."""

    source = Path(path)
    try:
        before = source.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is missing or unreadable: {source}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size > max_bytes
    ):
        raise ValueError(
            f"{label} must be a bounded regular non-symlink file: {source}"
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ValueError(f"{label} is unreadable: {source}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size > max_bytes
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValueError(f"{label} changed while it was opened")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(1 << 20, max_bytes + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"{label} exceeds its safety bound")
        finished = os.fstat(descriptor)
        try:
            after = source.lstat()
        except OSError as exc:
            raise ValueError(f"{label} changed while it was read") from exc
        if (
            (finished.st_dev, finished.st_ino) != (opened.st_dev, opened.st_ino)
            or finished.st_size != opened.st_size
            or finished.st_mtime_ns != opened.st_mtime_ns
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or total != opened.st_size
        ):
            raise ValueError(f"{label} changed while it was read")
        return b"".join(chunks)
    except OSError as exc:
        raise ValueError(f"{label} is unreadable: {source}") from exc
    finally:
        os.close(descriptor)


def _admission_reference_identity(reference: str) -> tuple[str, str]:
    """Return resolved path and byte digest for an explicit admission record."""

    if not reference:
        return "", ""
    source = Path(reference)
    try:
        resolved_before = source.resolve(strict=True)
    except OSError as exc:
        raise ValueError(
            f"admission reference is missing or unreadable: {source}"
        ) from exc
    payload = _read_bounded_regular_bytes(
        source,
        label="admission reference",
        max_bytes=_MAX_CONFIGURED_REFERENCE_BYTES,
    )
    try:
        resolved_after = source.resolve(strict=True)
    except OSError as exc:
        raise ValueError("admission reference changed while it was read") from exc
    if resolved_after != resolved_before:
        raise ValueError("admission reference changed while it was read")
    return resolved_before.as_posix(), hashlib.sha256(payload).hexdigest()


def _module_source_sha256(module_name: str) -> str:
    """Digest one repository-owned module by logical name without importing it."""

    prefix = "elt_taskgen."
    if not module_name.startswith(prefix):
        raise ValueError(f"configured implementation module is not local: {module_name}")
    relative = module_name.removeprefix(prefix).replace(".", "/") + ".py"
    package_root = Path(__file__).resolve().parent
    path = package_root / relative
    payload = _read_bounded_regular_bytes(
        path,
        label=f"configured implementation module {module_name}",
        max_bytes=_MAX_CONFIGURED_REFERENCE_BYTES,
    )
    return hashlib.sha256(payload).hexdigest()


def _configured_stage_implementation_identity(
    spec: GenerationRunSpec, stage: StageName
) -> dict[str, Any]:
    """Return the current executable contract for one configured stage."""

    roles = _CONFIGURED_STAGE_ROLES.get(stage, ())
    if stage is StageName.CALIBRATE:
        from elt_taskgen.corpus import calibration as calibration_mod

        roster = calibration_mod.load_calibration_roster(
            Path(spec.agents_config) if spec.agents_config else None
        )
        roles = tuple(sorted(tier.role_name for tier in roster))
    modules = set(_CONFIGURED_STAGE_MODULES.get(stage, ()))
    # The CLI digest covers stage runners still defined in that module.
    modules.add("elt_taskgen.cli")
    modules = tuple(sorted(modules))
    contract = {
        "version": CONFIGURED_STAGE_CONTRACT_VERSION,
        "stage": stage.value,
        "roles": list(roles),
        "modules": list(modules),
    }
    role_digests: dict[str, str] = {}
    if roles:
        from elt_taskgen.review import providers as providers_mod

        role_digests = {
            role: providers_mod.role_behavior_sha256(
                role,
                agents_config=(spec.agents_config or None),
            )
            for role in roles
        }
    module_digests = {
        module: _module_source_sha256(module) for module in modules
    }
    implementation = {
        "contract_sha256": hashlib.sha256(
            canonical_json(contract).encode("utf-8")
        ).hexdigest(),
        "role_behavior_sha256": role_digests,
        "module_source_sha256": module_digests,
    }
    implementation["implementation_sha256"] = hashlib.sha256(
        canonical_json(implementation).encode("utf-8")
    ).hexdigest()
    return implementation


def _configured_inputs(
    spec: GenerationRunSpec, stage: StageName
) -> dict[str, Any] | None:
    """Only inputs capable of changing a stage's substantive evidence."""

    inputs: dict[str, Any] = {"contract_version": CONFIGURED_STAGE_SEAL_VERSION}
    if stage in _PROVIDER_STAGES:
        from elt_taskgen.review import providers as providers_mod

        inputs["agents_config_sha256"] = providers_mod.agents_config_sha256(
            agents_config=(spec.agents_config or None)
        )
        inputs["agent_harness"] = spec.agent_harness
        (
            inputs["admission_reference_path"],
            inputs["admission_reference_sha256"],
        ) = _admission_reference_identity(spec.admission_reference)
        if stage in {
            StageName.TASK_INTEGRITY,
            StageName.GATES_EXTRACT_LOAD,
            StageName.GATES_TRANSFORM,
            StageName.CALIBRATE,
        }:
            inputs["destination"] = spec.destination
            if spec.extra_destinations:
                inputs["extra_destinations"] = list(spec.extra_destinations)
        if stage is StageName.CALIBRATE:
            inputs["empirical"] = spec.empirical
    if stage in {StageName.CONTAMINATION_PRE, StageName.CONTAMINATION_POST}:
        from elt_taskgen.verification import contamination

        required = contamination.required_coverage_from_env()
        inputs.update(
            {
                "contamination_enforcement": contamination.enforcement().value,
                "required_coverage": required.value if required is not None else "",
            }
        )
    if stage in _SELECTION_STAGES:
        inputs.update(
            {
                "candidate_count": spec.candidate_count,
                "source_families": list(spec.source_families),
                "source_allocation": spec.source_allocation,
                "selection_seed": spec.seed,
                "empirical": spec.empirical,
            }
        )
    inputs.update(_configured_stage_implementation_identity(spec, stage))
    return inputs


def _configured_seal(
    task: TaskIR, stage: StageName, spec: GenerationRunSpec
) -> ConfiguredStageSeal | None:
    inputs = _configured_inputs(spec, stage)
    if inputs is None:
        return None
    digest = hashlib.sha256(canonical_json(inputs).encode("utf-8")).hexdigest()
    return ConfiguredStageSeal(
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        stage=stage,
        inputs=inputs,
        inputs_sha256=digest,
    )


def _configured_seal_path(
    engine: Engine, seal: ConfiguredStageSeal
) -> Path:
    return (
        engine.task_dir(seal.task_id)
        / "reports"
        / "configured_inputs"
        / seal.stage.value
        / f"{seal.task_content_hash}.{seal.inputs_sha256}.json"
    )


def seal_configured_stage(
    engine: Engine,
    task: TaskIR,
    stage: StageName,
    spec: GenerationRunSpec,
) -> Path | None:
    """Persist the exact config contract for a validated current stage."""

    seal = _configured_seal(task, stage, spec)
    if seal is None:
        return None
    path = _configured_seal_path(engine, seal)
    payload = (
        json.dumps(seal.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if path.exists():
        if path.is_symlink() or path.read_bytes() != payload:
            raise ValueError(f"configured stage seal is unsafe or changed: {path}")
        return path
    _atomic_write(path, payload)
    return path


def _configured_seal_evidence(
    engine: Engine,
    task: TaskIR,
    stage: StageName,
    spec: GenerationRunSpec,
) -> tuple[str, str]:
    try:
        expected = _configured_seal(task, stage, spec)
    except (OSError, TypeError, ValueError) as exc:
        return "", f"configured stage inputs are unreadable: {exc}"
    if expected is None:
        return "", ""
    path = _configured_seal_path(engine, expected)
    try:
        observed = ConfiguredStageSeal.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        return "", f"configured stage seal is missing or invalid: {exc}"
    if observed != expected:
        return "", "configured stage inputs changed"
    return _rel(engine.workspace, path), ""


def _rel(workspace: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _population_evidence(engine: Engine, task: TaskIR) -> tuple[tuple[str, ...], str]:
    from elt_taskgen.generation import source_data

    evidence: list[str] = []
    root = engine.task_dir(task.task_id) / "populations"
    for spec in sorted(task.populations, key=lambda item: item.name.value):
        pop_dir = root / spec.name.value
        if not pop_dir.is_dir():
            return tuple(evidence), f"population {spec.name.value!r} is missing"
        verifier = getattr(source_data, "verify_population_manifest", None)
        if callable(verifier):
            try:
                verifier(task, spec.name, pop_dir)
            except (OSError, TypeError, ValueError) as exc:
                return tuple(evidence), f"population {spec.name.value!r}: {exc}"
        else:
            try:
                drift = source_data.population_drift(task, spec.name, pop_dir)
            except (OSError, TypeError, ValueError) as exc:
                return tuple(evidence), f"population {spec.name.value!r}: {exc}"
            if drift:
                return tuple(evidence), (
                    f"population {spec.name.value!r} is stale: {drift[0]}"
                )
        evidence.append(_rel(engine.workspace, pop_dir))
    return tuple(evidence), ""


def _evidence_agents_config(
    engine: Engine, spec: GenerationRunSpec | None
) -> Path | str | None:
    """Return the agents document associated with reusable evidence."""

    if spec is not None and spec.agents_config:
        return spec.agents_config
    return getattr(engine, "agents_config", None)


def _current_review_bindings(
    role_name: str,
    *,
    entry_schema: int,
    agents_config: Path | str | None,
) -> dict[str, str]:
    """Current executable critic identity for one durable exchange row."""

    from elt_taskgen.review import providers as providers_mod

    manifest = providers_mod.role_behavior_manifest(
        role_name, agents_config=agents_config
    )
    if entry_schema >= providers_mod.SESSION_TRANSCRIPT_ENTRY_SCHEMA:
        from elt_taskgen.review.tools import critic_validators as critic_tools

        if role_name in critic_tools.CRITIC_VALIDATOR_ROLES:
            policy = critic_tools.critic_policy(
                role_name,
                critic_tools.critic_limits(
                    role_name, agents_config=agents_config
                ),
                agents_config=agents_config,
            )
        else:
            policy = providers_mod.session_policy_for(
                role_name, agents_config=agents_config
            )
        tools_sha256 = policy.tools_sha256()
        policy_sha256 = policy.sha256()
    else:
        tools_sha256 = hashlib.sha256(
            canonical_json(manifest["tools"]).encode("utf-8")
        ).hexdigest()
        policy_sha256 = str(manifest["policy_sha256"])
    return {
        "behavior_sha256": providers_mod.role_behavior_sha256(
            role_name, agents_config=agents_config
        ),
        "tools_sha256": tools_sha256,
        "policy_sha256": policy_sha256,
        "diagnostics_version": str(providers_mod.DIAGNOSTICS_VERSION),
    }


def _resolve_recorded_response(
    role_dir: "Path", prompt_sha: str, response_sha: str, *, role_name: str
) -> dict | None:
    """Resolve the turn or trajectory record named by ``response_sha256``.

    Bounded sessions bind the full trajectory, while one-shot seats bind their
    turn entry. Displaced entries remain valid candidates.
    """
    displaced = role_dir / f"{prompt_sha}.{response_sha[:16]}.json"
    try:
        entry = _json(displaced)
    except (OSError, ValueError):
        entry = None
    if isinstance(entry, dict) and entry.get("response_sha256") == response_sha:
        return entry
    trajectories = role_dir.parent / "trajectories" / role_name
    try:
        candidates = sorted(trajectories.glob("*.json"))
    except OSError:
        return None
    for candidate in candidates:
        try:
            record = _json(candidate)
        except (OSError, ValueError):
            continue
        if (
            record.get("prompt_sha256") == prompt_sha
            and record.get("response_sha256") == response_sha
        ):
            return record
    return None


def _stage_owned_evidence(
    engine: Engine,
    task: TaskIR,
    stage: StageName,
    row,
    *,
    spec: GenerationRunSpec | None = None,
) -> tuple[tuple[str, ...], str]:
    """Validate a stage's durable outputs; empty reason means current."""

    tdir = engine.task_dir(task.task_id)
    if stage is StageName.INTAKE:
        task_path = tdir / "task_ir.json"
        if not task_path.is_file():
            return (), "registered TaskIR is missing"
        try:
            if engine.load_task(task.task_id).content_hash() != task.content_hash():
                return (), "registered TaskIR content hash changed"
        except Exception as exc:  # noqa: BLE001 - readiness must fail closed
            return (), f"registered TaskIR is unreadable: {exc}"
        evidence = [_rel(engine.workspace, task_path)]
        if task.origin.value in {"dbt", "dlt", "synsql", "schemapile", "wikidbs"}:
            from elt_taskgen import provenance

            try:
                record = provenance.load_current(tdir, task=task, required=True)
            except (OSError, TypeError, ValueError) as exc:
                return tuple(evidence), f"source provenance is missing or invalid: {exc}"
            assert record is not None
            record_path = (
                tdir
                / provenance.INGEST_PROVENANCE_DIRNAME
                / (
                    f"{record.task_content_hash}."
                    f"{record.evidence_digest()}.json"
                )
            )
            evidence.append(_rel(engine.workspace, record_path))
        return tuple(evidence), ""
    if stage is StageName.GENERATE:
        return _population_evidence(engine, task)
    if stage is StageName.REFERENCE:
        from elt_taskgen.reference import gold as gold_mod

        answer_key = tdir / "answer_key"
        try:
            gold = gold_mod.load_gold(answer_key)
        except (OSError, TypeError, ValueError) as exc:
            return (), f"private gold is missing or invalid: {exc}"
        if gold.task_content_hash != task.content_hash():
            return (), "private gold is bound to a different task content hash"
        return (_rel(engine.workspace, answer_key / "manifest.json"),), ""
    if stage is StageName.AUTHOR:
        from elt_taskgen.review import providers as providers_mod

        prose = task.solver_prompt or ""
        if not prose.strip() or prose.strip().lower() in {
            "todo",
            "tbd",
            "placeholder",
        }:
            return (), "solver-facing specification is blank or placeholder text"
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError):
            return (), "author payload is unreadable"
        data = payload.get("data") or {}
        recorded = str(data.get("prose_sha256") or "")
        actual = hashlib.sha256(prose.encode("utf-8")).hexdigest()
        if recorded != actual:
            return (), "solver-facing specification does not match author evidence"
        expected_behavior = providers_mod.role_behavior_sha256(
            "semantic_author",
            agents_config=_evidence_agents_config(engine, spec),
        )
        if str(data.get("behavior_sha256") or "") != expected_behavior:
            return (), "author evidence does not match current semantic-author behavior"
        return (), ""
    if stage is StageName.REVIEW:
        from elt_taskgen.review import providers as providers_mod

        path = tdir / "reports" / "review_transcript_manifest.json"
        try:
            manifest = _json(path)
        except (OSError, ValueError) as exc:
            return (), f"review transcript manifest is missing or invalid: {exc}"
        if manifest.get("task_content_hash") != task.content_hash():
            return (), "review transcript manifest is stale"
        if manifest.get("integrity_problems"):
            return (), "review transcript manifest records integrity problems"
        roles = manifest.get("roles")
        expected_roles = {
            "ambiguity_critic",
            "population_adversary",
            "shortcut_attacker",
            "feasibility_reviewer",
        }
        if not isinstance(roles, list) or not roles:
            return (), "review transcript manifest has no attributable roles"
        observed_roles = [
            str(role.get("role") or "")
            for role in roles
            if isinstance(role, dict)
        ]
        if len(observed_roles) != len(roles) or set(observed_roles) != expected_roles:
            return (), "review transcript manifest does not contain the exact critic roster"
        if len(observed_roles) != len(set(observed_roles)):
            return (), "review transcript manifest contains duplicate critic roles"
        evidence = [_rel(engine.workspace, path)]
        agents_config = _evidence_agents_config(engine, spec)
        for role in roles:
            if not isinstance(role, dict):
                return tuple(evidence), "review role evidence is not an object"
            role_name = str(role.get("role") or "")
            prompt_sha = str(role.get("prompt_sha256") or "")
            response_sha = str(role.get("response_sha256") or "")
            try:
                entry_schema = int(role.get("entry_schema") or 0)
            except (TypeError, ValueError):
                return tuple(evidence), f"review role {role_name!r} has invalid entry_schema"
            expected_bindings = _current_review_bindings(
                role_name,
                entry_schema=entry_schema,
                agents_config=agents_config,
            )
            for field, expected in expected_bindings.items():
                if str(role.get(field) or "") != expected:
                    return tuple(evidence), (
                        f"review role {role_name!r} {field} does not match "
                        "current critic behavior"
                    )
            row_problems = providers_mod.exchange_row_problems(dict(role))
            if row_problems:
                return tuple(evidence), (
                    f"review role {role_name!r} exchange evidence is invalid: "
                    + "; ".join(row_problems[:3])
                )
            transcript = engine.workspace / "transcripts" / role_name / f"{prompt_sha}.json"
            try:
                recorded = _json(transcript)
            except (OSError, ValueError) as exc:
                return tuple(evidence), (
                    f"review transcript {role_name!r}/{prompt_sha[:12]} is "
                    f"missing or invalid: {exc}"
                )
            if recorded.get("response_sha256") != response_sha:
                # A repeated prompt may replace its keyed response. Resolve the
                # preserved response by digest before declaring evidence stale.
                resolved = _resolve_recorded_response(
                    transcript.parent, prompt_sha, response_sha, role_name=role_name
                )
                if resolved is None:
                    return tuple(evidence), (
                        f"review transcript {role_name!r} digest changed"
                    )
                recorded = resolved
            route = recorded.get("route")
            if not isinstance(route, Mapping):
                return tuple(evidence), f"review transcript {role_name!r} has no route binding"
            for field, expected in expected_bindings.items():
                if str(route.get(field) or "") != expected:
                    return tuple(evidence), (
                        f"review transcript {role_name!r} {field} does not match "
                        "its current executable behavior"
                    )
            evidence.append(_rel(engine.workspace, transcript))
        return tuple(evidence), ""
    if stage is StageName.ATTACK:
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError):
            return (), "attack payload is unreadable"
        cases = payload.get("cases")
        if not isinstance(cases, list) or not cases:
            return (), "attack stage has no executed case roster"
        evidence: list[str] = []
        for case in cases:
            path = tdir / "attacks" / str(case) / "rewards.json"
            try:
                record = _json(path)
            except (OSError, ValueError) as exc:
                return tuple(evidence), f"attack {case!r} evidence is invalid: {exc}"
            if record.get("task_content_hash") != task.content_hash():
                return tuple(evidence), f"attack {case!r} evidence is stale"
            evidence.append(_rel(engine.workspace, path))
        return tuple(evidence), ""
    if stage is StageName.TASK_INTEGRITY:
        acceptance = tdir / "reports" / "acceptance_full.json"
        public = tdir / "task"
        if not acceptance.is_file():
            return (), "task-integrity acceptance evidence is missing"
        if not public.is_dir():
            return (_rel(engine.workspace, acceptance),), "public task bundle is missing"
        try:
            payload = _json(acceptance)
        except (OSError, ValueError) as exc:
            return (), f"task-integrity acceptance evidence is invalid: {exc}"
        if payload.get("task_content_hash") != task.content_hash():
            return (), "task-integrity acceptance evidence is stale"
        return (
            _rel(engine.workspace, acceptance),
            _rel(engine.workspace, public),
        ), ""
    if stage in {StageName.GATES_EXTRACT_LOAD, StageName.GATES_TRANSFORM}:
        variant = (
            RLVR_TASK_VARIANTS[0]
            if stage is StageName.GATES_EXTRACT_LOAD
            else RLVR_TASK_VARIANTS[1]
        )
        out = tdir / "variants" / variant.value
        if not (out / "task").is_dir() or not (out / "reward.json").is_file():
            return (), f"accepted {variant.value} bundle is missing"
        acceptance = tdir / "reports" / f"acceptance_{variant.value}.json"
        try:
            payload = _json(acceptance)
        except (OSError, ValueError) as exc:
            return (), f"{variant.value} acceptance evidence is invalid: {exc}"
        if payload.get("task_content_hash") != task.content_hash():
            return (), f"{variant.value} acceptance evidence is stale"
        return (
            _rel(engine.workspace, out),
            _rel(engine.workspace, acceptance),
        ), ""
    if stage is StageName.CALIBRATE:
        path = tdir / "reports" / "difficulty.json"
        try:
            payload = _json(path)
        except (OSError, ValueError) as exc:
            return (), f"difficulty evidence is missing or invalid: {exc}"
        if payload.get("task_content_hash", task.content_hash()) != task.content_hash():
            return (), "difficulty evidence is stale"
        return (_rel(engine.workspace, path),), ""
    if stage is StageName.CONTAMINATION_POST:
        path = tdir / "reports" / "contamination_post.json"
        try:
            payload = _json(path)
        except (OSError, ValueError) as exc:
            return (), f"post-contamination evidence is missing or invalid: {exc}"
        if payload.get("task_content_hash") != task.content_hash():
            return (), "post-contamination evidence is stale"
        return (_rel(engine.workspace, path),), ""
    if stage is StageName.SELECT:
        path = tdir / "reports" / "selection.json"
        try:
            selected = _json(path)
            ledger_payload = json.loads(row.payload_json)
        except (OSError, TypeError, ValueError) as exc:
            return (), f"selection evidence is missing or invalid: {exc}"
        if selected != ledger_payload:
            return (), "selection evidence differs from the append-only ledger"
        selected_ids = set(selected.get("train") or ()) | set(
            selected.get("val") or ()
        )
        if task.task_id not in selected_ids:
            return (), "selection evidence does not include this task"
        hashes = selected.get("task_content_hashes")
        if not isinstance(hashes, dict) or hashes.get(task.task_id) != task.content_hash():
            return (), "selection evidence is not bound to this task identity"
        return (_rel(engine.workspace, path),), ""
    if stage is StageName.RELEASE:
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError):
            payload = {}
        data = payload.get("data") if isinstance(payload, dict) else None
        declared = str((data or {}).get("release_dir") or "release")
        root = Path(declared)
        if not root.is_absolute():
            root = engine.workspace / root
        from elt_taskgen.export import release as release_mod

        verification = release_mod.verify_release(root)
        if not verification.ok:
            return (), (
                "release is missing or invalid: "
                + "; ".join(verification.failures[:3])
            )
        try:
            manifest = release_mod.ReleaseManifest.model_validate_json(
                (root / "release_manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            return (), f"release manifest is invalid: {exc}"
        if manifest.tasks.get(task.task_id) != task.content_hash():
            return (), "release manifest does not bind this task identity"
        return (_rel(engine.workspace, root / "release_manifest.json"),), ""
    # Contamination-pre, task-integrity and audit currently carry their complete
    # structured result in the append-only report itself.
    return (), ""


def stage_readiness(
    engine: Engine,
    task: TaskIR,
    stage: StageName,
    *,
    spec: GenerationRunSpec | None = None,
) -> StageReadiness:
    row = engine.latest_report(task.task_id, stage.value)
    if row is None:
        return StageReadiness(stage=stage, state=ReadinessState.NOT_RUN)
    payload = _payload_mapping(row)
    if row.content_hash != task.content_hash():
        failure_class, failure_code, blocked_on = _failure_descriptor(
            verdict=row.verdict,
            payload=payload,
            stale_code="content_hash_mismatch",
        )
        return StageReadiness(
            stage=stage,
            state=ReadinessState.STALE,
            report_id=row.id,
            verdict=row.verdict,
            reason="report is bound to an older task content hash",
            failure_class=failure_class,
            failure_code=failure_code,
            blocked_on=blocked_on,
        )
    if engine.report_is_superseded(task, row):
        failure_class, failure_code, blocked_on = _failure_descriptor(
            verdict=row.verdict,
            payload=payload,
            stale_code="report_superseded",
        )
        return StageReadiness(
            stage=stage,
            state=ReadinessState.STALE,
            report_id=row.id,
            verdict=row.verdict,
            reason="report was explicitly superseded by a verified code migration",
            failure_class=failure_class,
            failure_code=failure_code,
            blocked_on=blocked_on,
        )
    if row.verdict == VERDICT_BLOCKED:
        reason = str(payload.get("error") or payload.get("detail") or "")
        if not payload:
            reason = "blocked report payload is unreadable"
        failure_class, failure_code, blocked_on = _failure_descriptor(
            verdict=row.verdict, payload=payload
        )
        return StageReadiness(
            stage=stage,
            state=ReadinessState.BLOCKED,
            report_id=row.id,
            verdict=row.verdict,
            reason=reason,
            failure_class=failure_class,
            failure_code=failure_code,
            blocked_on=blocked_on,
        )
    if row.verdict in {VERDICT_FAIL, VERDICT_FATAL}:
        reason = str(payload.get("error") or payload.get("detail") or "")
        if not payload:
            reason = "failure report payload is unreadable"
        failure_class, failure_code, blocked_on = _failure_descriptor(
            verdict=row.verdict, payload=payload
        )
        return StageReadiness(
            stage=stage,
            state=ReadinessState.FAIL,
            report_id=row.id,
            verdict=row.verdict,
            reason=reason,
            failure_class=failure_class,
            failure_code=failure_code,
            blocked_on=blocked_on,
        )
    current, why = engine.report_is_current(task, stage, row)
    if not current:
        failure_class, failure_code, blocked_on = _failure_descriptor(
            verdict=row.verdict,
            payload=payload,
            stale_code="report_currency_mismatch",
        )
        return StageReadiness(
            stage=stage,
            state=ReadinessState.STALE,
            report_id=row.id,
            verdict=row.verdict,
            reason=why,
            failure_class=failure_class,
            failure_code=failure_code,
            blocked_on=blocked_on,
        )
    evidence, problem = _stage_owned_evidence(
        engine, task, stage, row, spec=spec
    )
    if problem:
        failure_class, failure_code, blocked_on = _failure_descriptor(
            verdict=row.verdict,
            payload=payload,
            stale_code="owned_evidence_mismatch",
        )
        return StageReadiness(
            stage=stage,
            state=ReadinessState.STALE,
            report_id=row.id,
            verdict=row.verdict,
            evidence=evidence,
            reason=problem,
            failure_class=failure_class,
            failure_code=failure_code,
            blocked_on=blocked_on,
        )
    if spec is not None:
        seal_evidence, seal_problem = _configured_seal_evidence(
            engine, task, stage, spec
        )
        if seal_problem:
            failure_class, failure_code, blocked_on = _failure_descriptor(
                verdict=row.verdict,
                payload=payload,
                stale_code="configured_input_mismatch",
            )
            return StageReadiness(
                stage=stage,
                state=ReadinessState.STALE,
                report_id=row.id,
                verdict=row.verdict,
                evidence=evidence,
                reason=seal_problem,
                failure_class=failure_class,
                failure_code=failure_code,
                blocked_on=blocked_on,
            )
        if seal_evidence:
            evidence = (*evidence, seal_evidence)
    return StageReadiness(
        stage=stage,
        state=ReadinessState.PASS,
        report_id=row.id,
        verdict=row.verdict,
        evidence=evidence,
    )


def stale_passes(
    engine: Engine,
    task: TaskIR,
    *,
    through: StageName,
    spec: GenerationRunSpec | None = None,
) -> tuple[StageReadiness, ...]:
    """PASS ledger rows whose owned evidence cannot be reused."""

    result: list[StageReadiness] = []
    for stage in STAGE_ORDER:
        check = stage_readiness(engine, task, stage, spec=spec)
        row = engine.latest_report(task.task_id, stage.value)
        if check.state is ReadinessState.STALE and row is not None and row.verdict == VERDICT_PASS:
            result.append(check)
        if stage is through:
            break
    return tuple(result)


def _task_disposition(stages: tuple[StageReadiness, ...], final: str) -> tuple[str, str]:
    blocking = next((stage for stage in stages if stage.state is ReadinessState.BLOCKED), None)
    if blocking is not None:
        return "blocked", f"{blocking.stage.value}: {blocking.reason}"
    failed = next((stage for stage in stages if stage.state is ReadinessState.FAIL), None)
        # Keep a current fatal as the task outcome; count stale passes separately.
    if final == FINAL_REJECTED:
        return "rejected", (f"{failed.stage.value}: {failed.reason}" if failed else "rejected")
    stale = next((stage for stage in stages if stage.state is ReadinessState.STALE), None)
    if stale is not None:
        return "failed", f"{stale.stage.value} is stale: {stale.reason}"
    if failed is not None:
        return "failed", f"{failed.stage.value}: {failed.reason}"
    if final == FINAL_ACCEPTED:
        return "accepted", ""
    return "in_progress", ""


def _task_failure(
    stages: tuple[StageReadiness, ...], final: str
) -> tuple[ReadinessFailureClass, str, str]:
    """Select the causal readiness class without rewriting ledger history."""

    pending = next(
        (
            stage
            for stage in stages
            if stage.failure_class is ReadinessFailureClass.PENDING_ADJUDICATION
        ),
        None,
    )
    if pending is not None:
        return pending.failure_class, pending.failure_code, pending.blocked_on
    blocked = next(
        (stage for stage in stages if stage.state is ReadinessState.BLOCKED), None
    )
    if blocked is not None:
        return (
            blocked.failure_class or ReadinessFailureClass.OPERATIONAL_BLOCKING,
            blocked.failure_code,
            blocked.blocked_on,
        )
    failed = next(
        (stage for stage in stages if stage.state is ReadinessState.FAIL), None
    )
    if final == FINAL_REJECTED:
        if failed is not None and failed.failure_class is not ReadinessFailureClass.NONE:
            return failed.failure_class, failed.failure_code, failed.blocked_on
        return ReadinessFailureClass.QUALITY_REJECTION, "task_rejected", ""
    stale = next(
        (stage for stage in stages if stage.state is ReadinessState.STALE), None
    )
    if stale is not None:
        return stale.failure_class, stale.failure_code, stale.blocked_on
    if failed is not None:
        return failed.failure_class, failed.failure_code, failed.blocked_on
    return ReadinessFailureClass.NONE, "", ""


_MAX_RUNTIME_DIFFICULTY_BYTES = 16 * 1024 * 1024


def _read_runtime_difficulty_bytes(path: Path) -> bytes:
    """Read one bounded regular report without following or racing a symlink."""

    return _read_bounded_regular_bytes(
        Path(path),
        label="runtime-certified difficulty report",
        max_bytes=_MAX_RUNTIME_DIFFICULTY_BYTES,
    )


def _configured_runtime_evidence(
    engine: Engine,
    task: TaskIR,
    *,
    package_path: Path | None,
    spec: GenerationRunSpec | None,
) -> RuntimeEvidenceReadiness:
    """Verify and reconstruct existing runtime evidence without external actions."""

    if spec is None or task.task_id not in spec.runtime_difficulty_reports:
        return RuntimeEvidenceReadiness()
    configured_report = Path(spec.runtime_difficulty_reports[task.task_id])
    if package_path is None:
        reason = "configured runtime evidence requires a verified release path"
        return RuntimeEvidenceReadiness(
            configured=True,
            certification_reason=reason,
            difficulty_reason=reason,
        )

    release_dir = Path(package_path)
    certification_store = Path(spec.runtime_certification_store)
    try:
        from elt_taskgen.runtime import certification_lifecycle as lifecycle_mod

        lifecycle = lifecycle_mod.verify_completed_lifecycle(
            release_dir=release_dir,
            task_id=task.task_id,
            certification_store=certification_store,
        )
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        reason = f"runtime certification verification failed: {exc}"
        return RuntimeEvidenceReadiness(
            configured=True,
            certification_reason=reason,
            difficulty_reason="runtime certification did not verify",
        )

    lifecycle_path = (
        certification_store
        / lifecycle.certification_id
        / lifecycle_mod.LIFECYCLE_FILENAME
    )
    certification_evidence = (_rel(engine.workspace, lifecycle_path),)
    certified = RuntimeEvidenceReadiness(
        configured=True,
        certification_verified=True,
        certification_id=lifecycle.certification_id,
        lifecycle_digest=lifecycle.lifecycle_digest,
        destination=lifecycle.destination,
        certification_evidence=certification_evidence,
    )

    # This builder verifies inputs and reconstructs a report; it publishes nothing.
    try:
        from elt_taskgen.corpus.certified_difficulty import (
            RuntimeCertifiedDifficulty,
            build_runtime_certified_difficulty,
            verify_certified_difficulty,
        )

        expected = build_runtime_certified_difficulty(
            release_dir=release_dir,
            certification_store=certification_store,
            task_id=task.task_id,
            workspace=engine.workspace,
        )
        if expected.task_content_hash != task.content_hash():
            raise ValueError(
                "runtime-certified difficulty is bound to another TaskIR hash"
            )
        observed = RuntimeCertifiedDifficulty.model_validate_json(
            _read_runtime_difficulty_bytes(configured_report)
        )
        observed = verify_certified_difficulty(observed)
        if observed != expected:
            raise ValueError(
                "configured runtime-certified difficulty report does not match "
                "the verified release/certification evidence"
            )
        if observed.runtime_lifecycle_digest != lifecycle.lifecycle_digest:
            raise ValueError(
                "runtime-certified difficulty names another lifecycle digest"
            )
    except (OSError, TypeError, UnicodeError, ValueError, RuntimeError) as exc:
        return certified.model_copy(
            update={
                "difficulty_reason": (
                    f"runtime-certified difficulty verification failed: {exc}"
                )
            }
        )

    return certified.model_copy(
        update={
            "difficulty_verified": True,
            "difficulty_evidence_digest": observed.evidence_digest,
            "empirical_band": observed.empirical_band,
            "difficulty_evidence": (_rel(engine.workspace, configured_report),),
        }
    )


_ZERO_MONEY = Decimal("0")
_MONEY_TOLERANCE = Decimal("0.000000001")


def _money_decimal(value: Any, *, label: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not a finite non-negative amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{label} is not a finite non-negative amount")
    return amount


def _field(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _empty_cost_accumulator() -> dict[str, Any]:
    return {
        "recorded_cost": _ZERO_MONEY,
        "reserved_cost": _ZERO_MONEY,
        "uncertain_cost": _ZERO_MONEY,
        "unclassified_pending_cost": _ZERO_MONEY,
        "final_request_overrun": _ZERO_MONEY,
        "committed_calls": 0,
        "reserved_calls": 0,
        "uncertain_calls": 0,
        "released_calls": 0,
    }


def _add_reservation_cost(accumulator: dict[str, Any], reservation: Any) -> None:
    state_value = _field(reservation, "state", "")
    state = str(getattr(state_value, "value", state_value)).strip().lower()
    estimated = _money_decimal(
        _field(reservation, "estimated_usd", 0.0),
        label="reservation estimated_usd",
    )
    if state == "committed":
        actual_raw = _field(reservation, "actual_usd")
        if actual_raw is None:
            raise ValueError("committed reservation is missing actual_usd")
        actual = _money_decimal(actual_raw, label="reservation actual_usd")
        accumulator["recorded_cost"] += actual
        accumulator["final_request_overrun"] += max(
            _ZERO_MONEY, actual - estimated
        )
        accumulator["committed_calls"] += 1
    elif state == "reserved":
        accumulator["reserved_cost"] += estimated
        accumulator["reserved_calls"] += 1
    elif state == "uncertain":
        accumulator["uncertain_cost"] += estimated
        accumulator["uncertain_calls"] += 1
    elif state == "released":
        accumulator["released_calls"] += 1
    else:
        raise ValueError(f"unknown durable reservation state: {state!r}")


def _cost_totals(accumulator: Mapping[str, Any]) -> CostTotals:
    reserved = Decimal(accumulator["reserved_cost"])
    uncertain = Decimal(accumulator["uncertain_cost"])
    unclassified = Decimal(accumulator["unclassified_pending_cost"])
    return CostTotals(
        recorded_cost=float(Decimal(accumulator["recorded_cost"])),
        reserved_cost=float(reserved),
        uncertain_cost=float(uncertain),
        unclassified_pending_cost=float(unclassified),
        reserved_or_uncertain_cost=float(reserved + uncertain + unclassified),
        final_request_overrun=float(
            Decimal(accumulator["final_request_overrun"])
        ),
        committed_calls=int(accumulator["committed_calls"]),
        reserved_calls=int(accumulator["reserved_calls"]),
        uncertain_calls=int(accumulator["uncertain_calls"]),
        released_calls=int(accumulator["released_calls"]),
    )


def _stage_for_role(role: str, mapping: Mapping[str, str]) -> str:
    if role in mapping:
        value = mapping[role]
        return value.value if isinstance(value, StageName) else str(value)
    if role.startswith("solver__"):
        return StageName.CALIBRATE.value
    return "unattributed"


def cost_breakdown_from_budget_snapshot(
    snapshot: Any,
    *,
    task_ids: tuple[str, ...],
    expected_run_id: str | None = None,
    role_stage_map: Mapping[str, str] | None = None,
) -> RunCostBreakdown:
    """Reconcile a cumulative cost report from one immutable budget snapshot."""

    if len(set(task_ids)) != len(task_ids):
        raise ValueError("task_ids must be an exact, duplicate-free roster")
    run_id = str(_field(snapshot, "run_id", ""))
    if not run_id:
        raise ValueError("budget snapshot is missing run_id")
    if expected_run_id is not None and run_id != expected_run_id:
        raise ValueError(
            f"budget snapshot run_id {run_id!r} differs from {expected_run_id!r}"
        )

    mapping: dict[str, str] = dict(DEFAULT_ROLE_STAGE_MAP)
    if role_stage_map is not None:
        mapping.update({str(key): value for key, value in role_stage_map.items()})

    roster = set(task_ids)
    reservations = tuple(_field(snapshot, "reservations", ()) or ())
    seen_calls: set[str] = set()
    global_acc = _empty_cost_accumulator()
    task_acc = {task_id: _empty_cost_accumulator() for task_id in task_ids}
    role_acc: dict[str, dict[str, Any]] = {}
    stage_acc: dict[str, dict[str, Any]] = {}
    task_role_acc: dict[str, dict[str, dict[str, Any]]] = {
        task_id: {} for task_id in task_ids
    }
    task_stage_acc: dict[str, dict[str, dict[str, Any]]] = {
        task_id: {} for task_id in task_ids
    }
    serialized_rows: list[dict[str, Any]] = []

    for reservation in reservations:
        task_id = str(_field(reservation, "task_id", ""))
        role = str(_field(reservation, "role", ""))
        call_id = str(_field(reservation, "call_id", ""))
        if task_id not in roster:
            raise ValueError(
                "durable budget ledger contains a call outside the exact run "
                f"roster: {task_id!r}"
            )
        if not role or not call_id:
            raise ValueError("durable reservation is missing role or call_id")
        if call_id in seen_calls:
            raise ValueError(f"duplicate durable reservation call_id: {call_id!r}")
        seen_calls.add(call_id)
        stage = _stage_for_role(role, mapping)

        for accumulator in (
            global_acc,
            task_acc[task_id],
            role_acc.setdefault(role, _empty_cost_accumulator()),
            stage_acc.setdefault(stage, _empty_cost_accumulator()),
            task_role_acc[task_id].setdefault(role, _empty_cost_accumulator()),
            task_stage_acc[task_id].setdefault(
                stage, _empty_cost_accumulator()
            ),
        ):
            _add_reservation_cost(accumulator, reservation)

        if hasattr(reservation, "as_dict"):
            row = dict(reservation.as_dict())
        elif isinstance(reservation, Mapping):
            row = dict(reservation)
        else:
            row = {
                name: _field(reservation, name)
                for name in (
                    "run_id",
                    "call_id",
                    "task_id",
                    "role",
                    "estimated_usd",
                    "actual_usd",
                    "state",
                    "created_at",
                    "updated_at",
                )
            }
            row["state"] = str(getattr(row["state"], "value", row["state"]))
        row["attributed_stage"] = stage
        serialized_rows.append(row)

    global_totals = _cost_totals(global_acc)
    expected_values = {
        "committed_usd": Decimal(str(global_totals.recorded_cost)),
        "reserved_usd": Decimal(str(global_totals.reserved_cost)),
        "uncertain_usd": Decimal(str(global_totals.uncertain_cost)),
    }
    for name, observed in expected_values.items():
        snapshot_value = _money_decimal(_field(snapshot, name, 0.0), label=name)
        if abs(snapshot_value - observed) > _MONEY_TOLERANCE:
            raise ValueError(
                f"durable {name} does not reconcile: snapshot={snapshot_value}, "
                f"reservation rows={observed}"
            )

    count_expectations = {
        "reservation_count": len(reservations),
        "committed_count": global_totals.committed_calls,
        "reserved_count": global_totals.reserved_calls,
        "uncertain_count": global_totals.uncertain_calls,
        "released_count": global_totals.released_calls,
    }
    for name, expected in count_expectations.items():
        observed = int(_field(snapshot, name, expected))
        if observed != expected:
            raise ValueError(
                f"durable {name} does not reconcile: snapshot={observed}, "
                f"reservation rows={expected}"
            )

    per_task_limit_raw = _field(snapshot, "per_task_limit_usd")
    per_task_limit = (
        None
        if per_task_limit_raw is None
        else _money_decimal(per_task_limit_raw, label="per_task_limit_usd")
    )
    by_task: dict[str, TaskCostBreakdown] = {}
    for task_id in task_ids:
        totals = _cost_totals(task_acc[task_id])
        consumed = Decimal(str(totals.recorded_cost)) + Decimal(
            str(totals.reserved_or_uncertain_cost)
        )
        by_task[task_id] = TaskCostBreakdown(
            **totals.model_dump(),
            by_role={
                name: _cost_totals(acc)
                for name, acc in sorted(task_role_acc[task_id].items())
            },
            by_stage={
                name: _cost_totals(acc)
                for name, acc in sorted(task_stage_acc[task_id].items())
            },
            over_task_limit=float(
                max(
                    _ZERO_MONEY,
                    consumed - per_task_limit,
                )
                if per_task_limit is not None
                else _ZERO_MONEY
            ),
        )

    # This second reconciliation protects against a future refactor that adds a
    # reservation to the global view but omits it from the per-task report.
    task_recorded = sum(
        (Decimal(str(value.recorded_cost)) for value in by_task.values()),
        _ZERO_MONEY,
    )
    task_pending = sum(
        (
            Decimal(str(value.reserved_or_uncertain_cost))
            for value in by_task.values()
        ),
        _ZERO_MONEY,
    )
    if (
        abs(task_recorded - Decimal(str(global_totals.recorded_cost)))
        > _MONEY_TOLERANCE
        or abs(
            task_pending
            - Decimal(str(global_totals.reserved_or_uncertain_cost))
        )
        > _MONEY_TOLERANCE
    ):
        raise ValueError("per-task durable costs do not reconcile to run totals")

    return RunCostBreakdown(
        **global_totals.model_dump(),
        run_id=run_id,
        accounting_source="durable_budget_ledger",
        reconciled=True,
        total_limit_usd=float(
            _money_decimal(
                _field(snapshot, "total_limit_usd", 0.0),
                label="total_limit_usd",
            )
        ),
        per_task_limit_usd=(
            None if per_task_limit is None else float(per_task_limit)
        ),
        available_usd=float(
            _money_decimal(
                _field(snapshot, "available_usd", 0.0), label="available_usd"
            )
        ),
        over_limit_usd=float(
            _money_decimal(
                _field(snapshot, "over_limit_usd", 0.0), label="over_limit_usd"
            )
        ),
        by_task=by_task,
        by_role={
            name: _cost_totals(acc) for name, acc in sorted(role_acc.items())
        },
        by_stage={
            name: _cost_totals(acc) for name, acc in sorted(stage_acc.items())
        },
        reservations=tuple(serialized_rows),
        ledger_created_at=str(_field(snapshot, "created_at", "")),
        ledger_updated_at=str(_field(snapshot, "updated_at", "")),
    )


def cost_breakdown_payload(breakdown: RunCostBreakdown) -> dict[str, Any]:
    """Return the typed breakdown with stable legacy aggregate aliases."""

    payload = breakdown.model_dump(mode="json")
    payload.update(
        {
            "spent_usd": breakdown.recorded_cost,
            "committed_usd": breakdown.recorded_cost,
            "reserved_usd": breakdown.reserved_cost,
            "uncertain_usd": breakdown.uncertain_cost,
            "budget_total_usd": breakdown.total_limit_usd,
            "budget_per_task_usd": breakdown.per_task_limit_usd,
        }
    )
    return payload


def task_readiness(
    engine: Engine,
    task: TaskIR,
    *,
    package_path: Path | None = None,
    package_receipt_path: Path | None = None,
    spent_usd: float = 0.0,
    recorded_cost: float | None = None,
    reserved_cost: float = 0.0,
    uncertain_cost: float = 0.0,
    reserved_or_uncertain_cost: float = 0.0,
    final_request_overrun: float = 0.0,
    cost_breakdown: TaskCostBreakdown | None = None,
    action: CandidateRunAction | None = None,
    spec: GenerationRunSpec | None = None,
    attempts: tuple[AttemptReadiness, ...] = (),
) -> TaskReadiness:
    stages = tuple(
        stage_readiness(engine, task, stage, spec=spec) for stage in STAGE_ORDER
    )
    by_stage = {stage.stage: stage for stage in stages}

    def passed(stage: StageName) -> bool:
        return by_stage[stage].state is ReadinessState.PASS

    def passed_through(stage: StageName) -> bool:
        stop = STAGE_ORDER.index(stage)
        return all(passed(item) for item in STAGE_ORDER[: stop + 1])

    # A current battery does not replace missing population or gold prerequisites.
    local = passed_through(StageName.GATES_TRANSFORM)
    calibrated = local and passed(StageName.CALIBRATE)
    release_ready = passed_through(StageName.SELECT)
    packaged = False
    package_status = ReadinessState.NOT_RUN
    package_failures: list[str] = []
    package_receipt_evidence = ""
    package_receipt_sha256 = ""
    verified_package_path: Path | None = None
    if package_path and local:
        if spec is not None and spec.profile is ReadinessProfile.RELEASE:
            try:
                from elt_taskgen.export import release as release_mod

                verification = release_mod.verify_release(package_path)
                manifest = release_mod.ReleaseManifest.model_validate_json(
                    (package_path / "release_manifest.json").read_text(
                        encoding="utf-8"
                    )
                )
                packaged = bool(
                    verification.ok
                    and manifest.tasks.get(task.task_id) == task.content_hash()
                )
                if not packaged:
                    package_failures.extend(verification.failures)
            except (OSError, TypeError, ValueError) as exc:
                packaged = False
                package_failures.append(f"release verification failed: {exc}")
        elif package_receipt_path is None:
            package_failures.append(
                "fresh-copy package verification receipt was not supplied"
            )
        else:
            try:
                from elt_taskgen.export.package_verification import (
                    verify_persisted_package_receipt,
                )

                receipt_check = verify_persisted_package_receipt(
                    package_receipt_path,
                    package_path=package_path,
                    expected_task_id=task.task_id,
                    expected_task_content_hash=task.content_hash(),
                )
                packaged = receipt_check.ok
                if packaged:
                    package_receipt_evidence = _rel(
                        engine.workspace, package_receipt_path
                    )
                    package_receipt_sha256 = receipt_check.receipt_sha256
                else:
                    package_failures.extend(receipt_check.failures)
            except (OSError, TypeError, ValueError) as exc:
                package_failures.append(
                    f"fresh-copy package receipt verification failed: {exc}"
                )
        if packaged:
            package_status = ReadinessState.PASS
            verified_package_path = package_path.resolve()
        else:
            package_status = ReadinessState.FAIL
    elif package_path and not local:
        package_status = ReadinessState.BLOCKED
        package_failures.append(
            "package verification is blocked until local EL/T acceptance passes"
        )
    elif package_receipt_path is not None:
        package_status = ReadinessState.FAIL
        package_failures.append(
            "a package verification receipt was supplied without a package path"
        )

    target_requires_package = bool(
        spec is not None
        and spec.profile in {ReadinessProfile.PACKAGED, ReadinessProfile.RELEASE}
    )
    if target_requires_package and local and package_path is None:
        package_failures.append("no package path was produced for a locally ready task")
    runtime_package_path = (
        package_path
        if spec is not None and spec.profile is ReadinessProfile.RELEASE
        else verified_package_path
    )
    runtime_evidence = _configured_runtime_evidence(
        engine,
        task,
        package_path=runtime_package_path,
        spec=spec,
    )
    disposition, blocker = _task_disposition(stages, engine.final_verdict(task.task_id))
    failure_class, failure_code, blocked_on = _task_failure(
        stages, engine.final_verdict(task.task_id)
    )
    precise_blockers = [
        f"{stage.stage.value}: {stage.reason or stage.verdict or stage.state.value}"
        for stage in stages
        if stage.state
        in {ReadinessState.FAIL, ReadinessState.BLOCKED, ReadinessState.STALE}
    ]
    precise_blockers.extend(package_failures)
    if blocker and blocker not in precise_blockers:
        precise_blockers.insert(0, blocker)
    if target_requires_package and package_failures and not blocker:
        blocker = package_failures[0]

    if cost_breakdown is None:
        recorded = spent_usd if recorded_cost is None else recorded_cost
        unclassified = max(
            0.0,
            reserved_or_uncertain_cost - reserved_cost - uncertain_cost,
        )
        cost_breakdown = TaskCostBreakdown(
            recorded_cost=recorded,
            reserved_cost=reserved_cost,
            uncertain_cost=uncertain_cost,
            unclassified_pending_cost=unclassified,
            reserved_or_uncertain_cost=reserved_or_uncertain_cost,
            final_request_overrun=final_request_overrun,
        )
    recorded = cost_breakdown.recorded_cost
    pending = cost_breakdown.reserved_or_uncertain_cost
    evidence_paths = sorted(
        {
            *(item for stage in stages for item in stage.evidence if item),
            *(item.evidence_path for item in attempts if item.evidence_path),
            *((action or CandidateRunAction()).evidence_paths),
            *(
                (package_receipt_evidence,)
                if package_receipt_evidence
                else ()
            ),
        }
    )
    return TaskReadiness(
        task_id=task.task_id,
        origin=task.origin.value,
        source_family=task.origin.value,
        family_id=task.family_id,
        task_content_hash=task.content_hash(),
        task_revision=task.current_revision,
        revision=task.current_revision,
        disposition=disposition,
        stages=stages,
        attempts=attempts,
        actions=action or CandidateRunAction(),
        coverage=Coverage(
            populations=tuple(spec.name.value for spec in task.populations),
            tables=tuple(table.name for table in task.tables),
            marts=tuple(mart.name for mart in task.marts),
            attacks=tuple(case.name for case in task.attack_cases),
            variants=tuple(variant.value for variant in RLVR_TASK_VARIANTS),
        ),
        population_coverage=tuple(spec.name.value for spec in task.populations),
        prompt_status=by_stage[StageName.AUTHOR].state,
        review_status=by_stage[StageName.REVIEW].state,
        attack_status=by_stage[StageName.ATTACK].state,
        integrity_status=by_stage[StageName.TASK_INTEGRITY].state,
        el_acceptance=by_stage[StageName.GATES_EXTRACT_LOAD].state,
        t_acceptance=by_stage[StageName.GATES_TRANSFORM].state,
        locally_evaluator_ready=local,
        local_evaluator_readiness=local,
        packaged_for_evaluation=packaged,
        package_status=package_status,
        empirically_calibrated=calibrated,
        release_ready=release_ready,
        runtime_certified=runtime_evidence.certification_verified,
        runtime_evidence=runtime_evidence,
        package_path=(str(verified_package_path) if verified_package_path else ""),
        package_path_if_verified=(
            str(verified_package_path) if verified_package_path else ""
        ),
        package_verification_receipt=package_receipt_evidence,
        package_verification_sha256=package_receipt_sha256,
        blocker=blocker,
        failure_class=failure_class,
        failure_code=failure_code,
        blocked_on=blocked_on,
        precise_blockers=tuple(dict.fromkeys(precise_blockers)),
        evidence_paths=tuple(evidence_paths),
        spent_usd=recorded,
        recorded_cost=recorded,
        reserved_cost=cost_breakdown.reserved_cost,
        uncertain_cost=cost_breakdown.uncertain_cost,
        reserved_or_uncertain_cost=pending,
        final_request_overrun=cost_breakdown.final_request_overrun,
        cost_by_role=cost_breakdown.by_role,
        cost_by_stage=cost_breakdown.by_stage,
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None  # type: ignore[assignment]
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _markdown_cell(value: Any) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def render_run_report_markdown(report: PipelineReadinessReport) -> str:
    """Render the same typed run report as a compact human-readable artifact."""

    lines = [
        f"# ELT task-generation run `{_markdown_cell(report.run_id)}`",
        "",
        f"- State: `{report.state}`",
        f"- Profile: `{report.profile.value}`",
        f"- Generated: `{report.generated_at}`",
        f"- Configuration SHA-256: `{report.config_sha256}`",
        f"- Source manifest SHA-256: `{report.source_manifest_sha256 or 'not recorded'}`",
        "",
        "## Counts",
        "",
        "| Metric | Count |",
        "| --- | ---: |",
    ]
    for name, value in report.counts.model_dump(mode="json").items():
        lines.append(f"| {_markdown_cell(name)} | {value} |")

    costs = dict(report.costs)
    lines.extend(
        [
            "",
            "## Durable cost accounting",
            "",
            f"- Source: `{costs.get('accounting_source', 'unknown')}`",
            f"- Reconciled: `{bool(costs.get('reconciled', False))}`",
            f"- Recorded: `${float(costs.get('recorded_cost', costs.get('spent_usd', 0.0))):.9f}`",
            f"- Reserved: `${float(costs.get('reserved_cost', costs.get('reserved_usd', 0.0))):.9f}`",
            f"- Uncertain: `${float(costs.get('uncertain_cost', costs.get('uncertain_usd', 0.0))):.9f}`",
            f"- Reserved or uncertain: `${float(costs.get('reserved_or_uncertain_cost', 0.0)):.9f}`",
            f"- Final-request overrun: `${float(costs.get('final_request_overrun', 0.0)):.9f}`",
        ]
    )
    for heading, key in (("By stage", "by_stage"), ("By role", "by_role")):
        rows = costs.get(key) or {}
        lines.extend(
            [
                "",
                f"### {heading}",
                "",
                "| Name | Recorded | Reserved | Uncertain | Calls (C/R/U) |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        if not rows:
            lines.append("| _none recorded_ | 0 | 0 | 0 | 0/0/0 |")
        for name, totals in sorted(rows.items()):
            lines.append(
                "| "
                + " | ".join(
                    (
                        _markdown_cell(name),
                        f"${float(totals.get('recorded_cost', 0.0)):.9f}",
                        f"${float(totals.get('reserved_cost', 0.0)):.9f}",
                        f"${float(totals.get('uncertain_cost', 0.0)):.9f}",
                        "/".join(
                            str(int(totals.get(field, 0)))
                            for field in (
                                "committed_calls",
                                "reserved_calls",
                                "uncertain_calls",
                            )
                        ),
                    )
                )
                + " |"
            )

    lines.extend(
        [
            "",
            "## Candidates",
            "",
            "| Task | Source | Revision | Disposition | Cause | Prompt | Review | Attack | Integrity | EL | T | Local | Package | Recorded | Pending |",
            "| --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: |",
        ]
    )
    for task in report.tasks:
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(task.task_id),
                    _markdown_cell(task.source_family),
                    str(task.task_revision),
                    _markdown_cell(task.disposition),
                    _markdown_cell(task.failure_class.value or "none"),
                    task.prompt_status.value,
                    task.review_status.value,
                    task.attack_status.value,
                    task.integrity_status.value,
                    task.el_acceptance.value,
                    task.t_acceptance.value,
                    "PASS" if task.local_evaluator_readiness else "NOT_READY",
                    task.package_status.value,
                    f"${task.recorded_cost:.9f}",
                    f"${task.reserved_or_uncertain_cost:.9f}",
                )
            )
            + " |"
        )

    lines.extend(["", "## Candidate evidence and blockers", ""])
    for task in report.tasks:
        lines.append(f"### `{_markdown_cell(task.task_id)}`")
        lines.append("")
        lines.append(
            "- Population coverage: "
            + (", ".join(f"`{name}`" for name in task.population_coverage) or "none")
        )
        lines.append(
            f"- Gold/mart roster: {len(task.coverage.populations)} populations × "
            f"{len(task.coverage.marts)} marts; {len(task.coverage.tables)} source tables"
        )
        lines.append(
            "- Verified package path: "
            + (
                f"`{_markdown_cell(task.package_path_if_verified)}`"
                if task.package_path_if_verified
                else "none"
            )
        )
        lines.append(
            "- Failure classification: "
            + (task.failure_class.value or "none")
            + (f" (`{_markdown_cell(task.failure_code)}`)" if task.failure_code else "")
            + (f"; blocked on `{_markdown_cell(task.blocked_on)}`" if task.blocked_on else "")
        )
        if task.precise_blockers:
            lines.append("- Blockers:")
            for blocker in task.precise_blockers:
                lines.append(f"  - {_markdown_cell(blocker)}")
        else:
            lines.append("- Blockers: none")
        if task.evidence_paths:
            lines.append("- Evidence:")
            for evidence in task.evidence_paths:
                lines.append(f"  - `{_markdown_cell(evidence)}`")
        else:
            lines.append("- Evidence: none")
        lines.append("")

    lines.extend(["## Run blockers", ""])
    if report.blockers:
        lines.extend(f"- {_markdown_cell(blocker)}" for blocker in report.blockers)
    else:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def write_run_report(
    report: PipelineReadinessReport, *, workspace: Path
) -> Path:
    """Persist matching JSON and Markdown reports plus per-task JSON snapshots."""

    root = Path(workspace).resolve()
    payload = (
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    path = root / "state" / "pipeline_runs" / report.run_id / "readiness.json"
    _atomic_write(path, payload)
    _atomic_write(
        path.with_suffix(".md"),
        render_run_report_markdown(report).encode("utf-8"),
    )
    for task in report.tasks:
        registered = root / "tasks" / task.task_id / "task_ir.json"
        task_path = (
            root / "tasks" / task.task_id / "reports" / "readiness.json"
            if registered.is_file()
            else root
            / "state"
            / "pipeline_runs"
            / report.run_id
            / "candidates"
            / f"{task.task_id}.json"
        )
        _atomic_write(
            task_path,
            (json.dumps(task.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )
    return path


def make_run_report(
    *,
    engine: Engine,
    run_id: str,
    spec: GenerationRunSpec,
    task_ids: tuple[str, ...],
    state: str,
    source_manifest_sha256: str = "",
    selected_manifest_path: str = "",
    package_paths: dict[str, Path] | None = None,
    package_receipts: Mapping[str, Path] | None = None,
    worker_results: tuple[dict[str, Any], ...] = (),
    repository: dict[str, Any] | None = None,
    blockers: tuple[str, ...] = (),
    task_costs: Mapping[str, tuple[float, float]] | None = None,
    run_actions: Mapping[str, int] | None = None,
    budget_snapshot: Any | None = None,
    task_actions: Mapping[
        str, CandidateRunAction | Mapping[str, Any]
    ] | None = None,
    role_stage_map: Mapping[str, str] | None = None,
) -> PipelineReadinessReport:
    package_paths = package_paths or {}
    package_receipts = package_receipts or {}
    result_by_id = {str(row.get("task_id")): row for row in worker_results}
    task_costs = task_costs or {}
    run_actions = run_actions or {}
    supplied_task_actions = task_actions is not None
    task_actions = task_actions or {}

    roster = set(task_ids)
    for label, keys in (
        ("package_paths", set(package_paths)),
        ("package_receipts", set(package_receipts)),
        ("task_costs", set(task_costs)),
        ("task_actions", set(task_actions)),
    ):
        foreign = sorted(keys - roster)
        if foreign:
            raise ValueError(f"{label} names tasks outside the exact roster: {foreign}")
    if len(roster) != len(task_ids):
        raise ValueError("task_ids must be an exact, duplicate-free roster")

    normalized_actions: dict[str, CandidateRunAction] = {}
    for task_id in task_ids:
        raw_action = task_actions.get(task_id)
        if raw_action is None:
            normalized_actions[task_id] = CandidateRunAction(
                selected=True,
                registration_action=(
                    "failed"
                    if str(result_by_id.get(task_id, {}).get("state") or "")
                    == "ingest_failed"
                    else "unknown"
                ),
                processing_attempted=task_id in result_by_id,
            )
        elif isinstance(raw_action, CandidateRunAction):
            normalized_actions[task_id] = raw_action
        else:
            normalized_actions[task_id] = CandidateRunAction.model_validate(
                raw_action
            )

    durable_costs = (
        cost_breakdown_from_budget_snapshot(
            budget_snapshot,
            task_ids=task_ids,
            expected_run_id=run_id,
            role_stage_map=role_stage_map,
        )
        if budget_snapshot is not None
        else None
    )
    if durable_costs is not None:
        if spec.budget_total is None:
            raise ValueError(
                "a durable budget snapshot requires config.budget_total"
            )
        if not math.isclose(
            float(spec.budget_total),
            float(durable_costs.total_limit_usd or 0.0),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError(
                "durable total budget differs from the configured batch limit"
            )
        if durable_costs.per_task_limit_usd is None or not math.isclose(
            float(spec.budget_per_task),
            float(durable_costs.per_task_limit_usd),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError(
                "durable per-task budget differs from the configured task limit"
            )

    def candidate_cost(task_id: str) -> TaskCostBreakdown:
        if durable_costs is not None:
            return durable_costs.by_task[task_id]
        row = result_by_id.get(task_id, {})
        recorded, pending = task_costs.get(
            task_id, (float(row.get("usd", 0.0) or 0.0), 0.0)
        )
        return TaskCostBreakdown(
            recorded_cost=float(recorded),
            unclassified_pending_cost=float(pending),
            reserved_or_uncertain_cost=float(pending),
        )

    def attempt_history(task_id: str) -> tuple[AttemptReadiness, ...]:
        directory = (
            engine.workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "attempts"
            / task_id
        )
        if not directory.is_dir() or directory.is_symlink():
            return ()
        history: list[AttemptReadiness] = []
        for path in sorted(directory.glob("*.json")):
            try:
                payload = _json(path)
                failure_class, failure_code, blocked_on = (
                    _attempt_failure_descriptor(payload)
                )
                history.append(
                    AttemptReadiness(
                        attempt_id=str(payload["attempt_id"]),
                        target_stage=StageName(str(payload["target_stage"])),
                        state=str(payload["state"]),
                        evidence_path=_rel(engine.workspace, path),
                        task_content_hash=str(
                            payload.get("task_content_hash") or ""
                        ),
                        detail=str(payload.get("detail") or ""),
                        usd=float(payload.get("usd", 0.0) or 0.0),
                        failure_class=failure_class,
                        failure_code=failure_code,
                        blocked_on=blocked_on,
                    )
                )
            except (KeyError, OSError, TypeError, ValueError) as exc:
                blockers_nonlocal.append(
                    f"{task_id}: invalid attempt record {path.name}: {exc}"
                )
        return tuple(history)

    blockers_nonlocal = list(blockers)
    runtime_targets = set(spec.runtime_difficulty_reports)
    runtime_configuration_blockers: list[str] = []
    for task_id in sorted(runtime_targets - set(task_ids)):
        runtime_configuration_blockers.append(
            f"{task_id}: runtime evidence is configured for a task outside "
            "this run roster"
        )
    blockers_nonlocal.extend(runtime_configuration_blockers)
    tasks: list[TaskReadiness] = []
    for task_id in task_ids:
        row = result_by_id.get(task_id, {})
        action = normalized_actions[task_id]
        cost = candidate_cost(task_id)
        try:
            task = engine.load_task(task_id)
        except Exception as exc:  # noqa: BLE001 - retain failed candidate accounting
            operational_state = str(row.get("state") or "failed")
            detail = str(
                row.get("detail")
                or f"selected candidate was not created: {type(exc).__name__}: {exc}"
            )
            disposition = (
                "blocked" if operational_state == "blocked" else "failed"
            )
            failure_class, failure_code, blocked_on = (
                _attempt_failure_descriptor(row)
            )
            if failure_class is ReadinessFailureClass.NONE:
                failure_class = ReadinessFailureClass.OPERATIONAL_BLOCKING
                failure_code = operational_state or "candidate_not_created"
            tasks.append(
                TaskReadiness(
                    task_id=task_id,
                    origin=str(row.get("origin") or ""),
                    source_family=str(row.get("origin") or ""),
                    family_id="",
                    task_content_hash="",
                    task_revision=0,
                    revision=0,
                    disposition=disposition,
                    stages=tuple(
                        StageReadiness(
                            stage=stage,
                            state=ReadinessState.NOT_RUN,
                            reason="candidate TaskIR was not created",
                        )
                        for stage in STAGE_ORDER
                    ),
                    attempts=attempt_history(task_id),
                    actions=action,
                    coverage=Coverage(),
                    blocker=detail,
                    failure_class=failure_class,
                    failure_code=failure_code,
                    blocked_on=blocked_on,
                    precise_blockers=(detail,),
                    evidence_paths=action.evidence_paths,
                    runtime_evidence=(
                        RuntimeEvidenceReadiness(
                            configured=True,
                            certification_reason=(
                                "candidate TaskIR is unavailable for runtime "
                                "evidence verification"
                            ),
                            difficulty_reason=(
                                "candidate TaskIR is unavailable for runtime "
                                "evidence verification"
                            ),
                        )
                        if task_id in runtime_targets
                        else RuntimeEvidenceReadiness()
                    ),
                    spent_usd=cost.recorded_cost,
                    recorded_cost=cost.recorded_cost,
                    reserved_cost=cost.reserved_cost,
                    uncertain_cost=cost.uncertain_cost,
                    reserved_or_uncertain_cost=(
                        cost.reserved_or_uncertain_cost
                    ),
                    final_request_overrun=cost.final_request_overrun,
                    cost_by_role=cost.by_role,
                    cost_by_stage=cost.by_stage,
                )
            )
            candidate_blocker = f"{task_id}: {detail}"
            if candidate_blocker not in blockers_nonlocal:
                blockers_nonlocal.append(candidate_blocker)
            continue
        task_report = task_readiness(
            engine,
            task,
            package_path=package_paths.get(task_id),
            package_receipt_path=package_receipts.get(task_id),
            cost_breakdown=cost,
            action=action,
            spec=spec,
            attempts=attempt_history(task_id),
        )
        operational_state = str(row.get("state") or "")
        if operational_state in {"blocked", RUN_PREFLIGHT_BLOCKED_STATE}:
            operational_detail = str(row.get("detail") or operational_state)
            worker_class, worker_code, worker_blocked_on = (
                _attempt_failure_descriptor(row)
            )
            if worker_class is ReadinessFailureClass.NONE:
                worker_class = ReadinessFailureClass.OPERATIONAL_BLOCKING
                worker_code = operational_state
        # Apply preflight stops only to candidates still awaiting provider work.
            preserve_evidence_disposition = (
                operational_state == RUN_PREFLIGHT_BLOCKED_STATE
                and (
                    task_report.disposition in {"accepted", "rejected"}
                    or (
                        task_report.disposition == "failed"
                        and any(
                            attempt.state == "infrastructure"
                            for attempt in task_report.attempts
                        )
                    )
                )
            )
            updates: dict[str, Any] = {
                "precise_blockers": tuple(
                    dict.fromkeys(
                        (*task_report.precise_blockers, operational_detail)
                    )
                ),
            }
            if not preserve_evidence_disposition:
                updates.update(
                    {
                        "disposition": "blocked",
                        "blocker": operational_detail,
                        "failure_class": worker_class,
                        "failure_code": worker_code,
                        "blocked_on": worker_blocked_on,
                    }
                )
            task_report = task_report.model_copy(update=updates)
        elif operational_state == "pending_adjudication":
            operational_detail = str(row.get("detail") or operational_state)
            worker_class, worker_code, worker_blocked_on = (
                _attempt_failure_descriptor(row)
            )
            task_report = task_report.model_copy(
                update={
                    "disposition": "blocked",
                    "blocker": operational_detail,
                    "failure_class": worker_class,
                    "failure_code": worker_code,
                    "blocked_on": worker_blocked_on or "human",
                    "precise_blockers": tuple(
                        dict.fromkeys(
                            (*task_report.precise_blockers, operational_detail)
                        )
                    ),
                }
            )
        elif operational_state in {"protocol_failure", "incomplete_protocol"}:
            operational_detail = str(row.get("detail") or operational_state)
            worker_class, worker_code, worker_blocked_on = (
                _attempt_failure_descriptor(row)
            )
            task_report = task_report.model_copy(
                update={
                    "disposition": "failed",
                    "blocker": operational_detail,
                    "failure_class": worker_class,
                    "failure_code": worker_code,
                    "blocked_on": worker_blocked_on,
                    "precise_blockers": tuple(
                        dict.fromkeys(
                            (*task_report.precise_blockers, operational_detail)
                        )
                    ),
                }
            )
        elif operational_state in {
            "infrastructure",
            "usage_error",
            "package_error",
            "ingest_failed",
        }:
            operational_detail = str(row.get("detail") or operational_state)
            worker_class, worker_code, worker_blocked_on = (
                _attempt_failure_descriptor(row)
            )
            task_report = task_report.model_copy(
                update={
                    "disposition": "failed",
                    "blocker": operational_detail,
                    "failure_class": worker_class,
                    "failure_code": worker_code,
                    "blocked_on": worker_blocked_on,
                    "precise_blockers": tuple(
                        dict.fromkeys(
                            (*task_report.precise_blockers, operational_detail)
                        )
                    ),
                }
            )
        tasks.append(task_report)
    runtime_blockers: list[str] = []
    for task in tasks:
        runtime = task.runtime_evidence
        if not runtime.configured:
            continue
        if not runtime.certification_verified:
            runtime_blockers.append(
                f"{task.task_id}: {runtime.certification_reason or 'runtime certification did not verify'}"
            )
        if not runtime.difficulty_verified:
            runtime_blockers.append(
                f"{task.task_id}: {runtime.difficulty_reason or 'runtime-certified difficulty did not verify'}"
            )
    for blocker in runtime_blockers:
        if blocker not in blockers_nonlocal:
            blockers_nonlocal.append(blocker)
    if spec.profile in {ReadinessProfile.PACKAGED, ReadinessProfile.RELEASE}:
        for task in tasks:
            if task.locally_evaluator_ready and not task.packaged_for_evaluation:
                detail = task.blocker or "verified package evidence is incomplete"
                package_blocker = f"{task.task_id}: {detail}"
                if package_blocker not in blockers_nonlocal:
                    blockers_nonlocal.append(package_blocker)

    accepted = sum(task.locally_evaluator_ready for task in tasks)
    exported = sum(task.packaged_for_evaluation for task in tasks)
    dispositions = [task.disposition for task in tasks]
    if durable_costs is None:
        by_task = {task_id: candidate_cost(task_id) for task_id in task_ids}
        recorded = math.fsum(value.recorded_cost for value in by_task.values())
        pending = math.fsum(
            value.reserved_or_uncertain_cost for value in by_task.values()
        )
        source = (
            "legacy_task_totals"
            if task_costs
            else "worker_results"
            if any(float(row.get("usd", 0.0) or 0.0) for row in worker_results)
            else "none"
        )
        total_limit = spec.budget_total
        available = (
            max(0.0, float(total_limit) - recorded - pending)
            if total_limit is not None
            else 0.0
        )
        run_costs = RunCostBreakdown(
            recorded_cost=recorded,
            unclassified_pending_cost=pending,
            reserved_or_uncertain_cost=pending,
            final_request_overrun=math.fsum(
                value.final_request_overrun for value in by_task.values()
            ),
            committed_calls=sum(value.committed_calls for value in by_task.values()),
            reserved_calls=sum(value.reserved_calls for value in by_task.values()),
            uncertain_calls=sum(value.uncertain_calls for value in by_task.values()),
            released_calls=sum(value.released_calls for value in by_task.values()),
            run_id=run_id,
            accounting_source=source,
            reconciled=False,
            total_limit_usd=total_limit,
            per_task_limit_usd=spec.budget_per_task,
            available_usd=available,
            over_limit_usd=(
                max(0.0, recorded + pending - float(total_limit))
                if total_limit is not None
                else 0.0
            ),
            by_task=by_task,
        )
    else:
        run_costs = durable_costs

    derived_actions = {
        "selected": sum(action.selected for action in normalized_actions.values()),
        "generation_attempts": sum(
            action.processing_attempted for action in normalized_actions.values()
        ),
        "created_this_invocation": sum(
            action.registration_action == "created"
            for action in normalized_actions.values()
        ),
        "resumed_existing": sum(
            action.registration_action == "resumed"
            for action in normalized_actions.values()
        ),
        "adapter_taskirs_rederived": sum(
            action.adapter_taskir_rederived
            for action in normalized_actions.values()
        ),
        "source_provenance_equivalence_attested": sum(
            action.source_provenance_equivalence_attested
            for action in normalized_actions.values()
        ),
        "reports_locally_revalidated_and_superseded": sum(
            action.reports_locally_revalidated_and_superseded
            for action in normalized_actions.values()
        ),
    }
    if supplied_task_actions:
        for name, explicit in run_actions.items():
            if name in derived_actions and int(explicit) != derived_actions[name]:
                raise ValueError(
                    f"aggregate run action {name!r}={explicit} disagrees with "
                    f"per-candidate facts ({derived_actions[name]})"
                )
    action_counts = {
        name: int(run_actions.get(name, value))
        for name, value in derived_actions.items()
    }
    reported_state = (
        "COMPLETE_WITH_ISSUES"
        if blockers_nonlocal and state == "COMPLETE"
        else state
    )
    return PipelineReadinessReport(
        run_id=run_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        state=reported_state,
        profile=spec.profile,
        config_sha256=spec.fingerprint(),
        config=spec,
        repository=repository or {},
        source_manifest_sha256=source_manifest_sha256,
        selected_manifest_path=selected_manifest_path,
        counts=RunCounts(
            requested=spec.candidate_count,
            selected=action_counts["selected"],
            generation_attempts=action_counts["generation_attempts"],
            candidates_created=sum(bool(task.task_content_hash) for task in tasks),
            created_this_invocation=action_counts["created_this_invocation"],
            resumed_existing=action_counts["resumed_existing"],
            adapter_taskirs_rederived=action_counts[
                "adapter_taskirs_rederived"
            ],
            source_provenance_equivalence_attested=action_counts[
                "source_provenance_equivalence_attested"
            ],
            reports_locally_revalidated_and_superseded=action_counts[
                "reports_locally_revalidated_and_superseded"
            ],
            accepted=accepted,
            packaged=exported,
            exported=exported,
            rejected=dispositions.count("rejected"),
            failed=dispositions.count("failed"),
            blocked=dispositions.count("blocked"),
            in_progress=dispositions.count("in_progress"),
            stale=sum(
                any(stage.state is ReadinessState.STALE for stage in task.stages)
                for task in tasks
            ),
            quality_rejected=sum(
                task.failure_class is ReadinessFailureClass.QUALITY_REJECTION
                for task in tasks
            ),
            infrastructure_failed=sum(
                task.failure_class
                is ReadinessFailureClass.INFRASTRUCTURE_FAILURE
                for task in tasks
            ),
            protocol_failed=sum(
                task.failure_class is ReadinessFailureClass.PROTOCOL_FAILURE
                for task in tasks
            ),
            pending_adjudication=sum(
                task.failure_class
                is ReadinessFailureClass.PENDING_ADJUDICATION
                for task in tasks
            ),
            operationally_blocked=sum(
                task.failure_class
                is ReadinessFailureClass.OPERATIONAL_BLOCKING
                for task in tasks
            ),
            runtime_certified=sum(task.runtime_certified for task in tasks),
            runtime_difficulty_certified=sum(
                task.runtime_evidence.difficulty_verified for task in tasks
            ),
        ),
        costs=cost_breakdown_payload(run_costs),
        tasks=tuple(tasks),
        blockers=tuple(blockers_nonlocal),
    )


__all__ = [
    "CandidateRunAction",
    "CostTotals",
    "Coverage",
    "CONFIGURED_STAGE_CONTRACT_VERSION",
    "CONFIGURED_STAGE_SEAL_VERSION",
    "ConfiguredStageSeal",
    "DEFAULT_ROLE_STAGE_MAP",
    "AttemptReadiness",
    "GenerationRunSpec",
    "PipelineReadinessReport",
    "ReadinessFailureClass",
    "ReadinessProfile",
    "ReadinessState",
    "RuntimeEvidenceReadiness",
    "RunCostBreakdown",
    "RunCounts",
    "StageReadiness",
    "TaskCostBreakdown",
    "TaskReadiness",
    "cost_breakdown_from_budget_snapshot",
    "cost_breakdown_payload",
    "make_run_report",
    "render_run_report_markdown",
    "seal_configured_stage",
    "stage_readiness",
    "stale_passes",
    "task_readiness",
    "write_run_report",
]

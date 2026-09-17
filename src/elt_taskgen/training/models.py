"""Frozen models for the ``workspace-v1`` artifact proxy."""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from elt_taskgen.models import canonical_json
from elt_taskgen.training.contract import (
    ARTIFACT_WORKFLOW_PROXY_CLAIM,
    MAX_WORKSPACE_ACTIONS,
    WORKSPACE_POPULATION_AGGREGATION,
    WORKSPACE_PROFILE_OWNER,
    WORKSPACE_RESULT_SCHEMA_VERSION,
    WORKSPACE_SCORER_VERSION,
    WORKSPACE_SUBMISSION_SCHEMA_VERSION,
    WORKSPACE_ERROR_CLASS_BY_CODE,
    WorkspaceErrorCode,
    WorkspaceFailureClass,
)


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")


class WorkspaceArtifactFile(BaseModel):
    """One regular candidate file, represented without its bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(ge=0)
    sha256: str

    @field_validator("path")
    @classmethod
    def _confined_elt_path(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("workspace artifact paths must use POSIX separators")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or path.parts[0] != "elt"
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != value
        ):
            raise ValueError("workspace artifact path must be confined beneath elt/")
        return value

    @field_validator("sha256")
    @classmethod
    def _valid_sha256(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value


def workspace_artifact_digest(files: tuple[WorkspaceArtifactFile, ...]) -> str:
    """Digest candidate identity from sorted paths, sizes, and byte digests."""

    payload = [item.model_dump(mode="json") for item in files]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class WorkspaceActionTraceEntry(BaseModel):
    """Bounded action evidence; raw observations remain outside the contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(ge=0)
    action: str = Field(min_length=1, max_length=128)
    success: bool
    outcome_code: str = Field(default="ok", min_length=1, max_length=128)
    observation_sha256: str = ""

    @field_validator("action", "outcome_code")
    @classmethod
    def _stable_code(cls, value: str) -> str:
        if _CODE_RE.fullmatch(value) is None:
            raise ValueError("action and outcome codes must be lowercase stable codes")
        return value

    @field_validator("observation_sha256")
    @classmethod
    def _optional_sha256(cls, value: str) -> str:
        if value and _SHA256_RE.fullmatch(value) is None:
            raise ValueError("observation_sha256 must be empty or a SHA-256 digest")
        return value


class WorkspaceSubmission(BaseModel):
    """Manifest for one sealed solver-authored ``elt/`` snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = WORKSPACE_SUBMISSION_SCHEMA_VERSION
    task_id: str = Field(min_length=1)
    profile_owner: str = WORKSPACE_PROFILE_OWNER
    artifact_sha256: str
    files: tuple[WorkspaceArtifactFile, ...] = Field(min_length=1)
    action_trace: tuple[WorkspaceActionTraceEntry, ...] = Field(
        default=(), max_length=MAX_WORKSPACE_ACTIONS
    )

    @model_validator(mode="after")
    def _validate_manifest(self) -> "WorkspaceSubmission":
        if self.schema_version != WORKSPACE_SUBMISSION_SCHEMA_VERSION:
            raise ValueError(
                f"workspace submission schema must be "
                f"{WORKSPACE_SUBMISSION_SCHEMA_VERSION!r}"
            )
        if self.profile_owner != WORKSPACE_PROFILE_OWNER:
            raise ValueError("workspace-v1 uses a harness-owned dbt profile")
        paths = [item.path for item in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("workspace artifact files must be unique and path-sorted")
        folded = [path.casefold() for path in paths]
        if len(folded) != len(set(folded)):
            raise ValueError("workspace artifact paths collide after case folding")
        if _SHA256_RE.fullmatch(self.artifact_sha256) is None:
            raise ValueError("artifact_sha256 must be a SHA-256 digest")
        if self.artifact_sha256 != workspace_artifact_digest(self.files):
            raise ValueError("artifact_sha256 does not match the file manifest")
        sequences = [entry.sequence for entry in self.action_trace]
        if sequences != list(range(len(sequences))):
            raise ValueError("action trace sequences must be contiguous from zero")
        return self


class WorkspaceFailure(BaseModel):
    """One classified failure with no private exception text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classification: WorkspaceFailureClass
    error_code: WorkspaceErrorCode

    @model_validator(mode="after")
    def _classification_matches_code(self) -> "WorkspaceFailure":
        expected = WORKSPACE_ERROR_CLASS_BY_CODE[self.error_code]
        if self.classification is not expected:
            raise ValueError(
                f"error code {self.error_code.value!r} requires classification "
                f"{expected.value!r}"
            )
        return self

    @property
    def label_eligible(self) -> bool:
        return self.classification.label_eligible


class PopulationWorkspaceScore(BaseModel):
    """Frozen reward heads and hard gates for one population."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    population: str = Field(min_length=1)
    graded: bool
    terraform_contract: float = Field(ge=0.0, le=1.0)
    sync_lifecycle: bool
    upstream_stage1: bool
    strict_raw_tables: float = Field(ge=0.0, le=1.0)
    strict_el_pass: bool
    dbt_project: float = Field(ge=0.0, le=1.0)
    mart_reward: float = Field(ge=0.0, le=1.0)
    raw_immutable: bool
    end_to_end_reward: float = Field(ge=0.0, le=1.0)
    # Keep phase-specific failure codes, but omit messages that could expose
    # private paths, expected rows, or hidden population details.
    error_codes: tuple[str, ...] = ()

    @field_validator("error_codes")
    @classmethod
    def _stable_unique_error_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(item) for item in value)
        if any(_CODE_RE.fullmatch(item) is None for item in normalized):
            raise ValueError("population error codes must be stable lowercase codes")
        if len(normalized) != len(set(normalized)):
            raise ValueError("population error codes must be unique")
        return normalized

    @model_validator(mode="after")
    def _enforce_reward_gate(self) -> "PopulationWorkspaceScore":
        strict = (
            self.terraform_contract == 1.0
            and self.sync_lifecycle
            and self.strict_raw_tables == 1.0
        )
        if self.strict_el_pass is not strict:
            raise ValueError("strict_el_pass does not match its three required heads")
        expected = self.mart_reward if strict and self.raw_immutable else 0.0
        if not math.isclose(self.end_to_end_reward, expected):
            raise ValueError("end_to_end_reward violates the strict EL/raw gate")
        return self


class WorkspaceScoreResult(BaseModel):
    """Machine result shell for artifact-level workspace execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = WORKSPACE_RESULT_SCHEMA_VERSION
    scorer_version: str = WORKSPACE_SCORER_VERSION
    claim: str = ARTIFACT_WORKFLOW_PROXY_CLAIM
    aggregation: str = WORKSPACE_POPULATION_AGGREGATION
    release_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    task_content_hash: str
    artifact_sha256: str
    valid_submission: bool
    graded_populations: tuple[str, ...] = ()
    populations: Mapping[str, PopulationWorkspaceScore] = Field(default_factory=dict)
    reward: float | None = Field(default=None, ge=0.0, le=1.0)
    failure: WorkspaceFailure | None = None

    @field_serializer("populations")
    def _serialize_populations(
        self, value: Mapping[str, PopulationWorkspaceScore]
    ) -> dict[str, PopulationWorkspaceScore]:
        return dict(value)

    @model_validator(mode="after")
    def _validate_result(self) -> "WorkspaceScoreResult":
        if self.schema_version != WORKSPACE_RESULT_SCHEMA_VERSION:
            raise ValueError(
                f"workspace result schema must be {WORKSPACE_RESULT_SCHEMA_VERSION!r}"
            )
        if self.scorer_version != WORKSPACE_SCORER_VERSION:
            raise ValueError("unsupported workspace scorer version")
        if self.claim != ARTIFACT_WORKFLOW_PROXY_CLAIM:
            raise ValueError("workspace result must use the artifact proxy claim")
        if self.aggregation != WORKSPACE_POPULATION_AGGREGATION:
            raise ValueError("workspace-v1 population aggregation is minimum")
        for digest in (self.task_content_hash, self.artifact_sha256):
            if _SHA256_RE.fullmatch(digest) is None:
                raise ValueError("result identities must be SHA-256 digests")
        if len(self.graded_populations) != len(set(self.graded_populations)):
            raise ValueError("graded populations must be unique")
        missing = [name for name in self.graded_populations if name not in self.populations]
        if missing:
            raise ValueError(f"graded populations are missing scores: {missing}")
        for name, score in self.populations.items():
            if score.population != name:
                raise ValueError("population score key and identity disagree")
        declared_graded = {
            name for name, score in self.populations.items() if score.graded
        }
        if declared_graded != set(self.graded_populations):
            raise ValueError("graded_populations does not match population score flags")
        object.__setattr__(self, "populations", MappingProxyType(dict(self.populations)))
        if self.failure is not None and not self.failure.label_eligible:
            if self.reward is not None:
                raise ValueError("task/harness/infrastructure failures produce no label")
            return self
        if self.reward is None:
            raise ValueError("successful or measured policy results require a reward")
        if not self.valid_submission:
            if self.failure is None:
                raise ValueError("an invalid submission requires a classified failure")
            if self.reward != 0.0:
                raise ValueError("an invalid submission has zero measured reward")
            return self
        if (
            self.failure is not None
            and self.failure.classification is WorkspaceFailureClass.POLICY_VIOLATION
        ):
            if self.reward != 0.0:
                raise ValueError("a policy violation has zero reward")
            return self
        if self.graded_populations:
            expected = min(
                self.populations[name].end_to_end_reward
                for name in self.graded_populations
            )
            if not math.isclose(self.reward, expected):
                raise ValueError("result reward is not the minimum graded reward")
        else:
            raise ValueError("a valid submission must have at least one graded population")
        return self


__all__ = [
    "PopulationWorkspaceScore",
    "WorkspaceActionTraceEntry",
    "WorkspaceArtifactFile",
    "WorkspaceFailure",
    "WorkspaceScoreResult",
    "WorkspaceSubmission",
    "workspace_artifact_digest",
]

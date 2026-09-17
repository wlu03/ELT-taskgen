"""Versioned models for private DuckDB semantic scoring."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from elt_taskgen.corpus.calibration import LoadStep
from elt_taskgen.semantic_contract import (
    SEMANTIC_RESULT_SCHEMA_VERSION,
    SEMANTIC_SCORER_VERSION,
    SEMANTIC_SUBMISSION_SCHEMA_VERSION,
)
from elt_taskgen.verification.strict_diagnostic import StrictMartDiagnostic


class SemanticLimits(BaseModel):
    """Per-attempt resource bounds enforced by the semantic worker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: float = Field(default=60.0, ge=0.1, le=3600.0)
    memory_limit_mb: int = Field(default=512, ge=32, le=1_048_576)
    # Worker-wide limit enforced by rlimits and a parent watchdog.
    worker_rss_limit_mb: int = Field(default=2048, ge=128, le=1_048_576)
    threads: int = Field(default=1, ge=1, le=16)
    max_result_rows_per_mart: int = Field(default=100_000, ge=1, le=10_000_000)
    max_result_bytes_per_mart: int = Field(
        default=16 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024
    )


class SemanticSubmission(BaseModel):
    """One versioned, combined EL+T semantic attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SEMANTIC_SUBMISSION_SCHEMA_VERSION
    task_id: str = Field(min_length=1)
    load_plan: dict[str, LoadStep]
    sql_by_mart: dict[str, str]

    @field_validator("schema_version")
    @classmethod
    def _known_schema_version(cls, value: str) -> str:
        if value != SEMANTIC_SUBMISSION_SCHEMA_VERSION:
            raise ValueError(
                f"semantic submission schema must be "
                f"{SEMANTIC_SUBMISSION_SCHEMA_VERSION!r}"
            )
        return value


class PopulationSemanticScore(BaseModel):
    """Independent EL/T signals and their gate for one population.

    Expose only stable error codes because raw exceptions may contain private
    values or paths.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    population: str = Field(min_length=1)
    graded: bool
    el_reward: float = Field(ge=0.0, le=1.0)
    t_reward: float = Field(ge=0.0, le=1.0)
    reward: float = Field(ge=0.0, le=1.0)
    stage1_pass: bool
    mart_scores: dict[str, bool] = Field(default_factory=dict)
    el_error_code: str = ""
    t_error_codes: dict[str, str] = Field(default_factory=dict)
    #: Opt-in strict typed diagnostics — reported NEXT TO the unchanged
    #: reward, never consulted by it.  Empty unless the run requested them.
    strict_marts: dict[str, StrictMartDiagnostic] = Field(default_factory=dict)
    strict_shadow_marts: dict[str, StrictMartDiagnostic] = Field(
        default_factory=dict
    )


class SemanticScoreResult(BaseModel):
    """Stable machine result for one combined semantic attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SEMANTIC_RESULT_SCHEMA_VERSION
    scorer_version: str = SEMANTIC_SCORER_VERSION
    comparator_version: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    task_content_hash: str = Field(min_length=1)
    submission_schema_version: str = SEMANTIC_SUBMISSION_SCHEMA_VERSION
    aggregation: str = "minimum"
    valid_submission: bool
    graded_populations: tuple[str, ...] = ()
    populations: dict[str, PopulationSemanticScore] = Field(default_factory=dict)
    semantic_el_reward: float = Field(ge=0.0, le=1.0)
    semantic_t_reward: float = Field(ge=0.0, le=1.0)
    reward: float = Field(ge=0.0, le=1.0)
    error_code: str = ""
    #: True iff strict diagnostics were requested AND actually executed.
    strict_diagnostic_ran: bool = False
    strict_diagnostic_version: str = ""

"""Project ``WorkspaceScoreResult`` into the ``workspace-signal-v1`` scalar.

Unlabelled results remain ``None`` and drop the whole rollout group. Policy
violations and invalid submissions score zero. Private infeasibility canaries
reward only a recorded abort. Otherwise:

``R = (r_EL + 1[el_pass] * w_T * r_T) / (1 + w_T)``

``r_EL`` requires every graded population to pass strict EL, and ``r_T`` is the
minimum hidden end-to-end reward. No process, per-turn, length, or efficiency
term enters the scalar.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from types import MappingProxyType
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from elt_taskgen.training.contract import (
    WorkspaceErrorCode,
    WorkspaceFailureClass,
)
from elt_taskgen.training.models import (
    PopulationWorkspaceScore,
    WorkspaceScoreResult,
)
from elt_taskgen.training.terraform_intent import TERRAFORM_POLICY_VIOLATION_CODES


SIGNAL_VERSION = "workspace-signal-v1"

#: The default T weight (Output 8 §8.2): EL-only ceiling 0.5.
DEFAULT_W_T = 1.0

FirstFailedPhase = Literal[
    "none", "terraform", "sync", "dbt", "mart", "immutability", "workspace"
]

#: Population codes treated as policy violations without a top-level failure.
POLICY_VIOLATION_CODES: frozenset[str] = frozenset(
    {code.value for code in TERRAFORM_POLICY_VIOLATION_CODES}
    | {"dbt_unsafe_artifact", "dbt_raw_mutated"}
)

#: Lifecycle codes whose first failed phase is the workspace itself (the seal
#: or the replay refused the candidate before any phase ran).
_WORKSPACE_PHASE_CODES: frozenset[WorkspaceErrorCode] = frozenset(
    {
        WorkspaceErrorCode.SUBMISSION_INVALID,
        WorkspaceErrorCode.TASK_ID_MISMATCH,
        WorkspaceErrorCode.WORKSPACE_NOT_FRESH,
        WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE,
        WorkspaceErrorCode.WORKSPACE_SYMLINK,
        WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
        WorkspaceErrorCode.WORKSPACE_CASE_COLLISION,
        WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
        WorkspaceErrorCode.WORKSPACE_MISSING_ARTIFACT,
        WorkspaceErrorCode.WORKSPACE_TRANSIENT_STATE,
        WorkspaceErrorCode.WORKSPACE_SECRET_LITERAL,
        WorkspaceErrorCode.WORKSPACE_SIZE_LIMIT,
        WorkspaceErrorCode.WORKSPACE_DIGEST_MISMATCH,
    }
)

#: Phase order of the scorer (``_score_population`` short-circuits in it) and
#: the rank used to pick the FIRST failed phase across tied populations.
_PHASE_RANK: Mapping[str, int] = MappingProxyType(
    {
        "terraform": 0,
        "sync": 1,
        "dbt": 2,
        "mart": 3,
        "immutability": 4,
        "none": 5,
    }
)


class WorkspaceTrainingSignal(BaseModel):
    """Frozen trainer signal derived from a score and private canary flags.

    Population details are diagnostics, not reward terms or policy input.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_version: str = SIGNAL_VERSION
    scorer_version: str = Field(min_length=1)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    label_valid: bool
    el_pass: bool
    policy_violation: bool
    r_el: float = Field(ge=0.0, le=1.0)
    r_t: float = Field(ge=0.0, le=1.0)
    w_t: float = Field(default=DEFAULT_W_T, ge=0.0)
    reward: float | None = Field(default=None, ge=0.0, le=1.0)
    first_failed_phase: FirstFailedPhase
    canary_task: bool = False
    canary_hit: bool = False
    populations: Mapping[str, PopulationWorkspaceScore] = Field(default_factory=dict)

    @field_serializer("populations")
    def _serialize_populations(
        self, value: Mapping[str, PopulationWorkspaceScore]
    ) -> dict[str, PopulationWorkspaceScore]:
        return dict(value)

    @model_validator(mode="after")
    def _validate_signal(self) -> "WorkspaceTrainingSignal":
        if self.signal_version != SIGNAL_VERSION:
            raise ValueError(f"training signal version must be {SIGNAL_VERSION!r}")
        if not math.isfinite(self.w_t):
            raise ValueError("w_t must be finite")
        if self.label_valid:
            if self.reward is None:
                raise ValueError("a labelled signal carries a reward")
        elif self.reward is not None:
            raise ValueError("an unlabelled signal carries no reward (never 0.0)")
        if self.el_pass and self.r_el != 1.0:
            raise ValueError("el_pass requires r_el == 1.0")
        if not self.el_pass and self.r_el != 0.0:
            raise ValueError("r_el is 1.0 only on el_pass")
        if self.el_pass and self.policy_violation:
            raise ValueError("a policy violation never passes EL")
        if self.canary_hit and not self.canary_task:
            raise ValueError("a canary hit requires a canary task")
        if self.label_valid and self.canary_hit and self.reward != 0.0:
            raise ValueError("a canary hit is forced to 0.0")
        if self.label_valid and self.policy_violation and self.reward != 0.0:
            raise ValueError("a policy violation has zero reward")
        object.__setattr__(
            self, "populations", MappingProxyType(dict(self.populations))
        )
        return self

    def require_label(self) -> float:
        """Return the scalar reward or raise when no valid label exists."""
        if self.reward is None:
            raise UnlabelledSignalError(
                "this rollout could not be measured (label_valid is false); its "
                "whole GRPO group must be dropped and re-sampled, never scored 0.0"
            )
        return float(self.reward)


class UnlabelledSignalError(RuntimeError):
    """Raised by ``WorkspaceTrainingSignal.require_label`` when a caller tries
    to read a reward that is ``None`` (a harness fault, not a label)."""


def _codes_of(result: WorkspaceScoreResult) -> frozenset[str]:
    codes: set[str] = set()
    for name in result.graded_populations:
        codes.update(result.populations[name].error_codes)
    return frozenset(codes)


def _population_phase(score: PopulationWorkspaceScore) -> str:
    """The first phase this population failed in, from its heads alone
    (the scorer short-circuits Terraform -> sync -> dbt -> marts and gates
    end-to-end on immutability)."""
    if score.terraform_contract != 1.0:
        return "terraform"
    if not score.sync_lifecycle or score.strict_raw_tables != 1.0:
        return "sync"
    if score.dbt_project != 1.0:
        return "dbt"
    if score.mart_reward != 1.0:
        return "mart"
    if not score.raw_immutable:
        return "immutability"
    return "none"


def first_failed_phase(result: WorkspaceScoreResult) -> FirstFailedPhase:
    """Return the earliest failed phase among populations binding the reward."""
    if result.failure is not None:
        if result.failure.error_code in _WORKSPACE_PHASE_CODES:
            return "workspace"
        return "none"
    graded = [result.populations[name] for name in result.graded_populations]
    if not graded:
        return "none"
    floor = min(score.end_to_end_reward for score in graded)
    binding = [score for score in graded if math.isclose(score.end_to_end_reward, floor)]
    phases = sorted((_population_phase(score) for score in binding), key=_PHASE_RANK.__getitem__)
    return phases[0]  # type: ignore[return-value]


def training_signal(
    result: WorkspaceScoreResult,
    *,
    w_t: float = DEFAULT_W_T,
    infeasible_task: bool = False,
    aborted_infeasible: bool = False,
) -> WorkspaceTrainingSignal:
    """Project a score and two private canary flags into a deterministic signal.

    ``w_t`` must be finite and non-negative.
    """
    if not isinstance(result, WorkspaceScoreResult):
        raise TypeError("training_signal projects a WorkspaceScoreResult")
    w_t = float(w_t)
    if not math.isfinite(w_t) or w_t < 0.0:
        raise ValueError("w_t must be a finite, non-negative weight")

    # (1) label_valid FIRST: a replay-time task, harness or infrastructure
    # failure arrives as reward None and is never a label (C7).
    label_valid = result.reward is not None

    codes = _codes_of(result)
    policy_violation = bool(
        (
            result.failure is not None
            and result.failure.classification is WorkspaceFailureClass.POLICY_VIOLATION
        )
        or (codes & POLICY_VIOLATION_CODES)
    )
    # Non-emptiness is load-bearing: a failure result leaves `populations`
    # empty and all() over an empty map is true.
    el_pass = bool(
        result.valid_submission
        and result.failure is None
        and result.graded_populations
        and all(
            result.populations[name].strict_el_pass for name in result.graded_populations
        )
        and not policy_violation
    )
    r_el = 1.0 if el_pass else 0.0
    r_t = float(result.reward) if result.reward is not None else 0.0

    canary_task = bool(infeasible_task)
    canary_hit = False
    reward: float | None
    if not label_valid:
        reward = None
    elif policy_violation:
        reward = 0.0
    elif canary_task and (el_pass or r_t > 0.0):
        # An impossible task "passed": a hack event, forced to 0.0 and logged.
        canary_hit = True
        reward = 0.0
    elif canary_task and aborted_infeasible:
        # Score a valid infeasibility abort before generic invalid-submission handling.
        reward = 1.0
    elif not result.valid_submission:
        reward = 0.0
    else:
        reward = (r_el + (w_t * r_t if el_pass else 0.0)) / (1.0 + w_t)
        reward = min(1.0, max(0.0, reward))

    return WorkspaceTrainingSignal(
        scorer_version=result.scorer_version,
        artifact_sha256=result.artifact_sha256,
        label_valid=label_valid,
        el_pass=el_pass,
        policy_violation=policy_violation,
        r_el=r_el,
        r_t=r_t,
        w_t=w_t,
        reward=reward,
        first_failed_phase=first_failed_phase(result),
        canary_task=canary_task,
        canary_hit=canary_hit,
        populations=dict(result.populations),
    )


def drop_group_if_unlabelled(group: Sequence[WorkspaceTrainingSignal]) -> bool:
    """Return whether any rollout is unlabelled and the whole group must be dropped."""
    for member in group:
        if not isinstance(member, WorkspaceTrainingSignal):
            raise TypeError("a rollout group holds WorkspaceTrainingSignal members")
    return any(not member.label_valid for member in group)


__all__ = [
    "DEFAULT_W_T",
    "POLICY_VIOLATION_CODES",
    "SIGNAL_VERSION",
    "UnlabelledSignalError",
    "WorkspaceTrainingSignal",
    "drop_group_if_unlabelled",
    "first_failed_phase",
    "training_signal",
]

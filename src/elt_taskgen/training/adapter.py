"""Build fully labelled rollout groups for the declarative environment.

A group contains ``group_size`` runs of one task. If any result is unlabelled,
discard and resample the whole group with fresh state. Trainer rewards are read
through ``WorkspaceTrainingSignal.require_label`` and never coerce null to zero.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from elt_taskgen.training.signal import (
    WorkspaceTrainingSignal,
    drop_group_if_unlabelled,
)

#: Maximum fresh resampling attempts; exhaustion raises as a persistent
#: infrastructure failure instead of returning a partial or zero-scored group.
DEFAULT_MAX_RESAMPLES = 8


class GroupDropExhaustedError(RuntimeError):
    """Raised when every group resample still contains an unlabelled result."""


class SingleStepEnv(Protocol):
    """Single-step environment returning a ``WorkspaceTrainingSignal``."""

    def reset(self, task_id: str) -> Any: ...

    def step(self, artifact: Mapping[str, bytes]) -> Any: ...


@dataclass(frozen=True)
class RolloutGroup:
    """A fully-LABELLED GRPO group: every member carries a real reward."""

    task_id: str
    signals: tuple[WorkspaceTrainingSignal, ...]
    resamples: int

    def rewards(self) -> tuple[float, ...]:
        """Return scalar rewards, raising if any member is unlabelled."""
        return tuple(signal.require_label() for signal in self.signals)


class EnvGroupBuilder:
    """Samples labelled GRPO groups from a single-step env, applying the
    whole-group drop rule (finding 2-5)."""

    def __init__(
        self,
        env: SingleStepEnv,
        *,
        group_size: int,
        max_resamples: int = DEFAULT_MAX_RESAMPLES,
    ) -> None:
        if group_size < 1:
            raise ValueError("group_size must be >= 1")
        if max_resamples < 0:
            raise ValueError("max_resamples must be >= 0")
        self.env = env
        self.group_size = int(group_size)
        self.max_resamples = int(max_resamples)

    def _sample_once(
        self, task_id: str, policy: Callable[[Any], Mapping[str, bytes]]
    ) -> list[WorkspaceTrainingSignal]:
        signals: list[WorkspaceTrainingSignal] = []
        for _ in range(self.group_size):
            observation = self.env.reset(task_id)
            step = self.env.step(policy(observation))
            signal = getattr(step, "signal", None)
            if not isinstance(signal, WorkspaceTrainingSignal):
                raise TypeError("env.step() must return a StepResult carrying a signal")
            signals.append(signal)
        return signals

    def sample_group(
        self, task_id: str, policy: Callable[[Any], Mapping[str, bytes]]
    ) -> RolloutGroup:
        """Sample a fully labelled group, resampling whole groups on null labels."""
        for attempt in range(self.max_resamples + 1):
            signals = self._sample_once(task_id, policy)
            if not drop_group_if_unlabelled(signals):
                return RolloutGroup(task_id, tuple(signals), resamples=attempt)
        raise GroupDropExhaustedError(
            f"group for task {task_id!r} still held an unlabelled member after "
            f"{self.max_resamples + 1} attempts; a persistent harness fault"
        )


__all__ = [
    "DEFAULT_MAX_RESAMPLES",
    "EnvGroupBuilder",
    "GroupDropExhaustedError",
    "RolloutGroup",
    "SingleStepEnv",
]

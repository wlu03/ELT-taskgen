"""The rollout group adapter (roadmap 2.b; finding 2-5): the in-repo caller of
``drop_group_if_unlabelled`` that never hands a dropped group's member to the
trainer, and ``WorkspaceTrainingSignal.require_label`` which refuses to read a
``None`` reward as a number."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from elt_taskgen.training.adapter import (
    EnvGroupBuilder,
    GroupDropExhaustedError,
    RolloutGroup,
)
from elt_taskgen.training.env import unlabelled_signal
from elt_taskgen.training.signal import (
    SIGNAL_VERSION,
    UnlabelledSignalError,
    WorkspaceTrainingSignal,
)

_ZERO = "0" * 64


def _labelled(reward: float = 1.0) -> WorkspaceTrainingSignal:
    return WorkspaceTrainingSignal(
        signal_version=SIGNAL_VERSION,
        scorer_version="0.2.0",
        artifact_sha256=_ZERO,
        label_valid=True,
        el_pass=True,
        policy_violation=False,
        r_el=1.0,
        r_t=1.0,
        w_t=1.0,
        reward=reward,
        first_failed_phase="none",
        populations={},
    )


class _FakeEnv:
    """A single-step env that replays a flat queue of pre-built signals."""

    def __init__(self, signals):
        self._queue = list(signals)
        self.resets: list[str] = []

    def reset(self, task_id: str):
        self.resets.append(task_id)
        return SimpleNamespace(task_id=task_id)

    def step(self, artifact):
        return SimpleNamespace(signal=self._queue.pop(0))


class _FakeTrainer:
    """Records every reward the adapter hands it; a numeric reward for a
    dropped/unlabelled rollout would show up here."""

    def __init__(self):
        self.rewards: list[float] = []

    def observe(self, group: RolloutGroup) -> None:
        for reward in group.rewards():
            assert reward is not None
            self.rewards.append(reward)


class EnvGroupBuilderTests(unittest.TestCase):
    def test_env_group_builder_never_hands_a_dropped_group_member_to_the_trainer(self):
        # Group 1 has an unlabelled member -> dropped; group 2 is fully
        # labelled -> kept. The trainer only ever sees group 2's rewards.
        env = _FakeEnv(
            [
                _labelled(1.0),
                unlabelled_signal(),  # drops group 1
                _labelled(0.5),
                _labelled(1.0),  # group 2, kept
            ]
        )
        builder = EnvGroupBuilder(env, group_size=2, max_resamples=3)
        trainer = _FakeTrainer()
        group = builder.sample_group("demo__t", policy=lambda obs: {})
        trainer.observe(group)
        self.assertEqual(group.resamples, 1)
        self.assertEqual(sorted(group.rewards()), [0.5, 1.0])
        self.assertTrue(all(s.label_valid for s in group.signals))
        # The trainer received exactly the labelled group, never the dropped
        # member (its reward is None and could never be a number).
        self.assertEqual(sorted(trainer.rewards), [0.5, 1.0])
        self.assertEqual(env.resets.count("demo__t"), 4)  # 2 dropped + 2 kept

    def test_env_group_builder_raises_when_a_group_never_labels(self):
        env = _FakeEnv([unlabelled_signal() for _ in range(10)])
        builder = EnvGroupBuilder(env, group_size=2, max_resamples=2)
        trainer = _FakeTrainer()
        with self.assertRaises(GroupDropExhaustedError):
            trainer.observe(builder.sample_group("demo__t", policy=lambda obs: {}))
        # The trainer was handed no reward for the unmeasurable group.
        self.assertEqual(trainer.rewards, [])

    def test_require_label_refuses_to_read_a_none_reward_as_a_number(self):
        with self.assertRaises(UnlabelledSignalError):
            unlabelled_signal().require_label()
        self.assertEqual(_labelled(0.5).require_label(), 0.5)


if __name__ == "__main__":
    unittest.main()

"""Roadmap Phase 2 item 2.b: the ``workspace-signal-v1`` projection.

``training_signal`` is a PURE projection of one ``WorkspaceScoreResult``
(Output 8 §8.2): ``label_valid`` first, then the zero rules (policy
violation, invalid submission), then the gated composition
``R = (r_EL + 1[el_pass] * w_T * r_T) / (1 + w_T)``; the abort canary and
the whole-group drop rule ride beside it. ``score_workspace`` is untouched
(``tests/test_training_scorer.py`` keeps its subset-refusal pin).
"""

from __future__ import annotations

import json
import unittest

from elt_taskgen.training.contract import (
    WORKSPACE_SCORER_VERSION,
    WorkspaceErrorCode,
    WorkspaceFailureClass,
)
from elt_taskgen.training.models import (
    PopulationWorkspaceScore,
    WorkspaceFailure,
    WorkspaceScoreResult,
)
from elt_taskgen.training.signal import (
    POLICY_VIOLATION_CODES,
    SIGNAL_VERSION,
    WorkspaceTrainingSignal,
    drop_group_if_unlabelled,
    first_failed_phase,
    training_signal,
)
from elt_taskgen.verification.gates import GRADED_POPULATIONS


TASK_HASH = "a" * 64
ARTIFACT = "b" * 64
GRADED = tuple(population.value for population in GRADED_POPULATIONS)


def _population(
    name: str,
    *,
    terraform: float = 1.0,
    lifecycle: bool = True,
    strict_raw: float = 1.0,
    dbt: float = 1.0,
    mart: float = 1.0,
    immutable: bool = True,
    codes: tuple[str, ...] = (),
) -> PopulationWorkspaceScore:
    strict = terraform == 1.0 and lifecycle and strict_raw == 1.0
    mart_reward = mart if strict else 0.0
    return PopulationWorkspaceScore(
        population=name,
        graded=name != "development",
        terraform_contract=terraform,
        sync_lifecycle=lifecycle,
        upstream_stage1=lifecycle,
        strict_raw_tables=strict_raw,
        strict_el_pass=strict,
        dbt_project=dbt,
        mart_reward=mart_reward,
        raw_immutable=immutable,
        end_to_end_reward=mart_reward if strict and immutable else 0.0,
        error_codes=codes,
    )


def _result(populations: dict[str, PopulationWorkspaceScore]) -> WorkspaceScoreResult:
    graded = tuple(name for name in GRADED if name in populations)
    return WorkspaceScoreResult(
        release_id="release-1",
        task_id="task-1",
        task_content_hash=TASK_HASH,
        artifact_sha256=ARTIFACT,
        valid_submission=True,
        graded_populations=graded,
        populations=populations,
        reward=min(populations[name].end_to_end_reward for name in graded),
    )


def _uniform(**kwargs) -> WorkspaceScoreResult:
    return _result({name: _population(name, **kwargs) for name in GRADED})


def _failure_result(code: WorkspaceErrorCode, *, valid_submission: bool) -> WorkspaceScoreResult:
    classification = WorkspaceFailureClass(
        {
            WorkspaceErrorCode.HARNESS_INTERNAL: "harness_defect",
            WorkspaceErrorCode.TASK_PACKAGE_INVALID: "task_defect",
            WorkspaceErrorCode.INFRASTRUCTURE_UNAVAILABLE: "transient_infrastructure",
            WorkspaceErrorCode.WORKSPACE_NOT_FRESH: "harness_defect",
            WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE: "policy_violation",
            WorkspaceErrorCode.WORKSPACE_SECRET_LITERAL: "policy_violation",
            WorkspaceErrorCode.SUBMISSION_INVALID: "policy_failure",
            WorkspaceErrorCode.WORKSPACE_MISSING_ARTIFACT: "policy_failure",
        }[code]
    )
    return WorkspaceScoreResult(
        release_id="release-1",
        task_id="task-1",
        task_content_hash=TASK_HASH,
        artifact_sha256=ARTIFACT,
        valid_submission=valid_submission,
        reward=0.0 if classification.label_eligible else None,
        failure=WorkspaceFailure(classification=classification, error_code=code),
    )


class TrainingSignalCompositionTest(unittest.TestCase):
    def test_training_signal_el_only_lies_strictly_between_fail_and_e2e(self):
        fail = training_signal(_uniform(terraform=0.0, codes=("terraform_stream_contract",)))
        el_only = training_signal(_uniform(mart=0.0, codes=("dbt_mart_missing",)))
        e2e = training_signal(_uniform())
        self.assertEqual(fail.reward, 0.0)
        self.assertEqual(e2e.reward, 1.0)
        self.assertAlmostEqual(el_only.reward, 0.5)  # 1 / (1 + w_T) at w_T = 1
        self.assertLess(fail.reward, el_only.reward)
        self.assertLess(el_only.reward, e2e.reward)
        self.assertTrue(el_only.el_pass and el_only.label_valid)
        self.assertEqual((el_only.r_el, el_only.r_t), (1.0, 0.0))
        self.assertFalse(fail.el_pass)
        # The ordering holds under another w_T and the EL-only ceiling moves
        # with it: 1 / (1 + 3) = 0.25.
        self.assertAlmostEqual(
            training_signal(_uniform(mart=0.0, codes=("dbt_mart_missing",)), w_t=3.0).reward,
            0.25,
        )
        # A partial T on a strict-EL pass sits between EL-only and end to end.
        partial = training_signal(_uniform(mart=0.5))
        self.assertAlmostEqual(partial.reward, 0.75)
        self.assertEqual((fail.first_failed_phase, el_only.first_failed_phase, e2e.first_failed_phase),
                         ("terraform", "mart", "none"))
        for signal in (fail, el_only, e2e):
            self.assertEqual(signal.signal_version, SIGNAL_VERSION)
            self.assertEqual(signal.scorer_version, WORKSPACE_SCORER_VERSION)
            self.assertEqual(signal.artifact_sha256, ARTIFACT)
            self.assertFalse(signal.canary_task or signal.canary_hit)

    def test_training_signal_policy_violation_zeroes_el_credit(self):
        # Strict EL passed on every graded population, but dbt mutated the
        # raw state: a population-level violation code that never sets
        # `result.failure`. Without the rule R would be the EL-only 0.5.
        mutated = _uniform(immutable=False, codes=("dbt_raw_mutated",))
        self.assertIsNone(mutated.failure)
        self.assertTrue(all(mutated.populations[name].strict_el_pass for name in GRADED))
        signal = training_signal(mutated)
        self.assertTrue(signal.label_valid)
        self.assertTrue(signal.policy_violation)
        self.assertFalse(signal.el_pass)
        self.assertEqual((signal.r_el, signal.reward), (0.0, 0.0))
        self.assertEqual(signal.first_failed_phase, "immutability")
        # Every Terraform violation code and the unsafe-artifact code zero
        # the EL credit the same way, even on one population only.
        for code in sorted(POLICY_VIOLATION_CODES):
            populations = {name: _population(name) for name in GRADED}
            populations["counterfactual"] = _population("counterfactual", codes=(code,))
            one = training_signal(_result(populations))
            self.assertTrue(one.policy_violation, code)
            self.assertEqual(one.reward, 0.0, code)
            self.assertEqual(one.r_el, 0.0, code)
        # A NON-violation failure code keeps the EL credit (dbt parse failure).
        kept = training_signal(_uniform(dbt=0.0, mart=0.0, codes=("dbt_parse_failed",)))
        self.assertFalse(kept.policy_violation)
        self.assertAlmostEqual(kept.reward, 0.5)
        self.assertEqual(kept.first_failed_phase, "dbt")

    def test_training_signal_none_label_sets_label_valid_false_and_no_reward(self):
        for code in (
            WorkspaceErrorCode.HARNESS_INTERNAL,
            WorkspaceErrorCode.TASK_PACKAGE_INVALID,
            WorkspaceErrorCode.INFRASTRUCTURE_UNAVAILABLE,
        ):
            result = _failure_result(code, valid_submission=True)
            self.assertIsNone(result.reward)
            signal = training_signal(result)
            self.assertFalse(signal.label_valid, code)
            self.assertIsNone(signal.reward, code)
            self.assertFalse(signal.el_pass)
            self.assertEqual(signal.first_failed_phase, "none")
            # label_valid is decided FIRST: the canary flags cannot turn a
            # harness fault into 1.0, and a violation cannot make it 0.0.
            canary = training_signal(result, infeasible_task=True, aborted_infeasible=True)
            self.assertIsNone(canary.reward)
            self.assertFalse(canary.canary_hit)
        # A replay-time WORKSPACE_NOT_FRESH arrives as valid_submission=False
        # with reward None (harness defect): no label, phase `workspace`.
        stale = training_signal(
            _failure_result(WorkspaceErrorCode.WORKSPACE_NOT_FRESH, valid_submission=False)
        )
        self.assertFalse(stale.label_valid)
        self.assertIsNone(stale.reward)
        self.assertEqual(stale.first_failed_phase, "workspace")
        # The model refuses the contradiction directly.
        with self.assertRaises(ValueError):
            WorkspaceTrainingSignal(
                scorer_version=WORKSPACE_SCORER_VERSION,
                artifact_sha256=ARTIFACT,
                label_valid=False,
                el_pass=False,
                policy_violation=False,
                r_el=0.0,
                r_t=0.0,
                reward=0.0,
                first_failed_phase="none",
            )

    def test_training_signal_reward_is_pure_function_of_result_json(self):
        cases = [
            _uniform(),
            _uniform(mart=0.5),
            _uniform(mart=0.0, codes=("dbt_mart_missing",)),
            _uniform(lifecycle=False, strict_raw=0.0, codes=("local_sync_missing_stream",)),
            _uniform(immutable=False, codes=("dbt_raw_mutated",)),
            _failure_result(WorkspaceErrorCode.HARNESS_INTERNAL, valid_submission=True),
            _failure_result(WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE, valid_submission=False),
            _failure_result(WorkspaceErrorCode.SUBMISSION_INVALID, valid_submission=False),
        ]
        for result in cases:
            direct = training_signal(result)
            round_tripped = training_signal(
                WorkspaceScoreResult.model_validate_json(result.model_dump_json())
            )
            self.assertEqual(direct.model_dump(mode="json"), round_tripped.model_dump(mode="json"))
            # Same input, same output, no hidden state across calls.
            self.assertEqual(
                training_signal(result).model_dump(mode="json"), direct.model_dump(mode="json")
            )
            # The signal itself round-trips through JSON.
            json.loads(direct.model_dump_json())
            self.assertEqual(
                WorkspaceTrainingSignal.model_validate_json(direct.model_dump_json()),
                direct,
            )
            # Nothing but the result enters: the digests and versions are
            # copied, never recomputed from an artifact.
            self.assertEqual(direct.artifact_sha256, result.artifact_sha256)
            self.assertEqual(direct.scorer_version, result.scorer_version)
            self.assertEqual(dict(direct.populations), dict(result.populations))
        # The projection refuses anything that is not a result object.
        with self.assertRaises(TypeError):
            training_signal({"reward": 1.0})  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            training_signal(_uniform(), w_t=-1.0)

    def test_training_signal_workspace_violation_is_read_from_result_failure(self):
        # The seal or the replay classified the workspace itself (a path
        # escape, a secret literal): `result.failure` carries the code, the
        # submission is invalid, the label is a measured 0.0.
        for code in (
            WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE,
            WorkspaceErrorCode.WORKSPACE_SECRET_LITERAL,
        ):
            result = _failure_result(code, valid_submission=False)
            self.assertIs(result.failure.classification, WorkspaceFailureClass.POLICY_VIOLATION)
            signal = training_signal(result)
            self.assertTrue(signal.label_valid, code)
            self.assertTrue(signal.policy_violation, code)
            self.assertFalse(signal.el_pass)
            self.assertEqual(signal.reward, 0.0)
            self.assertEqual(signal.first_failed_phase, "workspace")
            self.assertEqual(dict(signal.populations), {})
        # An invalid submission that is a policy FAILURE (missing artifact)
        # is a measured 0.0 too, but not a violation.
        missing = training_signal(
            _failure_result(WorkspaceErrorCode.WORKSPACE_MISSING_ARTIFACT, valid_submission=False)
        )
        self.assertTrue(missing.label_valid)
        self.assertFalse(missing.policy_violation)
        self.assertEqual(missing.reward, 0.0)
        self.assertEqual(missing.first_failed_phase, "workspace")

    def test_planted_infeasible_canary_rewards_abort_and_forces_a_pass_to_zero(self):
        # The abort seals whatever exists (usually nothing scoreable): the
        # private flag plus the harness's abort record earn 1.0.
        aborted = _failure_result(WorkspaceErrorCode.WORKSPACE_MISSING_ARTIFACT, valid_submission=False)
        signal = training_signal(aborted, infeasible_task=True, aborted_infeasible=True)
        self.assertEqual(signal.reward, 1.0)
        self.assertTrue(signal.canary_task)
        self.assertFalse(signal.canary_hit)
        # Without the private flag an abort is scored like any artifact.
        self.assertEqual(training_signal(aborted, aborted_infeasible=True).reward, 0.0)
        # A "pass" on an impossible task is a canary hit: 0.0, logged.
        for result in (_uniform(), _uniform(mart=0.0, codes=("dbt_mart_missing",))):
            hit = training_signal(result, infeasible_task=True)
            self.assertTrue(hit.canary_hit)
            self.assertEqual(hit.reward, 0.0)
            hit_after_abort = training_signal(result, infeasible_task=True, aborted_infeasible=True)
            self.assertTrue(hit_after_abort.canary_hit)
            self.assertEqual(hit_after_abort.reward, 0.0)
        # A violation on an impossible task is still 0.0, not the abort reward.
        violated = _failure_result(WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE, valid_submission=False)
        self.assertEqual(
            training_signal(violated, infeasible_task=True, aborted_infeasible=True).reward, 0.0
        )

    def test_first_failed_phase_follows_the_binding_population_in_scorer_order(self):
        populations = {name: _population(name) for name in GRADED}
        populations["stress"] = _population("stress", mart=0.5, codes=("dbt_mart_key_invalid",))
        populations["resampled"] = _population(
            "resampled", lifecycle=False, strict_raw=0.0, codes=("local_sync_missing_stream",)
        )
        self.assertEqual(first_failed_phase(_result(populations)), "sync")
        populations["primary"] = _population("primary", terraform=0.0, codes=("terraform_parse_error",))
        self.assertEqual(first_failed_phase(_result(populations)), "terraform")
        self.assertEqual(first_failed_phase(_uniform(immutable=False, codes=("dbt_raw_mutated",))), "immutability")


class RolloutAdapterGroupDropTest(unittest.TestCase):
    def test_rollout_adapter_drops_entire_group_when_any_member_is_unlabelled(self):
        labelled = [training_signal(_uniform(mart=float(k) / 4)) for k in range(4)]
        self.assertFalse(drop_group_if_unlabelled(labelled))
        self.assertFalse(drop_group_if_unlabelled(()))
        unlabelled = training_signal(
            _failure_result(WorkspaceErrorCode.HARNESS_INTERNAL, valid_submission=True)
        )
        for position in range(4):
            group = list(labelled)
            group[position] = unlabelled
            # The whole group is dropped: one boolean over the group, never a
            # filtered subset with a replacement spliced in.
            self.assertTrue(drop_group_if_unlabelled(group))
            self.assertTrue(drop_group_if_unlabelled(tuple(group)))
        # A measured zero is a label and keeps its group.
        zero = training_signal(_failure_result(WorkspaceErrorCode.SUBMISSION_INVALID, valid_submission=False))
        self.assertEqual(zero.reward, 0.0)
        self.assertFalse(drop_group_if_unlabelled([*labelled, zero]))
        with self.assertRaises(TypeError):
            drop_group_if_unlabelled([labelled[0], {"label_valid": False}])  # type: ignore[list-item]


if __name__ == "__main__":
    unittest.main()

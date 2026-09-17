"""Frozen contract tests for cloud-free workspace-v1 scoring."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from elt_taskgen.training import (
    ARTIFACT_WORKFLOW_PROXY_CLAIM,
    PopulationWorkspaceScore,
    WORKSPACE_ERROR_CLASS_BY_CODE,
    WorkspaceActionTraceEntry,
    WorkspaceArtifactFile,
    WorkspaceErrorCode,
    WorkspaceFailure,
    WorkspaceFailureClass,
    WorkspaceScoreResult,
    WorkspaceSubmission,
    workspace_artifact_digest,
)


_ZERO = "0" * 64
_ONE = "1" * 64


def _files() -> tuple[WorkspaceArtifactFile, ...]:
    return (
        WorkspaceArtifactFile(path="elt/dbt_project.yml", size_bytes=1, sha256=_ZERO),
        WorkspaceArtifactFile(path="elt/main.tf", size_bytes=1, sha256=_ONE),
    )


def _score(population: str, reward: float, *, graded: bool = True):
    return PopulationWorkspaceScore(
        population=population,
        graded=graded,
        terraform_contract=1.0,
        sync_lifecycle=True,
        upstream_stage1=True,
        strict_raw_tables=1.0,
        strict_el_pass=True,
        dbt_project=1.0,
        mart_reward=reward,
        raw_immutable=True,
        end_to_end_reward=reward,
    )


class WorkspaceModelTests(unittest.TestCase):
    def test_submission_is_exact_versioned_and_hash_bound(self) -> None:
        files = _files()
        submission = WorkspaceSubmission(
            task_id="task",
            artifact_sha256=workspace_artifact_digest(files),
            files=files,
            action_trace=(
                WorkspaceActionTraceEntry(
                    sequence=0,
                    action="write_main_tf",
                    success=True,
                ),
            ),
        )
        self.assertEqual(submission.schema_version, "workspace-v1")
        self.assertEqual(submission.profile_owner, "harness")
        with self.assertRaises(ValidationError):
            WorkspaceSubmission(
                task_id="task",
                artifact_sha256=_ZERO,
                files=files,
            )
        with self.assertRaises(ValidationError):
            submission.task_id = "changed"  # type: ignore[misc]

    def test_submission_rejects_path_and_trace_ambiguity(self) -> None:
        with self.assertRaises(ValidationError):
            WorkspaceArtifactFile(path="../private/gold", size_bytes=1, sha256=_ZERO)
        colliding = (
            WorkspaceArtifactFile(path="elt/models/Mart.sql", size_bytes=1, sha256=_ZERO),
            WorkspaceArtifactFile(path="elt/models/mart.sql", size_bytes=1, sha256=_ONE),
        )
        with self.assertRaisesRegex(ValidationError, "case folding"):
            WorkspaceSubmission(
                task_id="task",
                artifact_sha256=workspace_artifact_digest(colliding),
                files=colliding,
            )
        files = _files()
        with self.assertRaisesRegex(ValidationError, "contiguous"):
            WorkspaceSubmission(
                task_id="task",
                artifact_sha256=workspace_artifact_digest(files),
                files=files,
                action_trace=(
                    WorkspaceActionTraceEntry(
                        sequence=1,
                        action="write_main_tf",
                        success=True,
                    ),
                ),
            )

    def test_population_score_enforces_strict_el_and_raw_gate(self) -> None:
        with self.assertRaisesRegex(ValidationError, "strict_el_pass"):
            PopulationWorkspaceScore(
                population="primary",
                graded=True,
                terraform_contract=1.0,
                sync_lifecycle=True,
                upstream_stage1=True,
                strict_raw_tables=0.5,
                strict_el_pass=True,
                dbt_project=1.0,
                mart_reward=1.0,
                raw_immutable=True,
                end_to_end_reward=1.0,
            )

    def test_legacy_workspace_error_codes_keep_the_same_json_shape(self) -> None:
        score = PopulationWorkspaceScore(
            population="primary",
            graded=True,
            terraform_contract=0.0,
            sync_lifecycle=False,
            upstream_stage1=False,
            strict_raw_tables=0.0,
            strict_el_pass=False,
            dbt_project=0.0,
            mart_reward=0.0,
            raw_immutable=False,
            end_to_end_reward=0.0,
            error_codes=(WorkspaceErrorCode.SUBMISSION_INVALID,),
        )
        self.assertEqual(score.error_codes, ("submission_invalid",))
        self.assertEqual(
            score.model_dump(mode="json")["error_codes"],
            ["submission_invalid"],
        )
        with self.assertRaisesRegex(ValidationError, "strict_el_pass"):
            PopulationWorkspaceScore(
                population="primary",
                graded=True,
                terraform_contract=0.9999999999,
                sync_lifecycle=True,
                upstream_stage1=True,
                strict_raw_tables=1.0,
                strict_el_pass=True,
                dbt_project=1.0,
                mart_reward=1.0,
                raw_immutable=True,
                end_to_end_reward=1.0,
            )
        with self.assertRaisesRegex(ValidationError, "strict EL/raw gate"):
            PopulationWorkspaceScore(
                population="primary",
                graded=True,
                terraform_contract=1.0,
                sync_lifecycle=True,
                upstream_stage1=True,
                strict_raw_tables=1.0,
                strict_el_pass=True,
                dbt_project=1.0,
                mart_reward=1.0,
                raw_immutable=False,
                end_to_end_reward=1.0,
            )

    def test_result_uses_hidden_population_minimum_and_proxy_claim(self) -> None:
        populations = {
            "primary": _score("primary", 1.0),
            "counterfactual": _score("counterfactual", 0.5),
            "development": _score("development", 1.0, graded=False),
        }
        result = WorkspaceScoreResult(
            release_id="release",
            task_id="task",
            task_content_hash=_ZERO,
            artifact_sha256=_ONE,
            valid_submission=True,
            graded_populations=("primary", "counterfactual"),
            populations=populations,
            reward=0.5,
        )
        self.assertEqual(result.claim, ARTIFACT_WORKFLOW_PROXY_CLAIM)
        self.assertEqual(result.aggregation, "minimum")
        populations.clear()
        self.assertEqual(set(result.populations), {"primary", "counterfactual", "development"})
        with self.assertRaises(TypeError):
            result.populations["primary"] = _score("primary", 0.0)  # type: ignore[index]
        self.assertIn("populations", result.model_dump(mode="json"))
        with self.assertRaisesRegex(ValidationError, "minimum graded reward"):
            WorkspaceScoreResult(
                **{**result.model_dump(mode="json"), "reward": 1.0}
            )

    def test_harness_and_task_failures_produce_no_training_label(self) -> None:
        no_label = WorkspaceFailure(
            classification=WorkspaceFailureClass.HARNESS_DEFECT,
            error_code=WorkspaceErrorCode.HARNESS_INTERNAL,
        )
        result = WorkspaceScoreResult(
            release_id="release",
            task_id="task",
            task_content_hash=_ZERO,
            artifact_sha256=_ONE,
            valid_submission=False,
            reward=None,
            failure=no_label,
        )
        self.assertIsNone(result.reward)
        self.assertFalse(no_label.label_eligible)
        with self.assertRaisesRegex(ValidationError, "produce no label"):
            WorkspaceScoreResult(
                release_id="release",
                task_id="task",
                task_content_hash=_ZERO,
                artifact_sha256=_ONE,
                valid_submission=False,
                reward=0.0,
                failure=no_label,
            )

    def test_invalid_submission_and_policy_violation_cannot_earn_reward(self) -> None:
        invalid = WorkspaceFailure(
            classification=WorkspaceFailureClass.POLICY_FAILURE,
            error_code=WorkspaceErrorCode.SUBMISSION_INVALID,
        )
        with self.assertRaisesRegex(ValidationError, "zero measured reward"):
            WorkspaceScoreResult(
                release_id="release",
                task_id="task",
                task_content_hash=_ZERO,
                artifact_sha256=_ONE,
                valid_submission=False,
                reward=1.0,
                failure=invalid,
            )
        violation = WorkspaceFailure(
            classification=WorkspaceFailureClass.POLICY_VIOLATION,
            error_code=WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE,
        )
        with self.assertRaisesRegex(ValidationError, "zero reward"):
            WorkspaceScoreResult(
                release_id="release",
                task_id="task",
                task_content_hash=_ZERO,
                artifact_sha256=_ONE,
                valid_submission=True,
                graded_populations=("primary",),
                populations={"primary": _score("primary", 1.0)},
                reward=1.0,
                failure=violation,
            )

    def test_failure_code_cannot_change_label_classification(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires classification"):
            WorkspaceFailure(
                classification=WorkspaceFailureClass.POLICY_FAILURE,
                error_code=WorkspaceErrorCode.HARNESS_INTERNAL,
            )
        with self.assertRaises(TypeError):
            WORKSPACE_ERROR_CLASS_BY_CODE[  # type: ignore[index]
                WorkspaceErrorCode.HARNESS_INTERNAL
            ] = WorkspaceFailureClass.POLICY_FAILURE


if __name__ == "__main__":
    unittest.main()

"""Tests for export/certification.py (IR-006, migration-plan Phase 5).

WHY THIS EXISTS
Certification is the fail-closed record that one runtime bundle was actually
exercised under one execution matrix. The state machine must never leave a
partial CERTIFIED: stage-2 evidence without stage-1 verification is refused
outright, any refusal returns to UNCERTIFIED with the reason recorded, a
sealed attestation is immutable and checksum-bound, and a tampered record
reads as UNCERTIFIED — reported, never raised. Input changes reset state
STRUCTURALLY: a new matrix means a new certification id whose store directory
does not exist yet, while the old id's evidence stays intact for reuse.

The frozen-release fixture is borrowed from tests/test_export.py's
TestFreezeRelease harness (imported as a module, the way test_split_digests
imports test_reference, so its own tests are not re-collected here).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import warnings
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

try:
    import test_export as export_fixture
except ModuleNotFoundError:  # package-style invocation
    from tests import test_export as export_fixture

from elt_taskgen import runtime_matrix
from elt_taskgen.destinations import Destination
from elt_taskgen.export import certification, release
from elt_taskgen.runtime import execution as execution_mod
from elt_taskgen.runtime.evaluation import (
    Stage1EvaluationResult,
    Stage2EvaluationResult,
)
from elt_taskgen.runtime.execution import (
    Stage1Execution,
    Stage2Execution,
    Stage2Preflight,
)


class CertificationTestCase(unittest.TestCase):
    """One frozen current-schema release + one empty attestation store per test."""

    def setUp(self):
        self.harness = export_fixture.TestFreezeRelease("freeze")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.manifest = self.harness.freeze()
        self.task_id = self.harness.task.task_id
        self.certification_id = self.manifest.certification_ids[self.task_id]
        store = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(store, ignore_errors=True))
        self.store = Path(store)

    def evidence_pair(
        self,
        population="primary",
        *,
        attempt_id=None,
        stage1_updates=None,
        stage2_updates=None,
        stage1_window=None,
        stage2_window=None,
    ):
        tables = tuple(table.name for table in self.harness.task.tables)
        marts = tuple(mart.name for mart in self.harness.task.marts)
        counts = {table: 1 for table in tables}
        table_digests = {
            table: (str(index + 1) * 64)[:64]
            for index, table in enumerate(tables)
        }
        mart_digests = {
            mart: (format(index + 10, "x") * 64)[:64]
            for index, mart in enumerate(marts)
        }
        stage1_result = Stage1EvaluationResult(
            database=self.task_id,
            population=population,
            expected_counts=counts,
            actual_counts=counts,
            missing_tables=(),
            unexpected_tables=(),
            count_mismatches={},
            detail={table: "ok (1 row)" for table in tables},
            errors={},
            passed=True,
            reward=1.0,
            canonical_table_scores={table: True for table in tables},
            canonical_mismatch_codes={table: "" for table in tables},
            canonical_reference_fingerprints=table_digests,
            canonical_actual_fingerprints=table_digests,
        )
        if stage1_updates:
            stage1_result = replace(stage1_result, **stage1_updates)
        stage2_result = Stage2EvaluationResult(
            database=self.task_id,
            population=population,
            mart_scores={mart: True for mart in marts},
            errors={},
            reward=1.0,
            strict_mart_scores={mart: True for mart in marts},
            strict_mismatch_codes={mart: "" for mart in marts},
            column_fingerprints={
                mart.name: tuple(
                    (column.name, column.type.value) for column in mart.columns
                )
                for mart in self.harness.task.marts
            },
            canonical_mart_scores={mart: True for mart in marts},
            canonical_mismatch_codes={mart: "" for mart in marts},
            canonical_reference_fingerprints=mart_digests,
            canonical_actual_fingerprints=mart_digests,
        )
        if stage2_updates:
            stage2_result = replace(stage2_result, **stage2_updates)
        pending_path = self.store / self.certification_id / certification.PENDING_FILENAME
        resolved_attempt = attempt_id
        if resolved_attempt is None and pending_path.is_file():
            resolved_attempt = certification.pending_attempt_id(pending_path.parent)
        resolved_attempt = resolved_attempt or f"standalone-attempt-{population}"
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        stage1_window = stage1_window or (now, now)
        stage2_window = stage2_window or (now, now)
        matrix = self.manifest.certification_matrix[
            self.manifest.destinations[self.task_id]
        ]
        model_ids = tuple(
            sorted(f"model.certification.{mart}" for mart in marts)
        )
        stage1 = certification.stage1_evidence_from_result(
            self.manifest,
            self.task_id,
            stage1_result,
            attempt_id=resolved_attempt,
            execution_started_at=stage1_window[0],
            execution_completed_at=stage1_window[1],
            runner_image=matrix["runner_image:terraform"],
            terraform_input_tree_digest="e" * 64,
            terraform_state_digest="c" * 64,
            airbyte_job_ids={
                f"connection-{index}": index
                for index, _ in enumerate(tables, start=1)
            },
        )
        stage2 = certification.stage2_evidence_from_result(
            self.manifest,
            self.task_id,
            stage2_result,
            attempt_id=resolved_attempt,
            execution_started_at=stage2_window[0],
            execution_completed_at=stage2_window[1],
            runner_image=matrix["runner_image:dbt"],
            preflight_destination=matrix["destination"],
            preflight_dbt_core_version=matrix["dbt_core_version"],
            preflight_adapter_version=matrix["dbt_adapter_version"],
            dbt_input_tree_digest="f" * 64,
            dbt_invocation_id=f"dbt-{population}",
            dbt_run_results_digest="d" * 64,
            dbt_expected_model_ids=model_ids,
            dbt_observed_model_ids=model_ids,
        )
        return stage1, stage2

    def certify(self, *, populations=("primary",), observed_versions=None):
        """begin + complete one evidence-backed certification."""
        pending_dir = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        versions = observed_versions
        if versions is None:
            matrix = self.manifest.certification_matrix[
                self.manifest.destinations[self.task_id]
            ]
            versions = {
                key: matrix[key]
                for key in certification._required_observation_keys(matrix)
            }
        return pending_dir, certification.complete_certification(
            pending_dir,
            manifest=self.manifest,
            task_id=self.task_id,
            evidence_pairs=tuple(
                self.evidence_pair(population) for population in populations
            ),
            observed_versions=versions,
        )

    def status(self, certification_id=None):
        return certification.certification_status(
            self.store, certification_id or self.certification_id
        )

    def test_unknown_id_is_uncertified(self):
        status = self.status("certification-" + "0" * 16)
        self.assertIs(status.state, certification.CertificationState.UNCERTIFIED)
        self.assertTrue(status.reason)

    def test_begin_transitions_to_pending_and_refuses_concurrent_begin(self):
        pending_dir = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        self.assertEqual(pending_dir.name, self.certification_id)
        attempt_id = certification.pending_attempt_id(pending_dir)
        self.assertRegex(attempt_id, r"^certification-attempt-[0-9a-f]{64}$")
        self.assertTrue(
            (pending_dir / certification.PENDING_FILENAME).is_file()
        )
        self.assertIs(
            self.status().state, certification.CertificationState.PENDING
        )
        with self.assertRaisesRegex(
            certification.CertificationError, "pending"
        ):
            certification.begin_certification(
                self.store, self.manifest, self.task_id
            )

    def test_pending_attempt_nonce_refuses_replayed_evidence(self):
        stage1, stage2 = self.evidence_pair(attempt_id="old-attempt")
        pending_dir = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        with self.assertRaisesRegex(
            certification.CertificationError, "active pending attempt"
        ):
            certification.complete_certification(
                pending_dir,
                manifest=self.manifest,
                task_id=self.task_id,
                evidence_pairs=((stage1, stage2),),
                observed_versions={},
            )

    def test_first_completion_lock_winner_owns_refusal_for_one_nonce(self):
        pending_dir = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        pair = self.evidence_pair()
        matrix = self.manifest.certification_matrix[
            self.manifest.destinations[self.task_id]
        ]
        valid_observations = {
            key: matrix[key]
            for key in certification._required_observation_keys(matrix)
        }
        winner_entered = threading.Event()
        release_winner = threading.Event()
        winner_outcome = []
        real_normalize = certification._normalize_supplied_observations

        def block_refusal_winner(values):
            if threading.current_thread().name == "refusal-winner":
                winner_entered.set()
                if not release_winner.wait(timeout=5):
                    raise AssertionError("timed out releasing refusal winner")
            return real_normalize(values)

        def run_refusal_winner():
            try:
                certification.complete_certification(
                    pending_dir,
                    manifest=self.manifest,
                    task_id=self.task_id,
                    evidence_pairs=(pair,),
                    observed_versions={},
                )
            except BaseException as exc:  # recorded for assertion in main thread
                winner_outcome.append(exc)

        with mock.patch.object(
            certification,
            "_normalize_supplied_observations",
            side_effect=block_refusal_winner,
        ):
            winner = threading.Thread(
                target=run_refusal_winner,
                name="refusal-winner",
            )
            winner.start()
            self.assertTrue(winner_entered.wait(timeout=5))
            try:
                with self.assertRaisesRegex(
                    certification.CertificationError,
                    "already owns the pending certification attempt",
                ):
                    certification.complete_certification(
                        pending_dir,
                        manifest=self.manifest,
                        task_id=self.task_id,
                        evidence_pairs=(pair,),
                        observed_versions=valid_observations,
                    )
            finally:
                release_winner.set()
                winner.join(timeout=5)
        self.assertFalse(winner.is_alive())
        self.assertEqual(len(winner_outcome), 1)
        self.assertIsInstance(winner_outcome[0], certification.CertificationError)
        self.assertIn("observations are missing", str(winner_outcome[0]))
        self.assertFalse(
            (pending_dir / certification.PENDING_FILENAME).exists()
        )
        self.assertFalse(
            (pending_dir / certification.ATTESTATION_FILENAME).exists()
        )
        # The losing valid completion cannot publish after the winning refusal;
        # callers must explicitly begin a fresh nonce.
        with self.assertRaisesRegex(
            certification.CertificationError, "no pending certification attempt"
        ):
            certification.complete_certification(
                pending_dir,
                manifest=self.manifest,
                task_id=self.task_id,
                evidence_pairs=(pair,),
                observed_versions=valid_observations,
            )

    def test_execution_cannot_predate_pending_attempt(self):
        pending_dir = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        attempt_id = certification.pending_attempt_id(pending_dir)
        pending = json.loads(
            (pending_dir / certification.PENDING_FILENAME).read_text(encoding="utf-8")
        )
        before_pending = (
            datetime.fromisoformat(pending["started_at"].replace("Z", "+00:00"))
            - timedelta(microseconds=1)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        old_window = (before_pending, before_pending)
        stage1, stage2 = self.evidence_pair(
            attempt_id=attempt_id,
            stage1_window=old_window,
            stage2_window=old_window,
        )
        with self.assertRaisesRegex(
            certification.CertificationError, "predates the active pending attempt"
        ):
            certification.complete_certification(
                pending_dir,
                manifest=self.manifest,
                task_id=self.task_id,
                evidence_pairs=((stage1, stage2),),
                observed_versions={},
            )

    def test_stage1_failure_never_certifies_and_stage2_cannot_follow(self):
        pending_dir = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        stage1, stage2 = self.evidence_pair(
            stage1_updates={"passed": False, "reward": 0.0}
        )
        with self.assertRaisesRegex(
            certification.CertificationError, "Stage-1 evidence"
        ):
            certification.complete_certification(
                pending_dir,
                manifest=self.manifest,
                task_id=self.task_id,
                evidence_pairs=((stage1, stage2),),
                observed_versions={},
            )
        self.assertTrue(
            (pending_dir / certification.REFUSED_FILENAME).is_file()
        )
        self.assertFalse(
            (pending_dir / certification.PENDING_FILENAME).exists()
        )
        self.assertIs(
            self.status().state, certification.CertificationState.UNCERTIFIED
        )

        # A reversed pair cannot smuggle Stage 2 past the Stage-1 position.
        pending_dir = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        with self.assertRaisesRegex(
            certification.CertificationError,
            "Stage-1 evidence",
        ):
            certification.complete_certification(
                pending_dir,
                manifest=self.manifest,
                task_id=self.task_id,
                evidence_pairs=((stage2, stage1),),
                observed_versions={},
            )
        self.assertIs(
            self.status().state, certification.CertificationState.UNCERTIFIED
        )

    def test_sealed_attestation_certifies_and_verifies(self):
        pending_dir, attestation = self.certify()
        self.assertEqual(attestation.certification_id, self.certification_id)
        self.assertEqual(
            attestation.runtime_bundle_id,
            self.manifest.runtime_bundle_ids[self.task_id],
        )
        self.assertEqual(
            attestation.semantic_release_id, self.manifest.semantic_release_id
        )
        self.assertEqual(attestation.release_id, self.manifest.release_id)
        self.assertEqual(attestation.populations, ("primary",))
        self.assertTrue(attestation.attestation_digest)
        self.assertIs(
            self.status().state, certification.CertificationState.CERTIFIED
        )
        self.assertEqual(self.status().populations, ("primary",))
        attestation_path = pending_dir / certification.ATTESTATION_FILENAME
        verified = certification.verify_attestation(attestation_path)
        self.assertEqual(verified, attestation)
        # Immutable: read-only on disk, and a second completion is refused.
        self.assertEqual(attestation_path.stat().st_mode & 0o222, 0)
        with self.assertRaisesRegex(
            certification.CertificationError, "sealed attestation"
        ):
            stage1, stage2 = self.evidence_pair()
            certification.complete_certification(
                pending_dir,
                manifest=self.manifest,
                task_id=self.task_id,
                evidence_pairs=((stage1, stage2),),
                observed_versions={},
            )
        # ... as is a rerun from the top: reuse, do not re-certify.
        with self.assertRaisesRegex(
            certification.CertificationError, "reuse"
        ):
            certification.begin_certification(
                self.store, self.manifest, self.task_id
            )

    def test_attestation_records_multi_population_evidence_coverage(self):
        pending_dir, attestation = self.certify(
            populations=("primary", "counterfactual")
        )
        self.assertEqual(
            attestation.populations, ("counterfactual", "primary")
        )
        self.assertEqual(
            set(attestation.evidence_digests),
            {
                "counterfactual/stage1.json",
                "counterfactual/stage2.json",
                "primary/stage1.json",
                "primary/stage2.json",
            },
        )
        for rel in attestation.evidence_digests:
            evidence_path = pending_dir / certification.EVIDENCE_DIRNAME / rel
            self.assertTrue(evidence_path.is_file())
            self.assertEqual(evidence_path.stat().st_mode & 0o222, 0)
        status = self.status()
        self.assertIs(status.state, certification.CertificationState.CERTIFIED)
        self.assertEqual(status.populations, attestation.populations)

    def test_mismatched_attempt_pair_is_refused(self):
        pending_dir = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        stage1, _ = self.evidence_pair(attempt_id="attempt-one")
        _, stage2 = self.evidence_pair(attempt_id="attempt-two")
        with self.assertRaisesRegex(
            certification.CertificationError, "different attempts"
        ):
            certification.complete_certification(
                pending_dir,
                manifest=self.manifest,
                task_id=self.task_id,
                evidence_pairs=((stage1, stage2),),
                observed_versions={},
            )
        self.assertIs(
            self.status().state, certification.CertificationState.UNCERTIFIED
        )

    def test_execution_builders_bind_exact_jobs_images_and_dbt_artifact(self):
        synthetic_stage1, synthetic_stage2 = self.evidence_pair()
        stage1_result = Stage1EvaluationResult(
            **synthetic_stage1.result.model_dump(mode="python")
        )
        stage2_result = Stage2EvaluationResult(
            **synthetic_stage2.result.model_dump(mode="python")
        )
        matrix = self.manifest.certification_matrix[
            self.manifest.destinations[self.task_id]
        ]
        workspace = (self.store / "terraform-workspace").resolve()
        terraform_dir = workspace / "elt"
        terraform_dir.mkdir(parents=True)
        (terraform_dir / "main.tf").write_text(
            "terraform {}\n", encoding="utf-8"
        )
        state_path = terraform_dir / "terraform.tfstate"
        state_path.write_text(
            json.dumps(
                {
                    "lineage": "certification-test-lineage",
                    "serial": 1,
                    "resources": [
                        {
                            "mode": "managed",
                            "type": "airbyte_connection",
                            "name": f"connection_{index}",
                            "instances": [
                                {
                                    "attributes": {
                                        "connection_id": f"connection-{index}"
                                    }
                                }
                            ],
                        }
                        for index in (1, 2)
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        state_identity = execution_mod._terraform_state_identity(state_path)
        stage1_execution = Stage1Execution(
            connection_ids=("connection-1", "connection-2"),
            statuses={"connection-1": "succeeded", "connection-2": "succeeded"},
            job_ids={"connection-1": 101, "connection-2": 102},
            terraform_state_digest=state_identity.digest,
            runner_image=matrix["runner_image:terraform"],
            execution_started_at="2026-09-03T00:00:00Z",
            execution_completed_at="2026-09-03T00:00:01Z",
            terraform_input_tree_digest=(
                execution_mod._terraform_input_tree_digest(workspace)
            ),
            workspace_dir=workspace,
            terraform_state_path=state_path,
            terraform_state_lineage=state_identity.lineage,
            terraform_state_serial=state_identity.serial,
            terraform_connection_resources=state_identity.connection_resources,
        )
        project = self.store / "dbt-project"
        models_dir = project / "models"
        target_dir = project / ".elt-taskgen-dbt-target-certification"
        models_dir.mkdir(parents=True)
        target_dir.mkdir()
        (project / "dbt_project.yml").write_text(
            "name: certification\nprofile: certification\n", encoding="utf-8"
        )
        (project / "profiles.yml").write_text("{}\n", encoding="utf-8")
        for mart in self.harness.task.marts:
            (models_dir / f"{mart.name}.sql").write_text(
                "select 1 as value\n", encoding="utf-8"
            )
        dbt_input_digest, expected_model_ids = (
            execution_mod._dbt_submission_identity(project, project)
        )
        run_results = target_dir / "run_results.json"
        valid_run_results = {
            "metadata": {"invocation_id": "dbt-primary"},
            "results": [
                {
                    "status": "success",
                    "unique_id": f"model.certification.{mart.name}",
                }
                for mart in self.harness.task.marts
            ],
        }
        run_results.write_text(
            json.dumps(valid_run_results, sort_keys=True), encoding="utf-8"
        )
        stage2_execution = Stage2Execution(
            project_dir=project,
            profiles_dir=project,
            preflight=Stage2Preflight(
                destination=Destination.SNOWFLAKE,
                dbt_core_version=matrix["dbt_core_version"],
                adapter_version=matrix["dbt_adapter_version"],
                profile_name="profile",
                target_name="target",
                namespace=stage2_result.database,
            ),
            run_results_path=run_results,
            dbt_invocation_id="dbt-primary",
            dbt_run_results_digest=release._file_sha256(run_results),
            runner_image=matrix["runner_image:dbt"],
            execution_started_at="2026-09-03T00:00:02Z",
            execution_completed_at="2026-09-03T00:00:03Z",
            dbt_input_tree_digest=dbt_input_digest,
            dbt_expected_model_ids=expected_model_ids,
            dbt_observed_model_ids=expected_model_ids,
        )

        stage1 = certification.stage1_evidence_from_execution(
            self.manifest,
            self.task_id,
            stage1_result,
            stage1_execution,
            attempt_id="attempt-primary",
        )
        stage2 = certification.stage2_evidence_from_execution(
            self.manifest,
            self.task_id,
            stage2_result,
            stage2_execution,
            attempt_id="attempt-primary",
        )
        self.assertEqual(stage1.airbyte_job_ids["connection-1"], "101")
        self.assertEqual(
            stage1.terraform_input_tree_digest,
            execution_mod._terraform_input_tree_digest(workspace),
        )
        self.assertEqual(stage1.terraform_state_digest, state_identity.digest)
        self.assertEqual(stage1.execution_completed_at, "2026-09-03T00:00:01Z")
        self.assertEqual(stage1.runner_image, matrix["runner_image:terraform"])
        self.assertEqual(stage2.dbt_input_tree_digest, dbt_input_digest)
        self.assertEqual(stage2.dbt_invocation_id, "dbt-primary")
        self.assertEqual(
            stage2.dbt_run_results_digest,
            release._file_sha256(run_results),
        )
        self.assertEqual(stage2.runner_image, matrix["runner_image:dbt"])
        self.assertEqual(stage2.preflight_destination, matrix["destination"])
        self.assertEqual(
            stage2.preflight_dbt_core_version, matrix["dbt_core_version"]
        )
        self.assertEqual(
            stage2.preflight_adapter_version, matrix["dbt_adapter_version"]
        )
        self.assertEqual(stage2.dbt_expected_model_ids, expected_model_ids)
        self.assertEqual(stage2.dbt_observed_model_ids, expected_model_ids)

        wrong_image = replace(stage1_execution, runner_image="wrong@sha256:" + "0" * 64)
        with self.assertRaisesRegex(
            certification.CertificationError, "release-bound Terraform image"
        ):
            certification.stage1_evidence_from_execution(
                self.manifest,
                self.task_id,
                stage1_result,
                wrong_image,
                attempt_id="attempt-primary",
            )

        wrong_namespace = replace(
            stage2_execution,
            preflight=replace(stage2_execution.preflight, namespace="another_db"),
        )
        with self.assertRaisesRegex(
            certification.CertificationError, "preflight namespace"
        ):
            certification.stage2_evidence_from_execution(
                self.manifest,
                self.task_id,
                stage2_result,
                wrong_namespace,
                attempt_id="attempt-primary",
            )

        with self.assertRaisesRegex(
            certification.CertificationError, "requested physical container"
        ):
            certification.stage2_evidence_from_execution(
                self.manifest,
                self.task_id,
                stage2_result,
                stage2_execution,
                attempt_id="attempt-primary",
                physical_container="caller-invented-container",
            )

        invalid_artifacts = (
            (
                {"metadata": {"invocation_id": "dbt-primary"}},
                "no model results",
            ),
            (
                {
                    "metadata": {"invocation_id": "dbt-primary"},
                    "results": [
                        {
                            "status": "error",
                            "unique_id": expected_model_ids[0],
                        }
                    ],
                },
                "did not succeed",
            ),
            (
                {
                    "metadata": {"invocation_id": "dbt-primary"},
                    "results": [
                        {"status": "success", "unique_id": "model.other.wrong"}
                    ],
                },
                "model roster mismatch",
            ),
        )
        for payload, message in invalid_artifacts:
            with self.subTest(artifact=message):
                run_results.write_text(json.dumps(payload), encoding="utf-8")
                changed_execution = replace(
                    stage2_execution,
                    dbt_run_results_digest=release._file_sha256(run_results),
                )
                with self.assertRaisesRegex(certification.CertificationError, message):
                    certification.stage2_evidence_from_execution(
                        self.manifest,
                        self.task_id,
                        stage2_result,
                        changed_execution,
                        attempt_id="attempt-primary",
                    )

        run_results.write_text(
            json.dumps(valid_run_results, sort_keys=True), encoding="utf-8"
        )
        outside_artifact = self.store / "run_results.json"
        outside_artifact.write_bytes(run_results.read_bytes())
        outside_execution = replace(
            stage2_execution,
            run_results_path=outside_artifact,
            dbt_run_results_digest=release._file_sha256(outside_artifact),
        )
        with self.assertRaisesRegex(
            certification.CertificationError, "fresh harness target"
        ):
            certification.stage2_evidence_from_execution(
                self.manifest,
                self.task_id,
                stage2_result,
                outside_execution,
                attempt_id="attempt-primary",
            )

    def test_execution_order_uses_run_window_not_evidence_sealing_time(self):
        pending_dir = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        pending = json.loads(
            (pending_dir / certification.PENDING_FILENAME).read_text(encoding="utf-8")
        )
        later = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        stage1, stage2 = self.evidence_pair(
            stage1_window=(later, later),
            stage2_window=(pending["started_at"], pending["started_at"]),
        )
        with self.assertRaisesRegex(
            certification.CertificationError,
            "Stage-2 execution began before Stage-1 execution completed",
        ):
            certification.complete_certification(
                pending_dir,
                manifest=self.manifest,
                task_id=self.task_id,
                evidence_pairs=((stage1, stage2),),
                observed_versions={},
            )

    def test_sealed_evidence_rejects_invalid_execution_input_identity(self):
        stage1, stage2 = self.evidence_pair()
        invalid_stage1 = certification.seal_stage_evidence(
            stage1.model_copy(
                update={"terraform_input_tree_digest": "", "evidence_digest": ""}
            )
        )
        with self.assertRaisesRegex(
            certification.CertificationError, "Terraform input digest"
        ):
            certification.verify_stage1_evidence(invalid_stage1)

        invalid_stage2 = certification.seal_stage_evidence(
            stage2.model_copy(
                update={"execution_started_at": "not-a-time", "evidence_digest": ""}
            )
        )
        with self.assertRaisesRegex(
            certification.CertificationError, "execution_started_at"
        ):
            certification.verify_stage2_evidence(invalid_stage2)

    def test_tampered_persisted_stage_evidence_uncertifies(self):
        pending_dir, _ = self.certify()
        evidence_path = (
            pending_dir
            / certification.EVIDENCE_DIRNAME
            / "primary"
            / "stage1.json"
        )
        evidence_path.chmod(0o600)
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        table = next(iter(payload["result"]["canonical_actual_fingerprints"]))
        payload["result"]["canonical_actual_fingerprints"][table] = "f" * 64
        evidence_path.write_text(json.dumps(payload), encoding="utf-8")
        status = self.status()
        self.assertIs(status.state, certification.CertificationState.UNCERTIFIED)
        self.assertIn("evidence digest mismatch", status.reason)

    def test_uninventoried_evidence_file_uncertifies(self):
        pending_dir, _ = self.certify()
        extra = pending_dir / certification.EVIDENCE_DIRNAME / "extra.json"
        extra.write_text("{}\n", encoding="utf-8")
        status = self.status()
        self.assertIs(status.state, certification.CertificationState.UNCERTIFIED)
        self.assertIn("inventory", status.reason)

    def test_observed_version_contradiction_refuses(self):
        matrix = self.manifest.certification_matrix[
            self.manifest.destinations[self.task_id]
        ]
        versions = {
            key: matrix[key]
            for key in certification._required_observation_keys(matrix)
        }
        versions["destination_connector"] = "0.0.1"
        with self.assertRaisesRegex(
            certification.CertificationError, "contradict"
        ):
            self.certify(observed_versions=versions)
        self.assertIs(
            self.status().state, certification.CertificationState.UNCERTIFIED
        )

    def test_missing_runtime_observations_refuse_certification(self):
        with self.assertRaisesRegex(
            certification.CertificationError, "observations are missing"
        ):
            self.certify(observed_versions={})
        self.assertIs(
            self.status().state, certification.CertificationState.UNCERTIFIED
        )

    def test_unrecognized_observation_key_is_never_persisted(self):
        matrix = self.manifest.certification_matrix[
            self.manifest.destinations[self.task_id]
        ]
        versions = {
            key: matrix[key]
            for key in certification._required_observation_keys(matrix)
        }
        versions["password"] = "must-not-enter-an-attestation"
        with self.assertRaisesRegex(
            certification.CertificationError, "unrecognized execution observations"
        ):
            self.certify(observed_versions=versions)
        self.assertIs(
            self.status().state, certification.CertificationState.UNCERTIFIED
        )

    def test_receipt_observations_are_derived_and_caller_values_are_redacted(self):
        matrix = self.manifest.certification_matrix[
            self.manifest.destinations[self.task_id]
        ]
        derived = {
            "destination",
            "dbt_adapter_version",
            "dbt_core_version",
            "runner_image:dbt",
            "runner_image:terraform",
        }
        supplied = {
            key: matrix[key]
            for key in certification._required_observation_keys(matrix) - derived
        }
        pending_dir, attestation = self.certify(observed_versions=supplied)
        for key in derived:
            self.assertEqual(attestation.observed_versions[key], matrix[key])
        self.assertTrue(
            (pending_dir / certification.ATTESTATION_FILENAME).is_file()
        )

        second = CertificationTestCase("runTest")
        second.setUp()
        self.addCleanup(second.doCleanups)
        secret_value = "credential-like-value-must-not-be-recorded"
        bad_matrix = second.manifest.certification_matrix[
            second.manifest.destinations[second.task_id]
        ]
        bad = {
            key: bad_matrix[key]
            for key in certification._required_observation_keys(bad_matrix)
        }
        bad["destination_connector"] = secret_value
        with self.assertRaises(certification.CertificationError) as raised:
            second.certify(observed_versions=bad)
        self.assertIn("destination_connector", str(raised.exception))
        self.assertNotIn(secret_value, str(raised.exception))
        refused = (
            second.store
            / second.certification_id
            / certification.REFUSED_FILENAME
        ).read_text(encoding="utf-8")
        self.assertNotIn(secret_value, refused)

    def test_stage2_model_receipt_is_rechecked_against_release_roster(self):
        pending_dir = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        stage1, stage2 = self.evidence_pair()
        forged_ids = ("model.certification.not_a_released_mart",)
        forged = certification.seal_stage_evidence(
            stage2.model_copy(
                update={
                    "dbt_expected_model_ids": forged_ids,
                    "dbt_observed_model_ids": forged_ids,
                    "evidence_digest": "",
                }
            )
        )
        with self.assertRaisesRegex(
            certification.CertificationError, "released mart roster"
        ):
            certification.complete_certification(
                pending_dir,
                manifest=self.manifest,
                task_id=self.task_id,
                evidence_pairs=((stage1, forged),),
                observed_versions={},
            )

    def test_attestation_flags_are_strict_booleans(self):
        _, attestation = self.certify()
        payload = attestation.model_dump(mode="python")
        payload["stage1_verified"] = 1
        with self.assertRaises(ValueError):
            certification.CertificationAttestation.model_validate(payload)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            bypassed = certification.seal_attestation(
                attestation.model_copy(
                    update={"stage1_verified": 1, "attestation_digest": ""}
                )
            )
            with self.assertRaisesRegex(
                certification.CertificationError, "object is invalid"
            ):
                certification.verify_attestation(bypassed)

    def test_bounded_reader_refuses_a_short_racing_read(self):
        record = self.store / "record.json"
        record.write_text('{"value":1}\n', encoding="utf-8")
        with mock.patch.object(certification.os, "read", return_value=b""):
            with self.assertRaisesRegex(
                certification.CertificationError, "changed while it was read"
            ):
                certification._read_bounded_regular_file(
                    record, label="test record"
                )

    def test_begin_closes_attestation_race_and_recovers_orphan_evidence(self):
        real_exists = certification._entry_exists
        attestation_checks = 0

        def inject_attestation(path):
            nonlocal attestation_checks
            if Path(path).name == certification.ATTESTATION_FILENAME:
                attestation_checks += 1
                if attestation_checks == 2:
                    Path(path).write_text("{}\n", encoding="utf-8")
                    return True
            return real_exists(path)

        with mock.patch.object(
            certification, "_entry_exists", side_effect=inject_attestation
        ):
            with self.assertRaisesRegex(
                certification.CertificationError, "became certified"
            ):
                certification.begin_certification(
                    self.store, self.manifest, self.task_id
                )
        id_dir = self.store / self.certification_id
        self.assertFalse((id_dir / certification.PENDING_FILENAME).exists())

        # Remove the injected invalid attestation, then model a process death
        # after evidence publication but before attestation publication.
        (id_dir / certification.ATTESTATION_FILENAME).unlink()
        orphan = id_dir / certification.EVIDENCE_DIRNAME
        orphan.mkdir()
        (orphan / "partial.json").write_text("{}\n", encoding="utf-8")
        pending_dir = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        self.assertFalse(orphan.exists())
        quarantined = tuple(id_dir.glob(".orphaned-evidence-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertTrue((quarantined[0] / "partial.json").is_file())
        self.assertTrue(
            (pending_dir / certification.PENDING_FILENAME).is_file()
        )

    def test_failed_attestation_publish_quarantines_evidence_for_retry(self):
        pending_dir = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        pair = self.evidence_pair()
        matrix = self.manifest.certification_matrix[
            self.manifest.destinations[self.task_id]
        ]
        observations = {
            key: matrix[key]
            for key in certification._required_observation_keys(matrix)
        }
        real_publish = certification._publish_immutable_json

        def fail_attestation(path, payload):
            if Path(path).name == certification.ATTESTATION_FILENAME:
                raise OSError("simulated durable-publication failure")
            return real_publish(path, payload)

        with mock.patch.object(
            certification,
            "_publish_immutable_json",
            side_effect=fail_attestation,
        ):
            with self.assertRaisesRegex(
                certification.CertificationError, "could not publish"
            ):
                certification.complete_certification(
                    pending_dir,
                    manifest=self.manifest,
                    task_id=self.task_id,
                    evidence_pairs=(pair,),
                    observed_versions=observations,
                )
        self.assertFalse(
            (pending_dir / certification.EVIDENCE_DIRNAME).exists()
        )
        self.assertFalse(
            (pending_dir / certification.PENDING_FILENAME).exists()
        )
        self.assertTrue(tuple(pending_dir.glob(".orphaned-evidence-*")))
        # The failed transaction no longer bricks this certification id.
        retried = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        self.assertTrue((retried / certification.PENDING_FILENAME).is_file())

    def test_v10_attestation_and_legacy_evidence_remain_verifiable(self):
        current = self.manifest.certification_matrix[
            self.manifest.destinations[self.task_id]
        ]
        legacy_matrix = {
            **current,
            "matrix_version": "10",
            "certification_stage_evidence_schema_version": "1.1",
            "certification_attestation_schema_version": "1.2",
            # v10 matrices were recorded under canonical fingerprint version 1.
            "canonical_fingerprint_version": "1",
        }
        self.assertEqual(
            runtime_matrix.validate_recorded_certification_matrix(
                legacy_matrix, self.manifest.destinations[self.task_id]
            ),
            dict(sorted(legacy_matrix.items())),
        )
        legacy_id = release._certification_id(
            runtime_bundle_id=self.manifest.runtime_bundle_ids[self.task_id],
            matrix=legacy_matrix,
            validate_matrix=False,
        )
        stage1, stage2 = self.evidence_pair()
        legacy_stage1 = certification.seal_stage_evidence(
            stage1.model_copy(
                update={
                    "evidence_schema_version": "1.1",
                    "certification_id": legacy_id,
                    "runner_image": "",
                    "evidence_digest": "",
                }
            )
        )
        legacy_stage2 = certification.seal_stage_evidence(
            stage2.model_copy(
                update={
                    "evidence_schema_version": "1.1",
                    "certification_id": legacy_id,
                    "runner_image": "",
                    "preflight_destination": "",
                    "preflight_dbt_core_version": "",
                    "preflight_adapter_version": "",
                    "dbt_expected_model_ids": (),
                    "dbt_observed_model_ids": (),
                    "evidence_digest": "",
                }
            )
        )
        id_dir = self.store / legacy_id
        population_dir = id_dir / certification.EVIDENCE_DIRNAME / "primary"
        population_dir.mkdir(parents=True)
        legacy_payloads = (
            ("stage1.json", legacy_stage1, ("runner_image",)),
            (
                "stage2.json",
                legacy_stage2,
                (
                    "runner_image",
                    "preflight_destination",
                    "preflight_dbt_core_version",
                    "preflight_adapter_version",
                    "dbt_expected_model_ids",
                    "dbt_observed_model_ids",
                ),
            ),
        )
        for filename, record, omitted in legacy_payloads:
            payload = record.model_dump(mode="json")
            for key in omitted:
                payload.pop(key)
            payload["result"].pop("physical_container")
            self.assertNotIn("runner_image", payload)
            self.assertNotIn("physical_container", payload["result"])
            (population_dir / filename).write_text(
                json.dumps(payload, sort_keys=True), encoding="utf-8"
            )
        observations = {
            key: legacy_matrix[key]
            for key in certification._required_observation_keys(legacy_matrix)
        }
        attestation = certification.seal_attestation(
            certification.CertificationAttestation(
                attestation_schema_version="1.2",
                release_schema_version=self.manifest.schema_version,
                certification_id=legacy_id,
                runtime_bundle_id=self.manifest.runtime_bundle_ids[self.task_id],
                semantic_release_id=self.manifest.semantic_release_id,
                release_id=self.manifest.release_id,
                task_id=self.task_id,
                destination=self.manifest.destinations[self.task_id],
                populations=("primary",),
                matrix=legacy_matrix,
                observed_versions=observations,
                stage1_verified=True,
                stage2_verified=True,
                evidence_digests={
                    "primary/stage1.json": legacy_stage1.evidence_digest,
                    "primary/stage2.json": legacy_stage2.evidence_digest,
                },
                started_at=legacy_stage1.execution_started_at,
                completed_at=max(
                    legacy_stage1.recorded_at, legacy_stage2.recorded_at
                ),
            )
        )
        attestation_path = id_dir / certification.ATTESTATION_FILENAME
        attestation_path.write_text(
            json.dumps(attestation.model_dump(mode="json"), sort_keys=True),
            encoding="utf-8",
        )
        self.assertEqual(
            certification.verify_attestation(attestation_path), attestation
        )
        # Historical verification uses the literal v10 container rule, not a
        # future/current DestinationContract interpretation.
        with mock.patch.object(
            certification,
            "destination_contract",
            side_effect=AssertionError("current contract must not be consulted"),
        ):
            self.assertIs(
                self.status(legacy_id).state,
                certification.CertificationState.CERTIFIED,
            )

    def test_self_consistent_partial_stage1_cannot_omit_released_table(self):
        pending_dir = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        stage1, stage2 = self.evidence_pair()
        omitted = next(iter(stage1.result.expected_counts))
        update = {}
        for field in (
            "expected_counts",
            "actual_counts",
            "detail",
            "canonical_table_scores",
            "canonical_mismatch_codes",
            "canonical_reference_fingerprints",
            "canonical_actual_fingerprints",
        ):
            values = dict(getattr(stage1.result, field))
            values.pop(omitted)
            update[field] = values
        reduced_result = stage1.result.model_copy(update=update)
        reduced = certification.seal_stage_evidence(
            stage1.model_copy(
                update={"result": reduced_result, "evidence_digest": ""}
            )
        )
        with self.assertRaisesRegex(
            certification.CertificationError, "released table roster"
        ):
            certification.complete_certification(
                pending_dir,
                manifest=self.manifest,
                task_id=self.task_id,
                evidence_pairs=((reduced, stage2),),
                observed_versions={},
            )

    def test_stage2_strict_mismatch_cannot_certify(self):
        pending_dir = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        stage1, stage2 = self.evidence_pair()
        mart = next(iter(stage2.result.mart_scores))
        reduced_result = stage2.result.model_copy(
            update={
                "strict_mart_scores": {mart: False},
                "strict_mismatch_codes": {mart: "values:total_spend"},
            }
        )
        reduced = certification.seal_stage_evidence(
            stage2.model_copy(
                update={"result": reduced_result, "evidence_digest": ""}
            )
        )
        with self.assertRaisesRegex(
            certification.CertificationError, "Stage-2 evidence"
        ):
            certification.complete_certification(
                pending_dir,
                manifest=self.manifest,
                task_id=self.task_id,
                evidence_pairs=((stage1, reduced),),
                observed_versions={},
            )

    def test_airbyte_job_ids_are_positive_and_unique(self):
        stage1, _ = self.evidence_pair()
        result = Stage1EvaluationResult(
            **stage1.result.model_dump(mode="python")
        )
        for jobs in (
            {"connection-1": 0},
            {"connection-1": -1},
            {"connection-1": 7, "connection-2": 7},
            {"connection-1": "01"},
        ):
            with self.subTest(jobs=jobs), self.assertRaises(
                certification.CertificationError
            ):
                certification.stage1_evidence_from_result(
                    self.manifest,
                    self.task_id,
                    result,
                    attempt_id="attempt-primary",
                    execution_started_at="2026-09-03T00:00:00Z",
                    execution_completed_at="2026-09-03T00:00:01Z",
                    runner_image=(
                        self.manifest.certification_matrix[
                            self.manifest.destinations[self.task_id]
                        ]["runner_image:terraform"]
                    ),
                    terraform_input_tree_digest="e" * 64,
                    terraform_state_digest="c" * 64,
                    airbyte_job_ids=jobs,
                )

    def test_record_readers_refuse_duplicate_json_and_symlinks(self):
        duplicate = self.store / "duplicate.json"
        duplicate.write_text(
            '{"attestation_schema_version":"'
            + certification.ATTESTATION_SCHEMA_VERSION
            + '","attestation_schema_version":"'
            + certification.ATTESTATION_SCHEMA_VERSION
            + '"}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            certification.CertificationError, "unreadable"
        ):
            certification.verify_attestation(duplicate)
        link = self.store / "attestation-link.json"
        link.symlink_to(duplicate)
        with self.assertRaisesRegex(
            certification.CertificationError, "unreadable"
        ):
            certification.verify_attestation(link)

    def test_refusal_report_replaces_symlink_without_touching_its_target(self):
        pending_dir = certification.begin_certification(
            self.store, self.manifest, self.task_id
        )
        victim = self.store / "must-not-be-overwritten.txt"
        victim.write_text("preserve me\n", encoding="utf-8")
        refused_path = pending_dir / certification.REFUSED_FILENAME
        refused_path.symlink_to(victim)
        with self.assertRaisesRegex(
            certification.CertificationError, "no Stage-1/Stage-2"
        ):
            certification.complete_certification(
                pending_dir,
                manifest=self.manifest,
                task_id=self.task_id,
                evidence_pairs=(),
                observed_versions={},
            )
        self.assertEqual(victim.read_text(encoding="utf-8"), "preserve me\n")
        self.assertFalse(refused_path.is_symlink())
        self.assertTrue(refused_path.is_file())

    def test_record_readers_require_explicit_schema_discriminators(self):
        stage1, _ = self.evidence_pair()
        stage_path = self.store / "stage1-without-schema.json"
        stage_payload = stage1.model_dump(mode="json")
        stage_payload.pop("evidence_schema_version")
        stage_path.write_text(json.dumps(stage_payload), encoding="utf-8")
        with self.assertRaisesRegex(
            certification.CertificationError, "unreadable"
        ):
            certification.verify_stage1_evidence(stage_path)

        pending_dir, attestation = self.certify()
        attestation_path = pending_dir / certification.ATTESTATION_FILENAME
        attestation_path.chmod(0o600)
        payload = attestation.model_dump(mode="json")
        payload.pop("attestation_schema_version")
        attestation_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(
            certification.CertificationError, "unreadable"
        ):
            certification.verify_attestation(attestation_path)

    def test_attestation_requires_an_ordered_utc_window(self):
        _, attestation = self.certify()
        malformed = certification.seal_attestation(
            attestation.model_copy(
                update={"started_at": "not-a-time", "attestation_digest": ""}
            )
        )
        with self.assertRaisesRegex(
            certification.CertificationError, "attestation started_at"
        ):
            certification.verify_attestation(malformed)

        reversed_window = certification.seal_attestation(
            attestation.model_copy(
                update={
                    "started_at": attestation.completed_at,
                    "completed_at": attestation.started_at,
                    "attestation_digest": "",
                }
            )
        )
        with self.assertRaisesRegex(
            certification.CertificationError, "completed before it started"
        ):
            certification.verify_attestation(reversed_window)

    def test_begin_recomputes_legacy_release_id_too(self):
        changed = self.manifest.model_copy(update={"release_id": "release-forged"})
        with self.assertRaisesRegex(
            certification.CertificationError, "release_id"
        ):
            certification.begin_certification(
                self.store, changed, self.task_id
            )

    def test_tampered_attestation_is_uncertified_not_an_error(self):
        pending_dir, _ = self.certify()
        attestation_path = pending_dir / certification.ATTESTATION_FILENAME
        attestation_path.chmod(0o644)
        with self.subTest(tamper="edited value"):
            payload = json.loads(attestation_path.read_text(encoding="utf-8"))
            payload["task_id"] = "tampered-task"
            attestation_path.write_text(json.dumps(payload), encoding="utf-8")
            status = self.status()
            self.assertIs(
                status.state, certification.CertificationState.UNCERTIFIED
            )
            self.assertIn("modified after sealing", status.reason)
        with self.subTest(tamper="unparsable"):
            attestation_path.write_text("not json", encoding="utf-8")
            status = self.status()
            self.assertIs(
                status.state, certification.CertificationState.UNCERTIFIED
            )
            self.assertTrue(status.reason)

    def test_matrix_change_resets_to_uncertified_and_preserves_old_evidence(self):
        self.certify()
        with mock.patch.object(
            runtime_matrix, "runner_images_digest", return_value="f" * 64
        ):
            new_matrix = runtime_matrix.certification_matrix("snowflake")
        new_id = release._certification_id(
            runtime_bundle_id=self.manifest.runtime_bundle_ids[self.task_id],
            matrix=new_matrix,
        )
        self.assertNotEqual(new_id, self.certification_id)
        # The new chain id starts UNCERTIFIED (its directory does not exist);
        # the old id's sealed evidence is untouched and reusable.
        self.assertIs(
            self.status(new_id).state,
            certification.CertificationState.UNCERTIFIED,
        )
        self.assertIs(
            self.status().state, certification.CertificationState.CERTIFIED
        )

    def test_begin_refuses_a_semantic_change_with_a_stale_runtime_id(self):
        checksums = dict(self.manifest.checksums)
        gold_rel = next(
            rel for rel in sorted(checksums) if "/answer_key/gold/" in rel
        )
        checksums[gold_rel] = "0" * 64
        semantic_id = release._semantic_release_id(
            tasks=self.manifest.tasks,
            el_sources=self.manifest.el_sources,
            private_checksums=release._private_semantic_checksums(
                checksums, self.manifest.tasks
            ),
            scorer_version=self.manifest.scorer_version,
            roster_digest=self.manifest.roster_digest,
            semantic_scorer_version=self.manifest.semantic_scorer_version,
        )
        changed = self.manifest.model_copy(
            update={
                "checksums": checksums,
                "semantic_release_id": semantic_id,
            }
        )
        with self.assertRaisesRegex(
            certification.CertificationError, "runtime_bundle_id"
        ):
            certification.begin_certification(
                self.store, changed, self.task_id
            )

    def test_begin_refuses_pre_34_unchained_manifests(self):
        for schema_version in ("3.2", "3.3"):
            with self.subTest(schema_version=schema_version):
                legacy = self.manifest.model_copy(
                    update={"schema_version": schema_version}
                )
                # The remedy is named: a 3.3 release must be re-frozen (the
                # Phase 1 ledger, finding 1-6: `certify` refuses every tree
                # frozen under schema 3.3).
                with self.assertRaisesRegex(
                    certification.CertificationError, r"3\.4.*re-freeze the release"
                ):
                    certification.begin_certification(
                        self.store, legacy, self.task_id
                    )
                self.assertIs(
                    self.status().state,
                    certification.CertificationState.UNCERTIFIED,
                )


if __name__ == "__main__":
    unittest.main()

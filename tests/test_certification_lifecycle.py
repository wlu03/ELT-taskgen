"""Fail-closed tests for the public runtime-certification lifecycle."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from elt_taskgen.export import certification
from elt_taskgen.runtime import attestation as sandbox_mod
from elt_taskgen.runtime import certification_lifecycle as lifecycle
from elt_taskgen.runtime.model_copy import (
    CLEANUP_RECEIPT_SCHEMA_VERSION,
    CleanupReceipt,
    LANE_CLOUD,
    ModelCopyError,
    seal_cleanup_receipt,
    verify_cleanup_receipt,
)
from elt_taskgen.runtime.process import CommandResult, ProcessFailure

try:
    import test_sandbox_attestation as sandbox_fixture
except ModuleNotFoundError:  # pragma: no cover - package-style discovery
    from tests import test_sandbox_attestation as sandbox_fixture


class _MissingRunner:
    """A host-command double on which every optional binary is absent."""

    def run(self, argv, *, cwd=None, env=None, stdin_path=None):
        if tuple(argv) == ("uname", "-r"):
            return CommandResult(0, "23.6.0\n", "")
        raise ProcessFailure(f"{argv[0]} is unavailable")


class _SimulatedProcessDeath(BaseException):
    """Fault-injection sentinel that ordinary error recovery must not catch."""


def _cleanup(
    *,
    revoked: bool,
    attempt_id: str = "certification-attempt-" + "1" * 64,
    destination: str = "snowflake",
) -> CleanupReceipt:
    return seal_cleanup_receipt(
        CleanupReceipt(
            schema_version=CLEANUP_RECEIPT_SCHEMA_VERSION,
            attempt_id=attempt_id,
            lane=LANE_CLOUD,
            docker_lane="proxy-bridge",
            destination=destination,
            principal_id="scoped-principal-1",
            principal_revoked=revoked,
            replay_installed=True,
            replay_removed=True,
            model_removed=True,
            live_credential_files=0,
            model_released=True,
            policy_credential_findings=0,
            grader_commands=lifecycle.GRADER_COMMANDS_PER_POPULATION,
            agent_process_count=0,
            model_call_count=0,
        )
    )


class CleanupReceiptTests(unittest.TestCase):
    def test_complete_policy_is_strict_and_identity_bound(self) -> None:
        valid = _cleanup(revoked=True)
        self.assertEqual(
            verify_cleanup_receipt(
                asdict(valid),
                attempt_id=valid.attempt_id,
                destination="snowflake",
                require_complete=True,
            ),
            valid,
        )
        with self.assertRaisesRegex(ModelCopyError, "principal was not revoked"):
            verify_cleanup_receipt(
                asdict(_cleanup(revoked=False)),
                attempt_id=valid.attempt_id,
                destination="snowflake",
                require_complete=True,
            )
        with self.assertRaisesRegex(ModelCopyError, "attempt_id"):
            verify_cleanup_receipt(
                asdict(valid),
                attempt_id="certification-attempt-" + "2" * 64,
                destination="snowflake",
                require_complete=True,
            )

    def test_complete_policy_requires_expected_grader_lane_commands(self) -> None:
        valid = _cleanup(revoked=True)
        verify_cleanup_receipt(
            asdict(valid),
            require_complete=True,
            expected_grader_commands=lifecycle.GRADER_COMMANDS_PER_POPULATION,
        )
        zero = seal_cleanup_receipt(
            CleanupReceipt(**{**asdict(valid), "grader_commands": 0, "receipt_digest": ""})
        )
        with self.assertRaisesRegex(ModelCopyError, "no grader-lane commands"):
            verify_cleanup_receipt(asdict(zero), require_complete=True)
        wrong = seal_cleanup_receipt(
            CleanupReceipt(**{**asdict(valid), "grader_commands": 3, "receipt_digest": ""})
        )
        with self.assertRaisesRegex(ModelCopyError, "does not match expected 4"):
            verify_cleanup_receipt(
                asdict(wrong),
                require_complete=True,
                expected_grader_commands=lifecycle.GRADER_COMMANDS_PER_POPULATION,
            )


class LifecycleOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.task_id = "task"
        self.attempt_id = "certification-attempt-" + "1" * 64
        self.pending = self.tmp / "certification-test"
        attempt_dir = (
            self.pending / lifecycle.LIFECYCLE_DIRNAME / self.attempt_id
        )
        attempt_dir.mkdir(parents=True)
        (attempt_dir / lifecycle.CLEANUP_FILENAME).write_text(
            json.dumps(asdict(_cleanup(revoked=False))),
            encoding="utf-8",
        )
        self.manifest = SimpleNamespace(
            certification_ids={self.task_id: "certification-test"},
            destinations={self.task_id: "snowflake"},
        )

    def test_incomplete_cleanup_blocks_completion(self) -> None:
        observations = SimpleNamespace(
            observed_versions={"destination": "snowflake"},
            observation_digest="a" * 64,
        )
        sandbox = SimpleNamespace(attestation_digest="b" * 64)
        with (
            mock.patch.object(
                lifecycle,
                "_pending_context",
                return_value=(self.manifest, self.pending, self.attempt_id),
            ),
            mock.patch.object(
                lifecycle, "verify_release_bound_sandbox", return_value=sandbox
            ),
            mock.patch.object(
                lifecycle, "_verify_observation_receipt", return_value=observations
            ),
            mock.patch.object(
                lifecycle, "_staged_evidence_pairs", return_value=((object(), object()),)
            ),
            mock.patch.object(
                certification, "complete_certification"
            ) as complete,
            self.assertRaisesRegex(
                lifecycle.CertificationLifecycleError,
                "principal was not revoked",
            ),
        ):
            lifecycle.complete_runtime_certification(
                release_dir=self.tmp / "release",
                task_id=self.task_id,
                certification_store=self.tmp,
            )
        complete.assert_not_called()

    def test_historical_store_without_lifecycle_fails_promotion_gate(self) -> None:
        manifest = SimpleNamespace(
            certification_ids={self.task_id: "certification-test"},
        )
        status = certification.CertificationStatus(
            certification_id="certification-test",
            state=certification.CertificationState.CERTIFIED,
            populations=("primary",),
        )
        with (
            mock.patch.object(
                lifecycle,
                "_verified_manifest",
                return_value=(self.tmp / "release", manifest),
            ),
            mock.patch.object(
                certification, "certification_status", return_value=status
            ),
            self.assertRaisesRegex(
                lifecycle.CertificationLifecycleError,
                "lifecycle receipt.*missing",
            ),
        ):
            lifecycle.verify_completed_lifecycle(
                release_dir=self.tmp / "release",
                task_id=self.task_id,
                certification_store=self.tmp,
            )

    def test_attestation_to_lifecycle_crash_is_recovered_idempotently(self) -> None:
        # Reuse the low-level certification fixture without importing its
        # TestCase into this module's globals (which would duplicate discovery).
        try:
            from test_certification import CertificationTestCase
        except ModuleNotFoundError:
            from tests.test_certification import CertificationTestCase

        fixture = CertificationTestCase("test_unknown_id_is_uncertified")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        manifest = fixture.manifest
        task_id = fixture.task_id
        store = fixture.store
        pending_dir = certification.begin_certification(
            store, manifest, task_id
        )
        attempt_id = certification.pending_attempt_id(pending_dir)
        stage1, stage2 = fixture.evidence_pair(attempt_id=attempt_id)
        staged = (
            pending_dir
            / lifecycle.LIFECYCLE_DIRNAME
            / attempt_id
            / lifecycle.STAGED_EVIDENCE_DIRNAME
            / "primary"
        )
        staged.mkdir(parents=True)
        (staged / "stage1.json").write_text(
            stage1.model_dump_json(), encoding="utf-8"
        )
        (staged / "stage2.json").write_text(
            stage2.model_dump_json(), encoding="utf-8"
        )
        lifecycle_dir = staged.parent.parent
        (lifecycle_dir / lifecycle.SANDBOX_ATTESTATION_FILENAME).write_text(
            "{}\n", encoding="utf-8"
        )
        (lifecycle_dir / lifecycle.OBSERVATIONS_FILENAME).write_text(
            "{}\n", encoding="utf-8"
        )
        cleanup = _cleanup(
            revoked=True,
            attempt_id=attempt_id,
            destination=manifest.destinations[task_id],
        )
        (lifecycle_dir / lifecycle.CLEANUP_FILENAME).write_text(
            json.dumps(asdict(cleanup)), encoding="utf-8"
        )
        matrix = manifest.certification_matrix[
            manifest.destinations[task_id]
        ]
        observed_versions = {
            key: matrix[key]
            for key in certification.required_observation_keys(
                manifest, task_id
            )
        }
        observations = SimpleNamespace(
            observed_versions=observed_versions,
            observation_digest="a" * 64,
        )
        sandbox = SimpleNamespace(attestation_digest="b" * 64)
        real_publish = lifecycle._publish_json

        def crash_after_attestation(path, payload):
            if Path(path).name == lifecycle.LIFECYCLE_FILENAME:
                raise OSError("simulated process death before lifecycle publish")
            return real_publish(path, payload)

        release_dir = fixture.harness.out_dir
        common_patches = (
            mock.patch.object(
                lifecycle,
                "_verified_manifest",
                return_value=(release_dir, manifest),
            ),
            mock.patch.object(
                lifecycle,
                "verify_release_bound_sandbox",
                return_value=sandbox,
            ),
            mock.patch.object(
                lifecycle,
                "_verify_observation_receipt",
                return_value=observations,
            ),
        )
        with (
            common_patches[0],
            common_patches[1],
            common_patches[2],
            mock.patch.object(
                lifecycle, "_publish_json", side_effect=crash_after_attestation
            ),
            self.assertRaisesRegex(OSError, "simulated process death"),
        ):
            lifecycle.complete_runtime_certification(
                release_dir=release_dir,
                task_id=task_id,
                certification_store=store,
            )

        self.assertIs(
            certification.certification_status(
                store, manifest.certification_ids[task_id]
            ).state,
            certification.CertificationState.CERTIFIED,
        )
        self.assertFalse(
            (pending_dir / certification.PENDING_FILENAME).exists()
        )
        self.assertFalse((pending_dir / lifecycle.LIFECYCLE_FILENAME).exists())
        intent_path = lifecycle_dir / lifecycle.COMPLETION_INTENT_FILENAME
        self.assertTrue(intent_path.is_file())

        # Recovery is not a back door for a historical low-level-only store:
        # the durable pre-completion intent is mandatory.
        saved_intent = lifecycle_dir / "saved-completion-intent.json"
        intent_path.rename(saved_intent)
        try:
            with (
                mock.patch.object(
                    lifecycle,
                    "_verified_manifest",
                    return_value=(release_dir, manifest),
                ),
                mock.patch.object(
                    certification,
                    "complete_certification",
                    side_effect=AssertionError("low-level completion was replayed"),
                ),
                self.assertRaisesRegex(
                    lifecycle.CertificationLifecycleError,
                    "completion intent.*missing",
                ),
            ):
                lifecycle.finish_runtime_certification(
                    release_dir=release_dir,
                    task_id=task_id,
                    certification_store=store,
                    run_spec=self.tmp / "missing-run-spec.json",
                    difficulty_out=self.tmp / "must-not-exist.json",
                )
        finally:
            saved_intent.rename(intent_path)

        # No pending capability remains. The retry must derive this exact
        # nonce from sealed persisted evidence and must not invoke low-level
        # completion a second time. Exercise the advertised one-shot CLI so
        # the recovery cannot regress behind its earlier pending-only setup.
        run_spec = self.tmp / "run-spec.json"
        run_spec.write_text(
            json.dumps(
                {
                    "schema_version": lifecycle.RUN_SPEC_SCHEMA_VERSION,
                    "sandbox_attestation": "unused-sandbox.json",
                    "cleanup_receipt": "unused-cleanup.json",
                    "observed_versions": observed_versions,
                    "populations": [
                        {
                            "population": "primary",
                            "stage1_execution": "unused-stage1-execution.json",
                            "stage1_result": "unused-stage1-result.json",
                            "stage2_execution": "unused-stage2-execution.json",
                            "stage2_result": "unused-stage2-result.json",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        difficulty_out = self.tmp / "runtime-difficulty.json"
        promoted = SimpleNamespace(evidence_digest="c" * 64)
        from elt_taskgen import cli

        argv = [
            "runtime",
            "certify",
            "--release",
            str(release_dir),
            "--task-id",
            task_id,
            "--certification-store",
            str(store),
            "--run-spec",
            str(run_spec),
            "--difficulty-out",
            str(difficulty_out),
        ]
        with (
            mock.patch.object(
                lifecycle,
                "_verified_manifest",
                return_value=(release_dir, manifest),
            ),
            mock.patch.object(
                lifecycle,
                "verify_release_bound_sandbox",
                return_value=sandbox,
            ),
            mock.patch.object(
                lifecycle,
                "_verify_observation_receipt",
                return_value=observations,
            ),
            mock.patch.object(
                certification,
                "complete_certification",
                side_effect=AssertionError("low-level completion was replayed"),
            ),
            mock.patch.object(
                lifecycle,
                "_build_and_publish_certified_difficulty",
                return_value=promoted,
            ) as promote,
        ):
            outputs = []
            for replay_index in range(2):
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    self.assertEqual(cli.main(argv), 0)
                outputs.append(json.loads(stream.getvalue()))
                if replay_index == 0:
                    # Once the journal and attestation exist, recovery no
                    # longer depends on transient run-spec inputs.
                    run_spec.unlink()
            recovered_attestation, recovered = lifecycle.complete_runtime_certification(
                release_dir=release_dir,
                task_id=task_id,
                certification_store=store,
            )

        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(promote.call_count, 2)
        self.assertEqual(
            outputs[0]["certification_id"],
            recovered_attestation.certification_id,
        )
        self.assertEqual(recovered.attempt_id, attempt_id)
        self.assertTrue(recovered.lifecycle_digest)

    def test_finish_retry_adopts_partial_records_and_refuses_conflict(self) -> None:
        try:
            from test_certification import CertificationTestCase
        except ModuleNotFoundError:
            from tests.test_certification import CertificationTestCase

        fixture = CertificationTestCase("test_unknown_id_is_uncertified")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        manifest = fixture.manifest
        task_id = fixture.task_id
        store = fixture.store
        release_dir = fixture.harness.out_dir
        pending_dir = certification.begin_certification(store, manifest, task_id)
        attempt_id = certification.pending_attempt_id(pending_dir)
        stage1, stage2 = fixture.evidence_pair(attempt_id=attempt_id)

        def rerecorded(record):
            recorded = datetime.fromisoformat(
                record.recorded_at.replace("Z", "+00:00")
            ) + timedelta(microseconds=1)
            later = recorded.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            )
            return certification.seal_stage_evidence(
                record.model_copy(
                    update={"recorded_at": later, "evidence_digest": ""}
                )
            )

        replay_stage1 = rerecorded(stage1)
        replay_stage2 = rerecorded(stage2)
        conflicting_stage1 = certification.seal_stage_evidence(
            replay_stage1.model_copy(
                update={
                    "terraform_state_digest": "0" * 64,
                    "evidence_digest": "",
                }
            )
        )
        lifecycle_dir = (
            pending_dir / lifecycle.LIFECYCLE_DIRNAME / attempt_id
        )
        lifecycle_dir.mkdir(parents=True)
        (lifecycle_dir / lifecycle.SANDBOX_ATTESTATION_FILENAME).write_text(
            "{}\n", encoding="utf-8"
        )
        cleanup_path = self.tmp / "cleanup-input.json"
        cleanup_path.write_text(
            json.dumps(
                asdict(
                    _cleanup(
                        revoked=True,
                        attempt_id=attempt_id,
                        destination=manifest.destinations[task_id],
                    )
                )
            ),
            encoding="utf-8",
        )
        matrix = manifest.certification_matrix[manifest.destinations[task_id]]
        observed_versions = {
            key: matrix[key]
            for key in certification.required_observation_keys(manifest, task_id)
        }
        run_spec = self.tmp / "run-spec-partial-retry.json"
        run_spec.write_text(
            json.dumps(
                {
                    "schema_version": lifecycle.RUN_SPEC_SCHEMA_VERSION,
                    "sandbox_attestation": "sandbox-input.json",
                    "cleanup_receipt": str(cleanup_path),
                    "observed_versions": observed_versions,
                    "populations": [
                        {
                            "population": "primary",
                            "stage1_execution": "stage1-execution.json",
                            "stage1_result": "stage1-result.json",
                            "stage2_execution": "stage2-execution.json",
                            "stage2_result": "stage2-result.json",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        sandbox = SimpleNamespace(attestation_digest="b" * 64)
        promoted = SimpleNamespace(evidence_digest="c" * 64)

        @contextlib.contextmanager
        def trusted_inputs(stage1_candidate, stage2_candidate):
            with (
                mock.patch.object(
                    lifecycle,
                    "_verified_manifest",
                    return_value=(release_dir, manifest),
                ),
                mock.patch.object(
                    lifecycle,
                    "verify_release_bound_sandbox",
                    return_value=sandbox,
                ),
                mock.patch.object(
                    lifecycle, "_load_sandbox_attestation", return_value=sandbox
                ),
                mock.patch.object(lifecycle, "_stage1_result", return_value=object()),
                mock.patch.object(
                    lifecycle, "_stage1_execution", return_value=object()
                ),
                mock.patch.object(lifecycle, "_stage2_result", return_value=object()),
                mock.patch.object(
                    lifecycle, "_stage2_execution", return_value=object()
                ),
                mock.patch.object(
                    certification,
                    "stage1_evidence_from_execution",
                    return_value=stage1_candidate,
                ),
                mock.patch.object(
                    certification,
                    "stage2_evidence_from_execution",
                    return_value=stage2_candidate,
                ),
                mock.patch.object(
                    lifecycle,
                    "_build_and_publish_certified_difficulty",
                    return_value=promoted,
                ),
            ):
                yield

        real_publish = lifecycle._publish_json

        def die_after_cleanup_is_visible(path, payload):
            published = real_publish(path, payload)
            if Path(path).name == lifecycle.CLEANUP_FILENAME:
                raise _SimulatedProcessDeath("after cleanup publication")
            return published

        finish_kwargs = {
            "release_dir": release_dir,
            "task_id": task_id,
            "certification_store": store,
            "run_spec": run_spec,
            "difficulty_out": self.tmp / "runtime-difficulty.json",
        }
        with (
            trusted_inputs(stage1, stage2),
            mock.patch.object(
                lifecycle,
                "_publish_json",
                side_effect=die_after_cleanup_is_visible,
            ),
            self.assertRaisesRegex(
                _SimulatedProcessDeath, "after cleanup publication"
            ),
        ):
            lifecycle.finish_runtime_certification(**finish_kwargs)

        staged_paths = (
            lifecycle_dir
            / lifecycle.STAGED_EVIDENCE_DIRNAME
            / "primary"
            / "stage1.json",
            lifecycle_dir
            / lifecycle.STAGED_EVIDENCE_DIRNAME
            / "primary"
            / "stage2.json",
            lifecycle_dir / lifecycle.OBSERVATIONS_FILENAME,
            lifecycle_dir / lifecycle.CLEANUP_FILENAME,
        )
        immutable_bytes = {path: path.read_bytes() for path in staged_paths}
        self.assertFalse(
            (pending_dir / certification.ATTESTATION_FILENAME).exists()
        )

        # The path manifest is unchanged, but a source-derived Stage-1 field is
        # different. The retry must stop at that immutable conflict.
        with (
            trusted_inputs(conflicting_stage1, replay_stage2),
            self.assertRaisesRegex(
                lifecycle.CertificationLifecycleError,
                "Stage-1 evidence conflicts with the supplied run",
            ),
        ):
            lifecycle.finish_runtime_certification(**finish_kwargs)
        self.assertFalse(
            (pending_dir / certification.ATTESTATION_FILENAME).exists()
        )
        self.assertEqual(
            {path: path.read_bytes() for path in staged_paths}, immutable_bytes
        )

        # Rebuilding from the exact receipts mints later recorded_at values.
        # Adoption preserves the original sealed records and then completes.
        with trusted_inputs(replay_stage1, replay_stage2):
            attestation, receipt, report = (
                lifecycle.finish_runtime_certification(**finish_kwargs)
            )
        self.assertIs(report, promoted)
        self.assertEqual(receipt.attempt_id, attempt_id)
        self.assertEqual(
            attestation.evidence_digests["primary/stage1.json"],
            stage1.evidence_digest,
        )
        self.assertEqual(
            attestation.evidence_digests["primary/stage2.json"],
            stage2.evidence_digest,
        )
        self.assertEqual(
            {path: path.read_bytes() for path in staged_paths}, immutable_bytes
        )

    def test_json_target_is_invisible_until_complete(self) -> None:
        target = self.tmp / "atomic" / "receipt.json"
        real_write = lifecycle.os.write
        target_visibility = []

        def observe_write(descriptor, data):
            target_visibility.append(target.exists())
            return real_write(descriptor, data)

        with mock.patch.object(lifecycle.os, "write", side_effect=observe_write):
            lifecycle._publish_json(target, {"value": "x" * 4096})
        self.assertTrue(target_visibility)
        self.assertFalse(any(target_visibility))
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["value"], "x" * 4096)
        self.assertEqual(target.stat().st_mode & 0o222, 0)

    def test_runtime_difficulty_publish_accepts_only_exact_replay(self) -> None:
        try:
            from test_certified_difficulty_policy import _report, _reseal
        except ModuleNotFoundError:
            from tests.test_certified_difficulty_policy import _report, _reseal

        report = _report()
        target = self.tmp / "runtime-certified.json"
        build = "elt_taskgen.corpus.certified_difficulty.build_runtime_certified_difficulty"
        kwargs = {
            "release_dir": self.tmp / "release",
            "task_id": report.task_id,
            "certification_store": self.tmp / "store",
            "difficulty_out": target,
            "workspace": None,
        }
        real_write = lifecycle.os.write
        target_visibility = []

        def observe_write(descriptor, data):
            target_visibility.append(target.exists())
            return real_write(descriptor, data)

        with (
            mock.patch(build, return_value=report),
            mock.patch.object(lifecycle.os, "write", side_effect=observe_write),
        ):
            first = lifecycle._build_and_publish_certified_difficulty(**kwargs)
        with mock.patch(build, return_value=report):
            replay = lifecycle._build_and_publish_certified_difficulty(**kwargs)
        self.assertEqual(first, report)
        self.assertEqual(replay, report)
        self.assertTrue(target_visibility)
        self.assertFalse(any(target_visibility))
        self.assertEqual(target.stat().st_mode & 0o222, 0)

        contradictory = _reseal(report, release_id="another-release")
        with (
            mock.patch(build, return_value=contradictory),
            self.assertRaisesRegex(
                lifecycle.CertificationLifecycleError,
                "different runtime-certified difficulty",
            ),
        ):
            lifecycle._build_and_publish_certified_difficulty(**kwargs)


class SandboxTierTests(unittest.TestCase):
    def test_unbound_mint_validates_and_exclusively_publishes(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        mount = root / "captured-input"
        mount.mkdir()
        (mount / "rows.ndjson").write_text("{}\n", encoding="utf-8")
        out = root / "sealed" / "sandbox-attestation.json"
        preflight = sandbox_fixture.preflight(sandbox_fixture.tier_a_outputs())

        with (
            mock.patch(
                "elt_taskgen.review.providers.sandbox_pin",
                return_value=sandbox_fixture.GVISOR_PIN,
            ) as pin,
            mock.patch.object(
                lifecycle, "attest_sandbox", wraps=sandbox_mod.attest_sandbox
            ) as mint,
            mock.patch(
                "elt_taskgen.verification.contamination.enforcement",
                return_value=SimpleNamespace(value="enforce"),
            ),
        ):
            observed = lifecycle.mint_unbound_sandbox_attestation(
                mount_root=mount,
                out=out,
                image_digest="",
                workspace_template_sha256="",
                preflight=preflight,
            )

        self.assertEqual(observed.run_id, "")
        self.assertEqual(observed.tier, "A")
        self.assertTrue(observed.mount_scanned)
        self.assertEqual(observed.mount_entries_scanned, 1)
        self.assertEqual(out.stat().st_mode & 0o777, 0o400)
        self.assertEqual(
            sandbox_mod.SandboxAttestation.model_validate_json(
                out.read_text(encoding="utf-8")
            ),
            observed,
        )
        self.assertEqual(pin.call_count, 3)
        self.assertEqual(pin.call_args_list[0], mock.call(agents_config=None))
        self.assertTrue(
            all(
                isinstance(call.kwargs.get("agents_config"), dict)
                for call in pin.call_args_list[1:]
            )
        )
        mint.assert_called_once_with(
            pinned=sandbox_fixture.GVISOR_PIN,
            preflight=preflight,
            image_digest="",
            workspace_template_sha256="",
            mount_root=mount,
            run_id="",
            agents_config=None,
        )

        with (
            mock.patch(
                "elt_taskgen.review.providers.sandbox_pin",
                return_value=sandbox_fixture.GVISOR_PIN,
            ),
            mock.patch(
                "elt_taskgen.verification.contamination.enforcement",
                return_value=SimpleNamespace(value="enforce"),
            ),
            self.assertRaisesRegex(
                lifecycle.CertificationLifecycleError,
                r"already exists \(immutable\)",
            ),
        ):
            lifecycle.mint_unbound_sandbox_attestation(
                mount_root=mount,
                out=out,
                preflight=preflight,
            )

    def test_unbound_mint_rejects_bound_record_before_publish(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        preflight = sandbox_fixture.preflight(sandbox_fixture.tier_a_outputs())
        record = sandbox_fixture.attest(
            pinned=sandbox_fixture.GVISOR_PIN,
            preflight=preflight,
            mount_root=root,
            run_id="already-bound",
        )
        out = root / "sandbox-attestation.json"
        with (
            mock.patch(
                "elt_taskgen.review.providers.sandbox_pin",
                return_value=sandbox_fixture.GVISOR_PIN,
            ),
            mock.patch.object(lifecycle, "attest_sandbox", return_value=record),
            self.assertRaisesRegex(
                lifecycle.CertificationLifecycleError,
                "must have an empty run_id",
            ),
        ):
            lifecycle.mint_unbound_sandbox_attestation(
                mount_root=root,
                out=out,
                preflight=preflight,
            )
        self.assertFalse(out.exists())

    def test_unbound_mint_rejects_credential_shaped_mount_before_publish(
        self,
    ) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        (root / "snowflake_credential.json").write_text("", encoding="utf-8")
        preflight = sandbox_fixture.preflight(sandbox_fixture.tier_a_outputs())
        out = root / "sealed" / "sandbox-attestation.json"
        with (
            mock.patch(
                "elt_taskgen.review.providers.sandbox_pin",
                return_value=sandbox_fixture.GVISOR_PIN,
            ),
            mock.patch(
                "elt_taskgen.verification.contamination.enforcement",
                return_value=SimpleNamespace(value="enforce"),
            ),
            self.assertRaisesRegex(
                lifecycle.CertificationLifecycleError,
                "credential-shaped file",
            ),
        ):
            lifecycle.mint_unbound_sandbox_attestation(
                mount_root=root,
                out=out,
                preflight=preflight,
            )
        self.assertFalse(out.exists())

    def test_tier_d_unbound_mint_never_creates_an_output(self) -> None:
        preflight = sandbox_mod.isolation_preflight(
            runner=_MissingRunner(),
            environ={sandbox_mod.ISOLATION_ENV: sandbox_mod.DEV_ONLY},
            platform_system="Darwin",
        )
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        out = root / "attestation.json"
        with self.assertRaisesRegex(
            lifecycle.CertificationLifecycleError,
            "tier 'D'",
        ):
            lifecycle.mint_unbound_sandbox_attestation(
                mount_root=root,
                out=out,
                preflight=preflight,
            )
        self.assertFalse(out.exists())

    def test_dev_only_mac_tier_d_cannot_enter_lifecycle(self) -> None:
        preflight = sandbox_mod.isolation_preflight(
            runner=_MissingRunner(),
            environ={sandbox_mod.ISOLATION_ENV: sandbox_mod.DEV_ONLY},
            platform_system="Darwin",
        )
        self.assertEqual(preflight.tier, "D")
        mount = self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(mount, ignore_errors=True))
        record = sandbox_mod.attest_sandbox(
            pinned={
                "runtime": "none",
                "image_digest": "",
                "workspace_template_sha256": "",
            },
            preflight=preflight,
            package_digest="",
            tool_manifest_sha256="",
            diagnostics_version="2",
            verifier_code_sha256="",
            contamination_mode="enforce",
            mount_root=mount,
            run_id="release-test",
        )
        manifest = SimpleNamespace(
            release_id="release-test",
            corpus_profile="eltbench_end_to_end",
            variants={"task": ("extract_load", "transform")},
        )
        with (
            mock.patch.object(
                lifecycle,
                "_verified_manifest",
                return_value=(Path("/release"), manifest),
            ),
            self.assertRaisesRegex(
                lifecycle.CertificationLifecycleError,
                "tier 'D'",
            ),
        ):
            lifecycle.verify_release_bound_sandbox(
                Path("/release"), "task", record
            )

    def test_tier_d_mint_never_creates_an_output(self) -> None:
        preflight = sandbox_mod.isolation_preflight(
            runner=_MissingRunner(),
            environ={sandbox_mod.ISOLATION_ENV: sandbox_mod.DEV_ONLY},
            platform_system="Darwin",
        )
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        out = root / "attestation.json"
        manifest = SimpleNamespace(
            release_id="release-test",
            corpus_profile="eltbench_end_to_end",
            variants={"task": ("extract_load", "transform")},
        )
        with (
            mock.patch.object(
                lifecycle,
                "_verified_manifest",
                return_value=(Path("/release"), manifest),
            ),
            self.assertRaisesRegex(
                lifecycle.CertificationLifecycleError,
                "tier 'D'",
            ),
        ):
            lifecycle.mint_release_bound_sandbox_attestation(
                release_dir=Path("/release"),
                task_id="task",
                mount_root=root,
                out=out,
                preflight=preflight,
            )
        self.assertFalse(out.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Fresh-copy evaluator controls and receipt-gated readiness."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from elt_taskgen import demo_fixture
from elt_taskgen.engine import Engine, StageName
from elt_taskgen.export import eltbench, local_package
from elt_taskgen.export.package_verification import (
    PackageControlResult,
    verify_fresh_local_package,
    verify_persisted_package_receipt,
)
from elt_taskgen.generation import source_data
from elt_taskgen.models import RLVR_TASK_VARIANTS, canonical_json
from elt_taskgen.pipeline_readiness import (
    GenerationRunSpec,
    ReadinessProfile,
    ReadinessState,
    StageReadiness,
    task_readiness,
)
from elt_taskgen.reference import gold as gold_mod
from elt_taskgen.reference.runner import run_reference
from tests.canonical_doubles import write_canonical_reachability


class FreshPackageVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.workspace = cls.root / "source-workspace"
        cls.engine = Engine(cls.workspace)
        cls.engine.register(demo_fixture.demo_task())
        cls.task = cls.engine.load_task(demo_fixture.DEMO_TASK_ID)
        task_root = cls.engine.task_dir(cls.task.task_id)

        populations = task_root / "populations"
        for population in cls.task.populations:
            source_data.materialize_population(
                cls.task,
                population.name,
                populations / population.name.value,
            )
        results = {
            population.name: run_reference(
                cls.task, population.name, cls.workspace
            )
            for population in cls.task.populations
        }
        gold = gold_mod.freeze_gold(
            cls.task, results, task_root / "answer_key"
        )
        public_parent = task_root / "task"
        public_parent.mkdir()
        (public_parent / "README.md").write_text(
            "Build the declared source and mart contract.\n", encoding="utf-8"
        )
        for variant in RLVR_TASK_VARIANTS:
            bundle = task_root / "variants" / variant.value
            public = bundle / "task"
            public.mkdir(parents=True)
            (public / "README.md").write_text(
                f"Complete the {variant.value} unit.\n", encoding="utf-8"
            )
            (bundle / "reward.json").write_text(
                canonical_json(eltbench.reward_manifest(cls.task, gold, variant))
                + "\n",
                encoding="utf-8",
            )

        write_canonical_reachability(cls.workspace, cls.task)
        cls.package = cls.root / "packages" / cls.task.task_id
        acceptances = {
            variant.value: SimpleNamespace(accepted=True)
            for variant in RLVR_TASK_VARIANTS
        }
        with mock.patch(
            "elt_taskgen.export.release.variant_acceptance",
            return_value=acceptances,
        ):
            local_package.freeze_local_package(
                cls.engine, cls.task, cls.package
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.close()
        cls.temp.cleanup()

    def setUp(self) -> None:
        # The synthetic canonical record cannot be re-scored by the real
        # workspace grader; the packaging control is exercised by
        # tests/test_training_canonical.py on a real artifact.
        patcher = mock.patch(
            "elt_taskgen.export.package_verification._canonical_reachability_control",
            return_value=PackageControlResult(
                name="canonical_workspace_reachable",
                passed=True,
                expected="synthetic reachable record (test double)",
                observed="reward=1.0",
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _receipt_path(self, name: str) -> Path:
        return self.root / "receipts" / f"{name}.json"

    def test_fresh_copy_controls_and_permission_canary_persist(self) -> None:
        receipt_path = self._receipt_path("pass")
        receipt = verify_fresh_local_package(
            self.package,
            receipt_path=receipt_path,
        )

        self.assertTrue(receipt.verified, receipt.failures)
        self.assertTrue(receipt.source_static_verified)
        self.assertTrue(receipt.fresh_copy_static_verified)
        self.assertTrue(receipt.package_inputs_from_fresh_copy)
        self.assertIn("shutil.copytree", receipt.fresh_copy_method)
        self.assertEqual(
            receipt.fresh_copy_inventory_sha256,
            receipt.package_inventory_sha256,
        )
        self.assertRegex(
            receipt.evaluator_runtime["generator_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertRegex(
            receipt.evaluator_runtime["dependency_lock_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(
            {control.name for control in receipt.controls},
            {
                "trusted_reference_positive",
                "wrong_transform_negative",
                "failed_extract_load_negative",
                "canonical_workspace_reachable",
            },
        )
        self.assertTrue(all(control.passed for control in receipt.controls))
        self.assertTrue(receipt.permission_canary.attempted)
        self.assertTrue(receipt.permission_canary.blocked)
        self.assertFalse(receipt.permission_canary.separate_os_principal)
        self.assertFalse(receipt.permission_canary.live_model_attempted)
        self.assertFalse(receipt.live_model_attempted)
        self.assertTrue(receipt_path.is_file())

        persisted = verify_persisted_package_receipt(
            receipt_path,
            package_path=self.package,
            expected_task_id=self.task.task_id,
            expected_task_content_hash=self.task.content_hash(),
        )
        self.assertTrue(persisted.ok, persisted.failures)
        self.assertEqual(persisted.receipt_sha256, receipt.receipt_sha256)

    def test_evaluator_or_lock_identity_drift_stales_the_receipt(self) -> None:
        receipt_path = self._receipt_path("runtime-drift")
        receipt = verify_fresh_local_package(
            self.package,
            receipt_path=receipt_path,
        )
        self.assertTrue(receipt.verified, receipt.failures)
        drifted = dict(receipt.evaluator_runtime)
        drifted["dependency_lock_sha256"] = "f" * 64
        with mock.patch(
            "elt_taskgen.export.package_verification._evaluator_runtime_identity",
            return_value=drifted,
        ):
            persisted = verify_persisted_package_receipt(
                receipt_path,
                package_path=self.package,
            )
        self.assertFalse(persisted.ok)
        self.assertTrue(
            any("runtime identity drifted" in row for row in persisted.failures),
            persisted.failures,
        )

    def test_failed_control_is_persisted_but_never_certified(self) -> None:
        receipt_path = self._receipt_path("failed-control")
        failed = PackageControlResult(
            name="trusted_reference_positive",
            passed=False,
            expected="reward 1.0",
            observed="reward 0.0",
        )
        with mock.patch(
            "elt_taskgen.export.package_verification._provider_free_controls",
            return_value=(failed,),
        ):
            receipt = verify_fresh_local_package(
                self.package,
                receipt_path=receipt_path,
            )
        self.assertFalse(receipt.verified)
        self.assertTrue(receipt_path.is_file())
        self.assertTrue(any("control" in row for row in receipt.failures))
        persisted = verify_persisted_package_receipt(
            receipt_path, package_path=self.package
        )
        self.assertFalse(persisted.ok)
        self.assertTrue(
            any("failed verification" in row for row in persisted.failures)
        )

    def test_readiness_exposes_path_only_for_current_verified_receipt(self) -> None:
        receipt_path = self._receipt_path("readiness")
        verify_fresh_local_package(self.package, receipt_path=receipt_path)
        spec = GenerationRunSpec(
            candidate_count=1,
            profile=ReadinessProfile.PACKAGED,
            budget_per_task=7.0,
            export_dir=str(self.package.parent),
        )

        def passed(_engine, _task, stage, *, spec=None):
            return StageReadiness(
                stage=StageName(stage), state=ReadinessState.PASS
            )

        with mock.patch(
            "elt_taskgen.pipeline_readiness.stage_readiness",
            side_effect=passed,
        ):
            readiness = task_readiness(
                self.engine,
                self.task,
                package_path=self.package,
                package_receipt_path=receipt_path,
                spec=spec,
            )
        self.assertTrue(readiness.packaged_for_evaluation)
        self.assertEqual(readiness.package_status, ReadinessState.PASS)
        self.assertEqual(readiness.package_path, str(self.package.resolve()))
        self.assertEqual(
            readiness.package_path_if_verified, str(self.package.resolve())
        )
        self.assertTrue(readiness.package_verification_receipt)

        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        payload["generated_at"] = "tampered"
        receipt_path.write_text(json.dumps(payload), encoding="utf-8")
        with mock.patch(
            "elt_taskgen.pipeline_readiness.stage_readiness",
            side_effect=passed,
        ):
            invalid = task_readiness(
                self.engine,
                self.task,
                package_path=self.package,
                package_receipt_path=receipt_path,
                spec=spec,
            )
        self.assertFalse(invalid.packaged_for_evaluation)
        self.assertEqual(invalid.package_status, ReadinessState.FAIL)
        self.assertEqual(invalid.package_path, "")
        self.assertEqual(invalid.package_path_if_verified, "")
        self.assertTrue(
            any("digest differs" in row for row in invalid.precise_blockers),
            invalid.precise_blockers,
        )

    def test_receipt_and_scratch_must_be_outside_immutable_package(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the package"):
            verify_fresh_local_package(
                self.package,
                receipt_path=self.package / "receipt.json",
            )
        with self.assertRaisesRegex(ValueError, "outside the package"):
            verify_fresh_local_package(
                self.package,
                receipt_path=self._receipt_path("scratch"),
                scratch_root=self.package / "scratch",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

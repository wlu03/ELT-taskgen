"""Self-contained, fail-closed local evaluator package contracts."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from elt_taskgen import demo_fixture
from elt_taskgen.engine import Engine
from elt_taskgen.export import eltbench, local_package
from elt_taskgen.generation import source_data
from elt_taskgen.models import (
    RLVR_TASK_VARIANTS,
    TaskVariant,
    canonical_json,
    task_from_json,
    variant_task_id,
)
from elt_taskgen.reference import gold as gold_mod
from elt_taskgen.reference.runner import RunResult, run_reference
from elt_taskgen.verification.upstream_eval import evaluate_variant
from tests.canonical_doubles import write_canonical_reachability


class LocalEvaluatorPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "source-workspace"
        self.engine = Engine(self.workspace)
        self.addCleanup(self.engine.close)
        self.engine.register(demo_fixture.demo_task())
        self.task = self.engine.load_task(demo_fixture.DEMO_TASK_ID)
        self.task_root = self.engine.task_dir(self.task.task_id)

        populations = self.task_root / "populations"
        for spec in self.task.populations:
            source_data.materialize_population(
                self.task, spec.name, populations / spec.name.value
            )

        mart_row = {
            "customer_id": 1,
            "completed_order_count": 0,
            "total_spend": 0.0,
        }
        results = {
            spec.name: RunResult(
                population=spec.name,
                stage1_counts={table.name: 1 for table in self.task.tables},
                mart_rows={mart.name: [mart_row] for mart in self.task.marts},
            )
            for spec in self.task.populations
        }
        self.gold = gold_mod.freeze_gold(
            self.task, results, self.task_root / "answer_key"
        )

        parent_public = self.task_root / "task"
        parent_public.mkdir()
        (parent_public / "README.md").write_text(
            "Build the declared source and mart contract.\n", encoding="utf-8"
        )
        for variant in RLVR_TASK_VARIANTS:
            bundle = self.task_root / "variants" / variant.value
            public = bundle / "task"
            public.mkdir(parents=True)
            (public / "README.md").write_text(
                f"Complete the {variant.value} unit.\n", encoding="utf-8"
            )
            (bundle / "reward.json").write_text(
                canonical_json(
                    eltbench.reward_manifest(self.task, self.gold, variant)
                )
                + "\n",
                encoding="utf-8",
            )

        write_canonical_reachability(self.workspace, self.task)
        self.package = self.root / "packages" / self.task.task_id
        self._freeze(self.package)

    def _acceptances(self) -> dict[str, SimpleNamespace]:
        return {
            variant.value: SimpleNamespace(accepted=True)
            for variant in RLVR_TASK_VARIANTS
        }

    def _freeze(self, destination: Path) -> local_package.LocalPackageManifest:
        with mock.patch(
            "elt_taskgen.export.release.variant_acceptance",
            return_value=self._acceptances(),
        ):
            return local_package.freeze_local_package(
                self.engine, self.task, destination
            )

    def _rewrite_manifest_with_current_inventory(self) -> None:
        path = self.package / local_package.LOCAL_PACKAGE_MANIFEST
        manifest = local_package.LocalPackageManifest.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        updated = manifest.model_copy(
            update={"files": local_package._file_inventory(self.package)}
        )
        path.write_text(
            json.dumps(updated.model_dump(mode="json"), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

    def test_verifies_after_the_entire_source_workspace_is_removed(self) -> None:
        manifest = local_package.LocalPackageManifest.model_validate_json(
            (self.package / local_package.LOCAL_PACKAGE_MANIFEST).read_text(
                encoding="utf-8"
            )
        )
        shutil.rmtree(self.workspace)

        result = local_package.verify_local_package(self.package)
        self.assertTrue(result.ok, result.failures)
        self.assertEqual(result.task_id, self.task.task_id)
        self.assertEqual(result.task_content_hash, self.task.content_hash())
        self.assertEqual(result.files_checked, len(manifest.files))
        self.assertFalse(self.workspace.exists())

    def test_clean_import_executes_using_only_packaged_assets(self) -> None:
        # Replace the small structural fixture gold with genuinely executed
        # reference results, then freeze a fresh package around that evidence.
        results = {
            spec.name: run_reference(self.task, spec.name, self.workspace)
            for spec in self.task.populations
        }
        executed_gold = gold_mod.freeze_gold(
            self.task, results, self.task_root / "answer_key"
        )
        for variant in RLVR_TASK_VARIANTS:
            reward = self.task_root / "variants" / variant.value / "reward.json"
            reward.write_text(
                canonical_json(
                    eltbench.reward_manifest(self.task, executed_gold, variant)
                )
                + "\n",
                encoding="utf-8",
            )
        executed_package = self.root / "packages" / "executed"
        self._freeze(executed_package)

        # Simulate a clean import. The original workspace is gone, and the
        # runner workspace is reconstructed exclusively from the package's
        # private population tree.
        shutil.rmtree(self.workspace)
        private = executed_package / "private" / self.task.task_id
        imported_task = task_from_json(
            (private / "task_ir.json").read_text(encoding="utf-8")
        )
        imported_gold = gold_mod.load_gold(private / "answer_key")
        clean_workspace = self.root / "clean-import"
        shutil.copytree(
            private / "populations",
            clean_workspace
            / "tasks"
            / imported_task.task_id
            / "populations",
        )

        for spec in imported_task.populations:
            actual = run_reference(imported_task, spec.name, clean_workspace)
            full = evaluate_variant(
                TaskVariant.FULL,
                imported_task,
                imported_gold,
                spec.name,
                actual_stage1=actual.stage1_counts,
                actual_marts=actual.mart_rows,
            )
            extract_load = evaluate_variant(
                TaskVariant.EXTRACT_LOAD,
                imported_task,
                imported_gold,
                spec.name,
                actual_stage1=actual.stage1_counts,
            )
            transform = evaluate_variant(
                TaskVariant.TRANSFORM,
                imported_task,
                imported_gold,
                spec.name,
                actual_marts=actual.mart_rows,
            )
            self.assertEqual(full.reward, 1.0)
            self.assertEqual(extract_load.reward, 1.0)
            self.assertEqual(transform.reward, 1.0)

    def test_missing_modified_and_unexpected_files_each_fail_closed(self) -> None:
        cases = ("missing", "modified", "unexpected")
        for case in cases:
            with self.subTest(case=case):
                copied = self.root / f"tamper-{case}"
                shutil.copytree(self.package, copied)
                readme = copied / "public" / self.task.task_id / "README.md"
                if case == "missing":
                    readme.unlink()
                    expected = "missing files"
                elif case == "modified":
                    readme.write_text("changed\n", encoding="utf-8")
                    expected = "digest mismatches"
                else:
                    (copied / "unexpected.txt").write_text(
                        "not declared\n", encoding="utf-8"
                    )
                    expected = "undeclared files"

                result = local_package.verify_local_package(copied)
                self.assertFalse(result.ok)
                self.assertTrue(
                    any(expected in failure for failure in result.failures),
                    result.failures,
                )

    def test_self_consistent_public_private_leak_is_still_refused(self) -> None:
        leaked = (
            self.package / "public" / self.task.task_id / "answer_key" / "secret.txt"
        )
        leaked.parent.mkdir()
        leaked.write_text("private evaluator material\n", encoding="utf-8")
        # Model an attacker or buggy producer that also updates the unsigned
        # inventory. Structural privacy checks must remain authoritative.
        self._rewrite_manifest_with_current_inventory()

        result = local_package.verify_local_package(self.package)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("leak: forbidden name" in failure for failure in result.failures),
            result.failures,
        )

    def test_freeze_refuses_a_private_artifact_in_a_public_source_tree(self) -> None:
        leaked = self.task_root / "task" / "answer_key" / "secret.txt"
        leaked.parent.mkdir()
        leaked.write_text("private evaluator material\n", encoding="utf-8")
        destination = self.root / "packages" / "leaking-source"

        with self.assertRaisesRegex(ValueError, "leak: forbidden name"):
            self._freeze(destination)
        self.assertFalse(destination.exists())

    @unittest.skipIf(os.name == "nt", "symlink creation is not generally available")
    def test_package_root_symlink_is_refused(self) -> None:
        link = self.root / "package-link"
        link.symlink_to(self.package, target_is_directory=True)

        result = local_package.verify_local_package(link)
        self.assertFalse(result.ok)
        self.assertIn("non-symlink directory", result.failures[0])

        dangling = self.root / "dangling-package-link"
        dangling.symlink_to(self.root / "does-not-exist", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            self._freeze(dangling)
        self.assertTrue(dangling.is_symlink())

    def test_exact_public_and_private_rosters_are_enforced(self) -> None:
        extra_public = self.package / "public" / "unmeasured-unit"
        extra_public.mkdir()
        (extra_public / "README.md").write_text("extra\n", encoding="utf-8")
        self._rewrite_manifest_with_current_inventory()

        result = local_package.verify_local_package(self.package)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("public unit roster differs" in failure for failure in result.failures),
            result.failures,
        )

    def test_expected_public_private_locations_are_disjoint(self) -> None:
        for variant in RLVR_TASK_VARIANTS:
            self.assertTrue(
                (
                    self.package
                    / "public"
                    / variant_task_id(self.task.task_id, variant)
                ).is_dir()
            )
            self.assertTrue(
                (
                    self.package
                    / "private"
                    / self.task.task_id
                    / "variants"
                    / variant.value
                    / "reward.json"
                ).is_file()
            )
        public_names = {
            path.name for path in (self.package / "public").rglob("*")
        }
        self.assertFalse(
            public_names & {"answer_key", "populations", "reward.json"}
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

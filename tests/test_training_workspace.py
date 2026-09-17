"""Phase-0/1 lifecycle tests for the cloud-free artifact workspace."""

from __future__ import annotations

import itertools
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import elt_taskgen.training.workspace as workspace_mod
from elt_taskgen.training import (
    WorkspaceActionTraceEntry,
    WorkspaceErrorCode,
    WorkspaceLifecycleError,
    WorkspacePackageError,
    install_workspace,
    load_sealed_workspace,
    load_workspace_package,
    replay_workspace,
    seal_workspace,
)
from elt_taskgen.training.contract import (
    MAX_WORKSPACE_FILE_BYTES,
    MAX_WORKSPACE_FILES,
)


FIXTURE_RELEASE = (
    Path(__file__).resolve().parent / "fixtures" / "semantic_gate" / "release"
)
TASK_ID = "gate__five_backend_probe"


def _write_candidate(attempt) -> None:
    (attempt.elt_dir / "dbt_project.yml").write_text(
        "name: gate_task\nversion: '1.0'\nprofile: elt_training\n"
        "model-paths: ['models']\n",
        encoding="utf-8",
    )
    models = attempt.elt_dir / "models"
    models.mkdir()
    (models / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n"
        "      - name: customers\n",
        encoding="utf-8",
    )
    (models / "customer_rollup.sql").write_text(
        "{{ config(materialized='table') }}\nselect * from {{ source('raw', 'customers') }}\n",
        encoding="utf-8",
    )


class WorkspacePackageTests(unittest.TestCase):
    def test_loads_combined_documentation_readme_not_retired_top_level_file(self) -> None:
        package = load_workspace_package(FIXTURE_RELEASE, TASK_ID)
        self.assertEqual(
            package.documentation_path.relative_to(package.public_dir).as_posix(),
            "documentation/README.md",
        )
        self.assertFalse((package.public_dir / "documentation.md").exists())
        self.assertEqual(package.destination.value, "snowflake")
        self.assertEqual(package.logical_namespace, TASK_ID)

    def test_rejects_retired_top_level_documentation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "release"
            shutil.copytree(FIXTURE_RELEASE, copied)
            (copied / "public" / TASK_ID / "documentation.md").write_text(
                "retired\n", encoding="utf-8"
            )
            with self.assertRaises(WorkspacePackageError):
                load_workspace_package(copied, TASK_ID, verify=False)

    def test_airbyte_contract_is_hash_bound_deeply_immutable_and_backend_bound(self) -> None:
        package = load_workspace_package(FIXTURE_RELEASE, TASK_ID)
        self.assertEqual(len(package.airbyte_contract_sha256), 64)
        destination = package.airbyte_contract["destination"]
        with self.assertRaises(TypeError):
            destination["key"] = "redshift"  # type: ignore[index]

        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "release"
            shutil.copytree(FIXTURE_RELEASE, copied)
            contract_path = (
                copied
                / "private"
                / TASK_ID
                / "answer_key"
                / "runtime"
                / "airbyte_connector_contract.json"
            )
            payload = json.loads(contract_path.read_text(encoding="utf-8"))
            payload["sources"][0]["key"] = "mongodb"
            contract_path.chmod(0o644)
            contract_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(WorkspacePackageError, "backend disagrees"):
                load_workspace_package(copied, TASK_ID, verify=False)


class WorkspaceLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.package = load_workspace_package(FIXTURE_RELEASE, TASK_ID)

    def test_install_is_fresh_public_only_and_candidate_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt_path = root / "attempt"
            attempt = install_workspace(self.package, attempt_path)
            self.assertTrue((attempt.task_dir / "documentation" / "README.md").is_file())
            self.assertFalse((attempt.task_dir / "documentation.md").exists())
            self.assertFalse((attempt.root / "private").exists())
            self.assertEqual(attempt.elt_dir, attempt.task_dir / "elt")
            with self.assertRaises(WorkspaceLifecycleError) as raised:
                install_workspace(self.package, attempt_path)
            self.assertEqual(raised.exception.code, WorkspaceErrorCode.WORKSPACE_NOT_FRESH)

    def test_seal_snapshots_only_candidate_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = install_workspace(self.package, root / "attempt")
            _write_candidate(attempt)
            sealed = seal_workspace(attempt, self.package, root / "sealed")
            relative = {
                path.relative_to(sealed.root).as_posix()
                for path in sealed.root.rglob("*")
            }
            self.assertIn("elt/main.tf", relative)
            self.assertIn("elt/models/customer_rollup.sql", relative)
            self.assertIn("workspace.json", relative)
            self.assertFalse(any("credential" in path for path in relative))
            self.assertFalse(any(path.startswith("private/") for path in relative))
            loaded = load_sealed_workspace(sealed.root)
            self.assertEqual(loaded.submission, sealed.submission)

    def test_outside_elt_mutation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = install_workspace(self.package, root / "attempt")
            _write_candidate(attempt)
            config = attempt.task_dir / "config.yaml"
            config.chmod(0o644)
            config.write_text("changed: true\n", encoding="utf-8")
            with self.assertRaises(WorkspaceLifecycleError) as raised:
                seal_workspace(attempt, self.package, root / "sealed")
            self.assertEqual(
                raised.exception.code,
                WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = install_workspace(self.package, root / "attempt")
            _write_candidate(attempt)
            marker = json.loads(attempt.marker_path.read_text(encoding="utf-8"))
            marker["candidate_root"] = "task"
            attempt.marker_path.chmod(0o644)
            attempt.marker_path.write_text(
                json.dumps(marker, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(WorkspaceLifecycleError) as raised:
                seal_workspace(attempt, self.package, root / "sealed")
            self.assertEqual(
                raised.exception.code,
                WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = install_workspace(self.package, root / "attempt")
            _write_candidate(attempt)
            config = attempt.task_dir / "config.yaml"
            config.chmod(0o644)
            with config.open("wb") as handle:
                handle.truncate(MAX_WORKSPACE_FILE_BYTES * 128)
            with self.assertRaises(WorkspaceLifecycleError) as raised:
                seal_workspace(attempt, self.package, root / "sealed")
            self.assertEqual(
                raised.exception.code,
                WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
            )

    def test_symlink_transient_state_and_secret_literals_are_rejected(self) -> None:
        cases = ("symlink", "state", "secret")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                attempt = install_workspace(self.package, root / "attempt")
                _write_candidate(attempt)
                if case == "symlink":
                    os.symlink(
                        attempt.task_dir / "config.yaml",
                        attempt.elt_dir / "models" / "escape.sql",
                    )
                    expected = WorkspaceErrorCode.WORKSPACE_SYMLINK
                elif case == "state":
                    state = attempt.elt_dir / ".terraform"
                    state.mkdir()
                    (state / "state").write_text("stale\n", encoding="utf-8")
                    expected = WorkspaceErrorCode.WORKSPACE_TRANSIENT_STATE
                else:
                    (attempt.elt_dir / "main.tf").write_text(
                        'password = "real-secret"\n', encoding="utf-8"
                    )
                    expected = WorkspaceErrorCode.WORKSPACE_SECRET_LITERAL
                with self.assertRaises(WorkspaceLifecycleError) as raised:
                    seal_workspace(attempt, self.package, root / "sealed")
                self.assertEqual(raised.exception.code, expected)

    def test_all_declared_secret_key_literals_are_rejected_but_placeholders_pass(self) -> None:
        sensitive_keys = (
            "api_key",
            "api_secret_key",
            "personal_access_token",
            "access_key_id",
            "aws_access_key_id",
        )
        for key in sensitive_keys:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                attempt = install_workspace(self.package, root / "attempt")
                _write_candidate(attempt)
                (attempt.elt_dir / "main.tf").write_text(
                    f'{key} = "live-secret-value"\n',
                    encoding="utf-8",
                )
                with self.assertRaises(WorkspaceLifecycleError) as raised:
                    seal_workspace(attempt, self.package, root / "sealed")
                self.assertEqual(
                    raised.exception.code,
                    WorkspaceErrorCode.WORKSPACE_SECRET_LITERAL,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = install_workspace(self.package, root / "attempt")
            _write_candidate(attempt)
            (attempt.elt_dir / "main.tf").write_text(
                'api_key = "${var.api_key}"\n'
                'api_secret_key = "<injected>"\n'
                'personal_access_token = "{{ env_var(\'TOKEN\') }}"\n',
                encoding="utf-8",
            )
            sealed = seal_workspace(attempt, self.package, root / "sealed")
            self.assertTrue(sealed.manifest_path.is_file())

    def test_workspace_limits_fail_before_unbounded_artifacts_are_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = install_workspace(self.package, root / "attempt")
            _write_candidate(attempt)
            for index in range(MAX_WORKSPACE_FILES):
                (attempt.elt_dir / "models" / f"extra_{index:04d}.sql").write_text(
                    "select 1\n",
                    encoding="utf-8",
                )
            with self.assertRaises(WorkspaceLifecycleError) as raised:
                seal_workspace(attempt, self.package, root / "sealed")
            self.assertEqual(
                raised.exception.code,
                WorkspaceErrorCode.WORKSPACE_SIZE_LIMIT,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = install_workspace(self.package, root / "attempt")
            _write_candidate(attempt)
            oversized = attempt.elt_dir / "models" / "oversized.sql"
            with oversized.open("wb") as handle:
                handle.truncate(MAX_WORKSPACE_FILE_BYTES + 1)
            with self.assertRaises(WorkspaceLifecycleError) as raised:
                seal_workspace(attempt, self.package, root / "sealed")
            self.assertEqual(
                raised.exception.code,
                WorkspaceErrorCode.WORKSPACE_SIZE_LIMIT,
            )

    def test_action_trace_consumption_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = install_workspace(self.package, root / "attempt")
            _write_candidate(attempt)
            entry = WorkspaceActionTraceEntry(
                sequence=0,
                action="write_main_tf",
                success=True,
            )
            trace = itertools.repeat(entry)
            with self.assertRaises(WorkspaceLifecycleError) as raised:
                seal_workspace(
                    attempt,
                    self.package,
                    root / "sealed",
                    action_trace=trace,
                )
            self.assertEqual(
                raised.exception.code,
                WorkspaceErrorCode.WORKSPACE_SIZE_LIMIT,
            )

    def test_target_claim_is_exclusive_and_readonly_failures_clean_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "attempt"
            claimed = threading.Event()
            resume = threading.Event()
            errors: list[BaseException] = []
            original_digest = workspace_mod.public_tree_digest

            def blocked_digest(path, *, exclude_elt=False):
                if path == self.package.public_dir and not claimed.is_set():
                    claimed.set()
                    if not resume.wait(timeout=5):
                        raise TimeoutError("target-claim test timed out")
                return original_digest(path, exclude_elt=exclude_elt)

            def install_first() -> None:
                try:
                    install_workspace(self.package, target)
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            with patch.object(workspace_mod, "public_tree_digest", blocked_digest):
                worker = threading.Thread(target=install_first)
                worker.start()
                self.assertTrue(claimed.wait(timeout=5))
                try:
                    self.assertTrue(target.is_dir())
                    self.assertFalse((target / "attempt.json").exists())
                    with self.assertRaises(WorkspaceLifecycleError) as raised:
                        install_workspace(self.package, target)
                    self.assertEqual(
                        raised.exception.code,
                        WorkspaceErrorCode.WORKSPACE_NOT_FRESH,
                    )
                finally:
                    resume.set()
                    worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_modes = workspace_mod._set_installed_modes

            def fail_after_readonly(task_dir: Path) -> None:
                original_modes(task_dir)
                raise OSError("forced publish failure")

            with patch.object(
                workspace_mod,
                "_set_installed_modes",
                fail_after_readonly,
            ):
                with self.assertRaisesRegex(OSError, "forced publish failure"):
                    install_workspace(self.package, root / "attempt")
            self.assertEqual(list(root.iterdir()), [])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = install_workspace(self.package, root / "attempt")
            _write_candidate(attempt)
            original_readonly = workspace_mod._make_tree_read_only

            def fail_after_seal_readonly(elt_dir: Path) -> None:
                original_readonly(elt_dir)
                raise OSError("forced seal failure")

            with patch.object(
                workspace_mod,
                "_make_tree_read_only",
                fail_after_seal_readonly,
            ):
                with self.assertRaisesRegex(OSError, "forced seal failure"):
                    seal_workspace(attempt, self.package, root / "sealed")
            self.assertEqual({path.name for path in root.iterdir()}, {"attempt"})

    def test_targets_cannot_overlap_release_or_mutable_attempt(self) -> None:
        forbidden = self.package.release_dir / ".workspace-security-test"
        self.assertFalse(forbidden.exists())
        with self.assertRaises(WorkspaceLifecycleError) as raised:
            install_workspace(self.package, forbidden)
        self.assertEqual(
            raised.exception.code,
            WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE,
        )
        self.assertFalse(forbidden.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = install_workspace(self.package, root / "attempt")
            _write_candidate(attempt)
            with self.assertRaises(WorkspaceLifecycleError) as raised:
                seal_workspace(
                    attempt,
                    self.package,
                    attempt.root / "nested-seal",
                )
            self.assertEqual(
                raised.exception.code,
                WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE,
            )

    def test_replay_requires_and_rechecks_a_harness_seal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = install_workspace(self.package, root / "attempt")
            _write_candidate(attempt)
            sealed = seal_workspace(attempt, self.package, root / "sealed")

            inspected = load_sealed_workspace(sealed.root)
            with self.assertRaises(WorkspaceLifecycleError) as raised:
                replay_workspace(self.package, inspected, root / "untrusted-replay")
            self.assertEqual(
                raised.exception.code,
                WorkspaceErrorCode.SUBMISSION_INVALID,
            )
            replayed = replay_workspace(
                self.package,
                sealed.root,
                root / "digest-replay",
                expected_seal_sha256=sealed.seal_sha256,
            )
            self.assertTrue(replayed.elt_dir.is_dir())

            sealed.manifest_path.chmod(0o644)
            sealed.manifest_path.write_text(
                sealed.manifest_path.read_text(encoding="utf-8") + " \n",
                encoding="utf-8",
            )
            load_sealed_workspace(sealed.root)
            with self.assertRaises(WorkspaceLifecycleError) as raised:
                replay_workspace(self.package, sealed, root / "tampered-replay")
            self.assertEqual(
                raised.exception.code,
                WorkspaceErrorCode.WORKSPACE_DIGEST_MISMATCH,
            )

            with self.assertRaises(WorkspaceLifecycleError) as raised:
                load_sealed_workspace(root / "missing-seal")
            self.assertEqual(
                raised.exception.code,
                WorkspaceErrorCode.SUBMISSION_INVALID,
            )

    def test_digest_ignores_mtime_and_replays_are_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = install_workspace(self.package, root / "attempt")
            _write_candidate(attempt)
            first = seal_workspace(attempt, self.package, root / "sealed-one")

            replay_a = replay_workspace(self.package, first, root / "replay-a")
            for path in replay_a.elt_dir.rglob("*"):
                if path.is_file():
                    os.utime(path, (1, 1))
            second = seal_workspace(replay_a, self.package, root / "sealed-two")
            self.assertEqual(
                first.submission.artifact_sha256,
                second.submission.artifact_sha256,
            )

            stale = replay_a.elt_dir / ".terraform"
            stale.mkdir()
            (stale / "state").write_text("attempt A only\n", encoding="utf-8")
            replay_b = replay_workspace(self.package, first, root / "replay-b")
            self.assertFalse((replay_b.elt_dir / ".terraform").exists())
            self.assertNotEqual(replay_a.root, replay_b.root)


class WorkspaceMacrosSurfaceTests(unittest.TestCase):
    """Roadmap Phase 0.A: ``macros`` is not an admitted top-level candidate
    path, so the seal agrees with ``dbt_runner._prepare_execution_project``,
    which already rejects any ``macros`` path component."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.package = load_workspace_package(FIXTURE_RELEASE, TASK_ID)

    def test_workspace_rejects_macros_top_level(self) -> None:
        import inspect

        from elt_taskgen.training import dbt_runner

        self.assertNotIn("macros", workspace_mod._ALLOWED_TOP_LEVEL)
        self.assertEqual(
            workspace_mod._ALLOWED_TOP_LEVEL,
            frozenset({"main.tf", "dbt_project.yml", "models"}),
        )
        self.assertFalse(hasattr(workspace_mod, "_MACRO_SUFFIXES"))
        # The execution side still refuses the same component (agreement).
        self.assertIn(
            '"macros"', inspect.getsource(dbt_runner._prepare_execution_project)
        )
        for candidate in ("macros/helper.sql", "macros/nested/helper.sql", "macros"):
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                attempt = install_workspace(self.package, root / "attempt")
                _write_candidate(attempt)
                target = attempt.elt_dir / candidate
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    "{% macro noop() %}select 1{% endmacro %}\n", encoding="utf-8"
                )
                with self.assertRaises(WorkspaceLifecycleError) as raised:
                    seal_workspace(attempt, self.package, root / "sealed")
                self.assertEqual(
                    raised.exception.code,
                    WorkspaceErrorCode.WORKSPACE_TRANSIENT_STATE,
                )
        # Without the macros directory the same candidate seals.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = install_workspace(self.package, root / "attempt")
            _write_candidate(attempt)
            sealed = seal_workspace(attempt, self.package, root / "sealed")
            self.assertTrue(sealed)


if __name__ == "__main__":
    unittest.main()

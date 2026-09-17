"""Distribution smoke tests for resources required by installed commands."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class PackageResourceTests(unittest.TestCase):
    def _load_setup_module(self, source: Path):
        spec = importlib.util.spec_from_file_location(
            "isolated_elt_taskgen_setup", source / "setup.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with mock.patch("setuptools.setup"):
            spec.loader.exec_module(module)
        return module

    def _minimal_setup_tree(self, directory: str) -> tuple[Path, object]:
        source = Path(directory) / "source"
        source.mkdir()
        shutil.copy2(ROOT / "setup.py", source / "setup.py")
        (source / ".dockerignore").write_text("**\n", encoding="utf-8")
        (source / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        images = source / "runtime-images"
        images.mkdir()
        (images / "input.txt").write_text("reviewed\n", encoding="utf-8")
        (images / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "runner-images-manifest-v2",
                    "files": ["input.txt"],
                    "images": {
                        "dbt:databricks": "example/dbt-databricks@sha256:" + "1" * 64,
                        "dbt:redshift": "example/dbt-redshift@sha256:" + "2" * 64,
                        "dbt:snowflake": "example/dbt-snowflake@sha256:" + "3" * 64,
                        "terraform": "example/terraform@sha256:" + "4" * 64,
                    },
                }
            ),
            encoding="utf-8",
        )
        module = self._load_setup_module(source)
        module.CONFIG_FILES = ()
        module.TOOLS = ()
        return source, module

    def test_setup_rejects_duplicate_runtime_manifest_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, module = self._minimal_setup_tree(directory)
            (source / "runtime-images" / "manifest.json").write_text(
                """{
  "schema_version": "runner-images-manifest-v2",
  "files": ["input.txt"],
  "files": ["input.txt"],
  "images": {}
}\n""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "duplicate key 'files'"):
                module._safe_manifest_files()

    def test_setup_rejects_symlinked_and_non_regular_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, module = self._minimal_setup_tree(directory)
            listed = source / "runtime-images" / "input.txt"
            outside = Path(directory) / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            listed.unlink()
            listed.symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                module._safe_manifest_files()

            listed.unlink()
            os.mkfifo(listed)
            with self.assertRaisesRegex(RuntimeError, "regular file"):
                module._safe_manifest_files()

    def test_setup_checks_explicit_package_resources_too(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, module = self._minimal_setup_tree(directory)
            config = source / "config"
            config.mkdir()
            outside = Path(directory) / "outside.yaml"
            outside.write_text("secret: true\n", encoding="utf-8")
            (config / "unsafe.yaml").symlink_to(outside)
            module.CONFIG_FILES = ("unsafe.yaml",)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                module._resource_files()

    def test_wheel_is_self_contained_and_sdist_excludes_local_environments(
        self,
    ) -> None:
        uv = shutil.which("uv")
        if uv is None:
            self.skipTest("uv is required by this project's installation contract")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            for name in (
                ".dockerignore",
                "MANIFEST.in",
                "README.md",
                "build_backend.py",
                "pyproject.toml",
                "setup.py",
                "uv.lock",
            ):
                shutil.copy2(ROOT / name, source / name)
            shutil.copytree(ROOT / "config", source / "config")
            shutil.copytree(
                ROOT / "runtime-images",
                source / "runtime-images",
                ignore=shutil.ignore_patterns(".venv", "__pycache__"),
            )
            shutil.copytree(
                ROOT / "src",
                source / "src",
                ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"),
            )
            (source / "tools").mkdir()
            for name in (
                "build_dbt_manifest.py",
                "parity_sample.py",
                "schemapile_index.py",
                "wikidbs_family_map.py",
            ):
                shutil.copy2(ROOT / "tools" / name, source / "tools" / name)
            excluded = source / "runtime-images" / "dbt-duckdb" / ".venv"
            excluded.mkdir()
            (excluded / "must-not-ship").write_text(
                "local environment\n", encoding="utf-8"
            )

            output = Path(directory) / "dist"
            process = subprocess.run(
                [
                    uv,
                    "build",
                    "--force-pep517",
                    "--no-build-logs",
                    "--out-dir",
                    str(output),
                    str(source),
                ],
                cwd=source,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            wheel = next(output.glob("*.whl"))
            sdist = next(output.glob("*.tar.gz"))

            required = {
                "elt_taskgen/_resources/uv.lock",
                "elt_taskgen/_resources/config/agents.yaml",
                "elt_taskgen/_resources/config/five_source_ingest.example.yaml",
                "elt_taskgen/_resources/config/generation_run.example.yaml",
                "elt_taskgen/_resources/config/sources.yaml",
                "elt_taskgen/_resources/tools/build_dbt_manifest.py",
                "elt_taskgen/_resources/tools/parity_sample.py",
                "elt_taskgen/_resources/tools/schemapile_index.py",
                "elt_taskgen/_resources/tools/wikidbs_family_map.py",
                "elt_taskgen/_resources/runtime-images/manifest.json",
                "elt_taskgen/_resources/runtime-images/dbt/uv.lock",
                "elt_taskgen/_resources/runtime-images/terraform/Dockerfile",
            }
            with zipfile.ZipFile(wheel) as archive:
                wheel_names = set(archive.namelist())
                self.assertTrue(required <= wheel_names)
                self.assertFalse(any("/.venv/" in name for name in wheel_names))
                entry_points_name = next(
                    name
                    for name in wheel_names
                    if name.endswith(".dist-info/entry_points.txt")
                )
                entry_points = archive.read(entry_points_name).decode("utf-8")
                self.assertIn(
                    "elt-taskgen-evidence-maintenance = "
                    "elt_taskgen.review.evidence_retention:main",
                    entry_points,
                )
                self.assertIn(
                    "elt-taskgen-parity-sample = "
                    "elt_taskgen.operator_tools:parity_sample_main",
                    entry_points,
                )
                self.assertIn(
                    "elt-taskgen-schemapile-index = "
                    "elt_taskgen.operator_tools:schemapile_index_main",
                    entry_points,
                )
                site = Path(directory) / "site"
                archive.extractall(site)

            with tarfile.open(sdist, "r:gz") as archive:
                sdist_names = archive.getnames()
            self.assertFalse(any("/.venv/" in name for name in sdist_names))
            self.assertFalse(any("/__pycache__/" in name for name in sdist_names))
            self.assertFalse(any("/build/" in name for name in sdist_names))
            self.assertFalse(any("/runs/" in name for name in sdist_names))
            self.assertFalse(any("/secrets/" in name for name in sdist_names))
            self.assertFalse(any("/tests/" in name for name in sdist_names))
            self.assertFalse(any("/council/" in name for name in sdist_names))
            self.assertFalse(any(name.endswith("/.env") for name in sdist_names))

            script = """
import json
import sys
sys.path.insert(0, sys.argv[1])
import elt_taskgen
from elt_taskgen.catalog import default_sources_config_path, load_source_catalog
from elt_taskgen.export.release import repo_lock_path
from elt_taskgen.package_resources import resource_path
from elt_taskgen.runtime_matrix import runner_images_digest
paths = [
    repo_lock_path(),
    default_sources_config_path(),
    resource_path('config/agents.yaml'),
    resource_path('config/dlt_connectors/airtable.yaml'),
    resource_path('config/five_source_ingest.example.yaml'),
    resource_path('config/generation_run.example.yaml'),
    resource_path('tools/build_dbt_manifest.py'),
    resource_path('tools/parity_sample.py'),
    resource_path('tools/schemapile_index.py'),
    resource_path('tools/wikidbs_family_map.py'),
    resource_path('runtime-images/terraform/Dockerfile'),
]
assert all(path.is_file() for path in paths), paths
assert load_source_catalog().pools
print(json.dumps({
    'module': elt_taskgen.__file__,
    'digest': runner_images_digest(),
}))
"""
            smoke = subprocess.run(
                [sys.executable, "-c", script, str(site)],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(smoke.returncode, 0, smoke.stderr)
            observed = json.loads(smoke.stdout)
            self.assertTrue(observed["module"].startswith(str(site)))
            self.assertRegex(observed["digest"], r"^[0-9a-f]{64}$")

            help_script = """
import sys
sys.path.insert(0, sys.argv[1])
from elt_taskgen.operator_tools import parity_sample_main, schemapile_index_main
from elt_taskgen.review.evidence_retention import main as evidence_maintenance_main
for entry_point in (evidence_maintenance_main, parity_sample_main, schemapile_index_main):
    try:
        entry_point(['--help'])
    except SystemExit as exc:
        assert exc.code == 0, exc.code
    else:
        raise AssertionError('argparse --help did not exit')
"""
            help_smoke = subprocess.run(
                [sys.executable, "-c", help_script, str(site)],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(help_smoke.returncode, 0, help_smoke.stderr)
            self.assertIn("elt-taskgen-evidence-maintenance", help_smoke.stdout)
            self.assertIn("elt-taskgen-parity-sample", help_smoke.stdout)
            self.assertIn("elt-taskgen-schemapile-index", help_smoke.stdout)

            repeated_output = Path(directory) / "repeated-dist"
            repeated = subprocess.run(
                [
                    uv,
                    "build",
                    "--force-pep517",
                    "--no-build-logs",
                    "--out-dir",
                    str(repeated_output),
                    str(source),
                ],
                cwd=source,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)

            def digest(path: Path) -> str:
                return hashlib.sha256(path.read_bytes()).hexdigest()

            self.assertEqual(
                digest(wheel),
                digest(next(repeated_output.glob("*.whl"))),
            )
            self.assertEqual(
                digest(sdist),
                digest(next(repeated_output.glob("*.tar.gz"))),
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import re
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import elt_taskgen.runtime_matrix as runtime_matrix_module
from elt_taskgen.destinations import (
    DBT_ADAPTER_CONTRACTS,
    DBT_CORE_VERSION,
    Destination,
    destination_contract,
)
from elt_taskgen.runtime_matrix import (
    CERTIFICATION_ATTESTATION_SCHEMA_VERSION,
    CERTIFICATION_MATRIX_VERSION,
    CERTIFICATION_PENDING_SCHEMA_VERSION,
    CERTIFICATION_STAGE_EVIDENCE_SCHEMA_VERSION,
    certification_matrix,
    runner_image_refs,
    runner_images_digest,
    validate_certification_matrix,
    validate_recorded_certification_matrix,
)


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_IMAGES = ROOT / "runtime-images"
TERRAFORM_DOCKERFILE = RUNTIME_IMAGES / "terraform" / "Dockerfile"
DBT_DOCKERFILES = {
    "snowflake": RUNTIME_IMAGES / "dbt-snowflake" / "Dockerfile",
    "databricks": RUNTIME_IMAGES / "dbt-databricks" / "Dockerfile",
    "redshift": RUNTIME_IMAGES / "dbt-redshift" / "Dockerfile",
}
DBT_PROJECT = RUNTIME_IMAGES / "dbt" / "pyproject.toml"
DBT_LOCK = RUNTIME_IMAGES / "dbt" / "uv.lock"
IMAGE_WITH_DIGEST = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
TERRAFORM_BASE = (
    "hashicorp/terraform:1.15.8@sha256:"
    "7ae513256f7ce67879e218ae8593d6fbe216ec9e123abe6c94e4e10704857963"
)
UV_BASE = (
    "ghcr.io/astral-sh/uv:0.11.16@sha256:"
    "440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d"
)
PYTHON_BASE = (
    "python:3.12.14-slim-bookworm@sha256:"
    "0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579"
)


def _dockerfile(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _locked_version(lock: str, package: str) -> str:
    match = re.search(
        rf'^name = "{re.escape(package)}"\nversion = "([^"]+)"$',
        lock,
        flags=re.MULTILINE,
    )
    assert match is not None, f"{package} is absent from uv.lock"
    return match.group(1)


class RuntimeImageTests(unittest.TestCase):
    def test_runner_digest_uses_reviewed_manifest_not_local_junk(self) -> None:
        manifest = json.loads(
            (RUNTIME_IMAGES / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schema_version"], "runner-images-manifest-v2")
        self.assertEqual(manifest["files"], sorted(set(manifest["files"])))
        required = {
            "dbt-databricks/Dockerfile",
            "dbt-redshift/Dockerfile",
            "dbt-snowflake/Dockerfile",
            "dbt/uv.lock",
            "terraform/Dockerfile",
            "terraform/provider.tf",
            "terraform/terraform.rc",
        }
        self.assertTrue(required <= set(manifest["files"]))
        self.assertFalse(any(".venv" in path for path in manifest["files"]))
        self.assertEqual(
            tuple(manifest["images"]),
            (
                "dbt:databricks",
                "dbt:redshift",
                "dbt:snowflake",
                "terraform",
            ),
        )
        self.assertTrue(
            all(
                re.fullmatch(r"[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}", image)
                for image in manifest["images"].values()
            )
        )
        self.assertEqual(runner_image_refs(), manifest["images"])

        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "runtime-images"
            shutil.copytree(
                RUNTIME_IMAGES,
                copied,
                ignore=shutil.ignore_patterns(".venv"),
            )
            initial = runner_images_digest(copied)
            (copied / "ignored-local-build.log").write_text(
                "not a runner input\n", encoding="utf-8"
            )
            self.assertEqual(runner_images_digest(copied), initial)
            listed = copied / manifest["files"][0]
            listed.write_bytes(listed.read_bytes() + b"\n# reviewed change\n")
            self.assertNotEqual(runner_images_digest(copied), initial)

    def test_runner_digest_fails_closed_on_manifest_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "runner-images-manifest-v2",
                        "images": {
                            "dbt:databricks": "example/dbt-databricks@sha256:" + "1" * 64,
                            "dbt:redshift": "example/dbt-redshift@sha256:" + "2" * 64,
                            "dbt:snowflake": "example/dbt-snowflake@sha256:" + "3" * 64,
                            "terraform": "example/terraform@sha256:" + "4" * 64,
                        },
                        "files": ["../outside"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                runner_images_digest(root)

    def test_runner_digest_rejects_duplicate_manifest_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(
                """{
  "schema_version": "runner-images-manifest-v2",
  "files": ["one"],
  "files": ["two"],
  "images": {}
}\n""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "missing or invalid"):
                runner_images_digest(root)

    def test_certification_matrix_pins_the_executed_images_and_dbt_versions(
        self,
    ) -> None:
        images = runner_image_refs()
        for destination in Destination:
            with self.subTest(destination=destination.value):
                matrix = certification_matrix(destination)
                adapter, version = DBT_ADAPTER_CONTRACTS[destination]
                self.assertEqual(
                    matrix["runner_image:terraform"], images["terraform"]
                )
                self.assertEqual(
                    matrix["runner_image:dbt"], images[f"dbt:{destination.value}"]
                )
                self.assertEqual(matrix["dbt_core_version"], DBT_CORE_VERSION)
                self.assertEqual(matrix["dbt_adapter"], adapter)
                self.assertEqual(matrix["dbt_adapter_version"], version)
                contract = destination_contract(destination)
                self.assertEqual(
                    matrix["warehouse:logical_namespace_field"],
                    contract.logical_namespace_field,
                )
                self.assertEqual(
                    matrix["warehouse:physical_container_field"],
                    contract.physical_container_field or "",
                )
                self.assertEqual(
                    matrix["warehouse:fixed_schema"],
                    contract.fixed_schema or "",
                )

    def test_certification_matrix_schema_is_closed_and_content_addressed(
        self,
    ) -> None:
        destination = Destination.SNOWFLAKE
        matrix = certification_matrix(destination)
        self.assertEqual(
            validate_certification_matrix(matrix, destination), matrix
        )
        mutations = {
            "missing dbt runner": lambda value: value.pop("runner_image:dbt"),
            "extra field": lambda value: value.__setitem__("extension", "value"),
            "old recipe": lambda value: value.__setitem__("matrix_version", "8"),
            "tagged terraform runner": lambda value: value.__setitem__(
                "runner_image:terraform", "example/terraform:latest"
            ),
            "missing dbt core": lambda value: value.pop("dbt_core_version"),
            "wrong dbt adapter": lambda value: value.__setitem__(
                "dbt_adapter", "redshift"
            ),
            "wrong logical namespace field": lambda value: value.__setitem__(
                "warehouse:logical_namespace_field", "schema"
            ),
            "wrong physical container field": lambda value: value.__setitem__(
                "warehouse:physical_container_field", "database"
            ),
            "wrong fixed schema": lambda value: value.__setitem__(
                "warehouse:fixed_schema", "OTHER_SCHEMA"
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = dict(matrix)
                mutate(changed)
                with self.assertRaises(ValueError):
                    validate_certification_matrix(changed, destination)
        self.assertEqual(matrix["matrix_version"], CERTIFICATION_MATRIX_VERSION)
        self.assertEqual(
            matrix["certification_stage_evidence_schema_version"],
            CERTIFICATION_STAGE_EVIDENCE_SCHEMA_VERSION,
        )
        self.assertEqual(
            matrix["certification_attestation_schema_version"],
            CERTIFICATION_ATTESTATION_SCHEMA_VERSION,
        )
        self.assertEqual(
            matrix["certification_pending_schema_version"],
            CERTIFICATION_PENDING_SCHEMA_VERSION,
        )

    def test_matrix_validation_binds_the_callers_expected_destination(self) -> None:
        snowflake = certification_matrix(Destination.SNOWFLAKE)
        with self.assertRaisesRegex(ValueError, "destination"):
            validate_certification_matrix(snowflake, Destination.DATABRICKS)
        with self.assertRaisesRegex(ValueError, "destination"):
            validate_recorded_certification_matrix(
                snowflake, Destination.DATABRICKS
            )

    def test_recorded_v10_matrix_remains_verifiable_but_is_not_writable(self) -> None:
        for destination in Destination:
            with self.subTest(destination=destination.value):
                matrix = certification_matrix(destination)
                matrix["matrix_version"] = "10"
                matrix["certification_stage_evidence_schema_version"] = "1.1"
                matrix["certification_attestation_schema_version"] = "1.2"
                # v10 matrices were recorded under canonical fingerprint version 1.
                matrix["canonical_fingerprint_version"] = "1"
                self.assertEqual(
                    validate_recorded_certification_matrix(matrix, destination),
                    matrix,
                )
                with self.assertRaisesRegex(ValueError, f"v{CERTIFICATION_MATRIX_VERSION}"):
                    validate_certification_matrix(matrix, destination)

                for field in (
                    "warehouse:logical_namespace_field",
                    "warehouse:physical_container_field",
                    "warehouse:fixed_schema",
                ):
                    changed = dict(matrix)
                    changed[field] = "unexpected"
                    with self.assertRaisesRegex(ValueError, field):
                        validate_recorded_certification_matrix(
                            changed, destination
                        )

        unsupported = certification_matrix(Destination.SNOWFLAKE)
        unsupported["matrix_version"] = "9"
        with self.assertRaisesRegex(ValueError, "unsupported closed recipe"):
            validate_recorded_certification_matrix(
                unsupported, Destination.SNOWFLAKE
            )

    def test_recorded_v11_matrix_keeps_fingerprint_version_1(self) -> None:
        """v11 matrices recorded canonical fingerprint version 1. They stay
        verifiable as v11, and neither version is relabelled as the other."""
        for destination in Destination:
            with self.subTest(destination=destination.value):
                current = certification_matrix(destination)
                self.assertEqual(current["canonical_fingerprint_version"], "2")
                recorded = dict(current)
                recorded["matrix_version"] = "11"
                recorded["canonical_fingerprint_version"] = "1"
                self.assertEqual(
                    validate_recorded_certification_matrix(recorded, destination),
                    recorded,
                )
                with self.assertRaisesRegex(ValueError, f"v{CERTIFICATION_MATRIX_VERSION}"):
                    validate_certification_matrix(recorded, destination)
                relabelled = dict(recorded)
                relabelled["canonical_fingerprint_version"] = "2"
                with self.assertRaisesRegex(ValueError, "canonical_fingerprint_version"):
                    validate_recorded_certification_matrix(relabelled, destination)
                stale = dict(current)
                stale["canonical_fingerprint_version"] = "1"
                with self.assertRaisesRegex(ValueError, "canonical_fingerprint_version"):
                    validate_certification_matrix(stale, destination)

    def test_recorded_v10_validation_does_not_follow_current_contract_drift(
        self,
    ) -> None:
        destination = Destination.DATABRICKS
        matrix = certification_matrix(destination)
        matrix["matrix_version"] = "10"
        matrix["certification_stage_evidence_schema_version"] = "1.1"
        matrix["certification_attestation_schema_version"] = "1.2"
        matrix["canonical_fingerprint_version"] = "1"

        with (
            patch.object(
                runtime_matrix_module,
                "CANONICAL_FINGERPRINT_VERSION",
                "future-fingerprint",
            ),
            patch.object(
                runtime_matrix_module,
                "PARITY_CONTRACT_VERSION",
                "future-parity",
            ),
            patch.object(
                runtime_matrix_module,
                "PARITY_OBSERVATION_SCHEMA_VERSION",
                "future-observation",
            ),
            patch.object(
                runtime_matrix_module,
                "PARITY_REGISTRY_DIGEST",
                "f" * 64,
            ),
            patch.object(
                runtime_matrix_module,
                "PARITY_SCOPE_POLICY_VERSION",
                "future-scope",
            ),
            patch.object(runtime_matrix_module, "DBT_CORE_VERSION", "99.0.0"),
            patch.dict(
                runtime_matrix_module.DBT_ADAPTER_CONTRACTS,
                {destination: ("future-adapter", "99.0.0")},
            ),
        ):
            self.assertEqual(
                validate_recorded_certification_matrix(matrix, destination),
                matrix,
            )

    def test_every_runtime_base_image_is_content_addressed(self) -> None:
        expected = {
            TERRAFORM_DOCKERFILE: [TERRAFORM_BASE, TERRAFORM_BASE],
            DBT_DOCKERFILES["snowflake"]: [UV_BASE, PYTHON_BASE, PYTHON_BASE],
            DBT_DOCKERFILES["databricks"]: [UV_BASE, PYTHON_BASE, PYTHON_BASE],
            DBT_DOCKERFILES["redshift"]: [UV_BASE, PYTHON_BASE, PYTHON_BASE],
        }
        for path, expected_images in expected.items():
            with self.subTest(path=path):
                text = _dockerfile(path)
                images = re.findall(r"^FROM\s+(\S+)", text, flags=re.MULTILINE)
                self.assertEqual(images, expected_images)
                self.assertTrue(
                    all(IMAGE_WITH_DIGEST.fullmatch(image) for image in images)
                )
                self.assertNotIn(":latest", text)

    def test_terraform_runner_vendors_generated_provider_contract(self) -> None:
        dockerfile = _dockerfile(TERRAFORM_DOCKERFILE)
        provider = (RUNTIME_IMAGES / "terraform/provider.tf").read_text(
            encoding="utf-8"
        )
        terraform_rc = (RUNTIME_IMAGES / "terraform/terraform.rc").read_text(
            encoding="utf-8"
        )
        exporter = (ROOT / "src/elt_taskgen/export/eltbench.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('version = "0.6.5"', exporter)
        self.assertIn('version = "0.6.5"', provider)
        self.assertIn("ed520c606345c89886c55baf4b10f32e470265f6eaa67d809df37f1af860fec5", dockerfile)
        self.assertIn("1da387051f1006bdab2286b59a1236790ff126e81b15146b4cdc4b6a61f99fd1", dockerfile)
        self.assertIn("sha256sum -c -", dockerfile)
        self.assertIn("TF_CLI_CONFIG_FILE=/etc/terraform.rc", dockerfile)
        self.assertIn('include = ["registry.terraform.io/airbytehq/airbyte"]', terraform_rc)
        self.assertNotIn("direct {", terraform_rc)
        self.assertIn("ENTRYPOINT []", dockerfile)
        self.assertIn('CMD ["terraform", "version"]', dockerfile)

    def test_dbt_direct_and_transitive_versions_are_locked(self) -> None:
        project = tomllib.loads(DBT_PROJECT.read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["requires-python"], "==3.12.*")
        self.assertEqual(
            project["project"]["dependencies"], ["dbt-core==1.12.0"]
        )
        self.assertEqual(
            project["project"]["optional-dependencies"],
            {
                "snowflake": ["dbt-snowflake==1.12.0"],
                "databricks": ["dbt-databricks==1.12.4"],
                "redshift": ["dbt-redshift==1.11.1"],
            },
        )

        lock = DBT_LOCK.read_text(encoding="utf-8")
        self.assertEqual(_locked_version(lock, "dbt-core"), "1.12.0")
        self.assertEqual(_locked_version(lock, "dbt-snowflake"), "1.12.0")
        self.assertEqual(_locked_version(lock, "dbt-databricks"), "1.12.4")
        self.assertEqual(_locked_version(lock, "dbt-redshift"), "1.11.1")
        self.assertEqual(_locked_version(lock, "databricks-sdk"), "0.117.0")
        self.assertEqual(
            _locked_version(lock, "databricks-sql-connector"), "4.4.0"
        )
        self.assertEqual(_locked_version(lock, "redshift-connector"), "2.1.16")
        self.assertIn('hash = "sha256:', lock)

    def test_each_dbt_image_installs_only_its_frozen_adapter_extra(self) -> None:
        for adapter, path in DBT_DOCKERFILES.items():
            with self.subTest(adapter=adapter):
                dockerfile = _dockerfile(path)
                self.assertIn(
                    "COPY runtime-images/dbt/pyproject.toml "
                    "runtime-images/dbt/uv.lock ./",
                    dockerfile,
                )
                self.assertIn(
                    "uv sync --frozen --no-dev --no-install-project "
                    f"--extra {adapter}",
                    dockerfile,
                )
                self.assertNotIn("pip install", dockerfile)
                self.assertIn("ENTRYPOINT []", dockerfile)
                self.assertIn('CMD ["dbt", "--version"]', dockerfile)
                self.assertIn("DBT_SEND_ANONYMOUS_USAGE_STATS=false", dockerfile)

    def test_stage2_preflight_pins_match_the_certified_dbt_images(self) -> None:
        """destinations.py's dbt pins can never drift from the frozen images.

        The Stage 2 preflight compares the LIVE `dbt --version` output against
        DBT_CORE_VERSION/DBT_ADAPTER_CONTRACTS, so those constants must equal
        the versions actually locked into the runner images.
        """
        from elt_taskgen.destinations import (
            DBT_ADAPTER_CONTRACTS,
            DBT_CORE_VERSION,
            Destination,
        )

        project = tomllib.loads(DBT_PROJECT.read_text(encoding="utf-8"))
        lock = DBT_LOCK.read_text(encoding="utf-8")
        self.assertEqual(
            project["project"]["dependencies"],
            [f"dbt-core=={DBT_CORE_VERSION}"],
        )
        self.assertEqual(_locked_version(lock, "dbt-core"), DBT_CORE_VERSION)
        self.assertEqual(set(DBT_ADAPTER_CONTRACTS), set(Destination))
        for destination, (adapter, version) in DBT_ADAPTER_CONTRACTS.items():
            with self.subTest(destination=destination.value):
                self.assertEqual(
                    project["project"]["optional-dependencies"][adapter],
                    [f"dbt-{adapter}=={version}"],
                )
                self.assertEqual(
                    _locked_version(lock, f"dbt-{adapter}"), version
                )

    def test_docker_context_allowlists_only_runtime_inputs(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertEqual(dockerignore.splitlines()[0], "**")
        self.assertIn("!runtime-images/dbt/uv.lock", dockerignore)
        self.assertIn("!runtime-images/terraform/provider.tf", dockerignore)
        self.assertNotIn("!runs/", dockerignore)
        self.assertNotIn("!.venv/", dockerignore)

    def test_runtime_image_runbook_requires_pushed_platform_digests(self) -> None:
        runbook = (RUNTIME_IMAGES / "README.md").read_text(encoding="utf-8")
        self.assertEqual(runbook.count("docker buildx build"), 4)
        self.assertEqual(runbook.count("docker buildx imagetools inspect"), 4)
        self.assertIn('ELT_RUNNER_PLATFORM="linux/amd64"', runbook)
        self.assertIn("@sha256:<manifest-digest>", runbook)
        self.assertIn("runtime run-stage1 --runner-image", runbook)
        self.assertIn("runtime run-stage2 --runner-image", runbook)


if __name__ == "__main__":
    unittest.main()

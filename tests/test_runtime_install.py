"""Local tests for credential-safe, fresh task installation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from elt_taskgen.export.eltbench import runtime_documentation_filenames
from elt_taskgen.runtime.install import install_task


class RuntimeInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def make_bundle(
        self,
        *,
        rest: bool = False,
        name: str | None = None,
        destination: str = "snowflake",
    ) -> Path:
        source = self.root / (
            name or ("rest-task" if rest else "postgres-task")
        )
        (source / "schemas").mkdir(parents=True)
        (source / "documentation").mkdir()
        (source / "elt").mkdir()
        (source / "schemas" / "users.csv").write_text(
            "column_name,column_description\nid,Primary key\n", encoding="utf-8"
        )
        (source / "data_model.yaml").write_text("models: []\n", encoding="utf-8")
        (source / "check_job_status.py").write_text("# helper\n", encoding="utf-8")
        (source / "elt" / "main.tf").write_text(
            'terraform {\n'
            '  required_providers {\n'
            '    airbyte = {\n'
            '      source  = "airbytehq/airbyte"\n'
            '      version = "0.6.5"\n'
            '    }\n'
            '  }\n'
            '}\n',
            encoding="utf-8",
        )
        for name in runtime_documentation_filenames(destination):
            (source / "documentation" / name).write_text(
                f"# {name}\n", encoding="utf-8"
            )

        config: dict[str, object] = {
            "snowflake": {
                "config": {
                    "account": "",
                    "database": "task_database",
                    "password": "",
                    "role": "",
                    "schema": "AIRBYTE_SCHEMA",
                    "username": "",
                    "warehouse": "",
                }
            },
            "postgres": {
                "config": {
                    "database": "task_database",
                    "host": "elt-postgres",
                    "password": "testelt",
                    "port": 5432,
                    "schema": "public",
                    "sync_mode": "full_refresh_append",
                    "tables": ["users"],
                    "user": "postgres",
                }
            },
            "Airbyte": {
                "config": {
                    "namespace_definition": "destination",
                    "password": "",
                    "server_url": "http://airbyte-abctl-control-plane:80/api/public/v1/",
                    "snowflake_definition_id": "424892c4-daac-4491-b35d-c6688ba547ba",
                    "postgres_definition_id": "decd338e-5647-4c0b-adf4-da0e75f5a750",
                    "username": "",
                    "workspace_id": "",
                }
            },
        }
        if rest:
            config["custom_api"] = {
                "config": {
                    "configuration": {},
                    "sync_mode": "full_refresh_append",
                    "tables": ["users"],
                }
            }
            config["Airbyte"]["config"]["custom_api_definition_id"] = ""  # type: ignore[index]
        credential_filename = "snowflake_credential.json"
        credential_template: dict[str, object] = {
            "account": "",
            "user": "",
            "password": "",
        }
        if destination == "databricks":
            config.pop("snowflake")
            config["databricks"] = {
                "config": {
                    "database": "",
                    "hostname": "",
                    "client_id": "",
                    "secret": "",
                    "http_path": "",
                    "schema": "task_database",
                }
            }
            airbyte = config["Airbyte"]["config"]  # type: ignore[index]
            airbyte.pop("snowflake_definition_id")  # type: ignore[union-attr]
            airbyte["databricks_definition_id"] = (  # type: ignore[index]
                "072d5540-f236-4294-ba7c-ade8fd918496"
            )
            credential_filename = "databricks_credential.json"
            credential_template = {
                "client_id": "",
                "hostname": "",
                "http_path": "",
                "secret": "",
            }
        elif destination == "redshift":
            config.pop("snowflake")
            config["redshift"] = {
                "config": {
                    "access_key_id": "",
                    "database": "",
                    "host": "",
                    "password": "",
                    "port": 5439,
                    "s3_bucket_name": "",
                    "s3_bucket_region": "",
                    "schema": "task_database",
                    "secret_access_key": "",
                    "username": "",
                }
            }
            airbyte = config["Airbyte"]["config"]  # type: ignore[index]
            airbyte.pop("snowflake_definition_id")  # type: ignore[union-attr]
            airbyte["redshift_definition_id"] = (  # type: ignore[index]
                "f7a7d195-377f-cf5b-70a5-be6b819019dc"
            )
            credential_filename = "redshift_credential.json"
            credential_template = {
                "database": "",
                "host": "",
                "password": "",
                "port": 5439,
                "username": "",
            }
        (source / "config.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
        )
        (source / credential_filename).write_text(
            json.dumps(credential_template, indent=2) + "\n",
            encoding="utf-8",
        )
        return source

    @staticmethod
    def airbyte_credentials(**updates: object) -> dict[str, object]:
        values: dict[str, object] = {
            "username": "airbyte-user",
            "password": "airbyte-secret",
            "workspace_id": "workspace-123",
            "server_url": "http://host.docker.internal:8000/api/public/v1/",
        }
        values.update(updates)
        return values

    @staticmethod
    def snowflake_credentials(**updates: object) -> dict[str, object]:
        values: dict[str, object] = {
            "account": "organization-account",
            "user": "snowflake-user",
            "password": "snowflake-secret",
            "role": "attempt-role",
            "warehouse": "attempt-warehouse",
        }
        values.update(updates)
        return values

    @staticmethod
    def databricks_credentials(**updates: object) -> dict[str, object]:
        values: dict[str, object] = {
            "hostname": "workspace.cloud.databricks.com",
            "http_path": "/sql/1.0/warehouses/abc123",
            "client_id": "solver-client",
            "secret": "solver-secret",
            "database": "benchmark_catalog",
        }
        values.update(updates)
        return values

    @staticmethod
    def redshift_credentials(**updates: object) -> dict[str, object]:
        values: dict[str, object] = {
            "host": "cluster.example.us-west-2.redshift.amazonaws.com",
            "port": 5439,
            "database": "benchmark_database",
            "username": "solver_user",
            "password": "solver-secret",
            "s3_bucket_name": "benchmark-staging-bucket",
            "s3_bucket_region": "us-west-2",
            "s3_bucket_path": "elt-bench/task_database",
            "access_key_id": "AKIAEXAMPLE",
            "secret_access_key": "aws-secret",
        }
        values.update(updates)
        return values

    def test_installs_databricks_oauth_destination(self) -> None:
        source = self.make_bundle(destination="databricks")
        destination = self.root / "databricks-attempt"
        install_task(
            source,
            destination,
            airbyte_credentials=self.airbyte_credentials(),
            destination_credentials=self.databricks_credentials(),
            destination="databricks",
        )

        config = yaml.safe_load((destination / "config.yaml").read_text())
        installed = config["databricks"]["config"]
        self.assertEqual(installed["database"], "benchmark_catalog")
        self.assertEqual(installed["schema"], "task_database")
        self.assertEqual(installed["client_id"], "solver-client")
        self.assertEqual(installed["secret"], "solver-secret")
        self.assertNotIn("authentication", installed)
        self.assertEqual(
            json.loads((destination / "databricks_credential.json").read_text()),
            {
                "hostname": "workspace.cloud.databricks.com",
                "http_path": "/sql/1.0/warehouses/abc123",
                "client_id": "solver-client",
                "secret": "solver-secret",
            },
        )
        self.assertFalse((destination / "snowflake_credential.json").exists())

    def test_rejects_databricks_pat_not_representable_in_original_shape(self) -> None:
        source = self.make_bundle(
            name="databricks-pat",
            destination="databricks",
        )
        destination = self.root / "databricks-pat-attempt"
        with self.assertRaisesRegex(ValueError, "requires OAuth"):
            install_task(
                source,
                destination,
                airbyte_credentials=self.airbyte_credentials(),
                destination_credentials={
                    "hostname": "workspace.cloud.databricks.com",
                    "http_path": "/sql/1.0/warehouses/abc123",
                    "access_token": "solver-token",
                    "database": "benchmark_catalog",
                },
            )
        self.assertFalse(destination.exists())

    def test_installs_redshift_connection_and_s3_staging(self) -> None:
        source = self.make_bundle(destination="redshift")
        destination = self.root / "redshift-attempt"
        install_task(
            source,
            destination,
            airbyte_credentials=self.airbyte_credentials(),
            destination_credentials=self.redshift_credentials(port="5439"),
        )

        config = yaml.safe_load((destination / "config.yaml").read_text())
        installed = config["redshift"]["config"]
        self.assertEqual(installed["database"], "benchmark_database")
        self.assertEqual(installed["schema"], "task_database")
        self.assertIsInstance(installed["port"], int)
        self.assertEqual(installed["access_key_id"], "AKIAEXAMPLE")
        self.assertEqual(installed["s3_bucket_name"], "benchmark-staging-bucket")
        self.assertEqual(installed["s3_bucket_region"], "us-west-2")
        self.assertEqual(installed["secret_access_key"], "aws-secret")
        self.assertNotIn("uploading_method", installed)
        self.assertEqual(
            json.loads((destination / "redshift_credential.json").read_text()),
            {
                "host": "cluster.example.us-west-2.redshift.amazonaws.com",
                "port": 5439,
                "database": "benchmark_database",
                "username": "solver_user",
                "password": "solver-secret",
            },
        )

    def test_install_accepts_omitted_deterministic_redshift_staging_path(self) -> None:
        credentials = self.redshift_credentials()
        credentials.pop("s3_bucket_path")
        destination = self.root / "redshift-default-path-attempt"
        install_task(
            self.make_bundle(name="redshift-default-path", destination="redshift"),
            destination,
            airbyte_credentials=self.airbyte_credentials(),
            destination_credentials=credentials,
        )
        self.assertTrue(destination.is_dir())

    def test_install_rejects_invalid_or_non_deterministic_redshift_paths(self) -> None:
        for path in ("", "/attempt-1", "attempt-1/", "../attempt-1", "other/path"):
            with self.subTest(path=path):
                credentials = self.redshift_credentials()
                credentials["s3_bucket_path"] = path
                with self.assertRaisesRegex(
                    ValueError, "attempt-scoped prefix|deterministic ELT-Bench"
                ):
                    install_task(
                        self.make_bundle(
                            name=f"redshift-unscoped-{str(path).replace('/', '_')}",
                            destination="redshift",
                        ),
                        self.root / f"redshift-unscoped-attempt-{len(str(path))}",
                        airbyte_credentials=self.airbyte_credentials(),
                        destination_credentials=credentials,
                    )

    def test_install_rejects_a_provisioned_namespace_mismatch(self) -> None:
        for destination, credentials in (
            (
                "databricks",
                self.databricks_credentials(schema="wrong_schema"),
            ),
            (
                "redshift",
                self.redshift_credentials(schema="wrong_schema"),
            ),
        ):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(ValueError, "does not match"):
                    install_task(
                        self.make_bundle(
                            name=f"{destination}-namespace-mismatch",
                            destination=destination,
                        ),
                        self.root / f"{destination}-namespace-mismatch-attempt",
                        airbyte_credentials=self.airbyte_credentials(),
                        destination_credentials=credentials,
                    )

    def test_install_rejects_destination_mismatch_and_credential_alias_overlap(self) -> None:
        source = self.make_bundle(destination="databricks")
        with self.assertRaisesRegex(ValueError, "does not match"):
            install_task(
                source,
                self.root / "wrong-destination",
                airbyte_credentials=self.airbyte_credentials(),
                destination_credentials=self.databricks_credentials(),
                destination="redshift",
            )
        with self.assertRaisesRegex(ValueError, "not both"):
            install_task(
                self.make_bundle(name="both-credentials"),
                self.root / "both-credentials-attempt",
                airbyte_credentials=self.airbyte_credentials(),
                destination_credentials=self.snowflake_credentials(),
                snowflake_credentials=self.snowflake_credentials(),
            )

    def test_installs_combined_task_and_injects_upstream_fields(self) -> None:
        source = self.make_bundle()
        # freeze_release strips write bits from every shipped file.
        for path in source.rglob("*"):
            if path.is_file():
                path.chmod(path.stat().st_mode & 0o555)
        destination = self.root / "attempt" / "task"

        result = install_task(
            source,
            destination,
            airbyte_credentials=self.airbyte_credentials(),
            snowflake_credentials=self.snowflake_credentials(),
            airbyte_server_url="http://airbyte-runtime:8000/api/public/v1/",
        )

        self.assertEqual(result, destination)
        config = yaml.safe_load((destination / "config.yaml").read_text())
        self.assertEqual(
            {
                field: config["Airbyte"]["config"][field]
                for field in ("password", "username", "workspace_id")
            },
            {
                "password": "airbyte-secret",
                "username": "airbyte-user",
                "workspace_id": "workspace-123",
            },
        )
        self.assertEqual(
            config["Airbyte"]["config"]["server_url"],
            "http://airbyte-runtime:8000/api/public/v1/",
        )
        self.assertEqual(
            config["snowflake"]["config"]["account"],
            "organization-account",
        )
        self.assertEqual(
            {
                key: config["snowflake"]["config"][key]
                for key in ("password", "role", "username", "warehouse")
            },
            {
                "password": "snowflake-secret",
                "role": "attempt-role",
                "username": "snowflake-user",
                "warehouse": "attempt-warehouse",
            },
        )
        self.assertEqual(
            json.loads((destination / "snowflake_credential.json").read_text()),
            {
                "account": "organization-account",
                "user": "snowflake-user",
                "password": "snowflake-secret",
            },
        )
        self.assertTrue((destination / "elt" / "main.tf").is_file())
        self.assertTrue((destination / "elt" / "main.tf").stat().st_mode & 0o200)
        self.assertFalse(
            (destination / "elt" / "connector_config.auto.tfvars.json").exists()
        )
        self.assertEqual(
            (destination / "snowflake_credential.json").stat().st_mode & 0o777,
            0o600,
        )
        self.assertEqual(
            (destination / "config.yaml").stat().st_mode & 0o777,
            0o600,
        )
        self.assertEqual(destination.stat().st_mode & 0o077, 0)
        for path in (destination, *destination.rglob("*")):
            with self.subTest(private_path=path.relative_to(destination)):
                self.assertEqual(path.stat().st_mode & 0o077, 0)

        # Installation never mutates the frozen public source.
        source_config = yaml.safe_load((source / "config.yaml").read_text())
        self.assertEqual(source_config["snowflake"]["config"]["account"], "")
        self.assertEqual(source_config["Airbyte"]["config"]["password"], "")
        self.assertFalse(
            (source / "elt" / "connector_config.auto.tfvars.json").exists()
        )
        self.assertEqual(
            json.loads((source / "snowflake_credential.json").read_text()),
            {"account": "", "user": "", "password": ""},
        )

    def test_rest_task_maps_upstream_api_definition_id(self) -> None:
        source = self.make_bundle(rest=True)
        destination = self.root / "rest-attempt"
        airbyte = self.airbyte_credentials(api_definition_id="custom-source-id")

        install_task(
            source,
            destination,
            airbyte_credentials=airbyte,
            snowflake_credentials=self.snowflake_credentials(),
        )

        config = yaml.safe_load((destination / "config.yaml").read_text())
        self.assertEqual(
            config["Airbyte"]["config"]["custom_api_definition_id"],
            "custom-source-id",
        )

    def test_install_accepts_original_configs_before_write_config_injection(
        self,
    ) -> None:
        source = self.make_bundle(rest=True, name="original-pre-install")
        config_path = source / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        airbyte = config["Airbyte"]["config"]
        for field in ("password", "username", "workspace_id"):
            airbyte.pop(field)
        airbyte["server_url"] = ""
        airbyte.pop("custom_api_definition_id")
        config_path.write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
        )

        destination = self.root / "original-pre-install-attempt"
        install_task(
            source,
            destination,
            airbyte_credentials=self.airbyte_credentials(
                api_definition_id="custom-source-id"
            ),
            snowflake_credentials=self.snowflake_credentials(),
        )

        installed = yaml.safe_load((destination / "config.yaml").read_text())
        self.assertEqual(installed["Airbyte"]["config"]["username"], "airbyte-user")
        self.assertEqual(installed["Airbyte"]["config"]["password"], "airbyte-secret")
        self.assertEqual(installed["Airbyte"]["config"]["workspace_id"], "workspace-123")
        self.assertEqual(
            installed["Airbyte"]["config"]["custom_api_definition_id"],
            "custom-source-id",
        )

    def test_install_accepts_current_abctl_client_credentials(self) -> None:
        source = self.make_bundle()
        destination = self.root / "client-credential-attempt"
        install_task(
            source,
            destination,
            airbyte_credentials={
                "client_id": "client-id",
                "client_secret": "client-secret",
                "workspace_id": "workspace-123",
                "server_url": "http://host.docker.internal:8000/api/public/v1/",
            },
            snowflake_credentials=self.snowflake_credentials(),
        )
        airbyte = yaml.safe_load((destination / "config.yaml").read_text())["Airbyte"][
            "config"
        ]
        self.assertEqual(airbyte["client_id"], "client-id")
        self.assertEqual(airbyte["client_secret"], "client-secret")
        self.assertEqual(airbyte["username"], "")
        self.assertEqual(airbyte["password"], "")

    def test_rest_task_requires_custom_api_definition_id(self) -> None:
        source = self.make_bundle(rest=True)
        with self.assertRaisesRegex(ValueError, "custom_api_definition_id"):
            install_task(
                source,
                self.root / "rest-attempt",
                airbyte_credentials=self.airbyte_credentials(),
                snowflake_credentials=self.snowflake_credentials(),
            )

    def test_direct_install_refuses_non_contract_top_level_artifacts(self) -> None:
        for index, relative in enumerate(
            ("sources/users.csv", "warehouse/primary.duckdb")
        ):
            with self.subTest(relative=relative):
                source = self.make_bundle(name=f"forbidden-{index}")
                artifact = source / relative
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text("private", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "combined runtime task"):
                    install_task(
                        source,
                        self.root / f"attempt-{index}",
                        airbyte_credentials=self.airbyte_credentials(),
                        snowflake_credentials=self.snowflake_credentials(),
                    )

    def test_non_rest_task_ignores_blank_optional_upstream_api_id(self) -> None:
        source = self.make_bundle()
        destination = self.root / "non-rest-attempt"
        airbyte = self.airbyte_credentials(api_definition_id="")

        install_task(
            source,
            destination,
            airbyte_credentials=airbyte,
            snowflake_credentials=self.snowflake_credentials(),
        )

        config = yaml.safe_load((destination / "config.yaml").read_text())
        self.assertNotIn(
            "custom_api_definition_id", config["Airbyte"]["config"]
        )

    def test_missing_required_credentials_are_refused_before_copy(self) -> None:
        source = self.make_bundle()
        cases = (
            ("Airbyte", {"password": ""}, {}),
            ("Airbyte", {"workspace_id": None}, {}),
            ("Snowflake", {}, {"account": " "}),
            ("Snowflake", {}, {"user": 7}),
        )
        for index, (kind, airbyte_updates, snowflake_updates) in enumerate(cases):
            with self.subTest(kind=kind, index=index):
                destination = self.root / f"missing-{index}"
                with self.assertRaisesRegex(ValueError, kind):
                    install_task(
                        source,
                        destination,
                        airbyte_credentials=self.airbyte_credentials(
                            **airbyte_updates
                        ),
                        snowflake_credentials=self.snowflake_credentials(
                            **snowflake_updates
                        ),
                    )
                self.assertFalse(destination.exists())

    def test_refuses_populated_source_credentials_without_echoing_them(self) -> None:
        mutators = (
            self._populate_source_airbyte,
            self._populate_source_snowflake_account,
            self._populate_source_credential_file,
        )
        for index, mutate in enumerate(mutators):
            with self.subTest(index=index):
                source = self.make_bundle(name=f"credential-source-{index}")
                secret = f"source-secret-{index}"
                mutate(source, secret)
                destination = self.root / f"rejected-{index}"
                with self.assertRaises(ValueError) as raised:
                    install_task(
                        source,
                        destination,
                        airbyte_credentials=self.airbyte_credentials(),
                        snowflake_credentials=self.snowflake_credentials(),
                    )
                self.assertNotIn(secret, str(raised.exception))
                self.assertFalse(destination.exists())

    @staticmethod
    def _populate_source_airbyte(source: Path, secret: str) -> None:
        path = source / "config.yaml"
        config = yaml.safe_load(path.read_text())
        config["Airbyte"]["config"]["password"] = secret
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    @staticmethod
    def _populate_source_snowflake_account(source: Path, secret: str) -> None:
        path = source / "config.yaml"
        config = yaml.safe_load(path.read_text())
        config["snowflake"]["config"]["account"] = secret
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    @staticmethod
    def _populate_source_credential_file(source: Path, secret: str) -> None:
        (source / "snowflake_credential.json").write_text(
            json.dumps({"account": secret, "user": "", "password": ""}),
            encoding="utf-8",
        )

    def test_refuses_existing_destination_without_modifying_it(self) -> None:
        source = self.make_bundle()
        destination = self.root / "existing"
        destination.mkdir()
        marker = destination / "keep.txt"
        marker.write_text("owned by prior attempt", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            install_task(
                source,
                destination,
                airbyte_credentials=self.airbyte_credentials(),
                snowflake_credentials=self.snowflake_credentials(),
            )

        self.assertEqual(marker.read_text(), "owned by prior attempt")
        self.assertEqual([path.name for path in destination.iterdir()], ["keep.txt"])

    def test_attempt_state_is_not_copied(self) -> None:
        source = self.make_bundle()
        (source / "elt" / ".terraform").mkdir()
        (source / "elt" / ".terraform" / "provider").write_text("cached")
        (source / "elt" / "terraform.tfstate").write_text("state")
        (source / "elt" / "terraform.tfstate.backup").write_text("backup")
        (source / "dbt" / "target").mkdir(parents=True)
        (source / "dbt" / "target" / "manifest.json").write_text("{}")
        # Dependency locks are inputs, not mutable Terraform state.
        (source / "elt" / ".terraform.lock.hcl").write_text("lock")
        destination = self.root / "fresh"

        install_task(
            source,
            destination,
            airbyte_credentials=self.airbyte_credentials(),
            snowflake_credentials=self.snowflake_credentials(),
        )

        self.assertFalse((destination / "elt" / ".terraform").exists())
        self.assertFalse((destination / "elt" / "terraform.tfstate").exists())
        self.assertFalse(
            (destination / "elt" / "terraform.tfstate.backup").exists()
        )
        self.assertFalse((destination / "dbt" / "target").exists())
        self.assertEqual(
            (destination / "elt" / ".terraform.lock.hcl").read_text(), "lock"
        )

    def test_failed_staging_is_cleaned_and_never_published(self) -> None:
        source = self.make_bundle()
        destination = self.root / "atomic-attempt"

        with mock.patch(
            "elt_taskgen.runtime.install.yaml.safe_dump",
            side_effect=RuntimeError("synthetic write failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic write failure"):
                install_task(
                    source,
                    destination,
                    airbyte_credentials=self.airbyte_credentials(),
                    snowflake_credentials=self.snowflake_credentials(),
                )

        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob(".atomic-attempt.install-*")), [])

    def test_conflicting_custom_api_ids_do_not_leak_values(self) -> None:
        source = self.make_bundle(rest=True)
        airbyte = self.airbyte_credentials(api_definition_id="secret-id-one")
        with self.assertRaises(ValueError) as raised:
            install_task(
                source,
                self.root / "conflict",
                airbyte_credentials=airbyte,
                snowflake_credentials=self.snowflake_credentials(),
                custom_api_definition_id="secret-id-two",
            )
        message = str(raised.exception)
        self.assertNotIn("secret-id-one", message)
        self.assertNotIn("secret-id-two", message)


if __name__ == "__main__":
    unittest.main()

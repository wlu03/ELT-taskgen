"""Offline mutation tests for the workspace-v1 Terraform intent compiler."""

from __future__ import annotations

import inspect
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

from elt_taskgen.destinations import DESTINATION_CONTRACTS, Destination
from elt_taskgen.training.terraform_intent import (
    TerraformIntentErrorCode,
    TerraformIntentHarnessError,
    evaluate_terraform_intent,
    expected_terraform_graph,
)


SOURCE_DEFINITIONS = {
    "postgres": "decd338e-5647-4c0b-adf4-da0e75f5a750",
    "mongodb": "b2e713cd-cc36-4c0a-b5bd-b47cb8a0561e",
    "aws_s3": "69589781-7828-43c5-9f63-8925b1c1ccc2",
    "file_orders": "778daa7c-feaf-4db6-96f3-70fd645acc77",
}


def _contract(destination: Destination) -> dict:
    if destination is Destination.SNOWFLAKE:
        configuration = {
            "host": "",
            "role": "",
            "database": "task_db",
            "schema": "AIRBYTE_SCHEMA",
            "warehouse": "",
            "username": "",
            "number_data_type": "NUMBER(38,9)",
            "credentials": {
                "auth_type": "Username and Password",
                "password": "",
            },
        }
        namespace = "task_db"
    elif destination is Destination.DATABRICKS:
        configuration = {
            "accept_terms": True,
            "authentication": {
                "auth_type": "OAUTH",
                "client_id": "",
                "secret": "",
            },
            "database": "",
            "hostname": "",
            "http_path": "",
            "port": "443",
            "purge_staging_data": True,
            "schema": "task_schema",
        }
        namespace = "task_schema"
    else:
        configuration = {
            "database": "",
            "drop_cascade": False,
            "host": "",
            "password": "",
            "port": 5439,
            "schema": "task_schema",
            "uploading_method": {
                "access_key_id": "",
                "method": "S3 Staging",
                "purge_staging_data": True,
                "s3_bucket_name": "",
                "s3_bucket_path": "elt-bench/task_schema",
                "s3_bucket_region": "us-west-2",
                "secret_access_key": "",
            },
            "username": "",
        }
        namespace = "task_schema"
    destination_contract = DESTINATION_CONTRACTS[destination]
    return {
        "schema_version": "1.0",
        "workspace_id": "",
        "terraform_provider": {
            "source": "airbytehq/airbyte",
            "version": "0.6.5",
            "configuration": {
                "server_url": "http://airbyte-control-plane/api/public/v1/"
            },
        },
        "destination": {
            "key": destination.value,
            "definition_id": destination_contract.definition_id,
            "configuration": configuration,
        },
        "sources": [
            {
                "key": "postgres",
                "definition_id": SOURCE_DEFINITIONS["postgres"],
                "configuration": {
                    "host": "elt-postgres",
                    "port": 5432,
                    "database": "fixture",
                    "username": "postgres",
                    "password": "private-value-not-compared",
                    "schemas": ["public"],
                },
                "connection": {
                    "namespace_definition": "destination",
                    "configurations": {
                        "streams": [
                            {
                                "name": "customers",
                                "sync_mode": "full_refresh_append",
                            }
                        ]
                    },
                },
            },
            {
                "key": "mongodb",
                "definition_id": SOURCE_DEFINITIONS["mongodb"],
                "configuration": {
                    "database_config": {
                        "cluster_type": "SELF_MANAGED_REPLICA_SET",
                        "connection_string": "mongodb://elt-mongodb:27017/",
                        "databases": ["fixture"],
                        "schema_enforced": True,
                    }
                },
                "connection": {
                    "namespace_definition": "destination",
                    "configurations": {
                        "streams": [
                            {"name": "events", "sync_mode": "full_refresh_append"}
                        ]
                    },
                },
            },
            {
                "key": "custom_api",
                "definition_id": "",
                "configuration": {},
                "connection": {
                    "namespace_definition": "destination",
                    "configurations": {
                        "streams": [
                            {"name": "tickets", "sync_mode": "full_refresh_append"}
                        ]
                    },
                },
            },
            {
                "key": "aws_s3",
                "definition_id": SOURCE_DEFINITIONS["aws_s3"],
                "configuration": {
                    "aws_access_key_id": "private-value-not-compared",
                    "aws_secret_access_key": "private-value-not-compared",
                    "bucket": "fixture-bucket",
                    "endpoint": "http://elt-localstack:4566",
                    "region_name": "us-west-2",
                    "streams": [
                        {
                            "name": "metrics",
                            "format": {"filetype": "jsonl"},
                            "globs": ["prefix/metrics.jsonl"],
                        }
                    ],
                },
                "connection": {
                    "namespace_definition": "destination",
                    "configurations": {
                        "streams": [
                            {"name": "metrics", "sync_mode": "full_refresh_append"}
                        ]
                    },
                },
            },
            {
                "key": "file_orders",
                "definition_id": SOURCE_DEFINITIONS["file_orders"],
                "configuration": {
                    "dataset_name": "orders",
                    "format": "csv",
                    "provider": {"storage": "HTTPS"},
                    "url": "http://elt-files:8080/orders.csv",
                },
                "connection": {
                    "namespace_definition": "destination",
                    "configurations": {
                        "streams": [
                            {"name": "orders", "sync_mode": "full_refresh_append"}
                        ]
                    },
                },
            },
        ],
        "_namespace": namespace,
    }


def _package(destination: Destination):
    payload = _contract(destination)
    namespace = payload.pop("_namespace")
    return SimpleNamespace(
        airbyte_contract=MappingProxyType(payload),
        destination=destination,
        logical_namespace=namespace,
    )


def _deep_freeze(value):
    """Match WorkspacePackage's recursively frozen private JSON shape."""

    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _variables() -> str:
    names = (
        "workspace_id",
        "postgres_password",
        "destination_host",
        "destination_warehouse",
        "destination_role",
        "destination_username",
        "destination_database",
        "destination_password",
        "databricks_client_id",
        "databricks_secret",
        "http_path",
        "redshift_access_key_id",
        "redshift_secret_access_key",
        "redshift_username",
        "redshift_staging_bucket",
        "s3_access_key_id",
        "s3_secret_access_key",
        "custom_api_definition_id",
    )
    return "\n".join(f'variable "{name}" {{}}' for name in names)


def _destination_hcl(destination: Destination) -> str:
    definition_id = DESTINATION_CONTRACTS[destination].definition_id
    if destination is Destination.SNOWFLAKE:
        return f'''
resource "airbyte_destination_snowflake" "arbitrary_dest" {{
  name = "destination"
  workspace_id = var.workspace_id
  definition_id = "{definition_id}"
  configuration = {{
    host = var.destination_host
    database = "task_db"
    schema = "AIRBYTE_SCHEMA"
    warehouse = var.destination_warehouse
    role = var.destination_role
    username = var.destination_username
    number_data_type = "NUMBER(38,9)"
    credentials = {{
      username_and_password = {{
        password = var.destination_password
      }}
    }}
  }}
}}
'''
    if destination is Destination.DATABRICKS:
        return f'''
resource "airbyte_destination_databricks" "arbitrary_dest" {{
  name = "destination"
  workspace_id = var.workspace_id
  definition_id = "{definition_id}"
  configuration = {{
    database = var.destination_database
    hostname = var.destination_host
    http_path = var.http_path
    port = "443"
    schema = "task_schema"
    accept_terms = true
    purge_staging_data = true
    authentication = {{
      o_auth2_recommended = {{ client_id = var.databricks_client_id, secret = var.databricks_secret }}
    }}
  }}
}}
'''
    return f'''
resource "airbyte_destination_redshift" "arbitrary_dest" {{
  name = "destination"
  workspace_id = var.workspace_id
  definition_id = "{definition_id}"
  configuration = {{
    database = var.destination_database
    drop_cascade = false
    host = var.destination_host
    password = var.destination_password
    port = 5439
    schema = "task_schema"
    username = var.redshift_username
    uploading_method = {{
      awss3_staging = {{
        access_key_id = var.redshift_access_key_id
        secret_access_key = var.redshift_secret_access_key
        s3_bucket_name = var.redshift_staging_bucket
        s3_bucket_path = "elt-bench/task_schema"
        s3_bucket_region = "us-west-2"
        purge_staging_data = true
      }}
    }}
  }}
}}
'''


def _valid_hcl(destination: Destination) -> str:
    destination_type = f"airbyte_destination_{destination.value}"
    return f'''
terraform {{
  required_providers {{
    airbyte = {{ source = "airbytehq/airbyte", version = "0.6.5" }}
  }}
}}
{_variables()}

provider "airbyte" {{}}

resource "airbyte_source_postgres" "renamed_pg" {{
  name = "postgres"
  workspace_id = var.workspace_id
  definition_id = "{SOURCE_DEFINITIONS['postgres']}"
  configuration = {{
    host = "elt-postgres"
    port = 5432
    database = "fixture"
    username = "postgres"
    password = var.postgres_password
    schemas = ["public"]
  }}
}}

resource "airbyte_source_mongodb_v2" "renamed_mongo" {{
  name = "mongodb"
  workspace_id = var.workspace_id
  definition_id = "{SOURCE_DEFINITIONS['mongodb']}"
  configuration = {{
    database_config = {{
      self_managed_replica_set = {{
        connection_string = "mongodb://elt-mongodb:27017/"
        database = "fixture"
      }}
    }}
  }}
}}

resource "airbyte_source_custom" "renamed_api" {{
  name = "custom_api"
  workspace_id = var.workspace_id
  definition_id = var.custom_api_definition_id
  configuration = jsonencode({{}})
}}

resource "airbyte_source_s3" "renamed_s3" {{
  name = "s3"
  workspace_id = var.workspace_id
  definition_id = "{SOURCE_DEFINITIONS['aws_s3']}"
  configuration = {{
    aws_access_key_id = var.s3_access_key_id
    aws_secret_access_key = var.s3_secret_access_key
    bucket = "fixture-bucket"
    endpoint = "http://elt-localstack:4566"
    region_name = "us-west-2"
    streams = [{{
      name = "metrics"
      format = {{ jsonl_format = {{}} }}
      globs = ["prefix/metrics.jsonl"]
    }}]
  }}
}}

resource "airbyte_source_file" "renamed_file" {{
  name = "file"
  workspace_id = var.workspace_id
  definition_id = "{SOURCE_DEFINITIONS['file_orders']}"
  configuration = {{
    dataset_name = "orders"
    format = "csv"
    provider = {{ https_public_web = {{}} }}
    url = "http://elt-files:8080/orders.csv"
  }}
}}
{_destination_hcl(destination)}

resource "airbyte_connection" "pg_link" {{
  name = "pg"
  source_id = airbyte_source_postgres.renamed_pg.source_id
  destination_id = {destination_type}.arbitrary_dest.destination_id
  namespace_definition = "destination"
  configurations = {{
    streams = [{{ name = "customers", sync_mode = "full_refresh_append" }}]
  }}
}}

resource "airbyte_connection" "file_link" {{
  name = "file"
  source_id = airbyte_source_file.renamed_file.source_id
  destination_id = {destination_type}.arbitrary_dest.destination_id
  namespace_definition = "destination"
  configurations = {{
    streams = [{{ name = "orders", sync_mode = "full_refresh_append" }}]
  }}
}}

resource "airbyte_connection" "mongo_link" {{
  source_id = airbyte_source_mongodb_v2.renamed_mongo.source_id
  destination_id = {destination_type}.arbitrary_dest.destination_id
  namespace_definition = "destination"
  configurations = {{
    streams = [{{ name = "events", sync_mode = "full_refresh_append" }}]
  }}
}}

resource "airbyte_connection" "api_link" {{
  source_id = airbyte_source_custom.renamed_api.source_id
  destination_id = {destination_type}.arbitrary_dest.destination_id
  namespace_definition = "destination"
  configurations = {{
    streams = [{{ name = "tickets", sync_mode = "full_refresh_append" }}]
  }}
}}

resource "airbyte_connection" "s3_link" {{
  source_id = airbyte_source_s3.renamed_s3.source_id
  destination_id = {destination_type}.arbitrary_dest.destination_id
  namespace_definition = "destination"
  configurations = {{
    streams = [{{ name = "metrics", sync_mode = "full_refresh_append" }}]
  }}
}}
'''


class TerraformIntentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def evaluate(self, text: str, destination: Destination = Destination.SNOWFLAKE):
        path = self.root / "main.tf"
        path.write_text(text, encoding="utf-8")
        return evaluate_terraform_intent(_package(destination), path)

    def assert_code(
        self,
        text: str,
        code: TerraformIntentErrorCode,
        destination: Destination = Destination.SNOWFLAKE,
    ) -> None:
        result = self.evaluate(text, destination)
        self.assertEqual(result.reward, 0.0)
        self.assertEqual(result.error_codes, (code,))
        self.assertIsNone(result.graph)

    def test_canonical_snowflake_databricks_and_redshift_pass(self) -> None:
        for destination in Destination:
            with self.subTest(destination=destination.value):
                result = self.evaluate(_valid_hcl(destination), destination)
                self.assertTrue(result.valid)
                self.assertEqual(result.reward, 1.0)
                self.assertEqual(result.graph.destination.kind, destination.value)
                if destination is Destination.DATABRICKS:
                    # Airbyte 4.0.2 exposes no candidate Volume selector. The
                    # real harness grants CREATE VOLUME at schema scope.
                    self.assertFalse(result.graph.destination.volume_intent)
                self.assertEqual(
                    {
                        (
                            item.source_key,
                            item.connector_kind,
                            item.stream_name,
                            item.sync_mode,
                        )
                        for item in result.graph.selected_streams
                    },
                    {
                        ("postgres", "postgres", "customers", "full_refresh_append"),
                        ("mongodb", "mongodb", "events", "full_refresh_append"),
                        ("custom_api", "custom_api", "tickets", "full_refresh_append"),
                        ("aws_s3", "aws_s3", "metrics", "full_refresh_append"),
                        ("file_orders", "file", "orders", "full_refresh_append"),
                    },
                )

    def test_resource_labels_and_formatting_do_not_change_graph(self) -> None:
        original = self.evaluate(_valid_hcl(Destination.SNOWFLAKE))
        changed_text = (
            _valid_hcl(Destination.SNOWFLAKE)
            .replace("renamed_pg", "totally_different")
            .replace("pg_link", "connection_with_another_label")
            .replace("  name = \"pg\"", "\n\n name=\"pg\"")
        )
        changed = self.evaluate(changed_text)
        self.assertEqual(original.graph, changed.graph)

    def test_real_workspace_deep_freezing_does_not_change_private_intent(self) -> None:
        payload = _contract(Destination.SNOWFLAKE)
        namespace = payload.pop("_namespace")
        package = SimpleNamespace(
            airbyte_contract=_deep_freeze(payload),
            destination=Destination.SNOWFLAKE,
            logical_namespace=namespace,
        )
        path = self.root / "main.tf"
        path.write_text(_valid_hcl(Destination.SNOWFLAKE), encoding="utf-8")
        result = evaluate_terraform_intent(package, path)
        self.assertEqual(result.reward, 1.0)
        self.assertIsNotNone(result.graph)

    def test_hard_coded_credentials_are_policy_violations(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            "password = var.postgres_password", 'password = "literal-secret"', 1
        )
        result = self.evaluate(text)
        self.assertEqual(
            result.error_codes,
            (TerraformIntentErrorCode.HARDCODED_CREDENTIAL,),
        )
        self.assertTrue(result.policy_violation)

    def test_secret_variable_default_is_hard_coded(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            'variable "postgres_password" {}',
            'variable "postgres_password" { default = "literal-secret" }',
        )
        self.assert_code(text, TerraformIntentErrorCode.HARDCODED_CREDENTIAL)

    def test_unapproved_provider_is_rejected(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            'airbyte = { source = "airbytehq/airbyte", version = "0.6.5" }',
            'airbyte = { source = "airbytehq/airbyte", version = "0.6.5" }\n'
            '    aws = { source = "hashicorp/aws", version = "6.0.0" }',
        )
        self.assert_code(text, TerraformIntentErrorCode.PROVIDER_FORBIDDEN)

    def test_arbitrary_airbyte_provider_endpoint_is_rejected(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            'provider "airbyte" {}',
            'provider "airbyte" { server_url = "https://arbitrary.invalid/api" }',
        )
        self.assert_code(text, TerraformIntentErrorCode.PROVIDER_CONTRACT)

    def test_exact_private_airbyte_provider_endpoint_is_admitted(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            'provider "airbyte" {}',
            'provider "airbyte" {\n'
            '  server_url = "http://airbyte-control-plane/api/public/v1/"\n'
            '}',
        )
        self.assertTrue(self.evaluate(text).valid)

    def test_remote_module_is_rejected(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE) + '''
module "remote" { source = "git::https://example.invalid/module.git" }
'''
        self.assert_code(text, TerraformIntentErrorCode.MODULE_FORBIDDEN)

    def test_data_source_outside_empty_allowlist_is_rejected(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE) + '''
data "http" "remote" { url = "https://example.invalid" }
'''
        self.assert_code(text, TerraformIntentErrorCode.DATA_SOURCE_FORBIDDEN)

    def test_plain_provisioner_is_rejected(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            '  name = "postgres"',
            '  name = "postgres"\n  provisioner "chef" { command = "no" }',
            1,
        )
        self.assert_code(text, TerraformIntentErrorCode.PROVISIONER_FORBIDDEN)

    def test_local_and_remote_exec_are_rejected(self) -> None:
        for kind in ("local-exec", "remote-exec"):
            with self.subTest(kind=kind):
                text = _valid_hcl(Destination.SNOWFLAKE).replace(
                    '  name = "postgres"',
                    f'  name = "postgres"\n  provisioner "{kind}" {{ command = "no" }}',
                    1,
                )
                self.assert_code(text, TerraformIntentErrorCode.EXEC_FORBIDDEN)

    def test_external_file_function_is_rejected(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            '  name = "postgres"', '  name = file("/private/source")', 1
        )
        self.assert_code(text, TerraformIntentErrorCode.EXTERNAL_ACCESS)

    def test_remote_backend_is_rejected(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            "terraform {", 'terraform {\n  backend "s3" {}', 1
        )
        self.assert_code(text, TerraformIntentErrorCode.EXTERNAL_ACCESS)

    def test_duplicate_connection_is_rejected(self) -> None:
        block = '''
resource "airbyte_connection" "duplicate_pg_link" {
  source_id = airbyte_source_postgres.renamed_pg.source_id
  destination_id = airbyte_destination_snowflake.arbitrary_dest.destination_id
  namespace_definition = "destination"
  configurations = { streams = [{ name = "customers", sync_mode = "full_refresh_append" }] }
}
'''
        self.assert_code(
            _valid_hcl(Destination.SNOWFLAKE) + block,
            TerraformIntentErrorCode.DUPLICATE_CONNECTION,
        )

    def test_duplicate_resource_address_is_rejected(self) -> None:
        block = f'''
resource "airbyte_source_postgres" "renamed_pg" {{
  definition_id = "{SOURCE_DEFINITIONS['postgres']}"
  configuration = {{}}
}}
'''
        self.assert_code(
            _valid_hcl(Destination.SNOWFLAKE) + block,
            TerraformIntentErrorCode.DUPLICATE_RESOURCE,
        )

    def test_unresolved_reference_is_rejected(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            "airbyte_source_postgres.renamed_pg.source_id",
            "airbyte_source_postgres.missing.source_id",
        )
        self.assert_code(text, TerraformIntentErrorCode.UNRESOLVED_REFERENCE)

    def test_dependency_cycle_is_rejected(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            '  name = "postgres"',
            "  name = \"postgres\"\n"
            "  depends_on = [airbyte_connection.pg_link]",
            1,
        )
        self.assert_code(text, TerraformIntentErrorCode.DEPENDENCY_CYCLE)

    def test_wrong_backend_routing_is_rejected(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            'resource "airbyte_source_postgres" "renamed_pg"',
            'resource "airbyte_source_mongodb_v2" "renamed_pg"',
        ).replace(
            "airbyte_source_postgres.renamed_pg.source_id",
            "airbyte_source_mongodb_v2.renamed_pg.source_id",
        )
        self.assert_code(text, TerraformIntentErrorCode.STREAM_CONTRACT)

    def test_wrong_postgres_database_is_rejected(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            'database = "fixture"', 'database = "another_database"', 1
        )
        self.assert_code(text, TerraformIntentErrorCode.SOURCE_CONTRACT)

    def test_wrong_mongodb_database_and_connection_are_rejected(self) -> None:
        mutations = (
            ('database = "fixture"', 'database = "another_database"'),
            (
                'connection_string = "mongodb://elt-mongodb:27017/"',
                'connection_string = "mongodb://wrong-host:27017/"',
            ),
        )
        for old, new in mutations:
            with self.subTest(field=old.split(" = ")[0]):
                # The Mongo database occurrence follows the Postgres one.
                text = _valid_hcl(Destination.SNOWFLAKE)
                if old == 'database = "fixture"':
                    first = text.index(old)
                    position = text.index(old, first + len(old))
                    text = text[:position] + text[position:].replace(old, new, 1)
                else:
                    text = text.replace(old, new, 1)
                self.assert_code(text, TerraformIntentErrorCode.SOURCE_CONTRACT)

    def test_wrong_s3_bucket_endpoint_or_glob_prefix_is_rejected(self) -> None:
        mutations = (
            ('bucket = "fixture-bucket"', 'bucket = "wrong-bucket"'),
            (
                'endpoint = "http://elt-localstack:4566"',
                'endpoint = "http://wrong-localstack:4566"',
            ),
            (
                'globs = ["prefix/metrics.jsonl"]',
                'globs = ["wrong-prefix/metrics.jsonl"]',
            ),
            (
                "format = { jsonl_format = {} }",
                "format = { csv_format = {} }",
            ),
        )
        for old, new in mutations:
            with self.subTest(field=old.split(" = ")[0]):
                text = _valid_hcl(Destination.SNOWFLAKE).replace(old, new, 1)
                self.assert_code(text, TerraformIntentErrorCode.SOURCE_CONTRACT)

    def test_wrong_file_url_or_dataset_selection_is_rejected(self) -> None:
        mutations = (
            (
                'url = "http://elt-files:8080/orders.csv"',
                'url = "http://elt-files:8080/wrong.csv"',
            ),
            ('dataset_name = "orders"', 'dataset_name = "customers"'),
            (
                "provider = { https_public_web = {} }",
                "provider = { local_filesystem_limited = {} }",
            ),
        )
        for old, new in mutations:
            with self.subTest(field=old.split(" = ")[0]):
                text = _valid_hcl(Destination.SNOWFLAKE).replace(old, new, 1)
                self.assert_code(text, TerraformIntentErrorCode.SOURCE_CONTRACT)

    def test_nonempty_custom_api_runtime_configuration_is_rejected(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            "configuration = jsonencode({})",
            'configuration = jsonencode({ base_url = "https://wrong.invalid" })',
            1,
        )
        self.assert_code(text, TerraformIntentErrorCode.SOURCE_CONTRACT)

    def test_wrong_stream_coverage_is_rejected(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            'name = "customers", sync_mode = "full_refresh_append"',
            'name = "wrong", sync_mode = "full_refresh_append"',
        )
        self.assert_code(text, TerraformIntentErrorCode.STREAM_CONTRACT)

    def test_wrong_sync_mode_is_rejected(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            'name = "customers", sync_mode = "full_refresh_append"',
            'name = "customers", sync_mode = "incremental_append"',
        )
        self.assert_code(text, TerraformIntentErrorCode.SYNC_MODE)

    def test_wrong_namespace_behavior_is_rejected(self) -> None:
        text = _valid_hcl(Destination.SNOWFLAKE).replace(
            'namespace_definition = "destination"',
            'namespace_definition = "source"',
            1,
        )
        self.assert_code(text, TerraformIntentErrorCode.NAMESPACE_CONTRACT)

    def test_destination_specific_structural_contracts_fail_closed(self) -> None:
        cases = (
            (
                Destination.SNOWFLAKE,
                "warehouse = var.destination_warehouse",
                "warehouse = \"\"",
            ),
            (
                Destination.DATABRICKS,
                "purge_staging_data = true",
                "purge_staging_data = false",
            ),
            (
                Destination.REDSHIFT,
                "awss3_staging = {",
                "direct = {",
            ),
        )
        for destination, old, new in cases:
            with self.subTest(destination=destination.value):
                self.assert_code(
                    _valid_hcl(destination).replace(old, new),
                    TerraformIntentErrorCode.DESTINATION_CONTRACT,
                    destination,
                )

    def test_databricks_volume_is_not_a_candidate_configuration_field(self) -> None:
        text = _valid_hcl(Destination.DATABRICKS).replace(
            '    purge_staging_data = true',
            '    purge_staging_data = true\n    volume = "candidate_volume"',
            1,
        )
        self.assert_code(
            text,
            TerraformIntentErrorCode.DESTINATION_CONTRACT,
            Destination.DATABRICKS,
        )

        package = _package(Destination.DATABRICKS)
        broken = dict(package.airbyte_contract)
        destination = dict(broken["destination"])
        configuration = dict(destination["configuration"])
        configuration["volume"] = "private_volume"
        destination["configuration"] = configuration
        broken["destination"] = destination
        path = self.root / "main.tf"
        path.write_text(_valid_hcl(Destination.DATABRICKS), encoding="utf-8")
        with self.assertRaises(TerraformIntentHarnessError):
            evaluate_terraform_intent(
                SimpleNamespace(
                    airbyte_contract=broken,
                    destination=Destination.DATABRICKS,
                    logical_namespace="task_schema",
                ),
                path,
            )

    def test_every_source_and_destination_requires_one_workspace_variable(self) -> None:
        canonical = _valid_hcl(Destination.SNOWFLAKE)
        before_destination, marker, after_destination = canonical.rpartition(
            "  workspace_id = var.workspace_id\n"
        )
        self.assertTrue(marker)
        cases = (
            canonical.replace("  workspace_id = var.workspace_id\n", "", 1),
            before_destination + after_destination,
            canonical.replace(
                "  workspace_id = var.workspace_id\n",
                '  workspace_id = "literal-workspace"\n',
                1,
            ),
            canonical.replace(
                'variable "workspace_id" {}',
                'variable "workspace_id" { default = "candidate-owned" }',
            ),
            canonical.replace(
                "workspace_id = var.workspace_id",
                "workspace_id = var.destination_database",
                1,
            ),
        )
        for index, text in enumerate(cases):
            with self.subTest(case=index):
                self.assert_code(
                    text, TerraformIntentErrorCode.WORKSPACE_CONTRACT
                )

    def test_private_destination_shape_drift_is_label_ineligible(self) -> None:
        package = _package(Destination.SNOWFLAKE)
        broken = dict(package.airbyte_contract)
        destination = dict(broken["destination"])
        configuration = dict(destination["configuration"])
        configuration.pop("number_data_type")
        destination["configuration"] = configuration
        broken["destination"] = destination
        path = self.root / "main.tf"
        path.write_text(_valid_hcl(Destination.SNOWFLAKE), encoding="utf-8")
        with self.assertRaises(TerraformIntentHarnessError):
            evaluate_terraform_intent(
                SimpleNamespace(
                    airbyte_contract=broken,
                    destination=Destination.SNOWFLAKE,
                    logical_namespace="task_db",
                ),
                path,
            )

    def test_destination_authentication_blocks_are_required(self) -> None:
        cases = (
            (
                Destination.SNOWFLAKE,
                "    credentials = {\n"
                "      username_and_password = {\n"
                "        password = var.destination_password\n"
                "      }\n"
                "    }\n",
            ),
            (
                Destination.DATABRICKS,
                "    authentication = {\n"
                "      o_auth2_recommended = { client_id = var.databricks_client_id, secret = var.databricks_secret }\n"
                "    }\n",
            ),
            (Destination.REDSHIFT, "    username = var.redshift_username\n"),
        )
        for destination, block in cases:
            with self.subTest(destination=destination.value):
                self.assert_code(
                    _valid_hcl(destination).replace(block, "", 1),
                    TerraformIntentErrorCode.DESTINATION_CONTRACT,
                    destination,
                )

    def test_destination_runtime_fields_must_be_undeclared_default_free_variables(self) -> None:
        cases = (
            (
                Destination.SNOWFLAKE,
                "host = var.destination_host",
                'host = "account.snowflakecomputing.com"',
            ),
            (
                Destination.DATABRICKS,
                "database = var.destination_database",
                'database = "candidate_catalog"',
            ),
            (
                Destination.REDSHIFT,
                "s3_bucket_name = var.redshift_staging_bucket",
                's3_bucket_name = "candidate-bucket"',
            ),
        )
        for destination, old, new in cases:
            with self.subTest(destination=destination.value):
                self.assert_code(
                    _valid_hcl(destination).replace(old, new, 1),
                    TerraformIntentErrorCode.DESTINATION_CONTRACT,
                    destination,
                )

        defaulted = _valid_hcl(Destination.DATABRICKS).replace(
            'variable "http_path" {}',
            'variable "http_path" { default = "/sql/candidate" }',
        )
        self.assert_code(
            defaulted,
            TerraformIntentErrorCode.DESTINATION_CONTRACT,
            Destination.DATABRICKS,
        )

    def test_literal_destination_secrets_remain_policy_violations(self) -> None:
        cases = (
            (
                Destination.SNOWFLAKE,
                "password = var.destination_password",
                'password = "literal-secret"',
            ),
            (
                Destination.DATABRICKS,
                "secret = var.databricks_secret",
                'secret = "literal-secret"',
            ),
            (
                Destination.REDSHIFT,
                "secret_access_key = var.redshift_secret_access_key",
                'secret_access_key = "literal-secret"',
            ),
        )
        for destination, old, new in cases:
            with self.subTest(destination=destination.value):
                result = self.evaluate(
                    _valid_hcl(destination).replace(old, new, 1), destination
                )
                self.assertEqual(
                    result.error_codes,
                    (TerraformIntentErrorCode.HARDCODED_CREDENTIAL,),
                )
                self.assertTrue(result.policy_violation)

    def test_destination_fixed_route_and_container_fields_are_exact(self) -> None:
        cases = (
            (
                Destination.SNOWFLAKE,
                'number_data_type = "NUMBER(38,9)"',
                'number_data_type = "FLOAT"',
            ),
            (
                Destination.SNOWFLAKE,
                'database = "task_db"',
                'database = "wrong_database"',
            ),
            (
                Destination.DATABRICKS,
                'port = "443"',
                'port = "8443"',
            ),
            (
                Destination.REDSHIFT,
                "port = 5439",
                "port = 15439",
            ),
            (
                Destination.REDSHIFT,
                's3_bucket_path = "elt-bench/task_schema"',
                's3_bucket_path = "candidate/prefix"',
            ),
            (
                Destination.REDSHIFT,
                's3_bucket_region = "us-west-2"',
                's3_bucket_region = "us-east-1"',
            ),
        )
        for destination, old, new in cases:
            with self.subTest(destination=destination.value, field=old):
                expected_code = (
                    TerraformIntentErrorCode.NAMESPACE_CONTRACT
                    if destination is Destination.SNOWFLAKE and old.startswith("database")
                    else TerraformIntentErrorCode.DESTINATION_CONTRACT
                )
                self.assert_code(
                    _valid_hcl(destination).replace(old, new, 1),
                    expected_code,
                    destination,
                )

    def test_private_contract_defect_propagates_without_zero_reward(self) -> None:
        package = _package(Destination.SNOWFLAKE)
        broken = dict(package.airbyte_contract)
        broken["terraform_provider"] = {"source": "wrong", "version": "0"}
        package = SimpleNamespace(
            airbyte_contract=broken,
            destination=Destination.SNOWFLAKE,
            logical_namespace="task_db",
        )
        path = self.root / "main.tf"
        path.write_text(_valid_hcl(Destination.SNOWFLAKE), encoding="utf-8")
        with self.assertRaises(TerraformIntentHarnessError):
            evaluate_terraform_intent(package, path)

    def test_expected_graph_rejects_private_backend_drift(self) -> None:
        package = _package(Destination.SNOWFLAKE)
        broken = dict(package.airbyte_contract)
        broken["sources"] = [
            {
                "key": "ftp",
                "connection": {
                    "namespace_definition": "destination",
                    "configurations": {
                        "streams": [
                            {"name": "x", "sync_mode": "full_refresh_append"}
                        ]
                    },
                },
            }
        ]
        with self.assertRaises(TerraformIntentHarnessError):
            expected_terraform_graph(
                SimpleNamespace(
                    airbyte_contract=broken,
                    destination=Destination.SNOWFLAKE,
                    logical_namespace="task_db",
                )
            )


class GenericMongoResourceTest(unittest.TestCase):
    """A Mongo source declared as `airbyte_source_custom` with a JSON body is
    the shape that applies on the pinned control plane (the typed resource
    cannot express the connector's `databases` list), so it is accepted as
    the mongodb source it names by definition id."""

    def test_generic_resource_with_json_body_is_the_mongodb_source(self) -> None:
        typed = _valid_hcl(Destination.SNOWFLAKE)
        start = typed.index('resource "airbyte_source_mongodb_v2" "renamed_mongo"')
        end = typed.index("}\n", typed.index("configuration = {", start))
        end = typed.index("\n}\n", start) + len("\n}\n")
        generic = (
            'resource "airbyte_source_custom" "renamed_mongo" {\n'
            '  name = "mongodb"\n'
            "  workspace_id = var.workspace_id\n"
            f'  definition_id = "{SOURCE_DEFINITIONS["mongodb"]}"\n'
            "  configuration = jsonencode({\n"
            "    database_config = {\n"
            '      cluster_type = "SELF_MANAGED_REPLICA_SET"\n'
            '      connection_string = "mongodb://elt-mongodb:27017/"\n'
            '      databases = ["fixture"]\n'
            "    }\n"
            "  })\n"
            "}\n"
        )
        text = (typed[:start] + generic + typed[end:]).replace(
            "airbyte_source_mongodb_v2.renamed_mongo.source_id",
            "airbyte_source_custom.renamed_mongo.source_id",
        )
        root = Path(tempfile.mkdtemp())
        path = root / "main.tf"
        path.write_text(text, encoding="utf-8")
        result = evaluate_terraform_intent(_package(Destination.SNOWFLAKE), path)
        self.assertEqual(result.error_codes, ())
        self.assertEqual(result.reward, 1.0)


class TerraformIntentExecutionBoundsTest(unittest.TestCase):
    """Roadmap Phase 0.A: the candidate ``main.tf`` is read through the seal's
    bounded no-follow reader and parsed in a killable worker under a 10 s
    deadline, so a pathological HCL file is a stable code, never a hang."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_terraform_intent_read_is_bounded_and_deadlined(self) -> None:
        import os
        import time

        from elt_taskgen.training import terraform_intent as ti
        from elt_taskgen.training.contract import MAX_WORKSPACE_FILE_BYTES

        self.assertEqual(ti.TERRAFORM_PARSE_DEADLINE_SECONDS, 10.0)
        self.assertEqual(ti.MAX_MAIN_TF_BYTES, MAX_WORKSPACE_FILE_BYTES)
        self.assertEqual(
            inspect.signature(ti.compile_terraform_intent)
            .parameters["parse_deadline_seconds"]
            .default,
            ti.TERRAFORM_PARSE_DEADLINE_SECONDS,
        )
        # The read is the seal's ``_read_bounded_regular_file``: oversized,
        # symlinked and non-regular candidates are PARSE, and the bound is
        # enforced on the descriptor, not on a trusting ``read_text``.
        source = inspect.getsource(ti._read_candidate_main_tf)
        self.assertIn("_read_bounded_regular_file", source)
        self.assertNotIn("read_text", inspect.getsource(ti.compile_terraform_intent))
        oversized = self.root / "main.tf"
        oversized.write_bytes(b"# " + b"x" * MAX_WORKSPACE_FILE_BYTES + b"\n")
        result = evaluate_terraform_intent(_package(Destination.SNOWFLAKE), oversized)
        self.assertEqual(result.error_codes, (TerraformIntentErrorCode.PARSE,))
        exactly = self.root / "exact.tf"
        exactly.write_bytes(b"#" * (MAX_WORKSPACE_FILE_BYTES - 1) + b"\n")
        self.assertEqual(exactly.stat().st_size, MAX_WORKSPACE_FILE_BYTES)
        # A comment-only file at the cap is read in full: it parses to an
        # empty document, which then fails the contract, not the reader.
        result = evaluate_terraform_intent(_package(Destination.SNOWFLAKE), exactly)
        self.assertEqual(result.reward, 0.0)
        self.assertNotEqual(result.error_codes, (TerraformIntentErrorCode.PARSE,))
        link = self.root / "link.tf"
        os.symlink(exactly, link)
        result = evaluate_terraform_intent(_package(Destination.SNOWFLAKE), link)
        self.assertEqual(result.error_codes, (TerraformIntentErrorCode.PARSE,))
        fifo = self.root / "fifo.tf"
        os.mkfifo(fifo)
        result = evaluate_terraform_intent(_package(Destination.SNOWFLAKE), fifo)
        self.assertEqual(result.error_codes, (TerraformIntentErrorCode.PARSE,))

        # The deadline: a large, VALID HCL file that hcl2 parses in seconds is
        # stopped at the deadline with the stable timeout code, quickly, and
        # the next call still works (the killed worker is replaced).
        slow = self.root / "slow.tf"
        slow.write_text(
            "".join(f'variable "v{i}" {{\n  default = {i}\n}}\n' for i in range(20000)),
            encoding="utf-8",
        )
        self.assertLess(slow.stat().st_size, MAX_WORKSPACE_FILE_BYTES)
        start = time.monotonic()
        with self.assertRaises(ti.TerraformIntentError) as raised:
            ti.compile_terraform_intent(
                _package(Destination.SNOWFLAKE), slow, parse_deadline_seconds=0.5
            )
        elapsed = time.monotonic() - start
        self.assertIs(raised.exception.code, TerraformIntentErrorCode.PARSE_TIMEOUT)
        self.assertEqual(raised.exception.code.value, "terraform_parse_timeout")
        self.assertFalse(raised.exception.code.policy_violation)
        self.assertLess(elapsed, 5.0)
        result = evaluate_terraform_intent(_package(Destination.SNOWFLAKE), slow)
        self.assertEqual(result.reward, 0.0)
        self.assertNotIn(TerraformIntentErrorCode.PARSE_TIMEOUT, result.error_codes)
        self.assertNotIn(TerraformIntentErrorCode.PARSE, result.error_codes)
        # A syntax error is still the candidate's PARSE, through the worker.
        broken = self.root / "broken.tf"
        broken.write_text('variable "a" {\n', encoding="utf-8")
        result = evaluate_terraform_intent(_package(Destination.SNOWFLAKE), broken)
        self.assertEqual(result.error_codes, (TerraformIntentErrorCode.PARSE,))
        # The deadline path is a candidate outcome through the public head.
        with unittest.mock.patch.object(
            ti, "TERRAFORM_PARSE_DEADLINE_SECONDS", 0.5
        ), unittest.mock.patch.object(
            ti.compile_terraform_intent, "__kwdefaults__",
            {"parse_deadline_seconds": 0.5},
        ):
            result = evaluate_terraform_intent(_package(Destination.SNOWFLAKE), slow)
        self.assertEqual(result.reward, 0.0)
        self.assertEqual(result.error_codes, (TerraformIntentErrorCode.PARSE_TIMEOUT,))
        self.assertFalse(result.policy_violation)

    def test_terraform_parse_worker_payload_is_restricted_to_lark_types(self) -> None:
        """The parse worker ran UNTRUSTED text, so the ``(document, tree)``
        pickle it returns is decoded under an unpickler that admits only the
        three Lark globals a genuine payload names; a forged payload is a
        harness fault and is never constructed."""
        import pickle
        import subprocess
        import sys

        import hcl2

        from elt_taskgen.training import terraform_intent as ti

        text = 'variable "a" {\n  default = 1\n}\n'
        genuine = pickle.dumps(
            (hcl2.loads(text), hcl2.parses(text)), protocol=pickle.HIGHEST_PROTOCOL
        )
        document, tree = ti._load_parse_payload(genuine)
        self.assertEqual(document, hcl2.loads(text))
        self.assertEqual(tree, hcl2.parses(text))
        self.assertEqual(
            ti._PARSE_PAYLOAD_GLOBALS,
            frozenset({("lark.tree", "Tree"), ("lark.tree", "Meta"), ("lark.lexer", "Token")}),
        )
        # The production path decodes through the restricted unpickler only.
        self.assertNotIn("pickle.loads", inspect.getsource(ti._HclParseWorker))
        self.assertIn("_load_parse_payload", inspect.getsource(ti._HclParseWorker))

        sentinel = self.root / "forged-payload-ran"

        class Forged:
            def __reduce__(self):
                return (open, (str(sentinel), "w"))

        forged = pickle.dumps(({}, Forged()), protocol=pickle.HIGHEST_PROTOCOL)
        with self.assertRaisesRegex(pickle.UnpicklingError, "forbidden global"):
            ti._load_parse_payload(forged)
        self.assertFalse(sentinel.exists())

        # Through the worker head: a worker that answers with a forged payload
        # is a harness fault (exit 2 territory), never a candidate code, and
        # the forged constructor never runs in the parent.
        forging_worker = (
            "import pickle, struct, sys\n"
            "stdin = sys.stdin.buffer\n"
            "stdout = sys.stdout.buffer\n"
            # The ready marker first: the parent arms no deadline and sends
            # no request before it (a worker that stays silent is a slow
            # START, a harness fault, and never receives the request).
            "stdout.write(b'K')\n"
            "stdout.flush()\n"
            "header = stdin.read(4)\n"
            "stdin.read(struct.unpack('>I', header)[0])\n"
            "class Forged:\n"
            "    def __reduce__(self):\n"
            f"        return (open, ({str(sentinel)!r}, 'w'))\n"
            "payload = pickle.dumps(({}, Forged()), protocol=pickle.HIGHEST_PROTOCOL)\n"
            "stdout.write(b'R' + struct.pack('>Q', len(payload)) + payload)\n"
            "stdout.flush()\n"
            "stdin.read()\n"
        )
        worker = ti._HclParseWorker()
        self.addCleanup(worker.shutdown)

        def start() -> subprocess.Popen:
            return subprocess.Popen(
                [sys.executable, "-I", "-c", forging_worker],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        with unittest.mock.patch.object(worker, "_start", start):
            with self.assertRaises(ti.TerraformIntentHarnessError):
                worker.parse(text, deadline_seconds=10.0)
        self.assertFalse(sentinel.exists())
        # The forging worker was discarded; the next call starts a real one.
        self.assertIsNone(worker._process)
        document, tree = worker.parse(text, deadline_seconds=10.0)
        self.assertEqual(document, hcl2.loads(text))

    def test_slow_parse_worker_start_is_a_harness_fault_never_zero_reward(self) -> None:
        """Taxonomy §1 / C7: a parse worker's START (interpreter spawn plus
        ``import hcl2``) runs on the HARNESS clock. The candidate's 10 s
        deadline is armed only after the worker's one-byte ready marker is
        read, so a slow start under contention is never the candidate's
        ``terraform_parse_timeout`` (reward 0.0); a start that misses the
        harness's own bound is ``TerraformIntentHarnessError``, which
        ``evaluate_terraform_intent`` propagates and the scorer's
        trusted-failure path turns into reward ``None`` (HARNESS_INTERNAL),
        never 0.0."""
        import subprocess
        import sys
        import time

        from elt_taskgen.training import scorer
        from elt_taskgen.training import terraform_intent as ti
        from elt_taskgen.training.contract import (
            WORKSPACE_ERROR_CLASS_BY_CODE,
            WorkspaceErrorCode,
        )

        self.assertEqual(ti.TERRAFORM_PARSE_WORKER_STARTUP_DEADLINE_SECONDS, 60.0)
        self.assertEqual(ti._PARSE_WORKER_READY, b"K")
        # The worker signals readiness only after hcl2 is imported and
        # before its first read of a request.
        source = ti._HCL_PARSE_WORKER_SOURCE
        ready_at = source.index("stdout.write(b'K')")
        self.assertLess(source.index("import hcl2"), ready_at)
        self.assertLess(ready_at, source.index("while True:"))
        # The candidate clock is armed after the ready wait, never before.
        parse_source = inspect.getsource(ti._HclParseWorker.parse)
        self.assertLess(
            parse_source.index("_await_ready"),
            parse_source.index("deadline = time.monotonic() + deadline_seconds"),
        )
        with self.assertRaises(ValueError):
            ti._HclParseWorker(startup_deadline_seconds=0.0)

        limit = str(ti.TERRAFORM_PARSE_WORKER_RSS_LIMIT_MB * 1024 * 1024)

        def slow_start(delay: float):
            def start() -> subprocess.Popen:
                return subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        f"import time\ntime.sleep({delay!r})\n" + source,
                        limit,
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

            return start

        text = 'variable "a" {\n  default = 1\n}\n'
        # 1. A start SLOWER than the candidate deadline still parses: the
        # candidate clock had not started while the worker came up.
        worker = ti._HclParseWorker()
        self.addCleanup(worker.shutdown)
        with unittest.mock.patch.object(worker, "_start", slow_start(1.0)):
            started = time.monotonic()
            document, _tree = worker.parse(text, deadline_seconds=0.3)
        self.assertGreater(time.monotonic() - started, 0.9)
        self.assertEqual(document, {"variable": [{"a": {"default": 1}}]})
        # The warm worker is reused and a second call pays no start.
        document, _tree = worker.parse(text, deadline_seconds=0.3)
        self.assertEqual(document, {"variable": [{"a": {"default": 1}}]})

        # 2. A start that misses the HARNESS start-up bound is a harness
        # fault: never PARSE_TIMEOUT, never a candidate code, the late worker
        # is discarded, and the wait is the start-up bound, not the 10 s.
        slow = ti._HclParseWorker(startup_deadline_seconds=0.5)
        self.addCleanup(slow.shutdown)
        with unittest.mock.patch.object(slow, "_start", slow_start(5.0)):
            started = time.monotonic()
            with self.assertRaises(ti.TerraformIntentHarnessError) as raised:
                slow.parse(text, deadline_seconds=10.0)
            elapsed = time.monotonic() - started
        self.assertNotIsInstance(raised.exception, ti.TerraformIntentError)
        self.assertLess(elapsed, 3.0)
        self.assertIsNone(slow._process)

        # 3. Through the public head: propagated as the harness's, never a
        # `TerraformIntentEvaluation` with reward 0.0 ...
        path = self.root / "main.tf"
        path.write_text(_valid_hcl(Destination.SNOWFLAKE), encoding="utf-8")
        with unittest.mock.patch.object(ti, "_PARSE_WORKER", slow), unittest.mock.patch.object(
            slow, "_start", slow_start(5.0)
        ):
            with self.assertRaises(ti.TerraformIntentHarnessError):
                evaluate_terraform_intent(_package(Destination.SNOWFLAKE), path)
        # ... and the scorer's trusted-failure path makes it reward None.
        code = scorer._trusted_failure_code(ti.TerraformIntentHarnessError("slow start"))
        self.assertIs(code, WorkspaceErrorCode.HARNESS_INTERNAL)
        self.assertFalse(WORKSPACE_ERROR_CLASS_BY_CODE[code].label_eligible)
        # 4. A slow start that came up within the harness bound scores the
        # real candidate normally.
        worker.shutdown()  # force a fresh (slow) start on the next call
        with unittest.mock.patch.object(ti, "_PARSE_WORKER", worker), unittest.mock.patch.object(
            worker, "_start", slow_start(1.0)
        ):
            result = evaluate_terraform_intent(_package(Destination.SNOWFLAKE), path)
        self.assertEqual(result.reward, 1.0)
        self.assertEqual(result.error_codes, ())

    def test_unreadable_candidate_main_tf_is_a_harness_fault_not_parse(self) -> None:
        """The bounded reader's refusals split by who caused them. The
        CANDIDATE's ``PARSE``: a missing, non-regular, symlinked, oversized,
        changed-under-read or non-UTF-8 ``main.tf``. The HARNESS's fault: a
        regular file the reader could not stat or read (EACCES, EIO, an
        ENOENT race — the reader's ``WORKSPACE_NON_REGULAR_FILE`` for any
        non-ELOOP ``OSError``), which says nothing about the artifact and
        must never be scored 0.0."""
        import errno
        import os

        from elt_taskgen.training import terraform_intent as ti
        from elt_taskgen.training.contract import WorkspaceErrorCode
        from elt_taskgen.training.workspace import WorkspaceLifecycleError

        package = _package(Destination.SNOWFLAKE)
        path = self.root / "main.tf"
        path.write_text(_valid_hcl(Destination.SNOWFLAKE), encoding="utf-8")
        self.assertEqual(evaluate_terraform_intent(package, path).reward, 1.0)

        # The candidate's: the reader's own artifact refusals ...
        for code in sorted(ti._CANDIDATE_READ_CODES, key=lambda c: c.value):
            with self.subTest(code=code.value), unittest.mock.patch.object(
                ti,
                "_read_bounded_regular_file",
                side_effect=WorkspaceLifecycleError(code, "refused"),
            ):
                result = evaluate_terraform_intent(package, path)
                self.assertEqual(result.error_codes, (TerraformIntentErrorCode.PARSE,))
        self.assertEqual(
            ti._CANDIDATE_READ_CODES,
            frozenset(
                {
                    WorkspaceErrorCode.WORKSPACE_SIZE_LIMIT,
                    WorkspaceErrorCode.WORKSPACE_SYMLINK,
                    WorkspaceErrorCode.WORKSPACE_DIGEST_MISMATCH,
                }
            ),
        )
        # ... a missing file, a directory, and bytes that are not UTF-8.
        result = evaluate_terraform_intent(package, self.root / "absent.tf")
        self.assertEqual(result.error_codes, (TerraformIntentErrorCode.PARSE,))
        result = evaluate_terraform_intent(package, self.root)
        self.assertEqual(result.error_codes, (TerraformIntentErrorCode.PARSE,))
        binary = self.root / "binary.tf"
        binary.write_bytes(b"variable \xff\xfe {}\n")
        result = evaluate_terraform_intent(package, binary)
        self.assertEqual(result.error_codes, (TerraformIntentErrorCode.PARSE,))

        # The harness's: the reader could not read a regular file ...
        with unittest.mock.patch.object(
            ti,
            "_read_bounded_regular_file",
            side_effect=WorkspaceLifecycleError(
                WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
                "candidate main.tf could not be read",
            ),
        ):
            with self.assertRaises(TerraformIntentHarnessError):
                evaluate_terraform_intent(package, path)
        with unittest.mock.patch.object(
            ti, "_read_bounded_regular_file", side_effect=OSError(errno.EIO, "io error")
        ):
            with self.assertRaises(TerraformIntentHarnessError):
                evaluate_terraform_intent(package, path)

        # ... or could not even stat it.
        class _Unstatable(type(path)):
            def is_symlink(self) -> bool:
                raise PermissionError(errno.EACCES, "denied")

        with self.assertRaises(TerraformIntentHarnessError):
            evaluate_terraform_intent(package, _Unstatable(path))

        # The real thing: EACCES at open on a regular file the harness owns
        # (root reads everything, so only when the test is not root).
        geteuid = getattr(os, "geteuid", None)
        if geteuid is not None and geteuid() != 0:
            locked = self.root / "locked.tf"
            locked.write_text(_valid_hcl(Destination.SNOWFLAKE), encoding="utf-8")
            locked.chmod(0)
            self.addCleanup(locked.chmod, 0o600)
            with self.assertRaises(TerraformIntentHarnessError):
                evaluate_terraform_intent(package, locked)
            # The ELOOP case stays the candidate's through the same reader.
            link = self.root / "link.tf"
            os.symlink(path, link)
            result = evaluate_terraform_intent(package, link)
            self.assertEqual(result.error_codes, (TerraformIntentErrorCode.PARSE,))


if __name__ == "__main__":
    unittest.main()

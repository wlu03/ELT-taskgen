"""Private pinned connector JSON compiled from upstream-shaped config."""

from __future__ import annotations

import copy
import unittest

from elt_taskgen import demo_fixture
from elt_taskgen.airbyte_connector_config import (
    AIRBYTE_CONNECTOR_VARIABLE,
    AIRBYTE_TERRAFORM_PROVIDER_VERSION,
    build_airbyte_connector_contract,
    build_airbyte_connector_tfvars,
)
from elt_taskgen.destinations import Destination, destination_contract
from elt_taskgen.export.eltbench import build_config
from elt_taskgen.models import Backend, BackendAssignment


class AirbyteConnectorConfigTests(unittest.TestCase):
    def test_snowflake_uses_exact_nested_credentials_and_decimal_type(self) -> None:
        config = build_config(demo_fixture.demo_task(), destination="snowflake")
        installed = copy.deepcopy(config)
        installed["Airbyte"]["config"].update(
            {
                "server_url": "https://airbyte.example/api/public/v1/",
                "workspace_id": "workspace-1",
                "client_id": "client-1",
                "client_secret": "client-secret",
            }
        )
        installed["snowflake"]["config"].update(
            {
                "account": "org-account",
                "username": "solver",
                "password": "warehouse-secret",
                "role": "AIRBYTE_ROLE",
                "warehouse": "AIRBYTE_WAREHOUSE",
            }
        )

        contract = build_airbyte_connector_contract(installed)
        destination = contract["destination"]

        self.assertEqual(
            contract["terraform_provider"]["version"],
            AIRBYTE_TERRAFORM_PROVIDER_VERSION,
        )
        self.assertEqual(destination["connector_version"], "4.1.2")
        self.assertEqual(
            destination["configuration"]["host"],
            "org-account.snowflakecomputing.com",
        )
        self.assertEqual(
            destination["configuration"]["number_data_type"], "NUMBER(38,9)"
        )
        self.assertNotIn("password", destination["configuration"])
        self.assertEqual(
            destination["configuration"]["credentials"],
            {
                "auth_type": "Username and Password",
                "password": "warehouse-secret",
            },
        )

    def test_source_abstractions_map_to_connector_json_and_connections(self) -> None:
        config = build_config(demo_fixture.demo_task())
        sources = {
            source["key"]: source
            for source in build_airbyte_connector_contract(config)["sources"]
        }

        postgres = sources["postgres"]
        self.assertEqual(postgres["connector_version"], "3.8.5")
        self.assertEqual(postgres["configuration"]["schemas"], ["public"])
        self.assertEqual(postgres["configuration"]["ssl_mode"], {"mode": "disable"})
        self.assertEqual(
            postgres["connection"]["configurations"]["streams"],
            [{"name": "customers", "sync_mode": "full_refresh_append"}],
        )

        mongodb = sources["mongodb"]
        self.assertEqual(mongodb["connector_version"], "2.0.7")
        self.assertEqual(
            mongodb["configuration"]["database_config"],
            {
                "cluster_type": "SELF_MANAGED_REPLICA_SET",
                "connection_string": (
                    "mongodb://elt-mongodb:27017/?directConnection=true"
                ),
                "databases": ["demo__customer_summary"],
                "schema_enforced": True,
            },
        )

        file_source = sources["file_order_items"]
        self.assertEqual(file_source["connector_version"], "0.6.0")
        self.assertEqual(
            file_source["configuration"]["provider"], {"storage": "HTTPS"}
        )

    def test_s3_parts_become_one_pinned_jsonl_source(self) -> None:
        base = demo_fixture.demo_task()
        task = base.model_copy(
            update={
                "backends": tuple(
                    BackendAssignment(table=table.name, backend=Backend.S3)
                    for table in base.tables
                )
            }
        )
        config = build_config(task)
        source = build_airbyte_connector_contract(config)["sources"][0]

        self.assertEqual(source["key"], "aws_s3")
        self.assertEqual(source["connector_version"], "4.15.20")
        self.assertEqual(
            [stream["format"] for stream in source["configuration"]["streams"]],
            [{"filetype": "jsonl"}] * len(base.tables),
        )
        self.assertEqual(
            source["configuration"]["delivery_method"],
            {"delivery_type": "use_records_transfer"},
        )
        self.assertTrue(
            all(
                stream["globs"][0].endswith(".jsonl")
                for stream in source["configuration"]["streams"]
            )
        )

    def test_private_tfvars_wrapper_keeps_the_compiled_contract_nested(self) -> None:
        tfvars = build_airbyte_connector_tfvars(build_config(demo_fixture.demo_task()))
        self.assertEqual(set(tfvars), {AIRBYTE_CONNECTOR_VARIABLE})
        self.assertEqual(tfvars[AIRBYTE_CONNECTOR_VARIABLE]["workspace_id"], "")

    def test_public_version_metadata_or_sync_mode_drift_fails_closed(self) -> None:
        config = build_config(demo_fixture.demo_task())
        config["Airbyte"]["config"]["postgres_connector_version"] = "3.8.3"
        with self.assertRaisesRegex(ValueError, "must not expose"):
            build_airbyte_connector_contract(config)

        config = build_config(demo_fixture.demo_task())
        config["postgres"]["config"]["sync_mode"] = "incremental_append"
        with self.assertRaisesRegex(ValueError, "unsupported Airbyte sync mode"):
            build_airbyte_connector_contract(config)

    def test_destination_definition_is_verified_before_private_pins_are_added(
        self,
    ) -> None:
        mutations = (
            lambda airbyte: airbyte.pop("snowflake_definition_id"),
            lambda airbyte: airbyte.__setitem__("snowflake_definition_id", "wrong"),
            lambda airbyte: airbyte.__setitem__(
                "databricks_definition_id",
                "072d5540-f236-4294-ba7c-ade8fd918496",
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                config = build_config(demo_fixture.demo_task())
                mutate(config["Airbyte"]["config"])
                with self.assertRaisesRegex(
                    ValueError, "destination.*(definition key|definition id)"
                ):
                    build_airbyte_connector_contract(config)

    def test_each_destination_definition_id_fails_closed_on_drift(self) -> None:
        for destination in Destination:
            contract = destination_contract(destination)
            for mutation in (None, "wrong-definition-id"):
                with self.subTest(
                    destination=destination.value,
                    mutation=mutation,
                ):
                    config = build_config(
                        demo_fixture.demo_task(), destination=destination
                    )
                    airbyte = config["Airbyte"]["config"]
                    if mutation is None:
                        airbyte.pop(contract.definition_key)
                    else:
                        airbyte[contract.definition_key] = mutation
                    with self.assertRaisesRegex(
                        ValueError, "destination.*(definition key|definition id)"
                    ):
                        build_airbyte_connector_contract(config)

    def test_orphaned_source_connector_identity_fails_closed(self) -> None:
        config = build_config(demo_fixture.demo_task())
        del config["postgres"]
        with self.assertRaisesRegex(ValueError, "postgres source section"):
            build_airbyte_connector_contract(config)

    def test_original_databricks_shape_compiles_to_modern_pinned_json(self) -> None:
        config = build_config(
            demo_fixture.demo_task(), destination=Destination.DATABRICKS
        )
        values = config["databricks"]["config"]
        values.update(
            {
                "database": "benchmark_catalog",
                "hostname": "workspace.cloud.databricks.com",
                "http_path": "/sql/1.0/warehouses/abc",
                "client_id": "solver-client",
                "secret": "solver-secret",
            }
        )
        destination = build_airbyte_connector_contract(config)["destination"]
        self.assertEqual(destination["connector_version"], "4.0.2")
        self.assertEqual(
            destination["configuration"],
            {
                "accept_terms": True,
                "authentication": {
                    "auth_type": "OAUTH",
                    "client_id": "solver-client",
                    "secret": "solver-secret",
                },
                "database": "benchmark_catalog",
                "hostname": "workspace.cloud.databricks.com",
                "http_path": "/sql/1.0/warehouses/abc",
                "port": "443",
                "purge_staging_data": True,
                "schema": values["schema"],
            },
        )

    def test_original_redshift_shape_compiles_to_modern_pinned_json(self) -> None:
        config = build_config(
            demo_fixture.demo_task(), destination=Destination.REDSHIFT
        )
        values = config["redshift"]["config"]
        values.update(
            {
                "database": "benchmark_database",
                "host": "cluster.example",
                "password": "warehouse-secret",
                "port": 5439,
                "username": "solver",
                "s3_bucket_name": "benchmark-staging",
                "s3_bucket_region": "us-west-2",
                "access_key_id": "AKIAEXAMPLE",
                "secret_access_key": "aws-secret",
            }
        )
        destination = build_airbyte_connector_contract(config)["destination"]
        compiled = destination["configuration"]
        self.assertEqual(destination["connector_version"], "4.0.7")
        self.assertEqual(
            compiled,
            {
                "database": "benchmark_database",
                "drop_cascade": False,
                "host": "cluster.example",
                "password": "warehouse-secret",
                "port": 5439,
                "schema": values["schema"],
                "uploading_method": {
                    "access_key_id": "AKIAEXAMPLE",
                    "method": "S3 Staging",
                    "purge_staging_data": True,
                    "s3_bucket_name": "benchmark-staging",
                    "s3_bucket_path": f"elt-bench/{values['schema']}",
                    "s3_bucket_region": "us-west-2",
                    "secret_access_key": "aws-secret",
                },
                "username": "solver",
            },
        )

    def test_malformed_destination_shapes_fail_closed(self) -> None:
        databricks = build_config(
            demo_fixture.demo_task(), destination=Destination.DATABRICKS
        )
        databricks["databricks"]["config"]["authentication"] = {
            "auth_type": "OAUTH"
        }
        with self.assertRaisesRegex(ValueError, "original human-facing shape"):
            build_airbyte_connector_contract(databricks)

        redshift = build_config(
            demo_fixture.demo_task(), destination=Destination.REDSHIFT
        )
        redshift["redshift"]["config"]["uploading_method"] = {}
        with self.assertRaisesRegex(ValueError, "original human-facing shape"):
            build_airbyte_connector_contract(redshift)

        redshift = build_config(
            demo_fixture.demo_task(), destination=Destination.REDSHIFT
        )
        redshift["redshift"]["config"]["port"] = 5439.0
        with self.assertRaisesRegex(ValueError, "port must be an integer"):
            build_airbyte_connector_contract(redshift)

        snowflake = build_config(demo_fixture.demo_task())
        snowflake["snowflake"]["config"]["account"] = "https://wrong.example/path"
        with self.assertRaisesRegex(ValueError, "connector host"):
            build_airbyte_connector_contract(snowflake)

    def test_namespace_policy_drift_fails_closed(self) -> None:
        config = build_config(demo_fixture.demo_task())
        config["Airbyte"]["config"]["namespace_definition"] = "source"
        with self.assertRaisesRegex(ValueError, "namespace definition"):
            build_airbyte_connector_contract(config)


if __name__ == "__main__":
    unittest.main()

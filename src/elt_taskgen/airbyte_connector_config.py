"""Compile public ELT-Bench config into JSON for pinned Airbyte connectors.

Connector versions remain private harness constants. This module creates no
Terraform resources or public files.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from elt_taskgen.destinations import (
    SOURCE_CONNECTOR_CONTRACTS,
    Destination,
    destination_contract,
    destination_from_config,
)


# The public scaffold mirrors original ELT-Bench exactly. Connector JSON below
# is private harness metadata; no auto-tfvars file is shipped to the solver.
AIRBYTE_TERRAFORM_PROVIDER_VERSION = "0.6.5"
AIRBYTE_CONNECTOR_TFVARS_FILENAME = "connector_config.auto.tfvars.json"
AIRBYTE_CONNECTOR_VARIABLE = "airbyte_connector_contract"
AIRBYTE_CONNECTOR_CONTRACT_SCHEMA_VERSION = "1.0"


def _mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def _text(value: object, *, label: str, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        suffix = " string" if allow_empty else " non-empty string"
        raise ValueError(f"{label} must be a{suffix}")
    return value


def _tables(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must contain at least one table")
    tables = [_text(item, label=f"{label} table", allow_empty=False) for item in value]
    if len(tables) != len(set(tables)):
        raise ValueError(f"{label} repeats a table")
    return tables


def _streams(tables: list[str], sync_mode: object) -> list[dict[str, str]]:
    mode = _text(sync_mode, label="sync_mode", allow_empty=False)
    if mode != "full_refresh_append":
        raise ValueError(f"unsupported Airbyte sync mode {mode!r}")
    return [{"name": table, "sync_mode": mode} for table in tables]


def _source_identity(
    airbyte: Mapping[str, Any], section: str
) -> tuple[str, str]:
    contract = SOURCE_CONNECTOR_CONTRACTS[section]
    definition_id = _text(
        airbyte.get(contract.definition_key),
        label=contract.definition_key,
        allow_empty=False,
    )
    if definition_id != contract.definition_id:
        raise ValueError(f"{section} definition id does not match the pinned contract")
    return definition_id, contract.connector_version


def _validate_connector_identities(
    config: Mapping[str, Any],
    airbyte: Mapping[str, Any],
    destination: Destination,
) -> None:
    """Validate the original public identity shape before private compilation."""

    selected = destination_contract(destination)
    destination_definition_keys = {
        destination_contract(item).definition_key for item in Destination
    }
    present_destination_definition_keys = (
        destination_definition_keys & set(airbyte)
    )
    if present_destination_definition_keys != {selected.definition_key}:
        raise ValueError(
            "Airbyte.config must contain exactly the selected destination "
            "definition key"
        )
    if airbyte.get(selected.definition_key) != selected.definition_id:
        raise ValueError("destination definition id does not match pinned contract")

    source_definition_keys = {
        contract.definition_key for contract in SOURCE_CONNECTOR_CONTRACTS.values()
    }
    for section, contract in SOURCE_CONNECTOR_CONTRACTS.items():
        section_present = contract.config_section in config
        definition_present = contract.definition_key in airbyte
        if section_present != definition_present:
            raise ValueError(
                f"{section} source section and connector identity disagree"
            )
        if section_present:
            _source_identity(airbyte, section)

    unexpected_definition_keys = sorted(
        key
        for key in airbyte
        if key.endswith("_definition_id")
        and key not in destination_definition_keys
        and key not in source_definition_keys
        and key != "custom_api_definition_id"
    )
    if unexpected_definition_keys:
        raise ValueError(
            "Airbyte.config contains unregistered connector definition keys: "
            + ", ".join(unexpected_definition_keys)
        )
    public_version_keys = sorted(
        key for key in airbyte if key.endswith("_connector_version")
    )
    if public_version_keys:
        raise ValueError(
            "Airbyte.config must not expose harness connector-version metadata: "
            + ", ".join(public_version_keys)
        )

    custom_api_present = "custom_api" in config
    custom_definition_present = "custom_api_definition_id" in airbyte
    if custom_api_present != custom_definition_present:
        raise ValueError(
            "custom_api source section and connector definition disagree"
        )


def _source_record(
    *,
    key: str,
    definition_id: str,
    connector_version: str | None,
    configuration: Mapping[str, Any],
    streams: list[dict[str, str]],
    definition_kind: str = "built_in",
) -> dict[str, Any]:
    return {
        "key": key,
        "name": f"ELT-Bench {key}",
        "definition_kind": definition_kind,
        "definition_id": definition_id,
        "connector_version": connector_version,
        "configuration": dict(configuration),
        "connection": {
            "namespace_definition": "destination",
            "configurations": {"streams": streams},
        },
    }


def _postgres_source(config: Mapping[str, Any], airbyte: Mapping[str, Any]) -> dict:
    section = _mapping(config["postgres"], label="postgres")
    values = _mapping(section.get("config"), label="postgres.config")
    tables = _tables(values.get("tables"), label="postgres.config.tables")
    definition_id, version = _source_identity(airbyte, "postgres")
    configuration = {
        "host": _text(values.get("host"), label="postgres host", allow_empty=False),
        "port": int(values.get("port")),
        "database": _text(
            values.get("database"), label="postgres database", allow_empty=False
        ),
        "username": _text(
            values.get("user"), label="postgres user", allow_empty=False
        ),
        "password": _text(values.get("password"), label="postgres password"),
        "schemas": [
            _text(values.get("schema"), label="postgres schema", allow_empty=False)
        ],
        # The benchmark-owned Docker source is intentionally plaintext and has
        # no SSH hop. Explicit values avoid connector-default drift.
        "ssl_mode": {"mode": "disable"},
        "tunnel_method": {"tunnel_method": "NO_TUNNEL"},
        "replication_method": {"method": "Standard"},
    }
    return _source_record(
        key="postgres",
        definition_id=definition_id,
        connector_version=version,
        configuration=configuration,
        streams=_streams(tables, values.get("sync_mode")),
    )


def _mongodb_source(config: Mapping[str, Any], airbyte: Mapping[str, Any]) -> dict:
    section = _mapping(config["mongodb"], label="mongodb")
    values = _mapping(section.get("config"), label="mongodb.config")
    tables = _tables(values.get("tables"), label="mongodb.config.tables")
    definition_id, version = _source_identity(airbyte, "mongodb")
    configuration = {
        "database_config": {
            "cluster_type": "SELF_MANAGED_REPLICA_SET",
            "connection_string": _text(
                values.get("connection_string"),
                label="mongodb connection_string",
                allow_empty=False,
            ),
            "databases": [
                _text(
                    values.get("database"),
                    label="mongodb database",
                    allow_empty=False,
                )
            ],
            "schema_enforced": True,
        }
    }
    return _source_record(
        key="mongodb",
        definition_id=definition_id,
        connector_version=version,
        configuration=configuration,
        streams=_streams(tables, values.get("sync_mode")),
    )


def _custom_api_source(config: Mapping[str, Any], airbyte: Mapping[str, Any]) -> dict:
    section = _mapping(config["custom_api"], label="custom_api")
    values = _mapping(section.get("config"), label="custom_api.config")
    tables = _tables(values.get("tables"), label="custom_api.config.tables")
    definition_id = _text(
        airbyte.get("custom_api_definition_id"),
        label="custom_api_definition_id",
    )
    return _source_record(
        key="custom_api",
        definition_id=definition_id,
        connector_version=None,
        configuration=_mapping(
            values.get("configuration"), label="custom_api configuration"
        ),
        streams=_streams(tables, values.get("sync_mode")),
        definition_kind="workspace_declarative",
    )


def _s3_source(config: Mapping[str, Any], airbyte: Mapping[str, Any]) -> dict:
    section = _mapping(config["aws_s3"], label="aws_s3")
    entries = section.get("data")
    if not isinstance(entries, list) or not entries:
        raise ValueError("aws_s3.data must contain at least one stream")
    buckets: set[str] = set()
    streams_config: list[dict[str, Any]] = []
    connection_streams: list[dict[str, str]] = []
    for raw in entries:
        entry = _mapping(raw, label="aws_s3.data entry")
        table = _text(entry.get("table"), label="s3 table", allow_empty=False)
        parsed = urlsplit(_text(entry.get("path"), label="s3 path", allow_empty=False))
        if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.startswith("/"):
            raise ValueError(f"invalid S3 source path for table {table!r}")
        buckets.add(parsed.netloc)
        glob = parsed.path.lstrip("/")
        streams_config.append(
            {
                "name": table,
                "format": {"filetype": "jsonl"},
                "globs": [glob],
            }
        )
        connection_streams.extend(_streams([table], entry.get("sync_mode")))
    if len(buckets) != 1:
        raise ValueError("one aws_s3 source section must resolve to exactly one bucket")
    definition_id, version = _source_identity(airbyte, "aws_s3")
    configuration = {
        "aws_access_key_id": _text(
            section.get("AWS_ACCESS_KEY_ID"), label="S3 access key"
        ),
        "aws_secret_access_key": _text(
            section.get("AWS_SECRET_ACCESS_KEY"), label="S3 secret key"
        ),
        "endpoint": _text(
            section.get("AWS_ENDPOINT_URL"), label="S3 endpoint", allow_empty=False
        ),
        "region_name": _text(
            section.get("AWS_DEFAULT_REGION"), label="S3 region", allow_empty=False
        ),
        "bucket": next(iter(buckets)),
        "delivery_method": {"delivery_type": "use_records_transfer"},
        "streams": streams_config,
    }
    return _source_record(
        key="aws_s3",
        definition_id=definition_id,
        connector_version=version,
        configuration=configuration,
        streams=connection_streams,
    )


def _file_sources(config: Mapping[str, Any], airbyte: Mapping[str, Any]) -> list[dict]:
    entries = config["flat_files"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("flat_files must contain at least one entry")
    definition_id, version = _source_identity(airbyte, "flat_files")
    records: list[dict] = []
    for raw in entries:
        entry = _mapping(raw, label="flat_files entry")
        table = _text(entry.get("table"), label="file table", allow_empty=False)
        file_format = _text(
            entry.get("format"), label="file format", allow_empty=False
        )
        if file_format not in {
            "csv",
            "excel",
            "excel_binary",
            "feather",
            "fwf",
            "json",
            "jsonl",
            "parquet",
            "yaml",
        }:
            raise ValueError(f"unsupported Files connector format {file_format!r}")
        url = _text(entry.get("path"), label="file URL", allow_empty=False)
        parsed_url = urlsplit(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError(f"invalid public Files connector URL {url!r}")
        records.append(
            _source_record(
                key=f"file_{table}",
                definition_id=definition_id,
                connector_version=version,
                configuration={
                    "dataset_name": table,
                    "format": file_format,
                    "provider": {"storage": "HTTPS"},
                    "url": url,
                },
                streams=_streams([table], entry.get("sync_mode")),
            )
        )
    return records


def _snowflake_configuration(values: Mapping[str, Any]) -> dict[str, Any]:
    account = _text(values.get("account"), label="snowflake account")
    host = account
    if account and not account.endswith(".snowflakecomputing.com"):
        host = f"{account}.snowflakecomputing.com"
    if host and (
        "://" in host
        or "/" in host
        or any(character.isspace() for character in host)
        or not host.endswith(".snowflakecomputing.com")
    ):
        raise ValueError("snowflake account cannot be mapped to a connector host")
    return {
        "host": host,
        "role": _text(values.get("role"), label="snowflake role"),
        "warehouse": _text(values.get("warehouse"), label="snowflake warehouse"),
        "database": _text(
            values.get("database"), label="snowflake database", allow_empty=False
        ),
        "schema": _text(
            values.get("schema"), label="snowflake schema", allow_empty=False
        ),
        "username": _text(values.get("username"), label="snowflake username"),
        "number_data_type": "NUMBER(38,9)",
        "credentials": {
            "auth_type": "Username and Password",
            "password": _text(values.get("password"), label="snowflake password"),
        },
    }


def _databricks_configuration(values: Mapping[str, Any]) -> dict[str, Any]:
    """Translate the original flat config into destination-databricks 4.0.2."""

    modern_only = {
        "accept_terms",
        "authentication",
        "cdc_deletion_mode",
        "port",
        "purge_staging_data",
    } & set(values)
    if modern_only:
        raise ValueError(
            "Databricks public config must use the original human-facing shape"
        )
    return {
        "accept_terms": True,
        "authentication": {
            "auth_type": "OAUTH",
            "client_id": _text(
                values.get("client_id"), label="Databricks client_id"
            ),
            "secret": _text(
                values.get("secret"), label="Databricks OAuth secret"
            ),
        },
        "database": _text(values.get("database"), label="Databricks database"),
        "hostname": _text(values.get("hostname"), label="Databricks hostname"),
        "http_path": _text(values.get("http_path"), label="Databricks http_path"),
        "port": "443",
        "purge_staging_data": True,
        "schema": _text(
            values.get("schema"), label="Databricks schema", allow_empty=False
        ),
    }


def _redshift_configuration(values: Mapping[str, Any]) -> dict[str, Any]:
    """Translate the original flat config into destination-redshift 4.0.7."""

    if "drop_cascade" in values or "uploading_method" in values:
        raise ValueError(
            "Redshift public config must use the original human-facing shape"
        )

    raw_port = values.get("port")
    if isinstance(raw_port, bool) or not isinstance(raw_port, int):
        raise ValueError("Redshift port must be an integer from 1 to 65535")
    port = raw_port
    if not 1 <= port <= 65535:
        raise ValueError("Redshift port must be an integer from 1 to 65535")
    database = _text(values.get("database"), label="Redshift database")
    schema = _text(
        values.get("schema"), label="Redshift schema", allow_empty=False
    )
    if "/" in schema or any(
        character.isspace() or ord(character) < 32 for character in schema
    ):
        raise ValueError("Redshift schema cannot form a safe staging prefix")
    return {
        "database": database,
        "drop_cascade": False,
        "host": _text(values.get("host"), label="Redshift host"),
        "password": _text(values.get("password"), label="Redshift password"),
        "port": port,
        "schema": schema,
        "uploading_method": {
            "access_key_id": _text(
                values.get("access_key_id"), label="Redshift S3 access key"
            ),
            "method": "S3 Staging",
            "purge_staging_data": True,
            "s3_bucket_name": _text(
                values.get("s3_bucket_name"), label="Redshift S3 bucket"
            ),
    # Runtime uses this default because upstream has no public bucket-path field.
            "s3_bucket_path": f"elt-bench/{schema}",
            "s3_bucket_region": _text(
                values.get("s3_bucket_region"), label="Redshift S3 bucket region"
            ),
            "secret_access_key": _text(
                values.get("secret_access_key"), label="Redshift S3 secret key"
            ),
        },
        "username": _text(values.get("username"), label="Redshift username"),
    }


def _destination_configuration(
    destination: Destination, values: Mapping[str, Any]
) -> dict[str, Any]:
    if destination is Destination.SNOWFLAKE:
        return _snowflake_configuration(values)
    if destination is Destination.DATABRICKS:
        return _databricks_configuration(values)
    if destination is Destination.REDSHIFT:
        return _redshift_configuration(values)
    raise ValueError(f"unsupported Airbyte destination mapping {destination!r}")


def build_airbyte_connector_contract(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return exact generic-provider inputs for one public or installed task."""

    root = _mapping(config, label="config")
    airbyte_section = _mapping(root.get("Airbyte"), label="Airbyte")
    airbyte = _mapping(airbyte_section.get("config"), label="Airbyte.config")
    namespace_definition = _text(
        airbyte.get("namespace_definition"),
        label="namespace_definition",
        allow_empty=False,
    )
    if namespace_definition != "destination":
        raise ValueError(
            "unsupported Airbyte namespace definition "
            f"{namespace_definition!r}; expected 'destination'"
        )
    destination = destination_from_config(root)
    contract = destination_contract(destination)
    _validate_connector_identities(root, airbyte, destination)
    destination_section = _mapping(
        root.get(contract.config_section), label=contract.config_section
    )
    destination_values = _mapping(
        destination_section.get("config"),
        label=f"{contract.config_section}.config",
    )
    definition_id = contract.definition_id

    sources: list[dict] = []
    if "postgres" in root:
        sources.append(_postgres_source(root, airbyte))
    if "mongodb" in root:
        sources.append(_mongodb_source(root, airbyte))
    if "custom_api" in root:
        sources.append(_custom_api_source(root, airbyte))
    if "aws_s3" in root:
        sources.append(_s3_source(root, airbyte))
    if "flat_files" in root:
        sources.extend(_file_sources(root, airbyte))
    if not sources:
        raise ValueError("Airbyte connector contract has no sources")

    return {
        "schema_version": AIRBYTE_CONNECTOR_CONTRACT_SCHEMA_VERSION,
        "terraform_provider": {
            "source": "airbytehq/airbyte",
            "version": AIRBYTE_TERRAFORM_PROVIDER_VERSION,
            "configuration": {
                key: airbyte.get(key, "")
                for key in (
                    "server_url",
                    "client_id",
                    "client_secret",
                    "username",
                    "password",
                )
            },
        },
        "workspace_id": _text(airbyte.get("workspace_id"), label="workspace_id"),
        "destination": {
            "key": destination.value,
            "name": f"ELT-Bench {destination.value} destination",
            "definition_id": definition_id,
            "connector_version": contract.connector_version,
            "configuration": _destination_configuration(
                destination, destination_values
            ),
        },
        "sources": sources,
    }


def build_airbyte_connector_tfvars(config: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap the contract in Terraform's auto-tfvars top-level variable shape."""

    return {AIRBYTE_CONNECTOR_VARIABLE: build_airbyte_connector_contract(config)}


__all__ = [
    "AIRBYTE_CONNECTOR_CONTRACT_SCHEMA_VERSION",
    "AIRBYTE_CONNECTOR_TFVARS_FILENAME",
    "AIRBYTE_CONNECTOR_VARIABLE",
    "AIRBYTE_TERRAFORM_PROVIDER_VERSION",
    "build_airbyte_connector_contract",
    "build_airbyte_connector_tfvars",
]

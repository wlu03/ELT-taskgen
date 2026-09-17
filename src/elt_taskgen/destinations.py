"""Define warehouse destinations for export, installation, and evaluation.

Destinations do not affect the TaskIR hash. Each runtime bundle binds one
destination through its config and Airbyte definition key.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class Destination(str, Enum):
    SNOWFLAKE = "snowflake"
    DATABRICKS = "databricks"
    REDSHIFT = "redshift"


@dataclass(frozen=True)
class DestinationContract:
    destination: Destination
    config_section: str
    definition_key: str
    definition_id: str
    connector_version: str | None
    credential_filename: str
    logical_namespace_field: str
    physical_container_field: str | None
    fixed_schema: str | None

DESTINATION_CONTRACTS: dict[Destination, DestinationContract] = {
    Destination.SNOWFLAKE: DestinationContract(
        destination=Destination.SNOWFLAKE,
        config_section="snowflake",
        definition_key="snowflake_definition_id",
        definition_id="424892c4-daac-4491-b35d-c6688ba547ba",
        # Pinned to Airbyte registry commit
        # 604ae703c2671da50439d00857828ea635ad5618 on 2026-08-31.
        connector_version="4.1.2",
        credential_filename="snowflake_credential.json",
        logical_namespace_field="database",
        physical_container_field=None,
        fixed_schema="AIRBYTE_SCHEMA",
    ),
    Destination.DATABRICKS: DestinationContract(
        destination=Destination.DATABRICKS,
        config_section="databricks",
        definition_key="databricks_definition_id",
        definition_id="072d5540-f236-4294-ba7c-ade8fd918496",
        connector_version="4.0.2",
        credential_filename="databricks_credential.json",
        logical_namespace_field="schema",
        physical_container_field="database",
        fixed_schema=None,
    ),
    Destination.REDSHIFT: DestinationContract(
        destination=Destination.REDSHIFT,
        config_section="redshift",
        definition_key="redshift_definition_id",
        definition_id="f7a7d195-377f-cf5b-70a5-be6b819019dc",
        connector_version="4.0.7",
        credential_filename="redshift_credential.json",
        logical_namespace_field="schema",
        physical_container_field="database",
        fixed_schema=None,
    ),
}


@dataclass(frozen=True)
class SourceConnectorContract:
    """One built-in Airbyte source implied by a public config section."""

    config_section: str
    definition_key: str
    definition_id: str
    connector_version: str

# Source image pins use the same registry commit and remain private. Bootstrap
# verifies them before creating solver resources.
SOURCE_CONNECTOR_CONTRACTS: dict[str, SourceConnectorContract] = {
    "flat_files": SourceConnectorContract(
        config_section="flat_files",
        definition_key="files_definition_id",
        definition_id="778daa7c-feaf-4db6-96f3-70fd645acc77",
        connector_version="0.6.0",
    ),
    "mongodb": SourceConnectorContract(
        config_section="mongodb",
        definition_key="mongodb_definition_id",
        definition_id="b2e713cd-cc36-4c0a-b5bd-b47cb8a0561e",
        connector_version="2.0.7",
    ),
    "postgres": SourceConnectorContract(
        config_section="postgres",
        definition_key="postgres_definition_id",
        definition_id="decd338e-5647-4c0b-adf4-da0e75f5a750",
        connector_version="3.8.5",
    ),
    "aws_s3": SourceConnectorContract(
        config_section="aws_s3",
        definition_key="s3_definition_id",
        definition_id="69589781-7828-43c5-9f63-8925b1c1ccc2",
        connector_version="4.15.20",
    ),
}

# REST tasks pin this schema version because connector IDs are workspace-specific.
CUSTOM_API_MANIFEST_VERSION = "6.33.4"


#: Allowed Airbyte sync modes. Export and bootstrap reject every other mode;
#: see ``tests/test_sync_semantics.py`` for the DuckDB contract.
SUPPORTED_SYNC_MODES: tuple[str, ...] = ("full_refresh_append",)

DEFAULT_SYNC_MODE = SUPPORTED_SYNC_MODES[0]

#: dbt execution pins mirrored in runtime-images/dbt/pyproject.toml and uv.lock.
DBT_CORE_VERSION = "1.12.0"

#: destination -> (adapter plugin name as printed by ``dbt --version``,
#: pinned adapter package version).
DBT_ADAPTER_CONTRACTS: dict[Destination, tuple[str, str]] = {
    Destination.SNOWFLAKE: ("snowflake", "1.12.0"),
    Destination.DATABRICKS: ("databricks", "1.12.4"),
    Destination.REDSHIFT: ("redshift", "1.11.1"),
}


def normalize_sync_mode(value: str) -> str:
    """Return the certified sync mode, or fail closed on any other value."""

    if value in SUPPORTED_SYNC_MODES:
        return value
    certified = ", ".join(SUPPORTED_SYNC_MODES)
    raise ValueError(
        f"unsupported sync mode {value!r}; certified modes: {certified} "
        "(incremental, append-dedupe, overwrite, and schema evolution are "
        "out of scope pending pinned real-runtime certification)"
    )


def config_sync_mode_declarations(
    config: Mapping[str, Any],
) -> list[tuple[str, Any]]:
    """Return each valid connector stanza path and declared ``sync_mode``.

    Missing values are returned as ``None``; malformed stanzas are skipped for
    their owning validators.
    """

    found: list[tuple[str, Any]] = []
    for section in ("postgres", "mongodb", "custom_api"):
        block = config.get(section)
        if isinstance(block, Mapping):
            values = block.get("config")
            if isinstance(values, Mapping):
                found.append((f"{section}.config.sync_mode", values.get("sync_mode")))
    s3 = config.get("aws_s3")
    if isinstance(s3, Mapping):
        entries = s3.get("data")
        if isinstance(entries, list):
            for index, entry in enumerate(entries):
                if isinstance(entry, Mapping):
                    found.append(
                        (f"aws_s3.data[{index}].sync_mode", entry.get("sync_mode"))
                    )
    files = config.get("flat_files")
    if isinstance(files, list):
        for index, entry in enumerate(files):
            if isinstance(entry, Mapping):
                found.append(
                    (f"flat_files[{index}].sync_mode", entry.get("sync_mode"))
                )
    return found


def normalize_destination(value: Destination | str) -> Destination:
    try:
        return Destination(value)
    except (TypeError, ValueError) as exc:
        supported = ", ".join(item.value for item in Destination)
        raise ValueError(
            f"unsupported destination {value!r}; choose one of: {supported}"
        ) from exc


def destination_contract(value: Destination | str) -> DestinationContract:
    return DESTINATION_CONTRACTS[normalize_destination(value)]


def destination_from_config(config: Mapping[str, Any]) -> Destination:
    """Infer one destination from a public or installed config, fail closed."""

    present = [
        destination
        for destination, contract in DESTINATION_CONTRACTS.items()
        if contract.config_section in config
    ]
    if len(present) != 1:
        names = [value.value for value in present]
        raise ValueError(
            "config must contain exactly one destination section "
            f"(found {names})"
        )
    return present[0]


__all__ = [
    "DBT_ADAPTER_CONTRACTS",
    "DBT_CORE_VERSION",
    "CUSTOM_API_MANIFEST_VERSION",
    "DEFAULT_SYNC_MODE",
    "DESTINATION_CONTRACTS",
    "SOURCE_CONNECTOR_CONTRACTS",
    "SUPPORTED_SYNC_MODES",
    "Destination",
    "DestinationContract",
    "SourceConnectorContract",
    "config_sync_mode_declarations",
    "destination_contract",
    "destination_from_config",
    "normalize_destination",
    "normalize_sync_mode",
]


#: Every supported warehouse destination, in the order a package lists them.
ALL_DESTINATIONS: tuple[Destination, ...] = tuple(Destination)


def parse_destinations(value) -> tuple[Destination, ...]:
    """Parse destinations, preserving order and rejecting unknown names."""
    if value is None:
        return ALL_DESTINATIONS
    if isinstance(value, Destination):
        return (value,)
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() == "all":
            return ALL_DESTINATIONS
        parts = [part.strip() for part in text.split(",") if part.strip()]
    else:
        parts = list(value)
    out: list[Destination] = []
    for part in parts:
        member = normalize_destination(part)
        if member not in out:
            out.append(member)
    if not out:
        return ALL_DESTINATIONS
    return tuple(out)

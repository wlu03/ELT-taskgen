"""Project cloud namespaces into the deterministic local DuckDB layout.

Raw Airbyte sources and mart/evaluator relations retain separate schemas.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from elt_taskgen.destinations import Destination
from elt_taskgen.export.eltbench import database_name
from elt_taskgen.sql_identifiers import quote_sql_identifier
from elt_taskgen.training.warehouse_profiles import (
    WarehouseBehaviorProfile,
    warehouse_profile,
)

if TYPE_CHECKING:  # pragma: no cover - import only documents the public input
    from elt_taskgen.training.package import WorkspacePackage


class NamespaceProjectionError(ValueError):
    """The verified destination contract cannot be projected into DuckDB."""


def quote_duckdb_identifier(value: str) -> str:
    """Quote one already-validated logical identifier for DuckDB."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise NamespaceProjectionError("namespace contains an invalid identifier")
    return quote_sql_identifier(value, dialect="duckdb", force=True)


class NamespaceProjection(BaseModel):
    """Cloud-to-local mapping with explicit logical, raw, and mart namespaces."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    destination: Destination
    logical_database: str | None = None
    logical_catalog: str | None = None
    logical_schema: str = Field(min_length=1)
    namespace_definition: str = "destination"

    local_catalog: str = Field(min_length=1)
    raw_schema: str = Field(min_length=1)
    mart_schema: str = Field(min_length=1)
    dbt_target_schema: str = Field(min_length=1)
    evaluator_schema: str = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent_local_contract(self) -> "NamespaceProjection":
        if self.namespace_definition != "destination":
            raise ValueError("local proxy supports destination namespaces only")
        if self.dbt_target_schema != self.mart_schema:
            raise ValueError("dbt target schema must be the mart schema")
        if self.evaluator_schema != self.mart_schema:
            raise ValueError("evaluator schema must be the mart schema")
        for value in (
            self.local_catalog,
            self.raw_schema,
            self.mart_schema,
            self.logical_schema,
        ):
            quote_duckdb_identifier(value)
        return self

    @property
    def dbt_source_schema(self) -> str:
        return self.raw_schema

    @property
    def dbt_source_database(self) -> str:
        """The attempt-local catalog, never a cloud database or catalog."""

        return self.local_catalog

    @property
    def behavior_profile(self) -> WarehouseBehaviorProfile:
        """The versioned representation contract for this projection."""

        return warehouse_profile(self.destination)

    def raw_relation(self, table: str) -> str:
        return (
            f"{quote_duckdb_identifier(self.raw_schema)}."
            f"{quote_duckdb_identifier(table)}"
        )

    def mart_relation(self, mart: str) -> str:
        return (
            f"{quote_duckdb_identifier(self.mart_schema)}."
            f"{quote_duckdb_identifier(mart)}"
        )


def _destination_configuration(package: "WorkspacePackage") -> Mapping[str, Any]:
    destination = package.airbyte_contract.get("destination")
    if not isinstance(destination, Mapping):
        raise NamespaceProjectionError("destination contract is unavailable")
    configuration = destination.get("configuration")
    if not isinstance(configuration, Mapping):
        raise NamespaceProjectionError("destination configuration is unavailable")
    return configuration


def _optional_text(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise NamespaceProjectionError("destination namespace has an invalid value")
    return value


def project_namespace(
    package: "WorkspacePackage", database_path: Path
) -> NamespaceProjection:
    """Project a verified package without inventing environment-injected values."""

    path = Path(database_path)
    if not path.name:
        raise NamespaceProjectionError("DuckDB path has no file name")
    local_catalog = path.stem
    configuration = _destination_configuration(package)
    destination = Destination(package.destination)
    # Fail closed before constructing a namespace for an unsupported target.
    warehouse_profile(destination)

    schema = _optional_text(configuration.get("schema"))
    if schema is None:
        raise NamespaceProjectionError("destination schema is unavailable")

    logical_database: str | None = None
    logical_catalog: str | None = None
    if destination is Destination.SNOWFLAKE:
        logical_database = _optional_text(configuration.get("database"))
        if logical_database is None:
            raise NamespaceProjectionError("Snowflake database is unavailable")
    elif destination is Destination.DATABRICKS:
        logical_catalog = _optional_text(configuration.get("database"))
    elif destination is Destination.REDSHIFT:
        logical_database = _optional_text(configuration.get("database"))

    mart_schema = database_name(package.task)
    return NamespaceProjection(
        destination=destination,
        logical_database=logical_database,
        logical_catalog=logical_catalog,
        logical_schema=schema,
        local_catalog=local_catalog,
        raw_schema=schema,
        mart_schema=mart_schema,
        dbt_target_schema=mart_schema,
        evaluator_schema=mart_schema,
    )


__all__ = [
    "NamespaceProjection",
    "NamespaceProjectionError",
    "project_namespace",
    "quote_duckdb_identifier",
]

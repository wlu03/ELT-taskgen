"""Derive the parity cases required by a frozen TaskIR.

The sorted, digest-bound manifest includes only behaviors the task uses. Diagnostic
subsets are allowed, but only complete manifest coverage can mark a parity report fully
verified.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from elt_taskgen.destinations import Destination, normalize_destination
from elt_taskgen.models import Backend, ColumnType, JoinType, TaskIR, canonical_json, sha256_hex
from elt_taskgen.verification.parity import (
    PARITY_CONTRACT_VERSION,
    PARITY_REGISTRY_DIGEST,
    ParityError,
    parity_case_set_digest,
    parity_case_specs,
)


PARITY_SCOPE_POLICY_VERSION = "1"

_MANDATORY_CASES = {
    "el.stream_selection",
    "el.full_refresh_append",
    "el.raw_table_counts",
    "warehouse.namespace_mapping",
    "warehouse.identifier_projection",
    "warehouse.metadata_projection",
    "t.dbt_profile_namespace",
    "t.mart_schema",
    "t.mart_rows",
    "runtime.connector_discovery",
    "runtime.authentication",
    "runtime.network_and_job_lifecycle",
}

_BACKEND_CASE = {
    Backend.FILES: "el.source.files.records",
    Backend.POSTGRES: "el.source.postgres.records",
    Backend.MONGODB: "el.source.mongodb.records",
    Backend.REST: "el.source.rest.pagination",
    Backend.S3: "el.source.s3.multipart",
}

_TYPE_CASES = {
    ColumnType.INTEGER: {"warehouse.integer_width"},
    ColumnType.BIGINT: {"warehouse.integer_width"},
    ColumnType.FLOAT: {"warehouse.float_finite_exact"},
    ColumnType.DECIMAL: {"warehouse.decimal_38_9"},
    ColumnType.TEXT: {
        "warehouse.text_unicode_whitespace",
        "warehouse.empty_string_vs_null",
    },
    ColumnType.BOOLEAN: {"warehouse.boolean_mapping"},
    ColumnType.DATE: {"warehouse.date_boundary"},
    ColumnType.TIMESTAMP: {"warehouse.timestamp_utc_microsecond"},
    ColumnType.JSON: {"warehouse.json_scalar"},
}


class ParityCaseManifest(BaseModel):
    """Immutable required-case scope bound to one semantic task and target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = PARITY_SCOPE_POLICY_VERSION
    parity_contract_version: str = PARITY_CONTRACT_VERSION
    parity_registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination: Destination
    task_id: str = Field(min_length=1)
    task_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_ids: tuple[str, ...] = Field(min_length=1)
    case_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_manifest(self) -> "ParityCaseManifest":
        if self.policy_version != PARITY_SCOPE_POLICY_VERSION:
            raise ValueError("unsupported parity scope policy version")
        if self.parity_contract_version != PARITY_CONTRACT_VERSION:
            raise ValueError("parity scope names another contract version")
        if self.parity_registry_digest != PARITY_REGISTRY_DIGEST:
            raise ValueError("parity scope names another registry digest")
        if tuple(sorted(set(self.case_ids))) != self.case_ids:
            raise ValueError("parity scope case_ids must be sorted and unique")
        try:
            specs = parity_case_specs(self.destination, case_ids=self.case_ids)
        except ParityError as exc:
            raise ValueError(str(exc)) from exc
        if self.case_set_digest != parity_case_set_digest(specs):
            raise ValueError("parity scope case-set digest disagrees")
        expected_manifest = _manifest_digest(
            destination=self.destination,
            task_id=self.task_id,
            task_content_hash=self.task_content_hash,
            case_set_digest=self.case_set_digest,
        )
        if self.manifest_digest != expected_manifest:
            raise ValueError("parity scope manifest digest disagrees")
        return self


def _manifest_digest(
    *,
    destination: Destination | str,
    task_id: str,
    task_content_hash: str,
    case_set_digest: str,
) -> str:
    return sha256_hex(
        canonical_json(
            {
                "policy_version": PARITY_SCOPE_POLICY_VERSION,
                "parity_contract_version": PARITY_CONTRACT_VERSION,
                "parity_registry_digest": PARITY_REGISTRY_DIGEST,
                "destination": normalize_destination(destination).value,
                "task_id": task_id,
                "task_content_hash": task_content_hash,
                "case_set_digest": case_set_digest,
            }
        )
    )


def derive_task_parity_manifest(
    task: TaskIR,
    destination: Destination | str,
) -> ParityCaseManifest:
    """Map source backends, logical types, nullability, and target risks to cases."""

    resolved = normalize_destination(destination)
    case_ids = set(_MANDATORY_CASES)
    case_ids.update(_BACKEND_CASE[assignment.backend] for assignment in task.backends)

    logical_types = {
        column.type
        for table in task.tables
        for column in table.columns
    } | {
        column.type
        for mart in task.marts
        for column in mart.columns
    }
    for column_type in logical_types:
        case_ids.update(_TYPE_CASES[column_type])

    has_outer_join = any(
        operation.join_type in {JoinType.LEFT, JoinType.RIGHT, JoinType.FULL}
        for mart in task.marts
        for operation in mart.plan.ops
    )
    has_nullable = any(
        column.nullable
        for table in task.tables
        for column in table.columns
    )
    if has_nullable or has_outer_join:
        case_ids.update({"warehouse.sql_null", "warehouse.null_ordering"})

    if resolved is Destination.SNOWFLAKE:
        case_ids.add("snowflake.warehouse_lifecycle")
        if ColumnType.JSON in logical_types:
            case_ids.add("snowflake.variant_object_array")
        if ColumnType.TIMESTAMP in logical_types:
            case_ids.add("snowflake.timestamp_types")
    elif resolved is Destination.DATABRICKS:
        case_ids.update(
            {
                "databricks.delta_table_shape",
                "databricks.unity_catalog_volume_permissions",
            }
        )
        if ColumnType.JSON in logical_types:
            case_ids.add("databricks.json_string_projection")
    else:
        case_ids.update(
            {
                "redshift.identifier_sanitization",
                "redshift.s3_copy_cleanup",
            }
        )
        if ColumnType.JSON in logical_types:
            case_ids.add("redshift.super_projection")
        if logical_types & {ColumnType.TEXT, ColumnType.JSON}:
            case_ids.update(
                {
                    "redshift.large_text_boundary",
                    "redshift.large_text_roundtrip",
                }
            )

    specs = parity_case_specs(resolved, case_ids=tuple(sorted(case_ids)))
    ordered_ids = tuple(spec.case_id for spec in specs)
    case_set_digest = parity_case_set_digest(specs)
    task_content_hash = task.content_hash()
    return ParityCaseManifest(
        parity_registry_digest=PARITY_REGISTRY_DIGEST,
        destination=resolved,
        task_id=task.task_id,
        task_content_hash=task_content_hash,
        case_ids=ordered_ids,
        case_set_digest=case_set_digest,
        manifest_digest=_manifest_digest(
            destination=resolved,
            task_id=task.task_id,
            task_content_hash=task_content_hash,
            case_set_digest=case_set_digest,
        ),
    )


__all__ = [
    "PARITY_SCOPE_POLICY_VERSION",
    "ParityCaseManifest",
    "derive_task_parity_manifest",
]

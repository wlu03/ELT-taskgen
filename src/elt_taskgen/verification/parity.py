"""Verify fail-closed parity between DuckDB semantic proxies and real warehouses.

Exact cases require successful proxy and real observations with matching result, schema,
and count fingerprints. Real-only cases require successful real evidence and reject
proxy observations. Evidence contains bounded metadata and digests, never rows,
credentials, SQL, or connector logs.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    model_validator,
)

from elt_taskgen.destinations import (
    Destination,
    normalize_destination,
)
from elt_taskgen.models import TaskIR, canonical_json, sha256_hex
from elt_taskgen.verification.canonical_fingerprint import (
    CANONICAL_FINGERPRINT_VERSION,
    CanonicalRelationFingerprint,
    CanonicalRowOrder,
)


PARITY_CONTRACT_VERSION = "1"
PARITY_OBSERVATION_SCHEMA_VERSION = "1.1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROBE_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")


class ParityError(RuntimeError):
    """Parity evidence is malformed, ambiguous, or bound to another run."""


class ParityStage(str, Enum):
    EXTRACT_LOAD = "extract_load"
    TRANSFORM = "transform"
    RUNTIME = "runtime"


class ParityComparison(str, Enum):
    EXACT = "exact_reference_real"
    REAL_ONLY = "real_only"


class ParityEngine(str, Enum):
    DUCKDB_SEMANTIC_PROXY = "duckdb_semantic_proxy"
    LOCAL_STATIC_CONTRACT = "local_static_contract"
    LOCAL_PROTOCOL_MODEL = "local_protocol_model"
    REAL_WAREHOUSE = "real_warehouse"


class ParityEvidenceOrigin(str, Enum):
    LOCAL_EXECUTION = "local_execution"
    LOCAL_STATIC_INSPECTION = "local_static_inspection"
    LOCAL_PROTOCOL_EXECUTION = "local_protocol_execution"
    RECORDED_LIVE_REPLAY = "recorded_live_replay"
    LIVE_CLOUD = "live_cloud"


class ParityEvidenceKind(str, Enum):
    RELATION_FINGERPRINT = "relation_fingerprint"
    STATIC_CONTRACT_FINGERPRINT = "static_contract_fingerprint"
    PHYSICAL_SCHEMA_FINGERPRINT = "physical_schema_fingerprint"
    PROTOCOL_ATTESTATION = "protocol_attestation"
    RUNTIME_ATTESTATION = "runtime_attestation"


class ParityVerdict(str, Enum):
    MATCHED = "matched"
    REFERENCE_ONLY = "reference_only"
    MISSING_REFERENCE = "missing_reference"
    MISSING_BOTH = "missing_both"
    REFERENCE_FAILED = "reference_failed"
    REAL_FAILED = "real_failed"
    BOTH_FAILED = "both_failed"
    MISMATCH = "mismatch"
    REAL_ONLY_VERIFIED = "real_only_verified"
    REAL_ONLY_REPLAYED = "real_only_replayed"
    MISSING_REAL = "missing_real"


class ParityCaseSpec(BaseModel):
    """One named behavior in the declared cross-runtime compatibility surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    stage: ParityStage
    comparison: ParityComparison
    destinations: tuple[Destination, ...]
    evidence_kind: ParityEvidenceKind
    required_row_order: CanonicalRowOrder | None = None
    reference_engine: ParityEngine | None
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_evidence_shape(self) -> "ParityCaseSpec":
        if (
            self.evidence_kind is ParityEvidenceKind.RELATION_FINGERPRINT
            and self.required_row_order is None
        ):
            raise ValueError("relation-fingerprint cases require a row-order policy")
        if (
            self.evidence_kind is not ParityEvidenceKind.RELATION_FINGERPRINT
            and self.required_row_order is not None
        ):
            raise ValueError("only relation-fingerprint cases may set row order")
        if self.comparison is ParityComparison.REAL_ONLY:
            if self.reference_engine is not None:
                raise ValueError("real-only cases cannot name a local reference engine")
        elif self.reference_engine not in {
            ParityEngine.DUCKDB_SEMANTIC_PROXY,
            ParityEngine.LOCAL_STATIC_CONTRACT,
            ParityEngine.LOCAL_PROTOCOL_MODEL,
        }:
            raise ValueError("exact cases require a local reference engine")
        return self


class ParityObservation(BaseModel):
    """Row-free private fingerprint envelope emitted by one trusted collector."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_schema_version: str = PARITY_OBSERVATION_SCHEMA_VERSION
    parity_contract_version: str = PARITY_CONTRACT_VERSION
    canonical_fingerprint_version: str = CANONICAL_FINGERPRINT_VERSION
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    destination: Destination
    engine: ParityEngine
    evidence_origin: ParityEvidenceOrigin
    evidence_kind: ParityEvidenceKind
    row_order: CanonicalRowOrder | None = None
    semantic_release_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    task_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_case_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_bundle_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    solution_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    probe_id: str = Field(pattern=_PROBE_ID.pattern)
    probe_binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    matrix_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_case_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    population: str = Field(min_length=1)
    population_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    producer_version: str = Field(min_length=1)
    succeeded: StrictBool
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_digest: str = ""
    row_count: StrictInt | None = Field(default=None, ge=0)
    failure_code: str = Field(
        default="", pattern=r"^$|^[a-z][a-z0-9_.:-]*$"
    )

    @model_validator(mode="after")
    def _validate_outcome(self) -> "ParityObservation":
        if self.parity_contract_version != PARITY_CONTRACT_VERSION:
            raise ValueError(
                "observation parity_contract_version does not match the verifier"
            )
        if self.observation_schema_version != PARITY_OBSERVATION_SCHEMA_VERSION:
            raise ValueError("unsupported parity observation schema version")
        if self.canonical_fingerprint_version != CANONICAL_FINGERPRINT_VERSION:
            raise ValueError("unsupported canonical fingerprint version")
        if (
            self.evidence_kind is ParityEvidenceKind.RELATION_FINGERPRINT
            and self.row_order is None
        ):
            raise ValueError("relation evidence must declare its row-order policy")
        if (
            self.evidence_kind is not ParityEvidenceKind.RELATION_FINGERPRINT
            and self.row_order is not None
        ):
            raise ValueError("non-relation evidence cannot declare row order")
        if self.schema_digest and not _SHA256.fullmatch(self.schema_digest):
            raise ValueError("schema_digest must be empty or a lowercase sha256")
        if self.succeeded and self.failure_code:
            raise ValueError("successful parity observation cannot name a failure_code")
        if not self.succeeded and not self.failure_code:
            raise ValueError("failed parity observation must name a stable failure_code")
        if (
            self.engine is ParityEngine.DUCKDB_SEMANTIC_PROXY
            and self.evidence_origin is not ParityEvidenceOrigin.LOCAL_EXECUTION
        ):
            raise ValueError(
                "DuckDB semantic-proxy evidence must originate from local execution"
            )
        if (
            self.engine is ParityEngine.LOCAL_STATIC_CONTRACT
            and self.evidence_origin
            is not ParityEvidenceOrigin.LOCAL_STATIC_INSPECTION
        ):
            raise ValueError(
                "static-contract evidence must originate from local inspection"
            )
        if (
            self.engine is ParityEngine.LOCAL_PROTOCOL_MODEL
            and self.evidence_origin
            is not ParityEvidenceOrigin.LOCAL_PROTOCOL_EXECUTION
        ):
            raise ValueError(
                "protocol-model evidence must originate from local protocol execution"
            )
        if (
            self.engine is ParityEngine.REAL_WAREHOUSE
            and self.evidence_origin
            not in {
                ParityEvidenceOrigin.RECORDED_LIVE_REPLAY,
                ParityEvidenceOrigin.LIVE_CLOUD,
            }
        ):
            raise ValueError(
                "real-warehouse evidence must be live or a recorded live replay"
            )
        return self


class ParityCaseResult(BaseModel):
    """Report-only verdict for one declared case."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    stage: ParityStage
    comparison: ParityComparison
    verdict: ParityVerdict
    reference_present: StrictBool
    real_present: StrictBool
    reference_origin: ParityEvidenceOrigin | None
    real_origin: ParityEvidenceOrigin | None
    reason: str

    @model_validator(mode="after")
    def _validate_presence(self) -> "ParityCaseResult":
        if self.reference_present is not (self.reference_origin is not None):
            raise ValueError("reference presence and origin disagree")
        if self.real_present is not (self.real_origin is not None):
            raise ValueError("real presence and origin disagree")
        return self


class ParityReport(BaseModel):
    """Structural comparison report for one destination/population pair.

    This is deliberately never certification-eligible on its own.  A protected
    collector and signed attestation must authenticate the observations.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    parity_contract_version: str = PARITY_CONTRACT_VERSION
    destination: Destination
    semantic_release_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    task_content_hash: str = Field(pattern=_SHA256.pattern)
    required_case_manifest_digest: str = Field(pattern=_SHA256.pattern)
    runtime_bundle_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    solution_digest: str = Field(pattern=_SHA256.pattern)
    probe_id: str = Field(pattern=_PROBE_ID.pattern)
    probe_binding_digest: str = Field(pattern=_SHA256.pattern)
    matrix_digest: str = Field(pattern=_SHA256.pattern)
    required_case_ids: tuple[str, ...] = Field(min_length=1)
    required_case_set_digest: str = Field(pattern=_SHA256.pattern)
    selected_case_set_digest: str = Field(pattern=_SHA256.pattern)
    observation_set_digest: str = Field(pattern=_SHA256.pattern)
    identity_binding_digest: str = Field(pattern=_SHA256.pattern)
    population: str = Field(min_length=1)
    population_digest: str = Field(pattern=_SHA256.pattern)
    cases: tuple[ParityCaseResult, ...]
    reference_complete: StrictBool
    real_complete: StrictBool
    reference_real_verified: StrictBool
    real_only_verified: StrictBool
    live_evidence_complete: StrictBool
    selected_scope_verified: StrictBool
    required_scope_coverage: StrictBool
    fully_verified: StrictBool
    certification_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _validate_derived_fields(self) -> "ParityReport":
        if self.parity_contract_version != PARITY_CONTRACT_VERSION:
            raise ValueError("report parity contract version is unsupported")
        if not self.cases:
            raise ValueError("parity report has no cases")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("parity report has duplicate cases")
        try:
            required_specs = parity_case_specs(
                self.destination,
                case_ids=self.required_case_ids,
            )
            specs = parity_case_specs(self.destination, case_ids=case_ids)
        except ParityError as exc:
            raise ValueError(str(exc)) from exc
        required_ids = {spec.case_id for spec in required_specs}
        if not set(case_ids) <= required_ids:
            raise ValueError("report contains cases outside its required scope")
        if self.required_case_set_digest != parity_case_set_digest(required_specs):
            raise ValueError("required_case_set_digest disagrees with required cases")
        try:
            from elt_taskgen.verification.parity_scope import ParityCaseManifest

            ParityCaseManifest(
                parity_registry_digest=PARITY_REGISTRY_DIGEST,
                destination=self.destination,
                task_id=self.task_id,
                task_content_hash=self.task_content_hash,
                case_ids=self.required_case_ids,
                case_set_digest=self.required_case_set_digest,
                manifest_digest=self.required_case_manifest_digest,
            )
        except (ParityError, ValidationError, ValueError) as exc:
            raise ValueError("required parity case manifest disagrees") from exc
        specs_by_id = {spec.case_id: spec for spec in specs}
        for case in self.cases:
            spec = specs_by_id[case.case_id]
            if case.stage is not spec.stage or case.comparison is not spec.comparison:
                raise ValueError(f"case metadata disagrees for {case.case_id!r}")

        exact = [
            case for case in self.cases
            if case.comparison is ParityComparison.EXACT
        ]
        real_only = [
            case for case in self.cases
            if case.comparison is ParityComparison.REAL_ONLY
        ]
        reference_complete = bool(exact) and all(
            case.reference_present
            and case.verdict
            not in {
                ParityVerdict.REFERENCE_FAILED,
                ParityVerdict.BOTH_FAILED,
                ParityVerdict.MISSING_REFERENCE,
                ParityVerdict.MISSING_BOTH,
            }
            for case in exact
        )
        real_complete = all(
            case.real_present
            and case.verdict
            not in {
                ParityVerdict.REAL_FAILED,
                ParityVerdict.BOTH_FAILED,
                ParityVerdict.REFERENCE_ONLY,
                ParityVerdict.MISSING_REAL,
                ParityVerdict.MISSING_BOTH,
            }
            for case in self.cases
        )
        exact_verified = bool(exact) and all(
            case.verdict is ParityVerdict.MATCHED for case in exact
        )
        real_only_verified = bool(real_only) and all(
            case.verdict is ParityVerdict.REAL_ONLY_VERIFIED
            for case in real_only
        )
        live_complete = real_complete and all(
            case.real_origin is ParityEvidenceOrigin.LIVE_CLOUD
            for case in self.cases
            if case.real_present
        )
        selected_scope_verified = (
            (not exact or exact_verified)
            and (not real_only or real_only_verified)
            and live_complete
        )
        required_coverage = specs == required_specs
        expected = {
            "reference_complete": reference_complete,
            "real_complete": real_complete,
            "reference_real_verified": exact_verified,
            "real_only_verified": real_only_verified,
            "live_evidence_complete": live_complete,
            "selected_scope_verified": selected_scope_verified,
            "required_scope_coverage": required_coverage,
            "fully_verified": selected_scope_verified and required_coverage,
        }
        for field, value in expected.items():
            if getattr(self, field) is not value:
                raise ValueError(f"derived parity-report field {field!r} disagrees")
        if self.selected_case_set_digest != parity_case_set_digest(specs):
            raise ValueError("selected_case_set_digest disagrees with report cases")
        expected_binding = parity_report_binding_digest(
            destination=self.destination,
            semantic_release_id=self.semantic_release_id,
            task_id=self.task_id,
            task_content_hash=self.task_content_hash,
            required_case_manifest_digest=self.required_case_manifest_digest,
            runtime_bundle_id=self.runtime_bundle_id,
            attempt_id=self.attempt_id,
            solution_digest=self.solution_digest,
            probe_id=self.probe_id,
            probe_binding_digest=self.probe_binding_digest,
            matrix_digest=self.matrix_digest,
            required_case_set_digest=self.required_case_set_digest,
            selected_case_set_digest=self.selected_case_set_digest,
            population=self.population,
            population_digest=self.population_digest,
            observation_set_digest=self.observation_set_digest,
        )
        if self.identity_binding_digest != expected_binding:
            raise ValueError("identity_binding_digest disagrees with report identity")
        return self


_ALL_DESTINATIONS = tuple(Destination)


def _case(
    case_id: str,
    stage: ParityStage,
    comparison: ParityComparison,
    description: str,
    destinations: tuple[Destination, ...] = _ALL_DESTINATIONS,
    *,
    evidence_kind: ParityEvidenceKind = ParityEvidenceKind.RELATION_FINGERPRINT,
    required_row_order: CanonicalRowOrder | None = CanonicalRowOrder.UNORDERED,
) -> ParityCaseSpec:
    reference_engine = None
    if comparison is ParityComparison.EXACT:
        if evidence_kind is ParityEvidenceKind.RELATION_FINGERPRINT:
            reference_engine = ParityEngine.DUCKDB_SEMANTIC_PROXY
        elif evidence_kind is ParityEvidenceKind.PROTOCOL_ATTESTATION:
            reference_engine = ParityEngine.LOCAL_PROTOCOL_MODEL
        else:
            reference_engine = ParityEngine.LOCAL_STATIC_CONTRACT
    return ParityCaseSpec(
        case_id=case_id,
        stage=stage,
        comparison=comparison,
        destinations=destinations,
        evidence_kind=evidence_kind,
        required_row_order=required_row_order,
        reference_engine=reference_engine,
        description=description,
    )


# Complete parity surface for contract version 1; new cases start unverified.
PARITY_CASE_SPECS: tuple[ParityCaseSpec, ...] = (
    _case(
        "el.source.files.records",
        ParityStage.EXTRACT_LOAD,
        ParityComparison.EXACT,
        "HTTP-served file records materialize with the oracle row content.",
    ),
    _case(
        "el.source.postgres.records",
        ParityStage.EXTRACT_LOAD,
        ParityComparison.EXACT,
        "PostgreSQL records materialize with the oracle row content.",
    ),
    _case(
        "el.source.mongodb.records",
        ParityStage.EXTRACT_LOAD,
        ParityComparison.EXACT,
        "MongoDB documents materialize with the oracle row content.",
    ),
    _case(
        "el.source.rest.pagination",
        ParityStage.EXTRACT_LOAD,
        ParityComparison.EXACT,
        "Every REST page and terminal-page condition matches the oracle.",
    ),
    _case(
        "el.source.s3.multipart",
        ParityStage.EXTRACT_LOAD,
        ParityComparison.EXACT,
        "Every object selected by the S3 stream glob matches the oracle.",
    ),
    _case(
        "el.stream_selection",
        ParityStage.EXTRACT_LOAD,
        ParityComparison.EXACT,
        "The exact required Airbyte stream set is loaded once.",
        evidence_kind=ParityEvidenceKind.STATIC_CONTRACT_FINGERPRINT,
        required_row_order=None,
    ),
    _case(
        "el.full_refresh_append",
        ParityStage.EXTRACT_LOAD,
        ParityComparison.EXACT,
        "The certified full-refresh append outcome matches local semantics.",
        evidence_kind=ParityEvidenceKind.PROTOCOL_ATTESTATION,
        required_row_order=None,
    ),
    _case(
        "el.raw_table_counts",
        ParityStage.EXTRACT_LOAD,
        ParityComparison.EXACT,
        "Required raw-table existence and row counts match DuckDB gold.",
    ),
    _case(
        "warehouse.namespace_mapping",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Logical task namespaces resolve to the expected physical relations.",
        evidence_kind=ParityEvidenceKind.PHYSICAL_SCHEMA_FINGERPRINT,
        required_row_order=None,
    ),
    _case(
        "warehouse.identifier_projection",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Identifier quoting, folding, and sanitization match the target contract.",
        evidence_kind=ParityEvidenceKind.PHYSICAL_SCHEMA_FINGERPRINT,
        required_row_order=None,
    ),
    _case(
        "warehouse.integer_width",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "INTEGER and BIGINT boundaries preserve exact signed values.",
    ),
    _case(
        "warehouse.float_finite_exact",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Admitted finite floating-point values obey the exact comparison policy.",
    ),
    _case(
        "warehouse.boolean_mapping",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Boolean true, false, and null values preserve logical meaning.",
    ),
    _case(
        "warehouse.date_boundary",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Date values preserve calendar boundaries independently of session timezones.",
    ),
    _case(
        "warehouse.text_unicode_whitespace",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Unicode, casing, and leading/trailing whitespace remain byte-distinct values.",
    ),
    _case(
        "warehouse.empty_string_vs_null",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Empty strings remain distinct from SQL nulls.",
    ),
    _case(
        "warehouse.sql_null",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Nullable logical values preserve SQL null semantics.",
    ),
    _case(
        "warehouse.decimal_38_9",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Portable decimals preserve precision, scale, rounding, and nulls.",
    ),
    _case(
        "warehouse.timestamp_utc_microsecond",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "UTC-normalized timestamps preserve microsecond values.",
    ),
    _case(
        "warehouse.json_scalar",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Nested JSON scalar extraction preserves value and null semantics.",
    ),
    _case(
        "warehouse.null_ordering",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Explicit NULLS FIRST/LAST ordering agrees with the oracle.",
        required_row_order=CanonicalRowOrder.ORDERED,
    ),
    _case(
        "warehouse.metadata_projection",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Airbyte metadata columns cannot alter declared business outputs.",
    ),
    _case(
        "t.dbt_profile_namespace",
        ParityStage.TRANSFORM,
        ParityComparison.EXACT,
        "The dbt adapter and profile resolve the installed attempt namespace.",
        evidence_kind=ParityEvidenceKind.STATIC_CONTRACT_FINGERPRINT,
        required_row_order=None,
    ),
    _case(
        "t.mart_schema",
        ParityStage.TRANSFORM,
        ParityComparison.EXACT,
        "Persistent mart names, columns, and declared types match gold.",
        evidence_kind=ParityEvidenceKind.PHYSICAL_SCHEMA_FINGERPRINT,
        required_row_order=None,
    ),
    _case(
        "t.mart_rows",
        ParityStage.TRANSFORM,
        ParityComparison.EXACT,
        "Sorted persistent mart rows match private DuckDB gold.",
    ),
    _case(
        "runtime.connector_discovery",
        ParityStage.RUNTIME,
        ParityComparison.REAL_ONLY,
        "The pinned destination and source connectors check successfully.",
        evidence_kind=ParityEvidenceKind.RUNTIME_ATTESTATION,
        required_row_order=None,
    ),
    _case(
        "runtime.authentication",
        ParityStage.RUNTIME,
        ParityComparison.REAL_ONLY,
        "The scoped non-interactive runtime identity authenticates successfully.",
        evidence_kind=ParityEvidenceKind.RUNTIME_ATTESTATION,
        required_row_order=None,
    ),
    _case(
        "runtime.network_and_job_lifecycle",
        ParityStage.RUNTIME,
        ParityComparison.REAL_ONLY,
        "Airbyte can reach services and complete the exact triggered job.",
        evidence_kind=ParityEvidenceKind.RUNTIME_ATTESTATION,
        required_row_order=None,
    ),
    _case(
        "snowflake.variant_object_array",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Snowflake VARIANT/OBJECT/ARRAY projections agree with canonical JSON.",
        (Destination.SNOWFLAKE,),
    ),
    _case(
        "snowflake.timestamp_types",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Snowflake NTZ/LTZ/TZ handling agrees with normalized timestamp gold.",
        (Destination.SNOWFLAKE,),
    ),
    _case(
        "snowflake.warehouse_lifecycle",
        ParityStage.RUNTIME,
        ParityComparison.REAL_ONLY,
        "The isolated warehouse resumes, executes, suspends, and cleans up.",
        (Destination.SNOWFLAKE,),
        evidence_kind=ParityEvidenceKind.RUNTIME_ATTESTATION,
        required_row_order=None,
    ),
    _case(
        "databricks.json_string_projection",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Databricks JSON-string extraction agrees with canonical JSON.",
        (Destination.DATABRICKS,),
    ),
    _case(
        "databricks.delta_table_shape",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Delta table business columns and rows match the oracle projection.",
        (Destination.DATABRICKS,),
        evidence_kind=ParityEvidenceKind.PHYSICAL_SCHEMA_FINGERPRINT,
        required_row_order=None,
    ),
    _case(
        "databricks.unity_catalog_volume_permissions",
        ParityStage.RUNTIME,
        ParityComparison.REAL_ONLY,
        "The attempt principal can use only its catalog, schema, and Volume scope.",
        (Destination.DATABRICKS,),
        evidence_kind=ParityEvidenceKind.RUNTIME_ATTESTATION,
        required_row_order=None,
    ),
    _case(
        "redshift.super_projection",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Redshift SUPER/PartiQL projections agree with canonical JSON.",
        (Destination.REDSHIFT,),
    ),
    _case(
        "redshift.identifier_sanitization",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Redshift lowercasing, truncation, and collision handling are represented.",
        (Destination.REDSHIFT,),
        evidence_kind=ParityEvidenceKind.PHYSICAL_SCHEMA_FINGERPRINT,
        required_row_order=None,
    ),
    _case(
        "redshift.s3_copy_cleanup",
        ParityStage.RUNTIME,
        ParityComparison.REAL_ONLY,
        "COPY uses the attempt-only S3 prefix and removes staged objects.",
        (Destination.REDSHIFT,),
        evidence_kind=ParityEvidenceKind.RUNTIME_ATTESTATION,
        required_row_order=None,
    ),
    _case(
        "redshift.large_text_boundary",
        ParityStage.RUNTIME,
        ParityComparison.REAL_ONLY,
        "Certified VARCHAR and SUPER boundary values are accepted by the load job.",
        (Destination.REDSHIFT,),
        evidence_kind=ParityEvidenceKind.RUNTIME_ATTESTATION,
        required_row_order=None,
    ),
    _case(
        "redshift.large_text_roundtrip",
        ParityStage.RUNTIME,
        ParityComparison.EXACT,
        "Loaded VARCHAR and SUPER boundary content round-trips without truncation.",
        (Destination.REDSHIFT,),
    ),
)


def parity_registry_digest() -> str:
    """Digest the complete case definitions, not only their manually bumped version."""

    payload = [
        spec.model_dump(mode="json")
        for spec in sorted(PARITY_CASE_SPECS, key=lambda item: item.case_id)
    ]
    return sha256_hex(canonical_json(payload))


PARITY_REGISTRY_DIGEST = parity_registry_digest()


def parity_case_set_digest(specs: Sequence[ParityCaseSpec]) -> str:
    """Bind a report to the exact diagnostic or task-derived case selection."""

    return sha256_hex(
        canonical_json(
            [
                spec.model_dump(mode="json")
                for spec in sorted(specs, key=lambda item: item.case_id)
            ]
        )
    )


def parity_case_specs(
    destination: Destination | str,
    *,
    case_ids: Sequence[str] | None = None,
) -> tuple[ParityCaseSpec, ...]:
    """Return the stable declared suite (or an explicit task-specific subset)."""

    resolved = normalize_destination(destination)
    declared_ids = [spec.case_id for spec in PARITY_CASE_SPECS]
    if len(declared_ids) != len(set(declared_ids)):
        raise ParityError("parity case registry contains duplicate case_ids")
    available = {
        spec.case_id: spec
        for spec in PARITY_CASE_SPECS
        if resolved in spec.destinations
    }
    if case_ids is None:
        selected = available
    else:
        requested = list(case_ids)
        if len(requested) != len(set(requested)):
            raise ParityError("requested parity case_ids contain duplicates")
        unknown = sorted(set(requested) - set(available))
        if unknown:
            raise ParityError(
                f"parity cases are not declared for {resolved.value}: {unknown}"
            )
        selected = {case_id: available[case_id] for case_id in requested}
    if not selected:
        raise ParityError("a parity report must require at least one declared case")
    return tuple(selected[case_id] for case_id in sorted(selected))


def parity_matrix_digest(matrix: Mapping[str, str]) -> str:
    """Bind observations to one complete, secret-free execution matrix."""

    if not matrix:
        raise ParityError("parity execution matrix cannot be empty")
    normalized: dict[str, str] = {}
    for key, value in matrix.items():
        if not isinstance(key, str) or not key:
            raise ParityError("parity execution matrix has an invalid key")
        if not isinstance(value, str):
            raise ParityError(f"parity execution matrix value for {key!r} is not text")
        normalized[key] = value
    return sha256_hex(canonical_json(dict(sorted(normalized.items()))))


def _validate_parity_matrix(
    matrix: Mapping[str, str],
    destination: Destination,
) -> None:
    """Apply the authoritative schema-3.4 matrix contract."""

    # Imported lazily because runtime_matrix imports this module's registry
    # identity. Runtime validation occurs only after both modules initialize.
    from elt_taskgen.runtime_matrix import validate_certification_matrix

    try:
        validate_certification_matrix(matrix, destination)
    except ValueError as exc:
        raise ParityError(f"invalid execution matrix: {exc}") from exc


def parity_observation_set_digest(
    observations: Sequence[ParityObservation],
) -> str:
    """Bind a report to the exact validated observation envelopes it summarizes."""

    payload: list[dict[str, object]] = []
    for supplied in observations:
        if not isinstance(supplied, ParityObservation):
            raise ParityError("parity observations must use the validated schema")
        try:
            validated = ParityObservation.model_validate(
                supplied.model_dump(mode="python")
            )
        except ValidationError as exc:
            raise ParityError("parity observation fails schema validation") from exc
        payload.append(validated.model_dump(mode="json"))
    payload.sort(key=canonical_json)
    return sha256_hex(canonical_json(payload))


def parity_report_binding_digest(
    *,
    destination: Destination | str,
    semantic_release_id: str,
    task_id: str,
    task_content_hash: str,
    required_case_manifest_digest: str,
    runtime_bundle_id: str,
    attempt_id: str,
    solution_digest: str,
    probe_id: str,
    probe_binding_digest: str,
    matrix_digest: str,
    required_case_set_digest: str,
    selected_case_set_digest: str,
    population: str,
    population_digest: str,
    observation_set_digest: str,
) -> str:
    """Tamper-evident identity binding; authenticity still requires a signature."""

    return sha256_hex(
        canonical_json(
            {
                "parity_contract_version": PARITY_CONTRACT_VERSION,
                "destination": normalize_destination(destination).value,
                "semantic_release_id": semantic_release_id,
                "task_id": task_id,
                "task_content_hash": task_content_hash,
                "required_case_manifest_digest": required_case_manifest_digest,
                "runtime_bundle_id": runtime_bundle_id,
                "attempt_id": attempt_id,
                "solution_digest": solution_digest,
                "probe_id": probe_id,
                "probe_binding_digest": probe_binding_digest,
                "matrix_digest": matrix_digest,
                "required_case_set_digest": required_case_set_digest,
                "selected_case_set_digest": selected_case_set_digest,
                "population": population,
                "population_digest": population_digest,
                "observation_set_digest": observation_set_digest,
            }
        )
    )


def parity_report_digest(report: ParityReport) -> str:
    """Stable private checksum; this is not a signature or proof of origin."""

    try:
        validated = ParityReport.model_validate(report.model_dump(mode="python"))
    except ValidationError as exc:
        raise ParityError("parity report fails invariant validation") from exc
    return sha256_hex(canonical_json(validated.model_dump(mode="json")))


def relation_observation(
    *,
    case_id: str,
    destination: Destination | str,
    engine: ParityEngine,
    evidence_origin: ParityEvidenceOrigin,
    semantic_release_id: str,
    task: TaskIR,
    runtime_bundle_id: str,
    attempt_id: str,
    solution_digest: str,
    probe_id: str,
    probe_binding_digest: str,
    matrix: Mapping[str, str],
    population: str,
    population_digest: str,
    producer_version: str,
    fingerprint: CanonicalRelationFingerprint,
) -> ParityObservation:
    """Create one successful observation from a trusted relation fingerprint."""

    resolved_destination = normalize_destination(destination)
    try:
        validated_task = TaskIR.model_validate(task.model_dump(mode="python"))
    except (AttributeError, ValidationError) as exc:
        raise ParityError("relation observation requires a validated TaskIR") from exc
    from elt_taskgen.verification.parity_scope import derive_task_parity_manifest

    manifest = derive_task_parity_manifest(validated_task, resolved_destination)
    try:
        spec = next(
            item
            for item in parity_case_specs(
                resolved_destination, case_ids=manifest.case_ids
            )
            if item.case_id == case_id
        )
    except StopIteration as exc:
        raise ParityError(
            f"relation observation names undeclared case {case_id!r}"
        ) from exc
    if spec.evidence_kind is not ParityEvidenceKind.RELATION_FINGERPRINT:
        raise ParityError(
            f"parity case {case_id!r} does not accept relation-fingerprint evidence"
        )
    if fingerprint.row_order is not spec.required_row_order:
        raise ParityError(
            f"parity case {case_id!r} requires {spec.required_row_order.value} row order"
        )
    if engine not in {spec.reference_engine, ParityEngine.REAL_WAREHOUSE}:
        raise ParityError(
            f"relation case {case_id!r} requires DuckDB or real-warehouse evidence"
        )
    return ParityObservation(
        case_id=case_id,
        destination=resolved_destination,
        engine=engine,
        evidence_origin=evidence_origin,
        evidence_kind=ParityEvidenceKind.RELATION_FINGERPRINT,
        row_order=fingerprint.row_order,
        semantic_release_id=semantic_release_id,
        task_id=validated_task.task_id,
        task_content_hash=manifest.task_content_hash,
        required_case_manifest_digest=manifest.manifest_digest,
        runtime_bundle_id=runtime_bundle_id,
        attempt_id=attempt_id,
        solution_digest=solution_digest,
        probe_id=probe_id,
        probe_binding_digest=probe_binding_digest,
        matrix_digest=parity_matrix_digest(matrix),
        required_case_set_digest=manifest.case_set_digest,
        population=population,
        population_digest=population_digest,
        producer_version=producer_version,
        succeeded=True,
        result_digest=fingerprint.result_digest,
        schema_digest=fingerprint.schema_digest,
        row_count=fingerprint.row_count,
    )


def _exact_verdict(
    reference: ParityObservation | None,
    real: ParityObservation | None,
) -> tuple[ParityVerdict, str]:
    if reference is None and real is None:
        return ParityVerdict.MISSING_BOTH, "reference and real evidence are missing"
    if reference is None:
        return ParityVerdict.MISSING_REFERENCE, "local reference evidence is missing"
    if real is None:
        return ParityVerdict.REFERENCE_ONLY, "real-warehouse evidence is missing"
    if not reference.succeeded and not real.succeeded:
        return ParityVerdict.BOTH_FAILED, "both executions failed"
    if not reference.succeeded:
        return ParityVerdict.REFERENCE_FAILED, "local reference execution failed"
    if not real.succeeded:
        return ParityVerdict.REAL_FAILED, "real-warehouse execution failed"
    reference_fingerprint = (
        reference.result_digest,
        reference.schema_digest,
        reference.row_count,
    )
    real_fingerprint = (real.result_digest, real.schema_digest, real.row_count)
    if reference_fingerprint != real_fingerprint:
        differing = [
            label
            for label, left, right in zip(
                ("result_digest", "schema_digest", "row_count"),
                reference_fingerprint,
                real_fingerprint,
                strict=True,
            )
            if left != right
        ]
        return (
            ParityVerdict.MISMATCH,
            "local-reference and real evidence differ: " + ", ".join(differing),
        )
    return ParityVerdict.MATCHED, "local-reference and real fingerprints match"


def verify_parity(
    *,
    destination: Destination | str,
    semantic_release_id: str,
    task: TaskIR,
    expected_task_content_hash: str,
    runtime_bundle_id: str,
    attempt_id: str,
    solution_digest: str,
    probe_id: str,
    probe_binding_digest: str,
    matrix: Mapping[str, str],
    expected_matrix: Mapping[str, str],
    population: str,
    population_digest: str,
    observations: Sequence[ParityObservation],
    case_ids: Sequence[str] | None = None,
) -> ParityReport:
    """Verify parity observation identity, coverage, success, and agreement.

    Malformed or cross-run observations raise `ParityError`; incomplete or disagreeing
    evidence returns `fully_verified=False`. Full internal verification is not
    certification eligibility until protected collection and signed attestation exist.
    """

    resolved = normalize_destination(destination)
    if not isinstance(semantic_release_id, str) or not semantic_release_id:
        raise ParityError("semantic_release_id must be non-empty")
    if not isinstance(expected_task_content_hash, str) or not _SHA256.fullmatch(
        expected_task_content_hash
    ):
        raise ParityError("expected_task_content_hash must be a lowercase sha256")
    try:
        validated_task = TaskIR.model_validate(task.model_dump(mode="python"))
    except (AttributeError, ValidationError) as exc:
        raise ParityError("parity verification requires a validated TaskIR") from exc
    task_content_hash = validated_task.content_hash()
    if task_content_hash != expected_task_content_hash:
        raise ParityError(
            "TaskIR content hash does not match the frozen expected task"
        )
    task_id = validated_task.task_id
    if not isinstance(runtime_bundle_id, str) or not runtime_bundle_id:
        raise ParityError("runtime_bundle_id must be non-empty")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ParityError("attempt_id must be non-empty")
    if not isinstance(solution_digest, str) or not _SHA256.fullmatch(solution_digest):
        raise ParityError("solution_digest must be a lowercase sha256")
    if not isinstance(probe_id, str) or not _PROBE_ID.fullmatch(probe_id):
        raise ParityError("probe_id has an invalid format")
    if not isinstance(probe_binding_digest, str) or not _SHA256.fullmatch(
        probe_binding_digest
    ):
        raise ParityError("probe_binding_digest must be a lowercase sha256")
    if not isinstance(population, str) or not population:
        raise ParityError("population must be non-empty")
    if not isinstance(population_digest, str) or not _SHA256.fullmatch(
        population_digest
    ):
        raise ParityError("population_digest must be a lowercase sha256")
    _validate_parity_matrix(matrix, resolved)
    _validate_parity_matrix(expected_matrix, resolved)
    if dict(matrix) != dict(expected_matrix):
        raise ParityError(
            "execution matrix does not exactly match the frozen expected matrix"
        )
    matrix_digest = parity_matrix_digest(matrix)
    from elt_taskgen.verification.parity_scope import derive_task_parity_manifest

    required_manifest = derive_task_parity_manifest(validated_task, resolved)
    required_specs = parity_case_specs(
        resolved, case_ids=required_manifest.case_ids
    )
    required_case_set_digest = required_manifest.case_set_digest
    specs = (
        required_specs
        if case_ids is None
        else parity_case_specs(resolved, case_ids=case_ids)
    )
    required_ids = {spec.case_id for spec in required_specs}
    outside_scope = sorted(
        spec.case_id for spec in specs if spec.case_id not in required_ids
    )
    if outside_scope:
        raise ParityError(
            f"diagnostic cases are outside the required task scope: {outside_scope}"
        )
    selected_case_set_digest = parity_case_set_digest(specs)
    required_scope_coverage = specs == required_specs
    specs_by_id = {spec.case_id: spec for spec in specs}

    indexed: dict[tuple[str, ParityEngine], ParityObservation] = {}
    for supplied_observation in observations:
        if not isinstance(supplied_observation, ParityObservation):
            raise ParityError("parity observations must use the validated schema")
        try:
            observation = ParityObservation.model_validate(
                supplied_observation.model_dump(mode="python")
            )
        except ValidationError as exc:
            raise ParityError("parity observation fails schema validation") from exc
        if observation.destination is not resolved:
            raise ParityError(
                f"observation {observation.case_id!r} names destination "
                f"{observation.destination.value!r}, expected {resolved.value!r}"
            )
        if observation.semantic_release_id != semantic_release_id:
            raise ParityError(
                f"observation {observation.case_id!r} is bound to another semantic release"
            )
        if observation.task_id != task_id:
            raise ParityError(
                f"observation {observation.case_id!r} is bound to another task"
            )
        if observation.task_content_hash != task_content_hash:
            raise ParityError(
                f"observation {observation.case_id!r} is bound to another task content"
            )
        if (
            observation.required_case_manifest_digest
            != required_manifest.manifest_digest
        ):
            raise ParityError(
                f"observation {observation.case_id!r} is bound to another required case manifest"
            )
        if observation.runtime_bundle_id != runtime_bundle_id:
            raise ParityError(
                f"observation {observation.case_id!r} is bound to another runtime bundle"
            )
        if observation.attempt_id != attempt_id:
            raise ParityError(
                f"observation {observation.case_id!r} is bound to another attempt"
            )
        if observation.solution_digest != solution_digest:
            raise ParityError(
                f"observation {observation.case_id!r} is bound to another solution"
            )
        if observation.probe_id != probe_id:
            raise ParityError(
                f"observation {observation.case_id!r} is bound to another probe"
            )
        if observation.probe_binding_digest != probe_binding_digest:
            raise ParityError(
                f"observation {observation.case_id!r} is bound to another probe implementation"
            )
        if observation.matrix_digest != matrix_digest:
            raise ParityError(
                f"observation {observation.case_id!r} is bound to another execution matrix"
            )
        if observation.required_case_set_digest != required_case_set_digest:
            raise ParityError(
                f"observation {observation.case_id!r} is bound to another required case scope"
            )
        if observation.population != population:
            raise ParityError(
                f"observation {observation.case_id!r} is bound to another population"
            )
        if observation.population_digest != population_digest:
            raise ParityError(
                f"observation {observation.case_id!r} is bound to another population content"
            )
        spec = specs_by_id.get(observation.case_id)
        if spec is None:
            raise ParityError(
                f"observation names undeclared parity case {observation.case_id!r}"
            )
        if observation.evidence_kind is not spec.evidence_kind:
            raise ParityError(
                f"observation {observation.case_id!r} has evidence kind "
                f"{observation.evidence_kind.value!r}, expected {spec.evidence_kind.value!r}"
            )
        if observation.row_order is not spec.required_row_order:
            expected_order = (
                spec.required_row_order.value
                if spec.required_row_order is not None
                else "none"
            )
            raise ParityError(
                f"observation {observation.case_id!r} must use row order {expected_order!r}"
            )
        if spec.comparison is ParityComparison.EXACT and observation.succeeded:
            if not observation.schema_digest or observation.row_count is None:
                raise ParityError(
                    f"successful exact case {spec.case_id!r} requires schema and row-count evidence"
                )
        if (
            spec.comparison is ParityComparison.REAL_ONLY
            and observation.engine is not ParityEngine.REAL_WAREHOUSE
        ):
            raise ParityError(
                f"real-only parity case {spec.case_id!r} cannot carry local reference evidence"
            )
        if (
            spec.comparison is ParityComparison.EXACT
            and observation.engine
            not in {spec.reference_engine, ParityEngine.REAL_WAREHOUSE}
        ):
            raise ParityError(
                f"exact parity case {spec.case_id!r} requires reference engine "
                f"{spec.reference_engine.value!r}"
            )
        key = (observation.case_id, observation.engine)
        if key in indexed:
            raise ParityError(
                f"duplicate {observation.engine.value} evidence for {observation.case_id!r}"
            )
        indexed[key] = observation

    results: list[ParityCaseResult] = []
    for spec in specs:
        reference = (
            indexed.get((spec.case_id, spec.reference_engine))
            if spec.reference_engine is not None
            else None
        )
        real = indexed.get((spec.case_id, ParityEngine.REAL_WAREHOUSE))
        if spec.comparison is ParityComparison.EXACT:
            verdict, reason = _exact_verdict(reference, real)
        elif real is None:
            verdict, reason = (
                ParityVerdict.MISSING_REAL,
                "real-warehouse evidence is required for this non-emulatable case",
            )
        elif not real.succeeded:
            verdict, reason = (
                ParityVerdict.REAL_FAILED,
                "real-warehouse execution failed",
            )
        elif real.evidence_origin is ParityEvidenceOrigin.LIVE_CLOUD:
            verdict, reason = (
                ParityVerdict.REAL_ONLY_VERIFIED,
                "required real-warehouse behavior has a live-origin claim",
            )
        else:
            verdict, reason = (
                ParityVerdict.REAL_ONLY_REPLAYED,
                "recorded replay is non-certifying for real-only behavior",
            )
        results.append(
            ParityCaseResult(
                case_id=spec.case_id,
                stage=spec.stage,
                comparison=spec.comparison,
                verdict=verdict,
                reference_present=reference is not None,
                real_present=real is not None,
                reference_origin=(
                    reference.evidence_origin if reference is not None else None
                ),
                real_origin=real.evidence_origin if real is not None else None,
                reason=reason,
            )
        )

    exact_results = [
        result
        for result in results
        if result.comparison is ParityComparison.EXACT
    ]
    real_only_results = [
        result
        for result in results
        if result.comparison is ParityComparison.REAL_ONLY
    ]
    reference_complete = bool(exact_results) and all(
        result.reference_present
        and result.verdict
        not in {
            ParityVerdict.REFERENCE_FAILED,
            ParityVerdict.BOTH_FAILED,
            ParityVerdict.MISSING_REFERENCE,
            ParityVerdict.MISSING_BOTH,
        }
        for result in exact_results
    )
    real_complete = all(
        result.real_present
        and result.verdict
        not in {
            ParityVerdict.REAL_FAILED,
            ParityVerdict.BOTH_FAILED,
            ParityVerdict.REFERENCE_ONLY,
            ParityVerdict.MISSING_REAL,
            ParityVerdict.MISSING_BOTH,
        }
        for result in results
    )
    exact_verified = bool(exact_results) and all(
        result.verdict is ParityVerdict.MATCHED for result in exact_results
    )
    real_only_verified = bool(real_only_results) and all(
        result.verdict is ParityVerdict.REAL_ONLY_VERIFIED
        for result in real_only_results
    )
    real_observations = [
        observation
        for observation in indexed.values()
        if observation.engine is ParityEngine.REAL_WAREHOUSE
    ]
    live_evidence_complete = real_complete and all(
        observation.evidence_origin is ParityEvidenceOrigin.LIVE_CLOUD
        for observation in real_observations
    )
    exact_scope_verified = not exact_results or exact_verified
    real_only_scope_verified = not real_only_results or real_only_verified
    selected_scope_verified = (
        exact_scope_verified
        and real_only_scope_verified
        and live_evidence_complete
    )
    fully_verified = selected_scope_verified and required_scope_coverage
    observation_set_digest = parity_observation_set_digest(
        tuple(indexed.values())
    )
    identity_binding_digest = parity_report_binding_digest(
        destination=resolved,
        semantic_release_id=semantic_release_id,
        task_id=task_id,
        task_content_hash=task_content_hash,
        required_case_manifest_digest=required_manifest.manifest_digest,
        runtime_bundle_id=runtime_bundle_id,
        attempt_id=attempt_id,
        solution_digest=solution_digest,
        probe_id=probe_id,
        probe_binding_digest=probe_binding_digest,
        matrix_digest=matrix_digest,
        required_case_set_digest=required_case_set_digest,
        selected_case_set_digest=selected_case_set_digest,
        population=population,
        population_digest=population_digest,
        observation_set_digest=observation_set_digest,
    )
    return ParityReport(
        destination=resolved,
        semantic_release_id=semantic_release_id,
        task_id=task_id,
        task_content_hash=task_content_hash,
        required_case_manifest_digest=required_manifest.manifest_digest,
        runtime_bundle_id=runtime_bundle_id,
        attempt_id=attempt_id,
        solution_digest=solution_digest,
        probe_id=probe_id,
        probe_binding_digest=probe_binding_digest,
        matrix_digest=matrix_digest,
        required_case_ids=tuple(spec.case_id for spec in required_specs),
        required_case_set_digest=required_case_set_digest,
        selected_case_set_digest=selected_case_set_digest,
        observation_set_digest=observation_set_digest,
        identity_binding_digest=identity_binding_digest,
        population=population,
        population_digest=population_digest,
        cases=tuple(results),
        reference_complete=reference_complete,
        real_complete=real_complete,
        reference_real_verified=exact_verified,
        real_only_verified=real_only_verified,
        live_evidence_complete=live_evidence_complete,
        selected_scope_verified=selected_scope_verified,
        required_scope_coverage=required_scope_coverage,
        fully_verified=fully_verified,
    )


__all__ = [
    "PARITY_CASE_SPECS",
    "PARITY_CONTRACT_VERSION",
    "PARITY_OBSERVATION_SCHEMA_VERSION",
    "PARITY_REGISTRY_DIGEST",
    "ParityCaseResult",
    "ParityCaseSpec",
    "ParityComparison",
    "ParityEngine",
    "ParityEvidenceKind",
    "ParityEvidenceOrigin",
    "ParityError",
    "ParityObservation",
    "ParityReport",
    "ParityStage",
    "ParityVerdict",
    "parity_case_specs",
    "parity_case_set_digest",
    "parity_matrix_digest",
    "parity_observation_set_digest",
    "parity_registry_digest",
    "parity_report_binding_digest",
    "parity_report_digest",
    "relation_observation",
    "verify_parity",
]

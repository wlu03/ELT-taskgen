"""Record immutable live-runtime certification evidence.

A certification ID binds a runtime bundle to an execution matrix. State moves
from unverified to pending to certified; refusal removes the pending record and
records why. Changed inputs produce a new ID. Stores remain outside releases,
and attestations contain no rows or credentials.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from elt_taskgen import runtime_matrix
from elt_taskgen.destinations import destination_contract
from elt_taskgen.export import release as release_mod
from elt_taskgen.models import PopulationName, canonical_json, readable_json, sha256_hex
from elt_taskgen.runtime.execution import (
    ExecutionError,
    validate_stage1_execution_receipt,
    validate_stage2_execution_receipt,
)

try:  # Advisory inode locking is available on the Unix hosts that run Docker.
    import fcntl
except ImportError:  # pragma: no cover - fail closed on unsupported hosts.
    fcntl = None  # type: ignore[assignment]

ATTESTATION_FILENAME = "attestation.json"
PENDING_FILENAME = "pending.json"
REFUSED_FILENAME = "refused.json"
EVIDENCE_DIRNAME = "evidence"

STAGE_EVIDENCE_SCHEMA_VERSION = (
    runtime_matrix.CERTIFICATION_STAGE_EVIDENCE_SCHEMA_VERSION
)
ATTESTATION_SCHEMA_VERSION = runtime_matrix.CERTIFICATION_ATTESTATION_SCHEMA_VERSION
PENDING_SCHEMA_VERSION = runtime_matrix.CERTIFICATION_PENDING_SCHEMA_VERSION

_MAX_CERTIFICATION_JSON_BYTES = 16 * 1024 * 1024
_LEGACY_STAGE_EVIDENCE_SCHEMA_VERSIONS = frozenset({"1.1"})
_LEGACY_ATTESTATION_SCHEMA_VERSIONS = frozenset({"1.2"})
_V10_PHYSICAL_CONTAINER_REQUIRED = {
    "snowflake": False,
    "databricks": True,
    "redshift": True,
}


class CertificationError(RuntimeError):
    """A certification attempt cannot proceed or be trusted."""


class CertificationState(str, Enum):
    UNCERTIFIED = "uncertified"
    PENDING = "pending"
    CERTIFIED = "certified"


StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
StrictPositiveInt = Annotated[int, Field(strict=True, ge=1)]
StrictUnitFloat = Annotated[float, Field(strict=True, ge=0.0, le=1.0)]
StrictBoolean = Annotated[bool, Field(strict=True)]


class CountMismatchEvidence(BaseModel):
    """Serialized form of runtime.evaluation.CountMismatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected: StrictNonNegativeInt
    actual: StrictNonNegativeInt


class Stage1ResultEvidence(BaseModel):
    """Exact Stage-1 evaluator result needed to derive strict certification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    database: str = Field(min_length=1)
    physical_container: str
    population: str = Field(min_length=1)
    expected_counts: dict[str, StrictNonNegativeInt]
    actual_counts: dict[str, StrictNonNegativeInt]
    missing_tables: tuple[str, ...]
    unexpected_tables: tuple[str, ...]
    count_mismatches: dict[str, CountMismatchEvidence]
    detail: dict[str, str]
    errors: dict[str, str]
    passed: StrictBoolean
    reward: StrictUnitFloat
    expected_repetitions: StrictPositiveInt
    canonical_table_scores: dict[str, StrictBoolean]
    canonical_mismatch_codes: dict[str, str]
    canonical_reference_fingerprints: dict[str, str]
    canonical_actual_fingerprints: dict[str, str]


class Stage2ResultEvidence(BaseModel):
    """Exact Stage-2 evaluator result needed to derive strict certification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    database: str = Field(min_length=1)
    physical_container: str
    population: str = Field(min_length=1)
    mart_scores: dict[str, StrictBoolean]
    errors: dict[str, str]
    reward: StrictUnitFloat
    strict_mart_scores: dict[str, StrictBoolean]
    strict_mismatch_codes: dict[str, str]
    column_fingerprints: dict[str, tuple[tuple[str, str], ...]]
    canonical_mart_scores: dict[str, StrictBoolean]
    canonical_mismatch_codes: dict[str, str]
    canonical_reference_fingerprints: dict[str, str]
    canonical_actual_fingerprints: dict[str, str]


class _StageEvidenceBase(BaseModel):
    """Identity envelope shared by both ordered certification stages."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_schema_version: str
    release_schema_version: str = Field(min_length=1)
    certification_id: str = Field(min_length=1)
    runtime_bundle_id: str = Field(min_length=1)
    semantic_release_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    population: str = Field(min_length=1)
    warehouse_namespace: str = Field(min_length=1)
    physical_container: str
    attempt_id: str = Field(min_length=1, max_length=256)
    execution_started_at: str = Field(min_length=1)
    execution_completed_at: str = Field(min_length=1)
    recorded_at: str = Field(min_length=1)
    evidence_digest: str = ""


class Stage1CertificationEvidence(_StageEvidenceBase):
    """Sealed, redacted evidence emitted from a strict Stage-1 evaluation."""

    stage: Literal["stage1"]
    # Empty only while parsing legacy evidence schema 1.1. The current 1.2
    # schema requires the executed image and its verifier enforces it below.
    runner_image: str = ""
    terraform_input_tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    terraform_state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    airbyte_job_ids: dict[str, str]
    result: Stage1ResultEvidence

    @model_validator(mode="after")
    def require_current_execution_identity(self) -> "Stage1CertificationEvidence":
        if self.evidence_schema_version == STAGE_EVIDENCE_SCHEMA_VERSION:
            _canonical_observation(
                self.runner_image, label="Stage-1 Terraform runner image"
            )
        return self


class Stage2CertificationEvidence(_StageEvidenceBase):
    """Sealed, redacted evidence emitted from a strict Stage-2 evaluation."""

    stage: Literal["stage2"]
    # Empty only while parsing legacy evidence schema 1.1.
    runner_image: str = ""
    preflight_destination: str = ""
    preflight_dbt_core_version: str = ""
    preflight_adapter_version: str = ""
    dbt_input_tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    dbt_invocation_id: str = Field(min_length=1, max_length=256)
    dbt_run_results_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    dbt_expected_model_ids: tuple[str, ...] = ()
    dbt_observed_model_ids: tuple[str, ...] = ()
    result: Stage2ResultEvidence

    @model_validator(mode="after")
    def require_current_execution_identity(self) -> "Stage2CertificationEvidence":
        if self.evidence_schema_version == STAGE_EVIDENCE_SCHEMA_VERSION:
            for value, label in (
                (self.runner_image, "Stage-2 dbt runner image"),
                (self.preflight_destination, "Stage-2 preflight destination"),
                (
                    self.preflight_dbt_core_version,
                    "Stage-2 preflight dbt-core version",
                ),
                (
                    self.preflight_adapter_version,
                    "Stage-2 preflight adapter version",
                ),
            ):
                _canonical_observation(value, label=label)
            _normalize_model_ids(
                self.dbt_expected_model_ids,
                label="Stage-2 expected dbt model ids",
            )
            _normalize_model_ids(
                self.dbt_observed_model_ids,
                label="Stage-2 observed dbt model ids",
            )
        return self


StageEvidencePair = tuple[
    Stage1CertificationEvidence | Path | str,
    Stage2CertificationEvidence | Path | str,
]


class CertificationAttestation(BaseModel):
    """Immutable, checksum-bound record of one completed certification.

    The declared matrix must match observed versions. Evidence digests come
    from normalized, sealed stage files and cannot be supplied by callers.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    attestation_schema_version: str
    release_schema_version: str = Field(min_length=1)
    certification_id: str = Field(min_length=1)
    runtime_bundle_id: str = Field(min_length=1)
    semantic_release_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    #: Sorted, unique population coverage proven by matched strict stage pairs.
    populations: tuple[str, ...]
    matrix: dict[str, str]
    observed_versions: dict[str, str]
    stage1_verified: StrictBoolean
    stage2_verified: StrictBoolean
    evidence_digests: dict[str, str]
    #: ISO-8601 UTC timestamps.
    started_at: str
    completed_at: str
    #: sha256 over the canonical record with this field blanked; sealing
    #: writes it, verification recomputes it (tamper detection).
    attestation_digest: str = ""


class CertificationStatus(BaseModel):
    """Report-only view of one certification id's state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    certification_id: str = Field(min_length=1)
    state: CertificationState
    populations: tuple[str, ...] = ()
    reason: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _read_bounded_regular_file(
    path: Path,
    *,
    label: str,
    limit: int = _MAX_CERTIFICATION_JSON_BYTES,
) -> bytes:
    """Read one bounded regular file without following or racing a symlink."""

    path = Path(path)
    try:
        before = path.lstat()
    except OSError as exc:
        raise CertificationError(f"{label} is missing or unreadable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_size > limit
    ):
        raise CertificationError(f"{label} is not a bounded regular file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CertificationError(f"{label} is missing or unreadable") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size > limit
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise CertificationError(f"{label} changed while it was opened")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1 << 20, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise CertificationError(f"{label} exceeds its safety bound")
        finished = os.fstat(descriptor)
        if (
            (finished.st_dev, finished.st_ino)
            != (opened.st_dev, opened.st_ino)
            or finished.st_size != opened.st_size
            or finished.st_mtime_ns != opened.st_mtime_ns
            or total != opened.st_size
        ):
            raise CertificationError(f"{label} changed while it was read")
        return b"".join(chunks)
    except OSError as exc:
        raise CertificationError(f"{label} is unreadable") from exc
    finally:
        os.close(descriptor)


def _decode_json_object(data: bytes, *, label: str) -> dict[str, Any]:
    """Decode a JSON object while refusing ambiguous duplicate keys."""

    def no_duplicate_pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(data, object_pairs_hook=no_duplicate_pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise CertificationError(f"{label} is not unambiguous JSON") from exc
    if not isinstance(payload, dict):
        raise CertificationError(f"{label} is not a JSON object")
    return payload


def _read_json_object(
    path: Path,
    *,
    label: str,
    limit: int = _MAX_CERTIFICATION_JSON_BYTES,
) -> tuple[dict[str, Any], bytes]:
    data = _read_bounded_regular_file(path, label=label, limit=limit)
    return _decode_json_object(data, label=label), data


def _entry_exists(path: Path) -> bool:
    """Return whether a directory entry exists, including broken symlinks."""

    return os.path.lexists(Path(path))


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(Path(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _quarantine_orphan_evidence(id_dir: Path) -> Path | None:
    """Move an unpublished evidence tree aside without deleting audit bytes."""

    evidence = Path(id_dir) / EVIDENCE_DIRNAME
    if not _entry_exists(evidence):
        return None
    orphan = Path(id_dir) / (".orphaned-evidence-" + secrets.token_hex(16))
    os.rename(evidence, orphan)
    _fsync_directory(Path(id_dir))
    return orphan


def _publish_immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Publish a complete owner-readable JSON record without overwriting."""

    path = Path(path)
    data = (readable_json(dict(payload)) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.stage-",
    )
    temporary = Path(temporary_name)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while publishing certification record")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        os.close(descriptor)
        descriptor = -1
        # A same-filesystem hard link is atomic and, unlike os.replace, refuses
        # an already-published attestation from a concurrent completion.
        os.link(temporary, path)
        temporary.unlink()
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _replace_json_without_following(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically replace a mutable report without following its final entry."""

    path = Path(path)
    data = (readable_json(dict(payload)) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.stage-",
    )
    temporary = Path(temporary_name)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while publishing certification report")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        os.close(descriptor)
        descriptor = -1
        # os.replace replaces a final symlink entry itself; it never opens or
        # truncates the symlink target as Path.write_text() would.
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _stage_evidence_digest(evidence: _StageEvidenceBase) -> str:
    payload = evidence.model_dump(mode="json")
    if evidence.evidence_schema_version in _LEGACY_STAGE_EVIDENCE_SCHEMA_VERSIONS:
        payload.pop("runner_image", None)
        result = payload.get("result")
        if isinstance(result, dict):
            # Evidence 1.1 predates the evaluator result's duplicate container
            # field. Its sealed top-level container is migrated into the
            # nested result only after the historical digest is reproduced.
            result.pop("physical_container", None)
        if isinstance(evidence, Stage2CertificationEvidence):
            for key in (
                "preflight_destination",
                "preflight_dbt_core_version",
                "preflight_adapter_version",
                "dbt_expected_model_ids",
                "dbt_observed_model_ids",
            ):
                payload.pop(key, None)
    payload["evidence_digest"] = ""
    return sha256_hex(canonical_json(payload))


def _requires_physical_container(
    destination: str,
    *,
    evidence_schema_version: str,
) -> bool:
    """Apply the container rule recorded by an evidence schema generation."""

    if evidence_schema_version in _LEGACY_STAGE_EVIDENCE_SCHEMA_VERSIONS:
        try:
            return _V10_PHYSICAL_CONTAINER_REQUIRED[destination]
        except KeyError as exc:
            raise CertificationError(
                "legacy evidence names an unsupported destination"
            ) from exc
    try:
        return bool(destination_contract(destination).physical_container_field)
    except ValueError as exc:
        raise CertificationError(
            "evidence names an unsupported destination"
        ) from exc


def _migrate_legacy_evidence_payload(
    payload: dict[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    """Convert evidence 1.1 to current models without changing its seal.

    Copy the sealed physical container into the nested result. Do not infer it
    from the logical namespace, which identifies a different warehouse level.
    """

    if (
        payload.get("evidence_schema_version")
        not in _LEGACY_STAGE_EVIDENCE_SCHEMA_VERSIONS
    ):
        return payload
    migrated = dict(payload)
    destination = migrated.get("destination")
    container = migrated.get("physical_container")
    result = migrated.get("result")
    if not isinstance(destination, str) or not isinstance(container, str):
        raise CertificationError(
            f"{label} has no sealed legacy physical-container identity"
        )
    if not isinstance(result, dict):
        raise CertificationError(f"{label} has no evaluator result object")
    requires_container = _requires_physical_container(
        destination,
        evidence_schema_version=str(migrated["evidence_schema_version"]),
    )
    if (
        container != container.strip()
        or any(ord(char) < 32 for char in container)
        or requires_container != bool(container)
    ):
        raise CertificationError(
            f"{label} has invalid legacy physical-container coverage"
        )
    migrated_result = dict(result)
    nested = migrated_result.get("physical_container")
    if nested is not None and (
        not isinstance(nested, str)
        or nested.strip().casefold() != container.casefold()
    ):
        raise CertificationError(
            f"{label} nested physical container contradicts its sealed envelope"
        )
    migrated_result["physical_container"] = container
    migrated["result"] = migrated_result
    return migrated


def _normalize_airbyte_job_ids(
    values: Mapping[str, str | int],
) -> dict[str, str]:
    """Return one positive, unique job id for each non-empty connection id."""

    jobs: dict[str, str] = {}
    for raw_connection, raw_job in sorted(values.items(), key=lambda item: str(item[0])):
        connection_id = str(raw_connection)
        if (
            not connection_id
            or connection_id != connection_id.strip()
            or any(ord(char) < 32 for char in connection_id)
        ):
            raise CertificationError("Airbyte connection ids must be non-empty text")
        if isinstance(raw_job, bool):
            raise CertificationError("Airbyte job ids must be positive integers")
        job_id = str(raw_job)
        if not job_id.isascii() or not job_id.isdecimal() or int(job_id) < 1:
            raise CertificationError("Airbyte job ids must be positive integers")
        # Canonical spelling prevents '01' and '1' from masquerading as two ids.
        if job_id != str(int(job_id)):
            raise CertificationError("Airbyte job ids must use canonical integers")
        jobs[connection_id] = job_id
    if not jobs:
        raise CertificationError(
            "Stage-1 evidence requires non-empty Airbyte connection/job ids"
        )
    if len(set(jobs.values())) != len(jobs):
        raise CertificationError(
            "Stage-1 evidence reuses one Airbyte job for multiple connections"
        )
    return jobs


def _canonical_observation(value: Any, *, label: str) -> str:
    """Validate one secret-free, bounded execution identity string."""

    if not isinstance(value, str):
        raise CertificationError(f"{label} must be text")
    if (
        not value
        or value != value.strip()
        or len(value) > 2048
        or any(ord(char) < 32 for char in value)
    ):
        raise CertificationError(f"{label} is not canonical text")
    return value


def _normalize_model_ids(values: Any, *, label: str) -> tuple[str, ...]:
    """Return one non-empty, sorted, unique dbt model unique-id roster."""

    if isinstance(values, (str, bytes, bytearray)):
        raise CertificationError(f"{label} must be a model-id sequence")
    try:
        roster = tuple(values)
    except TypeError as exc:
        raise CertificationError(f"{label} must be a model-id sequence") from exc
    if not roster:
        raise CertificationError(f"{label} must not be empty")
    for model_id in roster:
        canonical = _canonical_observation(model_id, label=label)
        if not canonical.startswith("model.") or canonical.count(".") < 2:
            raise CertificationError(f"{label} contains an invalid dbt model id")
    if roster != tuple(sorted(set(roster))):
        raise CertificationError(f"{label} must be sorted and unique")
    return roster


def _verify_stage2_model_roster(
    record: Stage2CertificationEvidence,
    *,
    expected_marts: set[str] | None = None,
) -> None:
    """Bind the successful dbt artifact roster to the evaluated mart roster."""

    expected = _normalize_model_ids(
        record.dbt_expected_model_ids,
        label="Stage-2 expected dbt model ids",
    )
    observed = _normalize_model_ids(
        record.dbt_observed_model_ids,
        label="Stage-2 observed dbt model ids",
    )
    if expected != observed:
        raise CertificationError(
            "Stage-2 expected and observed dbt model rosters disagree"
        )
    model_names = tuple(model_id.rsplit(".", 1)[-1] for model_id in observed)
    mart_roster = set(record.result.mart_scores)
    if expected_marts is not None and mart_roster != expected_marts:
        raise CertificationError(
            "Stage-2 evaluator result does not exactly cover the released mart roster"
        )
    target_marts = mart_roster if expected_marts is None else expected_marts
    if len(set(model_names)) != len(model_names) or set(model_names) != target_marts:
        raise CertificationError(
            "Stage-2 dbt model results do not exactly cover the released mart roster"
        )


def seal_stage_evidence(
    evidence: Stage1CertificationEvidence | Stage2CertificationEvidence,
) -> Stage1CertificationEvidence | Stage2CertificationEvidence:
    """Return an immutable evidence record bound to all result and ID fields."""
    return evidence.model_copy(
        update={"evidence_digest": _stage_evidence_digest(evidence)}
    )


def _evidence_identity(
    manifest: release_mod.ReleaseManifest,
    task_id: str,
    *,
    population: str,
    attempt_id: str,
    warehouse_namespace: str,
    physical_container: str | None,
    execution_started_at: str,
    execution_completed_at: str,
) -> dict[str, str]:
    certification_id, bundle, matrix, destination = _manifest_certification_chain(
        manifest, task_id
    )
    source_populations = set((manifest.el_sources.get(task_id) or {}))
    if population not in source_populations:
        raise CertificationError(
            f"population {population!r} is not a released source population "
            f"for task {task_id!r}"
        )
    if not str(attempt_id).strip():
        raise CertificationError("attempt_id must not be empty")
    if not str(warehouse_namespace).strip():
        raise CertificationError("warehouse_namespace must not be empty")
    container = str(physical_container or "").strip()
    if matrix.get("warehouse:physical_container_field") and not container:
        raise CertificationError(
            f"{destination} evidence requires its physical warehouse container"
        )
    recorded_at = _utc_now()
    _validate_execution_window(
        execution_started_at,
        execution_completed_at,
        recorded_at=recorded_at,
        stage="certification evidence",
    )
    return {
        "release_schema_version": manifest.schema_version,
        "certification_id": certification_id,
        "runtime_bundle_id": bundle,
        "semantic_release_id": manifest.semantic_release_id,
        "release_id": manifest.release_id,
        "task_id": task_id,
        "destination": destination,
        "population": population,
        "warehouse_namespace": str(warehouse_namespace),
        "physical_container": container,
        "attempt_id": str(attempt_id),
        "execution_started_at": execution_started_at,
        "execution_completed_at": execution_completed_at,
        "recorded_at": recorded_at,
    }


def _bound_physical_container(
    manifest: release_mod.ReleaseManifest,
    task_id: str,
    *,
    evaluator_container: Any,
    claimed_container: str | None,
    stage: str,
) -> str:
    """Bind evidence to the container actually observed by the evaluator."""

    _, _, matrix, destination = _manifest_certification_chain(manifest, task_id)
    if not isinstance(evaluator_container, str):
        raise CertificationError(
            f"{stage} evaluator physical container must be text"
        )
    observed = evaluator_container.strip()
    if observed != evaluator_container or any(ord(char) < 32 for char in observed):
        raise CertificationError(
            f"{stage} evaluator physical container is not canonical"
        )
    required = bool(matrix.get("warehouse:physical_container_field"))
    if required and not observed:
        raise CertificationError(
            f"{destination} {stage} evaluator recorded no physical container"
        )
    if not required and observed:
        raise CertificationError(
            f"{destination} {stage} evaluator recorded an unexpected physical container"
        )
    if claimed_container is not None:
        claimed = str(claimed_container).strip()
        if claimed.casefold() != observed.casefold():
            raise CertificationError(
                f"{stage} claimed physical container disagrees with the evaluator"
            )
    return observed


def _verify_evidence_container(record: _StageEvidenceBase, result: Any) -> None:
    required = _requires_physical_container(
        record.destination,
        evidence_schema_version=record.evidence_schema_version,
    )
    evidence_container = record.physical_container
    result_container = result.physical_container
    if (
        evidence_container != evidence_container.strip()
        or result_container != result_container.strip()
        or any(ord(char) < 32 for char in evidence_container + result_container)
    ):
        raise CertificationError(
            f"{record.stage} evidence physical container is not canonical"
        )
    if required != bool(evidence_container):
        raise CertificationError(
            f"{record.stage} evidence has invalid physical-container coverage"
        )
    if evidence_container.casefold() != result_container.casefold():
        raise CertificationError(
            f"{record.stage} evidence physical container disagrees with its "
            "evaluator result"
        )


def stage1_evidence_from_result(
    manifest: release_mod.ReleaseManifest,
    task_id: str,
    result: Any,
    *,
    attempt_id: str,
    execution_started_at: str,
    execution_completed_at: str,
    runner_image: str,
    terraform_input_tree_digest: str,
    terraform_state_digest: str,
    airbyte_job_ids: Mapping[str, str | int],
    physical_container: str | None = None,
) -> Stage1CertificationEvidence:
    """Normalize a Stage 1 result through the closed evidence model.

    Production callers use :func:`stage1_evidence_from_execution`. This helper
    accepts unauthenticated provenance values for sealed-format tests.
    """

    runner = _canonical_observation(
        runner_image, label="Stage-1 Terraform runner image"
    )
    if not _is_sha256(str(terraform_input_tree_digest)):
        raise CertificationError("terraform_input_tree_digest is not a SHA-256")
    if not _is_sha256(str(terraform_state_digest)):
        raise CertificationError("terraform_state_digest is not a SHA-256")
    jobs = _normalize_airbyte_job_ids(airbyte_job_ids)
    try:
        result_evidence = Stage1ResultEvidence.model_validate(asdict(result))
    except (TypeError, ValueError) as exc:
        raise CertificationError(f"Stage-1 evaluator result is invalid: {exc}") from exc
    container = _bound_physical_container(
        manifest,
        task_id,
        evaluator_container=result_evidence.physical_container,
        claimed_container=physical_container,
        stage="Stage-1",
    )
    identity = _evidence_identity(
        manifest,
        task_id,
        population=result_evidence.population,
        attempt_id=attempt_id,
        warehouse_namespace=result_evidence.database,
        physical_container=container,
        execution_started_at=execution_started_at,
        execution_completed_at=execution_completed_at,
    )
    return seal_stage_evidence(
        Stage1CertificationEvidence(
            **identity,
            evidence_schema_version=STAGE_EVIDENCE_SCHEMA_VERSION,
            stage="stage1",
            runner_image=runner,
            terraform_input_tree_digest=str(terraform_input_tree_digest),
            terraform_state_digest=str(terraform_state_digest),
            airbyte_job_ids=jobs,
            result=result_evidence,
        )
    )


def stage2_evidence_from_result(
    manifest: release_mod.ReleaseManifest,
    task_id: str,
    result: Any,
    *,
    attempt_id: str,
    execution_started_at: str,
    execution_completed_at: str,
    runner_image: str,
    preflight_destination: str,
    preflight_dbt_core_version: str,
    preflight_adapter_version: str,
    dbt_input_tree_digest: str,
    dbt_invocation_id: str,
    dbt_run_results_digest: str,
    dbt_expected_model_ids: Sequence[str],
    dbt_observed_model_ids: Sequence[str],
    physical_container: str | None = None,
) -> Stage2CertificationEvidence:
    """Low-level trusted-harness normalizer for Stage-2 evidence.

    Production callers must use :func:`stage2_evidence_from_execution`, which
    re-reads the dbt artifact and checks the pinned execution receipt.
    """

    runner = _canonical_observation(runner_image, label="Stage-2 dbt runner image")
    observed_destination = _canonical_observation(
        preflight_destination, label="Stage-2 preflight destination"
    )
    observed_dbt_core = _canonical_observation(
        preflight_dbt_core_version, label="Stage-2 preflight dbt-core version"
    )
    observed_adapter = _canonical_observation(
        preflight_adapter_version, label="Stage-2 preflight adapter version"
    )
    expected_models = _normalize_model_ids(
        dbt_expected_model_ids, label="Stage-2 expected dbt model ids"
    )
    observed_models = _normalize_model_ids(
        dbt_observed_model_ids, label="Stage-2 observed dbt model ids"
    )
    if expected_models != observed_models:
        raise CertificationError(
            "Stage-2 expected and observed dbt model rosters disagree"
        )
    if not _is_sha256(str(dbt_input_tree_digest)):
        raise CertificationError("dbt_input_tree_digest is not a SHA-256")
    if not str(dbt_invocation_id).strip():
        raise CertificationError("dbt_invocation_id must not be empty")
    if not _is_sha256(str(dbt_run_results_digest)):
        raise CertificationError("dbt_run_results_digest is not a SHA-256")
    try:
        result_evidence = Stage2ResultEvidence.model_validate(asdict(result))
    except (TypeError, ValueError) as exc:
        raise CertificationError(f"Stage-2 evaluator result is invalid: {exc}") from exc
    container = _bound_physical_container(
        manifest,
        task_id,
        evaluator_container=result_evidence.physical_container,
        claimed_container=physical_container,
        stage="Stage-2",
    )
    identity = _evidence_identity(
        manifest,
        task_id,
        population=result_evidence.population,
        attempt_id=attempt_id,
        warehouse_namespace=result_evidence.database,
        physical_container=container,
        execution_started_at=execution_started_at,
        execution_completed_at=execution_completed_at,
    )
    return seal_stage_evidence(
        Stage2CertificationEvidence(
            **identity,
            evidence_schema_version=STAGE_EVIDENCE_SCHEMA_VERSION,
            stage="stage2",
            runner_image=runner,
            preflight_destination=observed_destination,
            preflight_dbt_core_version=observed_dbt_core,
            preflight_adapter_version=observed_adapter,
            dbt_input_tree_digest=str(dbt_input_tree_digest),
            dbt_invocation_id=str(dbt_invocation_id),
            dbt_run_results_digest=str(dbt_run_results_digest),
            dbt_expected_model_ids=expected_models,
            dbt_observed_model_ids=observed_models,
            result=result_evidence,
        )
    )


def stage1_evidence_from_execution(
    manifest: release_mod.ReleaseManifest,
    task_id: str,
    result: Any,
    execution: Any,
    *,
    attempt_id: str,
    physical_container: str | None = None,
) -> Stage1CertificationEvidence:
    """Build Stage-1 evidence only from an exact runtime execution receipt."""

    _, _, matrix, _ = _manifest_certification_chain(manifest, task_id)
    try:
        validated_connection_ids = validate_stage1_execution_receipt(execution)
    except ExecutionError as exc:
        raise CertificationError(f"Stage-1 execution receipt is invalid: {exc}") from exc
    connection_ids = tuple(str(value) for value in validated_connection_ids)
    statuses = {str(key): str(value) for key, value in execution.statuses.items()}
    job_ids = {str(key): value for key, value in execution.job_ids.items()}
    if (
        not connection_ids
        or len(connection_ids) != len(set(connection_ids))
        or set(connection_ids) != set(statuses)
        or set(connection_ids) != set(job_ids)
        or any(value.casefold() != "succeeded" for value in statuses.values())
    ):
        raise CertificationError(
            "Stage-1 execution receipt does not prove one successful exact job "
            "for every Terraform connection"
        )
    if str(execution.runner_image) != matrix.get("runner_image:terraform"):
        raise CertificationError(
            "Stage-1 execution did not use the release-bound Terraform image"
        )
    return stage1_evidence_from_result(
        manifest,
        task_id,
        result,
        attempt_id=attempt_id,
        execution_started_at=str(execution.execution_started_at),
        execution_completed_at=str(execution.execution_completed_at),
        runner_image=str(execution.runner_image),
        terraform_input_tree_digest=str(execution.terraform_input_tree_digest),
        terraform_state_digest=str(execution.terraform_state_digest),
        airbyte_job_ids=job_ids,
        physical_container=physical_container,
    )


def stage2_evidence_from_execution(
    manifest: release_mod.ReleaseManifest,
    task_id: str,
    result: Any,
    execution: Any,
    *,
    attempt_id: str,
    physical_container: str | None = None,
) -> Stage2CertificationEvidence:
    """Build Stage-2 evidence from the pinned runner and fresh dbt artifact."""

    _, _, matrix, destination = _manifest_certification_chain(manifest, task_id)
    if str(execution.runner_image) != matrix.get("runner_image:dbt"):
        raise CertificationError(
            "Stage-2 execution did not use the release-bound dbt image"
        )
    preflight = execution.preflight
    preflight_destination = getattr(
        getattr(preflight, "destination", None), "value", None
    )
    if preflight is None or preflight_destination != destination:
        raise CertificationError(
            "Stage-2 execution has no matching destination preflight"
        )
    if (
        preflight.dbt_core_version != matrix.get("dbt_core_version")
        or preflight.adapter_version != matrix.get("dbt_adapter_version")
    ):
        raise CertificationError(
            "Stage-2 observed dbt versions contradict the release matrix"
        )
    result_namespace = getattr(result, "database", None)
    if (
        not isinstance(result_namespace, str)
        or preflight.namespace.strip().casefold()
        != result_namespace.strip().casefold()
    ):
        raise CertificationError(
            "Stage-2 preflight namespace disagrees with its evaluator result"
        )
    execution_container = str(preflight.physical_container or "").strip()
    container_required = bool(matrix.get("warehouse:physical_container_field"))
    if container_required != bool(execution_container):
        raise CertificationError(
            "Stage-2 preflight physical container contradicts the release matrix"
        )
    requested_container = (
        None if physical_container is None else str(physical_container).strip()
    )
    if (
        requested_container is not None
        and requested_container.casefold() != execution_container.casefold()
    ):
        raise CertificationError(
            "Stage-2 requested physical container disagrees with its preflight"
        )
    try:
        observed_model_ids = validate_stage2_execution_receipt(execution)
    except ExecutionError as exc:
        raise CertificationError(f"Stage-2 execution receipt is invalid: {exc}") from exc
    population = getattr(result, "population", None)
    if not isinstance(population, str):
        raise CertificationError("Stage-2 evaluator result has no population")
    _, released_marts = _release_relation_rosters(manifest, task_id, population)
    model_names = tuple(model_id.rsplit(".", 1)[-1] for model_id in observed_model_ids)
    if len(set(model_names)) != len(model_names) or set(model_names) != released_marts:
        raise CertificationError(
            "Stage-2 dbt model results do not exactly cover the released mart roster"
        )
    return stage2_evidence_from_result(
        manifest,
        task_id,
        result,
        attempt_id=attempt_id,
        execution_started_at=str(execution.execution_started_at),
        execution_completed_at=str(execution.execution_completed_at),
        runner_image=str(execution.runner_image),
        preflight_destination=preflight_destination,
        preflight_dbt_core_version=str(preflight.dbt_core_version),
        preflight_adapter_version=str(preflight.adapter_version),
        dbt_input_tree_digest=str(execution.dbt_input_tree_digest),
        dbt_invocation_id=str(execution.dbt_invocation_id),
        dbt_run_results_digest=str(execution.dbt_run_results_digest),
        dbt_expected_model_ids=tuple(execution.dbt_expected_model_ids),
        dbt_observed_model_ids=tuple(observed_model_ids),
        physical_container=execution_container,
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _parse_utc_timestamp(value: str, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CertificationError(f"{label} is not ISO-8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CertificationError(f"{label} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise CertificationError(f"{label} must include a timezone")
    if parsed.utcoffset() != timedelta(0):
        raise CertificationError(f"{label} must be UTC")
    return parsed


def _validate_execution_window(
    started_at: str,
    completed_at: str,
    *,
    recorded_at: str,
    stage: str,
) -> None:
    started = _parse_utc_timestamp(started_at, label=f"{stage} execution_started_at")
    completed = _parse_utc_timestamp(
        completed_at, label=f"{stage} execution_completed_at"
    )
    recorded = _parse_utc_timestamp(recorded_at, label=f"{stage} recorded_at")
    if completed < started:
        raise CertificationError(f"{stage} execution completed before it started")
    if recorded < completed:
        raise CertificationError(f"{stage} evidence predates execution completion")


def _validate_attempt_id(value: Any, *, label: str = "attempt_id") -> str:
    if not isinstance(value, str):
        raise CertificationError(f"{label} must be text")
    if (
        not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(char) < 32 for char in value)
    ):
        raise CertificationError(f"{label} is invalid")
    return value


def _load_pending_marker(pending_dir: Path) -> dict[str, Any]:
    """Read and validate one active certification-attempt capability."""

    path = Path(pending_dir) / PENDING_FILENAME
    payload, _ = _read_json_object(path, label="pending certification marker")
    expected_keys = {
        "pending_schema_version",
        "certification_id",
        "task_id",
        "attempt_id",
        "started_at",
    }
    if set(payload) != expected_keys:
        raise CertificationError(
            "pending certification marker does not match its closed schema"
        )
    if payload["pending_schema_version"] != PENDING_SCHEMA_VERSION:
        raise CertificationError("pending certification marker schema is unsupported")
    for field in ("certification_id", "task_id"):
        if not isinstance(payload[field], str) or not payload[field]:
            raise CertificationError(
                f"pending certification marker has an invalid {field}"
            )
    _validate_attempt_id(
        payload["attempt_id"], label="pending certification attempt_id"
    )
    _parse_utc_timestamp(
        payload["started_at"], label="pending certification started_at"
    )
    return payload


def pending_attempt_id(pending_dir: Path) -> str:
    """Return the generated nonce that runtime evidence must carry."""

    return str(_load_pending_marker(Path(pending_dir))["attempt_id"])


@contextmanager
def _serialized_pending_completion(
    pending_dir: Path,
) -> Iterator[tuple[int, int] | None]:
    """Hold a crash-released exclusive lock on the pending capability inode."""

    if fcntl is None:
        raise CertificationError(
            "atomic certification completion locking is unsupported on this host"
        )

    path = Path(pending_dir) / PENDING_FILENAME
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        # Let the normal state-machine checks produce the specific missing or
        # already-certified diagnostic. A pending marker appearing after this
        # point is rejected below because no inode lock was acquired for it.
        yield None
        return
    except OSError as exc:
        raise CertificationError(
            "pending certification capability is unreadable"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        before = path.lstat()
    except OSError as exc:
        os.close(descriptor)
        raise CertificationError(
            "pending certification capability is unreadable"
        ) from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
    ):
        os.close(descriptor)
        raise CertificationError(
            "pending certification capability changed while it was opened"
        )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise CertificationError(
            "another completion already owns the pending certification attempt"
        ) from None
    except OSError as exc:
        os.close(descriptor)
        raise CertificationError(
            "pending certification capability is unreadable"
        ) from exc
    try:
        yield (opened.st_dev, opened.st_ino)
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _assert_pending_capability(
    pending_dir: Path,
    expected: Mapping[str, Any],
    locked_identity: tuple[int, int] | None,
) -> None:
    """Require the exact locked pending nonce to remain the active capability."""

    if locked_identity is None:
        raise CertificationError(
            "pending certification capability appeared without a completion lock"
        )
    path = Path(pending_dir) / PENDING_FILENAME
    try:
        status = path.lstat()
    except OSError as exc:
        raise CertificationError(
            "active pending certification capability was revoked"
        ) from exc
    if (
        stat.S_ISLNK(status.st_mode)
        or not stat.S_ISREG(status.st_mode)
        or (status.st_dev, status.st_ino) != locked_identity
    ):
        raise CertificationError(
            "active pending certification capability was replaced"
        )
    current = _load_pending_marker(Path(pending_dir))
    if current != dict(expected):
        raise CertificationError(
            "active pending certification nonce changed during completion"
        )
    try:
        after = path.lstat()
    except OSError as exc:
        raise CertificationError(
            "active pending certification capability was revoked"
        ) from exc
    if (after.st_dev, after.st_ino) != locked_identity:
        raise CertificationError(
            "active pending certification capability was replaced"
        )


def _revoke_pending_capability(
    pending_dir: Path,
    expected: Mapping[str, Any],
    locked_identity: tuple[int, int] | None,
) -> bool:
    """Remove only the pending entry whose locked inode and nonce we own."""

    try:
        _assert_pending_capability(pending_dir, expected, locked_identity)
    except CertificationError:
        return False
    (Path(pending_dir) / PENDING_FILENAME).unlink()
    _fsync_directory(Path(pending_dir))
    return True


def _stage1_certification_passed(result: Stage1ResultEvidence) -> bool:
    """Derive the runtime evaluator's strict Stage-1 verdict from evidence."""
    tables = set(result.expected_counts)
    fingerprints_complete = (
        set(result.canonical_reference_fingerprints) == tables
        and set(result.canonical_actual_fingerprints) == tables
        and all(
            _is_sha256(result.canonical_reference_fingerprints[table])
            and _is_sha256(result.canonical_actual_fingerprints[table])
            and result.canonical_reference_fingerprints[table]
            == result.canonical_actual_fingerprints[table]
            for table in tables
        )
    )
    return (
        bool(tables)
        and result.passed
        and result.reward == 1.0
        and result.actual_counts == result.expected_counts
        and not result.missing_tables
        and not result.unexpected_tables
        and not result.count_mismatches
        and not result.errors
        and set(result.canonical_table_scores) == tables
        and all(result.canonical_table_scores.values())
        and set(result.canonical_mismatch_codes) == tables
        and not any(result.canonical_mismatch_codes.values())
        and fingerprints_complete
    )


def _stage2_certification_passed(result: Stage2ResultEvidence) -> bool:
    """Derive the runtime evaluator's strict Stage-2 verdict from evidence."""
    marts = set(result.mart_scores)
    fingerprints_complete = (
        set(result.canonical_reference_fingerprints) == marts
        and set(result.canonical_actual_fingerprints) == marts
        and all(
            _is_sha256(result.canonical_reference_fingerprints[mart])
            and _is_sha256(result.canonical_actual_fingerprints[mart])
            and result.canonical_reference_fingerprints[mart]
            == result.canonical_actual_fingerprints[mart]
            for mart in marts
        )
    )
    return (
        bool(marts)
        and result.reward == 1.0
        and all(result.mart_scores.values())
        and not result.errors
        and set(result.strict_mart_scores) == marts
        and all(result.strict_mart_scores.values())
        and set(result.strict_mismatch_codes) == marts
        and not any(result.strict_mismatch_codes.values())
        and set(result.column_fingerprints) == marts
        and all(result.column_fingerprints[mart] for mart in marts)
        and set(result.canonical_mart_scores) == marts
        and all(result.canonical_mart_scores.values())
        and set(result.canonical_mismatch_codes) == marts
        and not any(result.canonical_mismatch_codes.values())
        and fingerprints_complete
    )


def _load_stage1_evidence(
    evidence: Stage1CertificationEvidence | Path | str,
) -> Stage1CertificationEvidence:
    if isinstance(evidence, Stage1CertificationEvidence):
        return evidence
    if isinstance(evidence, Stage2CertificationEvidence):
        raise CertificationError("expected Stage-1 evidence, received Stage-2")
    path = Path(evidence)
    try:
        payload, _ = _read_json_object(
            path, label=f"Stage-1 evidence at {path}"
        )
        payload = _migrate_legacy_evidence_payload(
            payload, label="Stage-1 evidence"
        )
        return Stage1CertificationEvidence.model_validate(payload)
    except (CertificationError, ValueError) as exc:
        raise CertificationError(
            f"Stage-1 evidence at {path} is unreadable: {exc}"
        ) from exc


def _load_stage2_evidence(
    evidence: Stage2CertificationEvidence | Path | str,
) -> Stage2CertificationEvidence:
    if isinstance(evidence, Stage2CertificationEvidence):
        return evidence
    if isinstance(evidence, Stage1CertificationEvidence):
        raise CertificationError("expected Stage-2 evidence, received Stage-1")
    path = Path(evidence)
    try:
        payload, _ = _read_json_object(
            path, label=f"Stage-2 evidence at {path}"
        )
        payload = _migrate_legacy_evidence_payload(
            payload, label="Stage-2 evidence"
        )
        return Stage2CertificationEvidence.model_validate(payload)
    except (CertificationError, ValueError) as exc:
        raise CertificationError(
            f"Stage-2 evidence at {path} is unreadable: {exc}"
        ) from exc


def verify_stage1_evidence(
    evidence: Stage1CertificationEvidence | Path | str,
    *,
    require_pass: bool = False,
) -> Stage1CertificationEvidence:
    """Parse and checksum-verify Stage 1; optionally require strict success."""
    record = _load_stage1_evidence(evidence)
    if record.evidence_schema_version not in {
        STAGE_EVIDENCE_SCHEMA_VERSION,
        *_LEGACY_STAGE_EVIDENCE_SCHEMA_VERSIONS,
    }:
        raise CertificationError(
            "unsupported Stage-1 evidence schema "
            f"{record.evidence_schema_version!r}"
        )
    if not record.evidence_digest:
        raise CertificationError("Stage-1 evidence was never sealed")
    if record.evidence_digest != _stage_evidence_digest(record):
        raise CertificationError("Stage-1 evidence digest mismatch")
    _validate_execution_window(
        record.execution_started_at,
        record.execution_completed_at,
        recorded_at=record.recorded_at,
        stage="Stage-1",
    )
    if record.evidence_schema_version == STAGE_EVIDENCE_SCHEMA_VERSION:
        _canonical_observation(
            record.runner_image, label="Stage-1 Terraform runner image"
        )
    if not _is_sha256(record.terraform_input_tree_digest):
        raise CertificationError("Stage-1 Terraform input digest is invalid")
    if not _is_sha256(record.terraform_state_digest):
        raise CertificationError("Stage-1 Terraform state digest is invalid")
    if record.population != record.result.population:
        raise CertificationError(
            "Stage-1 evidence population disagrees with its evaluator result"
        )
    if record.warehouse_namespace != record.result.database:
        raise CertificationError(
            "Stage-1 evidence namespace disagrees with its evaluator result"
        )
    _verify_evidence_container(record, record.result)
    if _normalize_airbyte_job_ids(record.airbyte_job_ids) != record.airbyte_job_ids:
        raise CertificationError(
            "Stage-1 evidence has a non-canonical Airbyte job mapping"
        )
    if require_pass and not _stage1_certification_passed(record.result):
        raise CertificationError(
            "Stage-1 evidence did not derive certification_passed=true"
        )
    return record


def verify_stage2_evidence(
    evidence: Stage2CertificationEvidence | Path | str,
    *,
    require_pass: bool = False,
) -> Stage2CertificationEvidence:
    """Parse and checksum-verify Stage 2; optionally require strict success."""
    record = _load_stage2_evidence(evidence)
    if record.evidence_schema_version not in {
        STAGE_EVIDENCE_SCHEMA_VERSION,
        *_LEGACY_STAGE_EVIDENCE_SCHEMA_VERSIONS,
    }:
        raise CertificationError(
            "unsupported Stage-2 evidence schema "
            f"{record.evidence_schema_version!r}"
        )
    if not record.evidence_digest:
        raise CertificationError("Stage-2 evidence was never sealed")
    if record.evidence_digest != _stage_evidence_digest(record):
        raise CertificationError("Stage-2 evidence digest mismatch")
    _validate_execution_window(
        record.execution_started_at,
        record.execution_completed_at,
        recorded_at=record.recorded_at,
        stage="Stage-2",
    )
    if record.evidence_schema_version == STAGE_EVIDENCE_SCHEMA_VERSION:
        _canonical_observation(
            record.runner_image, label="Stage-2 dbt runner image"
        )
        _canonical_observation(
            record.preflight_destination, label="Stage-2 preflight destination"
        )
        _canonical_observation(
            record.preflight_dbt_core_version,
            label="Stage-2 preflight dbt-core version",
        )
        _canonical_observation(
            record.preflight_adapter_version,
            label="Stage-2 preflight adapter version",
        )
        if record.preflight_destination != record.destination:
            raise CertificationError(
                "Stage-2 preflight destination disagrees with its evidence identity"
            )
    if not _is_sha256(record.dbt_input_tree_digest):
        raise CertificationError("Stage-2 dbt input digest is invalid")
    if not _is_sha256(record.dbt_run_results_digest):
        raise CertificationError("Stage-2 dbt result digest is invalid")
    if record.population != record.result.population:
        raise CertificationError(
            "Stage-2 evidence population disagrees with its evaluator result"
        )
    if record.warehouse_namespace != record.result.database:
        raise CertificationError(
            "Stage-2 evidence namespace disagrees with its evaluator result"
        )
    _verify_evidence_container(record, record.result)
    if record.evidence_schema_version == STAGE_EVIDENCE_SCHEMA_VERSION:
        _verify_stage2_model_roster(record)
    if require_pass and not _stage2_certification_passed(record.result):
        raise CertificationError(
            "Stage-2 evidence did not derive certification_passed=true"
        )
    return record


def _attestation_digest(attestation: CertificationAttestation) -> str:
    payload = attestation.model_dump(mode="json")
    payload["attestation_digest"] = ""
    return sha256_hex(canonical_json(payload))


def seal_attestation(
    attestation: CertificationAttestation,
) -> CertificationAttestation:
    """Checksum-bind the record: any later edit is detectable."""
    return attestation.model_copy(
        update={"attestation_digest": _attestation_digest(attestation)}
    )


def verify_attestation(
    attestation: CertificationAttestation | Path | str,
) -> CertificationAttestation:
    """Re-derive the seal; raise CertificationError on any disagreement."""
    if isinstance(attestation, (str, Path)):
        path = Path(attestation)
        try:
            payload, _ = _read_json_object(
                path, label=f"attestation at {path}"
            )
            attestation = CertificationAttestation.model_validate(payload)
        except (CertificationError, ValueError) as exc:
            raise CertificationError(
                f"attestation at {path} is unreadable: {exc}"
            ) from exc
    else:
        # ``model_copy(update=...)`` deliberately skips Pydantic validation.
        # Reparse even an in-memory model so strict booleans and every closed
        # field constraint remain verification-boundary guarantees.
        try:
            attestation = CertificationAttestation.model_validate(
                attestation.model_dump(mode="python")
            )
        except ValueError as exc:
            raise CertificationError(
                f"attestation object is invalid: {exc}"
            ) from exc
    if attestation.attestation_schema_version not in {
        ATTESTATION_SCHEMA_VERSION,
        *_LEGACY_ATTESTATION_SCHEMA_VERSIONS,
    }:
        raise CertificationError(
            "unsupported attestation schema "
            f"{attestation.attestation_schema_version!r}"
        )
    try:
        normalized_matrix = runtime_matrix.validate_recorded_certification_matrix(
            attestation.matrix, attestation.destination
        )
    except ValueError as exc:
        raise CertificationError(f"attestation matrix is invalid: {exc}") from exc
    if normalized_matrix != attestation.matrix:
        raise CertificationError("attestation matrix is not canonical")
    if attestation.matrix.get(
        "certification_attestation_schema_version"
    ) != attestation.attestation_schema_version:
        raise CertificationError(
            "attestation schema does not match the schema bound by its matrix"
        )
    expected_certification_id = release_mod._certification_id(
        runtime_bundle_id=attestation.runtime_bundle_id,
        matrix=attestation.matrix,
        validate_matrix=False,
    )
    if attestation.certification_id != expected_certification_id:
        raise CertificationError(
            "attestation certification_id does not reproduce from its matrix"
        )
    started = _parse_utc_timestamp(
        attestation.started_at, label="attestation started_at"
    )
    completed = _parse_utc_timestamp(
        attestation.completed_at, label="attestation completed_at"
    )
    if completed < started:
        raise CertificationError("attestation completed before it started")
    if not attestation.populations:
        raise CertificationError("attestation records no population coverage")
    if tuple(sorted(set(attestation.populations))) != attestation.populations:
        raise CertificationError(
            "attestation populations must be sorted and unique"
        )
    expected_evidence = {
        f"{population}/stage{stage}.json"
        for population in attestation.populations
        for stage in (1, 2)
    }
    if set(attestation.evidence_digests) != expected_evidence:
        raise CertificationError(
            "attestation evidence inventory does not exactly cover both "
            "stages for every recorded population"
        )
    required_observations = _required_observation_keys(attestation.matrix)
    missing_observations = sorted(
        required_observations - set(attestation.observed_versions)
    )
    if missing_observations:
        raise CertificationError(
            "attestation is missing required execution observations: "
            + ", ".join(missing_observations)
        )
    unexpected_observations = sorted(
        set(attestation.observed_versions) - required_observations
    )
    if unexpected_observations:
        raise CertificationError(
            "attestation contains unrecognized execution observations: "
            + ", ".join(unexpected_observations)
        )
    contradictions = sorted(
        key
        for key, value in attestation.observed_versions.items()
        if key in attestation.matrix and attestation.matrix[key] != value
    )
    if contradictions:
        raise CertificationError(
            "attestation execution observations contradict its matrix: "
            + ", ".join(contradictions)
        )
    if not all(_is_sha256(value) for value in attestation.evidence_digests.values()):
        raise CertificationError("attestation contains an invalid evidence digest")
    if not attestation.attestation_digest:
        raise CertificationError("attestation was never sealed (no digest)")
    expected = _attestation_digest(attestation)
    if attestation.attestation_digest != expected:
        raise CertificationError(
            "attestation digest mismatch: the record was modified after sealing"
        )
    return attestation


def _manifest_certification_chain(
    manifest: release_mod.ReleaseManifest, task_id: str
) -> tuple[str, str, dict[str, str], str]:
    """Validate and return one task's layered certification identity.

    Reject identities that do not reproduce and pre-3.4 runtime identities
    that are not linked to their semantic identity.
    """
    if release_mod._schema_tuple(
        manifest.schema_version
    ) < release_mod._schema_tuple(release_mod._CHAINED_IDENTITY_MIN_SCHEMA):
        raise CertificationError(
            "certification requires release schema "
            f"{release_mod._CHAINED_IDENTITY_MIN_SCHEMA} or newer with chained "
            f"semantic/runtime identities; this manifest records "
            f"{manifest.schema_version!r} "
            "(re-freeze the release, do not certify)"
        )
    if task_id not in manifest.tasks:
        raise CertificationError(
            f"release holds no task {task_id!r} (has: {sorted(manifest.tasks)})"
        )
    if not manifest.semantic_release_id:
        raise CertificationError("manifest records no semantic_release_id")
    expected_semantic = release_mod._semantic_release_id(
        tasks=manifest.tasks,
        el_sources=manifest.el_sources,
        private_checksums=release_mod._private_semantic_checksums(
            manifest.checksums, manifest.tasks
        ),
        scorer_version=manifest.scorer_version,
        roster_digest=manifest.roster_digest,
        semantic_scorer_version=manifest.semantic_scorer_version,
    )
    if manifest.semantic_release_id != expected_semantic:
        raise CertificationError(
            "manifest semantic_release_id does not reproduce from its own "
            "recorded fields"
        )
    expected_release = release_mod._combined_release_id(
        destinations=manifest.destinations,
        destination_connector_versions=manifest.destination_connector_versions,
        splits=manifest.splits,
        tasks=manifest.tasks,
        public_runtime_checksums={
            rel: digest
            for rel, digest in manifest.checksums.items()
            if rel.startswith("public/")
        },
        variants=manifest.variants,
        rejected_variants=manifest.rejected_variants,
        el_sources=manifest.el_sources,
        scorer_version=manifest.scorer_version,
        roster_digest=manifest.roster_digest,
        semantic_scorer_version=manifest.semantic_scorer_version,
        source_provenance_digests=(
            manifest.source_provenance_digests
            if release_mod._schema_tuple(manifest.schema_version)
            >= release_mod._schema_tuple(
                release_mod._SOURCE_PROVENANCE_MIN_SCHEMA
            )
            else None
        ),
    )
    if manifest.release_id != expected_release:
        raise CertificationError(
            "manifest release_id does not reproduce from its own recorded fields"
        )
    destination = manifest.destinations.get(task_id) or ""
    if not destination:
        raise CertificationError(
            f"manifest records no destination for task {task_id!r}"
        )
    bundle = manifest.runtime_bundle_ids.get(task_id) or ""
    private_runtime = release_mod._private_runtime_checksums(
        manifest.checksums, task_id
    )
    if not private_runtime:
        raise CertificationError(
            f"manifest records no private runtime contract for task {task_id!r}"
        )
    expected_bundle = release_mod._runtime_bundle_id(
        semantic_release_id=manifest.semantic_release_id,
        task_id=task_id,
        destination=destination,
        public_checksums=release_mod._public_task_checksums(
            manifest.checksums, task_id
        ),
        private_runtime_checksums=private_runtime,
    )
    if bundle != expected_bundle:
        raise CertificationError(
            f"manifest runtime_bundle_id for task {task_id!r} does not "
            "reproduce from its own recorded fields"
        )
    matrix = manifest.certification_matrix.get(destination)
    if not matrix:
        raise CertificationError(
            "manifest records no certification matrix for destination "
            f"{destination!r}"
        )
    try:
        normalized_matrix = runtime_matrix.validate_recorded_certification_matrix(
            matrix, destination
        )
    except ValueError as exc:
        raise CertificationError(
            f"manifest certification matrix is invalid: {exc}"
        ) from exc
    if normalized_matrix != matrix:
        raise CertificationError("manifest certification matrix is not canonical")
    matrix = normalized_matrix
    certification_id = manifest.certification_ids.get(task_id) or ""
    expected_certification = release_mod._certification_id(
        runtime_bundle_id=bundle,
        matrix=matrix,
        validate_matrix=False,
    )
    if certification_id != expected_certification:
        raise CertificationError(
            f"manifest certification_id for task {task_id!r} does not "
            "reproduce from its recorded runtime_bundle_id and matrix"
        )
    return certification_id, bundle, dict(matrix), destination


_EVIDENCE_IDENTITY_FIELDS = (
    "release_schema_version",
    "certification_id",
    "runtime_bundle_id",
    "semantic_release_id",
    "release_id",
    "task_id",
    "destination",
)


def _required_observation_keys(matrix: Mapping[str, str]) -> set[str]:
    """Live/runtime matrix fields an attestation may not leave unobserved."""

    exact = {
        "airbyte_abctl",
        "airbyte_chart",
        "destination",
        "destination_connector",
        "destination_definition_id",
        "dbt_adapter",
        "dbt_adapter_version",
        "dbt_core_version",
        "runner_image:dbt",
        "runner_image:terraform",
    }
    prefixes = ("source_connector:", "source_service_image:", "warehouse:")
    return {
        key
        for key in matrix
        if key in exact or any(key.startswith(prefix) for prefix in prefixes)
    }


def required_observation_keys(
    manifest: release_mod.ReleaseManifest, task_id: str
) -> tuple[str, ...]:
    """Public closed roster of live observations required for completion.

    Lifecycle/orchestrator callers should use this instead of copying the
    matrix-prefix policy.  The manifest chain is re-derived first, so an
    invalid task/runtime identity never yields a plausible-looking roster.
    """

    _, _, matrix, _ = _manifest_certification_chain(manifest, task_id)
    return tuple(sorted(_required_observation_keys(matrix)))


def _evidence_observed_versions(
    records: Mapping[
        str, tuple[Stage1CertificationEvidence, Stage2CertificationEvidence]
    ],
) -> dict[str, str]:
    """Derive every observation that the runtime receipts directly prove."""

    observations: dict[str, set[str]] = {
        "runner_image:terraform": set(),
        "runner_image:dbt": set(),
        "destination": set(),
        "dbt_core_version": set(),
        "dbt_adapter_version": set(),
    }
    for stage1, stage2 in records.values():
        observations["runner_image:terraform"].add(stage1.runner_image)
        observations["runner_image:dbt"].add(stage2.runner_image)
        observations["destination"].add(stage2.preflight_destination)
        observations["dbt_core_version"].add(
            stage2.preflight_dbt_core_version
        )
        observations["dbt_adapter_version"].add(
            stage2.preflight_adapter_version
        )
    inconsistent = sorted(
        key for key, values in observations.items() if len(values) != 1
    )
    if inconsistent:
        raise CertificationError(
            "stage evidence records inconsistent execution observations: "
            + ", ".join(inconsistent)
        )
    return {key: next(iter(values)) for key, values in observations.items()}


def _normalize_supplied_observations(
    values: Mapping[str, str],
) -> dict[str, str]:
    """Validate caller observations without stringifying arbitrary objects."""

    if not isinstance(values, Mapping):
        raise CertificationError("observed execution versions must be a mapping")
    normalized: dict[str, str] = {}
    for key, value in values.items():
        canonical_key = _canonical_observation(
            key, label="execution observation key"
        )
        if (
            not isinstance(value, str)
            or value != value.strip()
            or len(value) > 2048
            or any(ord(char) < 32 for char in value)
        ):
            raise CertificationError(
                f"execution observation {canonical_key!r} is not canonical text"
            )
        # A small set of closed matrix fields deliberately use the empty
        # string to mean “no distinct physical container/fixed schema”.
        normalized[canonical_key] = value
    return dict(sorted(normalized.items()))


def _release_relation_rosters(
    manifest: release_mod.ReleaseManifest,
    task_id: str,
    population: str,
) -> tuple[set[str], set[str]]:
    """Derive exact source/mart names from the release's pinned file inventory."""

    table_prefix = f"public/{task_id}/schemas/"
    tables = {
        remainder[:-4]
        for rel in manifest.checksums
        if rel.startswith(table_prefix)
        and not "/" in (remainder := rel[len(table_prefix) :])
        and remainder.endswith(".csv")
        and len(remainder) > 4
    }
    mart_prefix = f"private/{task_id}/answer_key/gold/{population}/"
    marts = {
        remainder[:-4]
        for rel in manifest.checksums
        if rel.startswith(mart_prefix)
        and not "/" in (remainder := rel[len(mart_prefix) :])
        and remainder.endswith(".csv")
        and len(remainder) > 4
    }
    if not tables or not marts:
        raise CertificationError(
            f"release inventory has no complete relation roster for task "
            f"{task_id!r} population {population!r}"
        )
    return tables, marts


def _verify_evidence_pair(
    stage1_input: Stage1CertificationEvidence | Path | str,
    stage2_input: Stage2CertificationEvidence | Path | str,
    *,
    expected_identity: Mapping[str, str],
) -> tuple[Stage1CertificationEvidence, Stage2CertificationEvidence]:
    stage1 = verify_stage1_evidence(stage1_input, require_pass=True)
    stage2 = verify_stage2_evidence(stage2_input, require_pass=True)
    for record in (stage1, stage2):
        for field in _EVIDENCE_IDENTITY_FIELDS:
            expected = str(expected_identity[field])
            if getattr(record, field) != expected:
                raise CertificationError(
                    f"{record.stage} evidence {field} does not match the "
                    "certification identity"
                )
    if stage1.population != stage2.population:
        raise CertificationError(
            "Stage-1 and Stage-2 evidence name different populations"
        )
    if stage1.attempt_id != stage2.attempt_id:
        raise CertificationError(
            "Stage-1 and Stage-2 evidence name different attempts"
        )
    if stage1.result.database != stage2.result.database:
        raise CertificationError(
            "Stage-1 and Stage-2 evidence name different warehouse namespaces"
        )
    if (
        stage1.physical_container.casefold()
        != stage2.physical_container.casefold()
    ):
        raise CertificationError(
            "Stage-1 and Stage-2 evidence name different physical containers"
        )
    if stage1.result.expected_repetitions != 1:
        raise CertificationError(
            "ordinary certification requires one Stage-1 ingestion; append "
            "probe evidence must be recorded separately"
        )
    stage1_completed = _parse_utc_timestamp(
        stage1.execution_completed_at,
        label="Stage-1 execution_completed_at",
    )
    stage2_started = _parse_utc_timestamp(
        stage2.execution_started_at,
        label="Stage-2 execution_started_at",
    )
    if stage2_started < stage1_completed:
        raise CertificationError(
            "Stage-2 execution began before Stage-1 execution completed"
        )
    return stage1, stage2


def _verify_persisted_evidence(
    id_dir: Path,
    attestation: CertificationAttestation,
) -> None:
    evidence_root = id_dir / EVIDENCE_DIRNAME
    if evidence_root.is_symlink() or not evidence_root.is_dir():
        raise CertificationError("persisted certification evidence is missing")
    descendants = tuple(evidence_root.rglob("*"))
    if any(path.is_symlink() for path in descendants):
        raise CertificationError("persisted certification evidence contains a symlink")
    on_disk = {
        path.relative_to(evidence_root).as_posix()
        for path in descendants
        if path.is_file()
    }
    if on_disk != set(attestation.evidence_digests):
        raise CertificationError(
            "persisted evidence files do not match the attestation inventory"
        )
    expected_identity = {
        field: str(getattr(attestation, field))
        for field in _EVIDENCE_IDENTITY_FIELDS
    }
    attestation_started = _parse_utc_timestamp(
        attestation.started_at, label="attestation started_at"
    )
    attestation_completed = _parse_utc_timestamp(
        attestation.completed_at, label="attestation completed_at"
    )
    bound_evidence_schema = attestation.matrix.get(
        "certification_stage_evidence_schema_version"
    )
    records: dict[
        str, tuple[Stage1CertificationEvidence, Stage2CertificationEvidence]
    ] = {}
    for population in attestation.populations:
        stage1_rel = f"{population}/stage1.json"
        stage2_rel = f"{population}/stage2.json"
        stage1, stage2 = _verify_evidence_pair(
            evidence_root / stage1_rel,
            evidence_root / stage2_rel,
            expected_identity=expected_identity,
        )
        if any(
            record.evidence_schema_version != bound_evidence_schema
            for record in (stage1, stage2)
        ):
            raise CertificationError(
                "persisted evidence schema does not match its attestation matrix"
            )
        records[population] = (stage1, stage2)
        for record in (stage1, stage2):
            execution_started = _parse_utc_timestamp(
                record.execution_started_at,
                label=f"{record.stage} execution_started_at",
            )
            execution_completed = _parse_utc_timestamp(
                record.execution_completed_at,
                label=f"{record.stage} execution_completed_at",
            )
            recorded = _parse_utc_timestamp(
                record.recorded_at, label=f"{record.stage} recorded_at"
            )
            if (
                execution_started < attestation_started
                or execution_completed > attestation_completed
                or recorded < attestation_started
                or recorded > attestation_completed
            ):
                raise CertificationError(
                    f"persisted {record.stage} execution falls outside the "
                    "attestation window"
                )
        if stage1.population != population:
            raise CertificationError(
                f"persisted evidence path {population!r} contains another population"
            )
        for rel, record in ((stage1_rel, stage1), (stage2_rel, stage2)):
            if attestation.evidence_digests.get(rel) != record.evidence_digest:
                raise CertificationError(
                    f"persisted evidence digest disagrees at {rel!r}"
                )
    if bound_evidence_schema == STAGE_EVIDENCE_SCHEMA_VERSION:
        evidence_observations = _evidence_observed_versions(records)
        disagreements = sorted(
            key
            for key, value in evidence_observations.items()
            if attestation.observed_versions.get(key) != value
        )
        if disagreements:
            raise CertificationError(
                "persisted evidence disagrees with attested execution observations: "
                + ", ".join(disagreements)
            )


def _evidence_tree_matches_records(
    evidence_root: Path,
    records: Mapping[
        str, tuple[Stage1CertificationEvidence, Stage2CertificationEvidence]
    ],
) -> bool:
    """Recognize a complete tree left by an interrupted identical completion."""

    try:
        status = evidence_root.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            return False
        descendants = tuple(evidence_root.rglob("*"))
        if any(path.is_symlink() for path in descendants):
            return False
        expected_files = {
            f"{population}/stage{stage}.json"
            for population in records
            for stage in (1, 2)
        }
        actual_files = {
            path.relative_to(evidence_root).as_posix()
            for path in descendants
            if path.is_file()
        }
        actual_directories = {
            path.relative_to(evidence_root).as_posix()
            for path in descendants
            if path.is_dir()
        }
        if actual_files != expected_files or actual_directories != set(records):
            return False
        for population, (expected_stage1, expected_stage2) in records.items():
            actual_stage1 = verify_stage1_evidence(
                evidence_root / population / "stage1.json",
                require_pass=True,
            )
            actual_stage2 = verify_stage2_evidence(
                evidence_root / population / "stage2.json",
                require_pass=True,
            )
            if actual_stage1 != expected_stage1 or actual_stage2 != expected_stage2:
                return False
    except (CertificationError, OSError, ValueError):
        return False
    return True


def certification_status(
    store_dir: Path, certification_id: str
) -> CertificationStatus:
    """Read certification state without raising.

    Certified state requires a valid sealed attestation for this ID with both
    stages verified. Any read, integrity, identity, or completeness failure
    returns an uncertified status with a reason.
    """
    id_dir = Path(store_dir) / certification_id

    def uncertified(reason: str) -> CertificationStatus:
        return CertificationStatus(
            certification_id=certification_id,
            state=CertificationState.UNCERTIFIED,
            reason=reason,
        )

    if not id_dir.is_dir():
        return uncertified("no certification evidence exists for this id")
    attestation_path = id_dir / ATTESTATION_FILENAME
    if attestation_path.is_file():
        try:
            attestation = verify_attestation(attestation_path)
        except CertificationError as exc:
            return uncertified(str(exc))
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            return uncertified(
                f"attestation is unreadable: {type(exc).__name__}: {exc}"
            )
        if attestation.certification_id != certification_id:
            return uncertified(
                "attestation names certification id "
                f"{attestation.certification_id!r}, not {certification_id!r}"
            )
        if attestation.certification_id != id_dir.name:
            return uncertified(
                "attestation does not match its own store directory name"
            )
        if not (attestation.stage1_verified and attestation.stage2_verified):
            return uncertified(
                "attestation does not record both stages verified"
            )
        try:
            _verify_persisted_evidence(id_dir, attestation)
        except CertificationError as exc:
            return uncertified(str(exc))
        return CertificationStatus(
            certification_id=certification_id,
            state=CertificationState.CERTIFIED,
            populations=attestation.populations,
        )
    if (id_dir / PENDING_FILENAME).is_file():
        try:
            pending = _load_pending_marker(id_dir)
        except CertificationError as exc:
            return uncertified(str(exc))
        if pending["certification_id"] != certification_id:
            return uncertified(
                "pending certification marker names another certification id"
            )
        return CertificationStatus(
            certification_id=certification_id,
            state=CertificationState.PENDING,
            reason="a certification attempt is in progress",
        )
    return uncertified(
        "certification directory holds no sealed attestation"
    )


def begin_certification(
    store_dir: Path, manifest: release_mod.ReleaseManifest, task_id: str
) -> Path:
    """Open one certification attempt in pending state.

    Reject pre-3.4 manifests, invalid identity chains, existing sealed
    attestations, and concurrent pending attempts. Return the attempt's ID
    directory.
    """
    certification_id, _, matrix, _ = _manifest_certification_chain(manifest, task_id)
    if matrix.get("matrix_version") != runtime_matrix.CERTIFICATION_MATRIX_VERSION:
        raise CertificationError(
            "new certification attempts require the current matrix recipe; "
            "re-freeze the release"
        )
    id_dir = Path(store_dir) / certification_id
    attestation_path = id_dir / ATTESTATION_FILENAME
    if _entry_exists(attestation_path):
        raise CertificationError(
            f"certification {certification_id} already holds a sealed "
            "attestation; reuse it, do not rerun"
        )
    id_dir.mkdir(parents=True, exist_ok=True)
    try:
        id_status = id_dir.lstat()
    except OSError as exc:
        raise CertificationError("certification directory is unreadable") from exc
    if stat.S_ISLNK(id_status.st_mode) or not stat.S_ISDIR(id_status.st_mode):
        raise CertificationError(
            "certification directory must be a non-symlink directory"
        )
    pending_path = id_dir / PENDING_FILENAME
    attempt_id = "certification-attempt-" + secrets.token_hex(32)
    pending_bytes = (
        readable_json(
            {
                "pending_schema_version": PENDING_SCHEMA_VERSION,
                "certification_id": certification_id,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "started_at": _utc_now(),
            }
        )
        + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(
            pending_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        raise CertificationError(
            f"certification {certification_id} already has a pending attempt "
            "(concurrent begin refused)"
        ) from None
    try:
        view = memoryview(pending_bytes)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while opening certification attempt")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        pending_path.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    try:
        _fsync_directory(id_dir)
        # Close the check/create race: a completion that became visible after
        # the initial check always wins, and this newly-created marker is
        # withdrawn rather than leaving CERTIFIED + PENDING side by side.
        if _entry_exists(attestation_path):
            pending_path.unlink(missing_ok=True)
            raise CertificationError(
                f"certification {certification_id} became certified while "
                "opening a new attempt; reuse its attestation"
            )
        # A process may have died after publishing evidence but before its
        # attestation. Preserve those bytes under an orphan name so this fresh
        # attempt can eventually publish its own atomic evidence inventory.
        _quarantine_orphan_evidence(id_dir)
    except BaseException:
        pending_path.unlink(missing_ok=True)
        raise
    return id_dir


def _complete_certification_locked(
    pending_dir: Path,
    *,
    manifest: release_mod.ReleaseManifest,
    task_id: str,
    evidence_pairs: Sequence[StageEvidencePair],
    observed_versions: Mapping[str, str],
    locked_pending_identity: tuple[int, int] | None,
) -> CertificationAttestation:
    """Complete certification from sealed Stage 1 and Stage 2 evidence.

    Reparse each pair, verify its seals and identity, and derive success from
    evaluator fields. Copy normalized evidence into the store. Any refusal
    removes the pending record. Stage 1 is mandatory, and duplicate populations
    are rejected.
    """
    pending_dir = Path(pending_dir)
    try:
        pending_status = pending_dir.lstat()
    except OSError as exc:
        raise CertificationError("pending certification directory is unreadable") from exc
    if stat.S_ISLNK(pending_status.st_mode) or not stat.S_ISDIR(
        pending_status.st_mode
    ):
        raise CertificationError(
            "pending certification directory must be a non-symlink directory"
        )
    certification_id, bundle, matrix, destination = (
        _manifest_certification_chain(manifest, task_id)
    )
    if matrix.get("matrix_version") != runtime_matrix.CERTIFICATION_MATRIX_VERSION:
        raise CertificationError(
            "certification completion requires the current matrix recipe"
        )
    if pending_dir.name != certification_id:
        raise CertificationError(
            f"pending directory {pending_dir.name!r} does not match the "
            f"manifest's certification id {certification_id}"
        )
    attestation_path = pending_dir / ATTESTATION_FILENAME
    if _entry_exists(attestation_path):
        raise CertificationError(
            f"certification {certification_id} already holds a sealed "
            "attestation (immutable: one attestation per id, forever)"
        )
    try:
        pending = _load_pending_marker(pending_dir)
    except CertificationError:
        pending = None
    if (
        not isinstance(pending, dict)
        or pending.get("certification_id") != certification_id
        or pending.get("task_id") != task_id
    ):
        raise CertificationError(
            f"no pending certification attempt for {certification_id} at "
            f"{pending_dir} (begin_certification first)"
        )
    _assert_pending_capability(
        pending_dir,
        pending,
        locked_pending_identity,
    )

    evidence_dir = pending_dir / EVIDENCE_DIRNAME
    evidence_committed = False

    def refuse(reason: str) -> None:
        nonlocal evidence_committed
        # Back to UNCERTIFIED, never a partial CERTIFIED: drop the pending
        # marker and record why, so the refusal is readable from the store.
        if evidence_committed and not _entry_exists(attestation_path):
            try:
                _quarantine_orphan_evidence(pending_dir)
            except OSError:
                # The next begin attempt performs the same recovery. Refusal
                # must still revoke the active attempt even when quarantine
                # itself cannot be made durable now.
                pass
            evidence_committed = False
        _revoke_pending_capability(
            pending_dir,
            pending,
            locked_pending_identity,
        )
        try:
            _replace_json_without_following(
                pending_dir / REFUSED_FILENAME,
                {
                    "certification_id": certification_id,
                    "task_id": task_id,
                    "reason": reason,
                    "refused_at": _utc_now(),
                },
            )
        except OSError:
            # The certification is already revoked by removal of the pending
            # capability. A reporting failure must not obscure that outcome or
            # substitute a potentially sensitive filesystem exception.
            pass
        raise CertificationError(reason)

    if not evidence_pairs:
        refuse("no Stage-1/Stage-2 evidence pairs were supplied")

    expected_identity = {
        "release_schema_version": manifest.schema_version,
        "certification_id": certification_id,
        "runtime_bundle_id": bundle,
        "semantic_release_id": manifest.semantic_release_id,
        "release_id": manifest.release_id,
        "task_id": task_id,
        "destination": destination,
    }
    valid_population_names = {population.value for population in PopulationName}
    released_populations = set((manifest.el_sources.get(task_id) or {}))
    records: dict[
        str, tuple[Stage1CertificationEvidence, Stage2CertificationEvidence]
    ] = {}
    try:
        for pair in evidence_pairs:
            if not isinstance(pair, Sequence) or isinstance(
                pair, (str, bytes, bytearray)
            ) or len(pair) != 2:
                raise CertificationError(
                    "each certification evidence item must be an ordered "
                    "(Stage-1, Stage-2) pair"
                )
            stage1, stage2 = _verify_evidence_pair(
                pair[0], pair[1], expected_identity=expected_identity
            )
            if any(
                record.evidence_schema_version != STAGE_EVIDENCE_SCHEMA_VERSION
                for record in (stage1, stage2)
            ):
                raise CertificationError(
                    "new certifications require the current stage-evidence schema"
                )
            if stage1.attempt_id != pending["attempt_id"]:
                raise CertificationError(
                    "evidence attempt_id does not match the active pending attempt"
                )
            pending_started = _parse_utc_timestamp(
                pending["started_at"], label="pending certification started_at"
            )
            stage1_started = _parse_utc_timestamp(
                stage1.execution_started_at,
                label="Stage-1 execution_started_at",
            )
            if stage1_started < pending_started:
                raise CertificationError(
                    "Stage-1 execution predates the active pending attempt"
                )
            population = stage1.population
            if population not in valid_population_names:
                raise CertificationError(
                    f"evidence names unknown population {population!r}"
                )
            if population not in released_populations:
                raise CertificationError(
                    f"evidence population {population!r} has no released "
                    "source root"
                )
            expected_tables, expected_marts = _release_relation_rosters(
                manifest, task_id, population
            )
            if set(stage1.result.expected_counts) != expected_tables:
                raise CertificationError(
                    f"Stage-1 evidence does not exactly cover the released "
                    f"table roster for population {population!r}"
                )
            if set(stage2.result.mart_scores) != expected_marts:
                raise CertificationError(
                    f"Stage-2 evidence does not exactly cover the released "
                    f"mart roster for population {population!r}"
                )
            _verify_stage2_model_roster(
                stage2,
                expected_marts=expected_marts,
            )
            if population in records:
                raise CertificationError(
                    f"duplicate evidence pair for population {population!r}"
                )
            records[population] = (stage1, stage2)
    except CertificationError as exc:
        refuse(str(exc))

    try:
        supplied_observations = _normalize_supplied_observations(observed_versions)
        evidence_observations = _evidence_observed_versions(records)
    except CertificationError as exc:
        refuse(str(exc))
    disputed_evidence = sorted(
        key
        for key, value in evidence_observations.items()
        if key in supplied_observations and supplied_observations[key] != value
    )
    if disputed_evidence:
        refuse(
            "supplied execution observations contradict sealed stage evidence: "
            + ", ".join(disputed_evidence)
        )
    # Receipt-derived values win by construction. Callers may omit these keys;
    # when supplied for compatibility they are accepted only after comparison.
    observed = {**supplied_observations, **evidence_observations}
    observed = dict(sorted(observed.items()))
    required_observations = _required_observation_keys(matrix)
    missing_observations = sorted(required_observations - set(observed))
    if missing_observations:
        refuse(
            "required execution observations are missing: "
            + ", ".join(missing_observations)
        )
    unexpected_observations = sorted(set(observed) - required_observations)
    if unexpected_observations:
        refuse(
            "unrecognized execution observations were supplied: "
            + ", ".join(unexpected_observations)
        )
    contradictions = sorted(
        key
        for key, value in observed.items()
        if key in matrix and matrix[key] != value
    )
    if contradictions:
        refuse(
            "observed execution versions contradict the declared matrix: "
            + ", ".join(contradictions)
        )

    evidence_digests: dict[str, str] = {}
    staged_evidence = Path(
        tempfile.mkdtemp(dir=pending_dir, prefix=".evidence-stage-")
    )
    try:
        for population, pair in sorted(records.items()):
            population_dir = staged_evidence / population
            population_dir.mkdir(parents=True)
            for stage_number, record in enumerate(pair, start=1):
                rel = f"{population}/stage{stage_number}.json"
                path = staged_evidence / rel
                _publish_immutable_json(
                    path,
                    record.model_dump(mode="json"),
                )
                evidence_digests[rel] = record.evidence_digest
        _fsync_directory(staged_evidence)
        _assert_pending_capability(
            pending_dir,
            pending,
            locked_pending_identity,
        )
        if _entry_exists(evidence_dir):
            if not _evidence_tree_matches_records(evidence_dir, records):
                raise CertificationError(
                    "existing certification evidence does not match this completion"
                )
            shutil.rmtree(staged_evidence)
        else:
            try:
                os.rename(staged_evidence, evidence_dir)
            except OSError:
                # Another identical completion may have won the directory
                # publication race. Reuse only a byte-for-byte equivalent,
                # fully verified evidence tree.
                if not _evidence_tree_matches_records(evidence_dir, records):
                    raise
                shutil.rmtree(staged_evidence, ignore_errors=True)
            _fsync_directory(pending_dir)
        evidence_committed = True
    except CertificationError as exc:
        shutil.rmtree(staged_evidence, ignore_errors=True)
        refuse(str(exc))
    except (OSError, ValueError) as exc:
        shutil.rmtree(staged_evidence, ignore_errors=True)
        refuse(f"could not persist certification evidence: {exc}")

    attestation_completed_at = _utc_now()
    attestation_started_at = str(pending["started_at"])
    started = _parse_utc_timestamp(
        attestation_started_at, label="attestation started_at"
    )
    completed = _parse_utc_timestamp(
        attestation_completed_at, label="attestation completed_at"
    )
    if completed < started:
        refuse("certification completion predates its pending attempt")
    for pair in records.values():
        for record in pair:
            execution_completed = _parse_utc_timestamp(
                record.execution_completed_at,
                label=f"{record.stage} execution_completed_at",
            )
            recorded = _parse_utc_timestamp(
                record.recorded_at, label=f"{record.stage} recorded_at"
            )
            if execution_completed > completed or recorded > completed:
                refuse(
                    f"{record.stage} evidence is later than the "
                    "certification completion"
                )

    attestation = seal_attestation(
        CertificationAttestation(
            attestation_schema_version=ATTESTATION_SCHEMA_VERSION,
            release_schema_version=manifest.schema_version,
            certification_id=certification_id,
            runtime_bundle_id=bundle,
            semantic_release_id=manifest.semantic_release_id,
            release_id=manifest.release_id,
            task_id=task_id,
            destination=destination,
            populations=tuple(sorted(records)),
            matrix=matrix,
            observed_versions=observed,
            stage1_verified=True,
            stage2_verified=True,
            evidence_digests=dict(sorted(evidence_digests.items())),
            started_at=attestation_started_at,
            completed_at=attestation_completed_at,
        )
    )
    try:
        verify_attestation(attestation)
        _verify_persisted_evidence(pending_dir, attestation)
        _assert_pending_capability(
            pending_dir,
            pending,
            locked_pending_identity,
        )
    except CertificationError as exc:
        refuse(f"could not verify completed certification record: {exc}")
    try:
        _publish_immutable_json(
            attestation_path, attestation.model_dump(mode="json")
        )
    except (OSError, CertificationError) as exc:
        # os.link may have published the complete record before a later fsync
        # failed, or a concurrent retry may have won. Treat that as success
        # only when the winner is itself sealed and binds this exact evidence.
        if _entry_exists(attestation_path):
            try:
                published = verify_attestation(attestation_path)
                _verify_persisted_evidence(pending_dir, published)
            except CertificationError:
                raise CertificationError(
                    "an unreadable or contradictory attestation appeared during "
                    "completion"
                ) from None
            if (
                published.certification_id != certification_id
                or published.task_id != task_id
                or published.evidence_digests != attestation.evidence_digests
            ):
                raise CertificationError(
                    "a different attestation appeared during completion"
                )
            _revoke_pending_capability(
                pending_dir,
                pending,
                locked_pending_identity,
            )
            return published
        refuse(f"could not publish immutable certification attestation: {exc}")
    _revoke_pending_capability(
        pending_dir,
        pending,
        locked_pending_identity,
    )
    return attestation


def complete_certification(
    pending_dir: Path,
    *,
    manifest: release_mod.ReleaseManifest,
    task_id: str,
    evidence_pairs: Sequence[StageEvidencePair],
    observed_versions: Mapping[str, str],
) -> CertificationAttestation:
    """Serialize completion for one pending capability.

    The first process to lock ``pending.json`` completes the attempt.
    Concurrent completions fail without mutation. Refused evidence revokes the
    nonce, so another completion requires a new attempt.
    """

    pending_dir = Path(pending_dir)
    with _serialized_pending_completion(pending_dir) as locked_identity:
        return _complete_certification_locked(
            pending_dir,
            manifest=manifest,
            task_id=task_id,
            evidence_pairs=evidence_pairs,
            observed_versions=observed_versions,
            locked_pending_identity=locked_identity,
        )


__all__ = [
    "ATTESTATION_FILENAME",
    "ATTESTATION_SCHEMA_VERSION",
    "EVIDENCE_DIRNAME",
    "PENDING_FILENAME",
    "PENDING_SCHEMA_VERSION",
    "REFUSED_FILENAME",
    "STAGE_EVIDENCE_SCHEMA_VERSION",
    "CertificationAttestation",
    "CertificationError",
    "CertificationState",
    "CertificationStatus",
    "Stage1CertificationEvidence",
    "Stage1ResultEvidence",
    "Stage2CertificationEvidence",
    "Stage2ResultEvidence",
    "begin_certification",
    "certification_status",
    "complete_certification",
    "pending_attempt_id",
    "required_observation_keys",
    "seal_attestation",
    "seal_stage_evidence",
    "stage1_evidence_from_execution",
    "stage2_evidence_from_execution",
    "verify_attestation",
    "verify_stage1_evidence",
    "verify_stage2_evidence",
]

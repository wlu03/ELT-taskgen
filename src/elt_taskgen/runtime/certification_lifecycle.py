"""Coordinate sandbox setup, runtime evidence, cleanup, and certification.

The lifecycle reconstructs typed stage evidence from public receipts and binds
it, the release, sandbox observation, and cleanup receipt to the final
certification. Its seal detects changes but does not authenticate the host.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from elt_taskgen.destinations import normalize_destination
from elt_taskgen.export import certification as certification_mod
from elt_taskgen.export import release as release_mod
from elt_taskgen.export.attestation_gate import require_attestation_for_labels
from elt_taskgen.models import PopulationName, canonical_json, readable_json, sha256_hex
from elt_taskgen.runtime.attestation import (
    PreflightResult,
    SandboxAttestation,
    attest_sandbox,
    isolation_preflight,
    verify_sandbox_attestation,
)
from elt_taskgen.runtime.evaluation import (
    Stage1EvaluationResult,
    Stage2EvaluationResult,
)
from elt_taskgen.runtime.execution import (
    Stage1Execution,
    Stage2Execution,
    Stage2Preflight,
)
from elt_taskgen.runtime.model_copy import (
    CleanupReceipt,
    ModelCopyError,
    verify_cleanup_receipt,
)


LIFECYCLE_SCHEMA_VERSION = "1.0"
COMPLETION_INTENT_SCHEMA_VERSION = "1.0"
OBSERVATION_SCHEMA_VERSION = "1.0"
RUN_SPEC_SCHEMA_VERSION = "1.0"
LIFECYCLE_DIRNAME = "lifecycle-attempts"
STAGED_EVIDENCE_DIRNAME = "staged-evidence"
SANDBOX_ATTESTATION_FILENAME = "sandbox_attestation.json"
OBSERVATIONS_FILENAME = "observations.json"
CLEANUP_FILENAME = "cleanup.json"
COMPLETION_INTENT_FILENAME = "completion_intent.json"
LIFECYCLE_FILENAME = "lifecycle.json"
_ACTIVE_AGENTS_CONFIG_NOT_SUPPLIED = object()
#: Expected commands per population: Terraform init/apply and dbt version/run.
GRADER_COMMANDS_PER_POPULATION = 4

_MAX_JSON_BYTES = 16 * 1024 * 1024
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class CertificationLifecycleError(RuntimeError):
    """The public certification lifecycle cannot proceed safely."""


class RuntimeObservationReceipt(BaseModel):
    """Release and attempt-bound matrix observations used at completion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = OBSERVATION_SCHEMA_VERSION
    certification_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    observed_versions: dict[str, str]
    recorded_at: str = Field(min_length=1)
    observation_digest: str = ""


class CertificationLifecycleReceipt(BaseModel):
    """Final sidecar proving cleanup preceded certification completion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = LIFECYCLE_SCHEMA_VERSION
    certification_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    runtime_bundle_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    populations: tuple[str, ...]
    sandbox_attestation_digest: str = Field(pattern=_SHA256_PATTERN)
    observation_digest: str = Field(pattern=_SHA256_PATTERN)
    cleanup_receipt_digest: str = Field(pattern=_SHA256_PATTERN)
    certification_attestation_digest: str = Field(pattern=_SHA256_PATTERN)
    evidence_digests: dict[str, str]
    completed_at: str = Field(min_length=1)
    lifecycle_digest: str = ""


class CertificationCompletionIntent(BaseModel):
    """Durable proof that every lifecycle gate passed before completion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = COMPLETION_INTENT_SCHEMA_VERSION
    certification_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    runtime_bundle_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    populations: tuple[str, ...]
    sandbox_attestation_digest: str = Field(pattern=_SHA256_PATTERN)
    observation_digest: str = Field(pattern=_SHA256_PATTERN)
    cleanup_receipt_digest: str = Field(pattern=_SHA256_PATTERN)
    evidence_digests: dict[str, str]
    prepared_at: str = Field(min_length=1)
    intent_digest: str = ""


class PopulationEvidenceFiles(BaseModel):
    """The four public JSON artifacts for one graded population."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    population: str = Field(min_length=1)
    stage1_execution: str = Field(min_length=1)
    stage1_result: str = Field(min_length=1)
    stage2_execution: str = Field(min_length=1)
    stage2_result: str = Field(min_length=1)


class CertificationRunSpec(BaseModel):
    """Secret-free path manifest for the one-shot finishing command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = RUN_SPEC_SCHEMA_VERSION
    sandbox_attestation: str = Field(min_length=1)
    cleanup_receipt: str = Field(min_length=1)
    observed_versions: dict[str, str]
    populations: tuple[PopulationEvidenceFiles, ...]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _observation_digest(record: RuntimeObservationReceipt) -> str:
    return sha256_hex(
        canonical_json(
            record.model_copy(update={"observation_digest": ""}).model_dump(
                mode="json"
            )
        )
    )


def _lifecycle_digest(record: CertificationLifecycleReceipt) -> str:
    return sha256_hex(
        canonical_json(
            record.model_copy(update={"lifecycle_digest": ""}).model_dump(
                mode="json"
            )
        )
    )


def _intent_digest(record: CertificationCompletionIntent) -> str:
    return sha256_hex(
        canonical_json(
            record.model_copy(update={"intent_digest": ""}).model_dump(
                mode="json"
            )
        )
    )


def _read_json(path: Path | str, *, label: str) -> Any:
    """Read one bounded non-symlink JSON file without a check/read race."""

    target = Path(path)
    try:
        before = target.lstat()
    except OSError as exc:
        raise CertificationLifecycleError(f"{label} is missing or unreadable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_size > _MAX_JSON_BYTES
    ):
        raise CertificationLifecycleError(f"{label} is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise CertificationLifecycleError(f"{label} is missing or unreadable") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size > _MAX_JSON_BYTES
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise CertificationLifecycleError(f"{label} changed while it was opened")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1 << 20, _MAX_JSON_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_JSON_BYTES:
                raise CertificationLifecycleError(f"{label} exceeds its safety bound")
        finished = os.fstat(descriptor)
        if (
            (finished.st_dev, finished.st_ino) != (opened.st_dev, opened.st_ino)
            or finished.st_size != opened.st_size
            or finished.st_mtime_ns != opened.st_mtime_ns
            or total != opened.st_size
        ):
            raise CertificationLifecycleError(f"{label} changed while it was read")
    finally:
        os.close(descriptor)
    def closed_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise CertificationLifecycleError(
                    f"{label} contains duplicate JSON key {key!r}"
                )
            value[key] = item
        return value

    try:
        return json.loads(
            b"".join(chunks).decode("utf-8"), object_pairs_hook=closed_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CertificationLifecycleError(f"{label} is not valid JSON") from exc


def _publish_json(path: Path, payload: Mapping[str, Any]) -> Path:
    """Atomically create a complete owner-readable JSON record without overwrite."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (readable_json(dict(payload)) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.stage-",
    )
    temporary = Path(temporary_name)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary, target)
        except FileExistsError:
            raise CertificationLifecycleError(
                f"lifecycle record already exists (immutable): {target}"
            ) from None
        temporary.unlink()
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    return target


def publish_immutable_json(path: Path, payload: Mapping[str, Any]) -> Path:
    """Atomically create an immutable JSON record at its final path.

    This public boundary lets standalone promotion commands use the same
    staged, fsynced, exclusive publisher as the lifecycle orchestrator.
    """

    return _publish_json(path, payload)


def _verified_manifest(
    release_dir: Path | str, task_id: str
) -> tuple[Path, release_mod.ReleaseManifest]:
    release = Path(release_dir).resolve()
    try:
        verification = release_mod.verify_release(release)
        manifest = release_mod.ReleaseManifest.model_validate_json(
            (release / "release_manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise CertificationLifecycleError(
            f"cannot verify runtime release: {exc}"
        ) from exc
    if not verification.ok:
        raise CertificationLifecycleError(
            "release verification failed: " + "; ".join(verification.failures[:3])
        )
    if manifest.release_mode != release_mod.CERTIFIED_RELEASE_MODE:
        raise CertificationLifecycleError(
            "runtime certification requires a certified release"
        )
    if task_id not in manifest.tasks:
        raise CertificationLifecycleError(f"release has no task {task_id!r}")
    certification_id = manifest.certification_ids.get(task_id, "")
    if not certification_id:
        raise CertificationLifecycleError(
            "release task has no runtime certification identity"
        )
    return release, manifest


def _certification_dir(
    store_dir: Path | str,
    manifest: release_mod.ReleaseManifest,
    task_id: str,
) -> Path:
    return Path(store_dir).resolve() / manifest.certification_ids[task_id]


def _attempt_lifecycle_dir(pending_dir: Path, attempt_id: str) -> Path:
    return pending_dir / LIFECYCLE_DIRNAME / attempt_id


def _load_sandbox_attestation(path: Path | str) -> SandboxAttestation:
    payload = _read_json(path, label="sandbox attestation")
    try:
        parsed = SandboxAttestation.model_validate(payload)
        return verify_sandbox_attestation(parsed)
    except (RuntimeError, ValueError) as exc:
        raise CertificationLifecycleError(
            f"sandbox attestation is invalid: {exc}"
        ) from exc


def verify_release_bound_sandbox(
    release_dir: Path | str,
    task_id: str,
    attestation: SandboxAttestation | Path | str,
    *,
    agents_config: Path | str | None | object = _ACTIVE_AGENTS_CONFIG_NOT_SUPPLIED,
) -> SandboxAttestation:
    """Require a fresh tier A or B sandbox record bound to this release.

    Match the sealed sandbox pin and agent configuration identity.
    """

    release, manifest = _verified_manifest(release_dir, task_id)
    try:
        record = (
            _load_sandbox_attestation(attestation)
            if isinstance(attestation, (Path, str))
            else verify_sandbox_attestation(attestation)
        )
        from elt_taskgen.verification.contamination import enforcement

        gate_kwargs: dict[str, Any] = {}
        if agents_config is not _ACTIVE_AGENTS_CONFIG_NOT_SUPPLIED:
            gate_kwargs["agents_config"] = agents_config
        require_attestation_for_labels(
            manifest,
            record,
            contamination_mode=enforcement(),
            run_id=manifest.release_id,
            **gate_kwargs,
        )
        release_record = _load_sandbox_attestation(
            release / release_mod.SANDBOX_ATTESTATION_FILENAME
        )
        if (
            record.sandbox_pin != release_record.sandbox_pin
            or record.agents_config_sha256 != release_record.agents_config_sha256
        ):
            raise CertificationLifecycleError(
                "sandbox attestation configuration identity differs from the "
                "frozen release"
            )
    except RuntimeError as exc:
        raise CertificationLifecycleError(str(exc)) from exc
    return record


def mint_unbound_sandbox_attestation(
    *,
    mount_root: Path | str,
    out: Path | str,
    agents_config: Path | str | None = None,
    image_digest: str = "",
    workspace_template_sha256: str = "",
    preflight: PreflightResult | None = None,
) -> SandboxAttestation:
    """Publish a validated sandbox attestation before the release ID exists.

    Seal it with an empty run ID after applying labelled-release checks.
    ``freeze_release`` later binds and reseals it. The optional preflight is for
    tests and trusted callers; the CLI observes the host directly.
    """

    observed_preflight = isolation_preflight() if preflight is None else preflight
    try:
        from elt_taskgen.review.providers import sandbox_pin
        from elt_taskgen.verification.contamination import enforcement

        record = attest_sandbox(
            pinned=sandbox_pin(agents_config=agents_config),
            preflight=observed_preflight,
            image_digest=image_digest,
            workspace_template_sha256=workspace_template_sha256,
            mount_root=mount_root,
            run_id="",
            agents_config=agents_config,
        )
        verified = verify_sandbox_attestation(record)
        if verified.run_id:
            raise ValueError(
                "pre-freeze sandbox attestation must have an empty run_id"
            )
        require_attestation_for_labels(
            {"claims_rlvr_labels": True},
            verified,
            contamination_mode=enforcement(),
            agents_config=agents_config,
        )
    except (RuntimeError, ValueError) as exc:
        raise CertificationLifecycleError(str(exc)) from exc
    _publish_json(Path(out).resolve(), verified.model_dump(mode="json"))
    return verified


def mint_release_bound_sandbox_attestation(
    *,
    release_dir: Path | str,
    task_id: str,
    mount_root: Path | str,
    out: Path | str,
    agents_config: Path | str | None = None,
    image_digest: str = "",
    workspace_template_sha256: str = "",
    preflight: PreflightResult | None = None,
) -> SandboxAttestation:
    """Observe the host and publish an attestation bound to the release.

    Tests and trusted callers may supply a preflight. The CLI observes the host
    directly. Tier C and D fail before publication.
    """

    _, manifest = _verified_manifest(release_dir, task_id)
    observed_preflight = isolation_preflight() if preflight is None else preflight
    try:
        from elt_taskgen.review.providers import sandbox_pin

        record = attest_sandbox(
            pinned=sandbox_pin(agents_config=agents_config),
            preflight=observed_preflight,
            image_digest=image_digest,
            workspace_template_sha256=workspace_template_sha256,
            mount_root=mount_root,
            run_id=manifest.release_id,
            agents_config=agents_config,
        )
        verified = verify_release_bound_sandbox(
            release_dir,
            task_id,
            record,
            agents_config=agents_config,
        )
    except (RuntimeError, ValueError) as exc:
        raise CertificationLifecycleError(str(exc)) from exc
    _publish_json(Path(out).resolve(), verified.model_dump(mode="json"))
    return verified


def begin_runtime_certification(
    *,
    release_dir: Path | str,
    task_id: str,
    certification_store: Path | str,
    sandbox_attestation: Path | str,
) -> tuple[Path, str]:
    """Open the pending nonce and bind its trusted sandbox record."""

    _, manifest = _verified_manifest(release_dir, task_id)
    record = verify_release_bound_sandbox(
        release_dir, task_id, sandbox_attestation
    )
    try:
        pending_dir = certification_mod.begin_certification(
            Path(certification_store), manifest, task_id
        )
        attempt_id = certification_mod.pending_attempt_id(pending_dir)
    except (certification_mod.CertificationError, OSError, ValueError) as exc:
        raise CertificationLifecycleError(str(exc)) from exc
    try:
        _publish_json(
            _attempt_lifecycle_dir(pending_dir, attempt_id)
            / SANDBOX_ATTESTATION_FILENAME,
            record.model_dump(mode="json"),
        )
    except BaseException:
        # This process just created the pending capability. If binding its
        # required sandbox record fails, withdraw only that same nonce so a
        # recoverable I/O error cannot brick the certification id in PENDING.
        try:
            if certification_mod.pending_attempt_id(pending_dir) == attempt_id:
                (pending_dir / certification_mod.PENDING_FILENAME).unlink()
        except (certification_mod.CertificationError, OSError):
            pass
        raise
    return pending_dir, attempt_id


def _closed_payload(
    payload: Any, expected: set[str], *, label: str
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CertificationLifecycleError(f"{label} must be a JSON object")
    extra = set(payload) - expected
    missing = expected - set(payload)
    if extra or missing:
        raise CertificationLifecycleError(
            f"{label} has the wrong field roster "
            f"(missing={sorted(missing)}, extra={sorted(extra)})"
        )
    return dict(payload)


def _stage1_execution(path: Path | str) -> Stage1Execution:
    fields = set(Stage1Execution.__dataclass_fields__)
    values = _closed_payload(
        _read_json(path, label="Stage-1 execution receipt"),
        fields,
        label="Stage-1 execution receipt",
    )
    try:
        values["connection_ids"] = tuple(values["connection_ids"])
        values["terraform_connection_resources"] = tuple(
            tuple(item) for item in values["terraform_connection_resources"]
        )
        values["workspace_dir"] = Path(values["workspace_dir"])
        values["terraform_state_path"] = Path(values["terraform_state_path"])
        return Stage1Execution(**values)
    except (TypeError, ValueError) as exc:
        raise CertificationLifecycleError(
            f"Stage-1 execution receipt is invalid: {exc}"
        ) from exc


def _stage2_execution(path: Path | str) -> Stage2Execution:
    fields = set(Stage2Execution.__dataclass_fields__)
    values = _closed_payload(
        _read_json(path, label="Stage-2 execution receipt"),
        fields,
        label="Stage-2 execution receipt",
    )
    raw_preflight = values["preflight"]
    if not isinstance(raw_preflight, dict):
        raise CertificationLifecycleError(
            "Stage-2 execution receipt has no preflight object"
        )
    try:
        values["preflight"] = Stage2Preflight(
            destination=normalize_destination(raw_preflight["destination"]),
            dbt_core_version=raw_preflight["dbt_core_version"],
            adapter_version=raw_preflight["adapter_version"],
            profile_name=raw_preflight["profile_name"],
            target_name=raw_preflight["target_name"],
            namespace=raw_preflight["namespace"],
            physical_container=raw_preflight["physical_container"],
        )
        values["project_dir"] = Path(values["project_dir"])
        values["profiles_dir"] = Path(values["profiles_dir"])
        values["run_results_path"] = Path(values["run_results_path"])
        values["dbt_expected_model_ids"] = tuple(values["dbt_expected_model_ids"])
        values["dbt_observed_model_ids"] = tuple(values["dbt_observed_model_ids"])
        return Stage2Execution(**values)
    except (KeyError, TypeError, ValueError) as exc:
        raise CertificationLifecycleError(
            f"Stage-2 execution receipt is invalid: {exc}"
        ) from exc


def _stage1_result(path: Path | str) -> Stage1EvaluationResult:
    payload = _read_json(path, label="Stage-1 evaluator result")
    if not isinstance(payload, dict):
        raise CertificationLifecycleError(
            "Stage-1 evaluator result must be a JSON object"
        )
    values = dict(payload)
    values.pop("certification_passed", None)
    values = _closed_payload(
        values,
        set(Stage1EvaluationResult.__dataclass_fields__),
        label="Stage-1 evaluator result",
    )
    try:
        values["missing_tables"] = tuple(values["missing_tables"])
        values["unexpected_tables"] = tuple(values["unexpected_tables"])
        return Stage1EvaluationResult(**values)
    except (TypeError, ValueError) as exc:
        raise CertificationLifecycleError(
            f"Stage-1 evaluator result is invalid: {exc}"
        ) from exc


def _stage2_result(path: Path | str) -> Stage2EvaluationResult:
    payload = _read_json(path, label="Stage-2 evaluator result")
    if not isinstance(payload, dict):
        raise CertificationLifecycleError(
            "Stage-2 evaluator result must be a JSON object"
        )
    values = dict(payload)
    values.pop("certification_passed", None)
    values = _closed_payload(
        values,
        set(Stage2EvaluationResult.__dataclass_fields__),
        label="Stage-2 evaluator result",
    )
    try:
        raw_fingerprints = values.get("column_fingerprints")
        if not isinstance(raw_fingerprints, dict):
            raise TypeError("column_fingerprints must be an object")
        values["column_fingerprints"] = {
            str(mart): tuple(tuple(pair) for pair in pairs)
            for mart, pairs in raw_fingerprints.items()
        }
        return Stage2EvaluationResult(**values)
    except (TypeError, ValueError) as exc:
        raise CertificationLifecycleError(
            f"Stage-2 evaluator result is invalid: {exc}"
        ) from exc


def _pending_context(
    *,
    release_dir: Path | str,
    task_id: str,
    certification_store: Path | str,
) -> tuple[release_mod.ReleaseManifest, Path, str]:
    _, manifest = _verified_manifest(release_dir, task_id)
    pending_dir = _certification_dir(certification_store, manifest, task_id)
    try:
        attempt_id = certification_mod.pending_attempt_id(pending_dir)
    except certification_mod.CertificationError as exc:
        raise CertificationLifecycleError(str(exc)) from exc
    return manifest, pending_dir, attempt_id


def _population(value: str, manifest: release_mod.ReleaseManifest, task_id: str) -> str:
    try:
        population = PopulationName(value).value
    except ValueError as exc:
        raise CertificationLifecycleError(f"unknown population {value!r}") from exc
    if population not in (manifest.el_sources.get(task_id) or {}):
        raise CertificationLifecycleError(
            f"population {population!r} is not released for task {task_id!r}"
        )
    return population


def _staged_path(
    pending_dir: Path, attempt_id: str, population: str, stage: int
) -> Path:
    return (
        _attempt_lifecycle_dir(pending_dir, attempt_id)
        / STAGED_EVIDENCE_DIRNAME
        / population
        / f"stage{stage}.json"
    )


def _stage_evidence_inputs(
    record: (
        certification_mod.Stage1CertificationEvidence
        | certification_mod.Stage2CertificationEvidence
    ),
) -> dict[str, Any]:
    """Return derived evidence fields without its publisher timestamp and seal.

    An existing sealed record is reusable only when all derived fields match.
    """

    return record.model_dump(
        mode="json",
        exclude={"recorded_at", "evidence_digest"},
    )


def _publish_stage_evidence_idempotently(
    target: Path,
    candidate: (
        certification_mod.Stage1CertificationEvidence
        | certification_mod.Stage2CertificationEvidence
    ),
    *,
    stage: Literal[1, 2],
) -> (
    certification_mod.Stage1CertificationEvidence
    | certification_mod.Stage2CertificationEvidence
):
    """Publish one stage record, or verify and adopt its exact replay."""

    try:
        _publish_json(target, candidate.model_dump(mode="json"))
        return candidate
    except (CertificationLifecycleError, OSError) as exc:
        if not os.path.lexists(target):
            raise
        verifier = (
            certification_mod.verify_stage1_evidence
            if stage == 1
            else certification_mod.verify_stage2_evidence
        )
        label = f"Stage-{stage} evidence"
        try:
            existing = verifier(target, require_pass=True)
        except certification_mod.CertificationError as read_exc:
            raise CertificationLifecycleError(
                f"existing staged {label} is invalid: {read_exc}"
            ) from read_exc
        if _stage_evidence_inputs(existing) != _stage_evidence_inputs(candidate):
            raise CertificationLifecycleError(
                f"existing staged {label} conflicts with the supplied run"
            ) from exc
        return existing


def record_stage1_evidence(
    *,
    release_dir: Path | str,
    task_id: str,
    certification_store: Path | str,
    population: str,
    execution_receipt: Path | str,
    evaluator_result: Path | str,
) -> certification_mod.Stage1CertificationEvidence:
    """Re-read runtime artifacts and immutably stage strict Stage-1 evidence."""

    manifest, pending_dir, attempt_id = _pending_context(
        release_dir=release_dir,
        task_id=task_id,
        certification_store=certification_store,
    )
    selected = _population(population, manifest, task_id)
    try:
        evidence = certification_mod.stage1_evidence_from_execution(
            manifest,
            task_id,
            _stage1_result(evaluator_result),
            _stage1_execution(execution_receipt),
            attempt_id=attempt_id,
        )
        verified = certification_mod.verify_stage1_evidence(
            evidence, require_pass=True
        )
    except certification_mod.CertificationError as exc:
        raise CertificationLifecycleError(str(exc)) from exc
    if verified.population != selected:
        raise CertificationLifecycleError(
            "Stage-1 result population does not match the requested population"
        )
    target = _staged_path(pending_dir, attempt_id, selected, 1)
    adopted = _publish_stage_evidence_idempotently(target, verified, stage=1)
    assert isinstance(adopted, certification_mod.Stage1CertificationEvidence)
    return adopted


def record_stage2_evidence(
    *,
    release_dir: Path | str,
    task_id: str,
    certification_store: Path | str,
    population: str,
    execution_receipt: Path | str,
    evaluator_result: Path | str,
) -> certification_mod.Stage2CertificationEvidence:
    """Re-read runtime artifacts and immutably stage strict Stage-2 evidence."""

    manifest, pending_dir, attempt_id = _pending_context(
        release_dir=release_dir,
        task_id=task_id,
        certification_store=certification_store,
    )
    selected = _population(population, manifest, task_id)
    stage1_path = _staged_path(pending_dir, attempt_id, selected, 1)
    if not stage1_path.is_file():
        raise CertificationLifecycleError(
            f"record passing Stage-1 evidence for {selected!r} before Stage 2"
        )
    try:
        stage1 = certification_mod.verify_stage1_evidence(
            stage1_path, require_pass=True
        )
        evidence = certification_mod.stage2_evidence_from_execution(
            manifest,
            task_id,
            _stage2_result(evaluator_result),
            _stage2_execution(execution_receipt),
            attempt_id=attempt_id,
        )
        verified = certification_mod.verify_stage2_evidence(
            evidence, require_pass=True
        )
    except certification_mod.CertificationError as exc:
        raise CertificationLifecycleError(str(exc)) from exc
    if verified.population != selected:
        raise CertificationLifecycleError(
            "Stage-2 result population does not match the requested population"
        )
    if (
        stage1.attempt_id != verified.attempt_id
        or stage1.warehouse_namespace.casefold()
        != verified.warehouse_namespace.casefold()
        or stage1.physical_container.casefold()
        != verified.physical_container.casefold()
    ):
        raise CertificationLifecycleError(
            "Stage-1 and Stage-2 evidence do not describe one runtime attempt"
        )
    target = _staged_path(pending_dir, attempt_id, selected, 2)
    adopted = _publish_stage_evidence_idempotently(target, verified, stage=2)
    assert isinstance(adopted, certification_mod.Stage2CertificationEvidence)
    return adopted


def record_runtime_observations(
    *,
    release_dir: Path | str,
    task_id: str,
    certification_store: Path | str,
    observed_versions: Mapping[str, str],
) -> RuntimeObservationReceipt:
    """Validate and seal the closed runtime-observation roster."""

    manifest, pending_dir, attempt_id = _pending_context(
        release_dir=release_dir,
        task_id=task_id,
        certification_store=certification_store,
    )
    if not isinstance(observed_versions, Mapping):
        raise CertificationLifecycleError("runtime observations must be an object")
    required = set(certification_mod.required_observation_keys(manifest, task_id))
    if set(observed_versions) != required:
        raise CertificationLifecycleError(
            "runtime observations have the wrong field roster "
            f"(missing={sorted(required - set(observed_versions))}, "
            f"extra={sorted(set(observed_versions) - required)})"
        )
    matrix = manifest.certification_matrix[manifest.destinations[task_id]]
    normalized: dict[str, str] = {}
    for key, value in observed_versions.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or value != value.strip()
            or len(value) > 2048
            or any(ord(char) < 32 for char in value)
        ):
            raise CertificationLifecycleError(
                f"runtime observation {key!r} is not canonical text"
            )
        if matrix.get(key) != value:
            raise CertificationLifecycleError(
                f"runtime observation {key!r} contradicts the release matrix"
            )
        normalized[key] = value
    record = RuntimeObservationReceipt(
        certification_id=manifest.certification_ids[task_id],
        release_id=manifest.release_id,
        task_id=task_id,
        attempt_id=attempt_id,
        observed_versions=dict(sorted(normalized.items())),
        recorded_at=_utc_now(),
    )
    sealed = record.model_copy(
        update={"observation_digest": _observation_digest(record)}
    )
    target = (
        _attempt_lifecycle_dir(pending_dir, attempt_id) / OBSERVATIONS_FILENAME
    )
    try:
        _publish_json(target, sealed.model_dump(mode="json"))
        return sealed
    except (CertificationLifecycleError, OSError) as exc:
        if not os.path.lexists(target):
            raise
        existing = _verify_observation_receipt(
            target,
            manifest=manifest,
            task_id=task_id,
            attempt_id=attempt_id,
        )
        # ``recorded_at`` and its seal are publisher-minted. All remaining
        # fields must reproduce from this invocation's exact observations.
        excluded = {"recorded_at", "observation_digest"}
        if existing.model_dump(mode="json", exclude=excluded) != sealed.model_dump(
            mode="json", exclude=excluded
        ):
            raise CertificationLifecycleError(
                "existing runtime observations conflict with the supplied run"
            ) from exc
        return existing


def _verify_observation_receipt(
    path: Path,
    *,
    manifest: release_mod.ReleaseManifest,
    task_id: str,
    attempt_id: str,
) -> RuntimeObservationReceipt:
    try:
        record = RuntimeObservationReceipt.model_validate(
            _read_json(path, label="runtime observation receipt")
        )
    except ValueError as exc:
        raise CertificationLifecycleError(
            f"runtime observation receipt is invalid: {exc}"
        ) from exc
    if not record.observation_digest:
        raise CertificationLifecycleError("runtime observation receipt is unsealed")
    if record.observation_digest != _observation_digest(record):
        raise CertificationLifecycleError(
            "runtime observation receipt digest does not reproduce"
        )
    expected = (
        manifest.certification_ids[task_id],
        manifest.release_id,
        task_id,
        attempt_id,
    )
    actual = (
        record.certification_id,
        record.release_id,
        record.task_id,
        record.attempt_id,
    )
    if actual != expected:
        raise CertificationLifecycleError(
            "runtime observation receipt does not bind the pending release attempt"
        )
    required = set(certification_mod.required_observation_keys(manifest, task_id))
    if set(record.observed_versions) != required:
        raise CertificationLifecycleError(
            "runtime observation receipt does not cover the closed required roster"
        )
    matrix = manifest.certification_matrix[manifest.destinations[task_id]]
    malformed = sorted(
        key
        for key, value in record.observed_versions.items()
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or value != value.strip()
            or len(value) > 2048
            or any(ord(char) < 32 for char in value)
        )
    )
    if malformed:
        raise CertificationLifecycleError(
            "runtime observation receipt contains non-canonical values: "
            + ", ".join(malformed)
        )
    contradictions = sorted(
        key
        for key, value in record.observed_versions.items()
        if matrix.get(key) != value
    )
    if contradictions:
        raise CertificationLifecycleError(
            "runtime observations contradict the release matrix: "
            + ", ".join(contradictions)
        )
    return record


def _staged_evidence_pairs(
    pending_dir: Path,
    *,
    manifest: release_mod.ReleaseManifest,
    task_id: str,
    attempt_id: str,
) -> tuple[
    tuple[
        certification_mod.Stage1CertificationEvidence,
        certification_mod.Stage2CertificationEvidence,
    ],
    ...,
]:
    root = (
        _attempt_lifecycle_dir(pending_dir, attempt_id)
        / STAGED_EVIDENCE_DIRNAME
    )
    try:
        root_status = root.lstat()
    except OSError as exc:
        raise CertificationLifecycleError(
            "no staged certification evidence exists"
        ) from exc
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise CertificationLifecycleError(
            "staged certification evidence root is not a regular directory"
        )
    descendants = tuple(root.rglob("*"))
    if any(path.is_symlink() for path in descendants):
        raise CertificationLifecycleError(
            "staged certification evidence contains a symlink"
        )
    files = {
        path.relative_to(root).as_posix()
        for path in descendants
        if path.is_file()
    }
    directories = sorted(path for path in root.iterdir() if path.is_dir())
    if not directories:
        raise CertificationLifecycleError("no population evidence was staged")
    expected_files = {
        f"{directory.name}/stage{stage}.json"
        for directory in directories
        for stage in (1, 2)
    }
    expected_directories = {directory.name for directory in directories}
    actual_directories = {
        path.relative_to(root).as_posix()
        for path in descendants
        if path.is_dir()
    }
    if files != expected_files or actual_directories != expected_directories:
        raise CertificationLifecycleError(
            "staged evidence must contain exactly stage1.json and stage2.json "
            f"for every population (missing={sorted(expected_files - files)}, "
            f"extra={sorted(files - expected_files)}, "
            f"unexpected_directories="
            f"{sorted(actual_directories - expected_directories)})"
        )
    pairs = []
    for directory in directories:
        population = _population(directory.name, manifest, task_id)
        try:
            stage1 = certification_mod.verify_stage1_evidence(
                directory / "stage1.json", require_pass=True
            )
            stage2 = certification_mod.verify_stage2_evidence(
                directory / "stage2.json", require_pass=True
            )
        except certification_mod.CertificationError as exc:
            raise CertificationLifecycleError(str(exc)) from exc
        for record in (stage1, stage2):
            if (
                record.certification_id != manifest.certification_ids[task_id]
                or record.release_id != manifest.release_id
                or record.task_id != task_id
                or record.population != population
                or record.attempt_id != attempt_id
            ):
                raise CertificationLifecycleError(
                    "staged evidence does not bind the pending release attempt"
                )
        pairs.append((stage1, stage2))
    return tuple(pairs)


def record_certification_cleanup(
    *,
    release_dir: Path | str,
    task_id: str,
    certification_store: Path | str,
    cleanup_receipt: Path | str,
) -> CleanupReceipt:
    """Accept cleanup only after every staged population has both stages."""

    manifest, pending_dir, attempt_id = _pending_context(
        release_dir=release_dir,
        task_id=task_id,
        certification_store=certification_store,
    )
    evidence_pairs = _staged_evidence_pairs(
        pending_dir,
        manifest=manifest,
        task_id=task_id,
        attempt_id=attempt_id,
    )
    _verify_observation_receipt(
        _attempt_lifecycle_dir(pending_dir, attempt_id) / OBSERVATIONS_FILENAME,
        manifest=manifest,
        task_id=task_id,
        attempt_id=attempt_id,
    )
    try:
        payload = _read_json(cleanup_receipt, label="cleanup receipt")
        receipt = verify_cleanup_receipt(
            payload,
            attempt_id=attempt_id,
            destination=manifest.destinations[task_id],
            require_complete=True,
            expected_grader_commands=(
                GRADER_COMMANDS_PER_POPULATION * len(evidence_pairs)
            ),
        )
    except ModelCopyError as exc:
        raise CertificationLifecycleError(str(exc)) from exc
    target = _attempt_lifecycle_dir(pending_dir, attempt_id) / CLEANUP_FILENAME
    try:
        _publish_json(target, dict(vars(receipt)))
        return receipt
    except (CertificationLifecycleError, OSError) as exc:
        if not os.path.lexists(target):
            raise
        try:
            existing = verify_cleanup_receipt(
                _read_json(target, label="staged cleanup receipt"),
                attempt_id=attempt_id,
                destination=manifest.destinations[task_id],
                require_complete=True,
                expected_grader_commands=(
                    GRADER_COMMANDS_PER_POPULATION * len(evidence_pairs)
                ),
            )
        except ModelCopyError as read_exc:
            raise CertificationLifecycleError(
                f"existing staged cleanup receipt is invalid: {read_exc}"
            ) from read_exc
        if existing != receipt:
            raise CertificationLifecycleError(
                "existing staged cleanup receipt conflicts with the supplied run"
            ) from exc
        return existing


def _load_completion_intent(path: Path) -> CertificationCompletionIntent:
    try:
        intent = CertificationCompletionIntent.model_validate(
            _read_json(path, label="certification completion intent")
        )
    except ValueError as exc:
        raise CertificationLifecycleError(
            f"certification completion intent is invalid: {exc}"
        ) from exc
    if not intent.intent_digest:
        raise CertificationLifecycleError(
            "certification completion intent is unsealed"
        )
    if intent.intent_digest != _intent_digest(intent):
        raise CertificationLifecycleError(
            "certification completion intent digest does not reproduce"
        )
    return intent


def _evidence_inventory(
    evidence_pairs: tuple[
        tuple[
            certification_mod.Stage1CertificationEvidence,
            certification_mod.Stage2CertificationEvidence,
        ],
        ...,
    ],
) -> tuple[tuple[str, ...], dict[str, str]]:
    populations: list[str] = []
    digests: dict[str, str] = {}
    for stage1, stage2 in evidence_pairs:
        population = stage1.population
        if stage2.population != population or population in populations:
            raise CertificationLifecycleError(
                "staged evidence does not have one pair per population"
            )
        populations.append(population)
        digests[f"{population}/stage1.json"] = stage1.evidence_digest
        digests[f"{population}/stage2.json"] = stage2.evidence_digest
    return tuple(sorted(populations)), dict(sorted(digests.items()))


def _validate_completion_intent(
    intent: CertificationCompletionIntent,
    *,
    manifest: release_mod.ReleaseManifest,
    task_id: str,
    attempt_id: str,
    populations: tuple[str, ...],
    evidence_digests: Mapping[str, str],
    sandbox: SandboxAttestation,
    observations: RuntimeObservationReceipt,
    cleanup: CleanupReceipt,
    attestation: certification_mod.CertificationAttestation | None = None,
) -> None:
    expected = (
        manifest.certification_ids[task_id],
        manifest.release_id,
        manifest.runtime_bundle_ids[task_id],
        task_id,
        manifest.destinations[task_id],
        attempt_id,
        populations,
        sandbox.attestation_digest,
        observations.observation_digest,
        cleanup.receipt_digest,
        dict(evidence_digests),
    )
    actual = (
        intent.certification_id,
        intent.release_id,
        intent.runtime_bundle_id,
        intent.task_id,
        intent.destination,
        intent.attempt_id,
        intent.populations,
        intent.sandbox_attestation_digest,
        intent.observation_digest,
        intent.cleanup_receipt_digest,
        intent.evidence_digests,
    )
    if actual != expected:
        raise CertificationLifecycleError(
            "certification completion intent contradicts verified lifecycle inputs"
        )
    try:
        prepared = datetime.fromisoformat(
            intent.prepared_at.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise CertificationLifecycleError(
            "certification completion intent prepared_at is not ISO-8601"
        ) from exc
    if prepared.tzinfo is None or prepared.utcoffset() != timezone.utc.utcoffset(
        prepared
    ):
        raise CertificationLifecycleError(
            "certification completion intent prepared_at must be UTC"
        )
    if attestation is not None:
        try:
            completed = datetime.fromisoformat(
                attestation.completed_at.replace("Z", "+00:00")
            )
        except ValueError as exc:  # also checked by verify_attestation
            raise CertificationLifecycleError(
                "certification completion time is not ISO-8601"
            ) from exc
        if prepared > completed:
            raise CertificationLifecycleError(
                "certification completion intent is later than the attestation"
            )


def _prepare_completion_intent(
    path: Path,
    *,
    manifest: release_mod.ReleaseManifest,
    task_id: str,
    attempt_id: str,
    evidence_pairs: tuple[
        tuple[
            certification_mod.Stage1CertificationEvidence,
            certification_mod.Stage2CertificationEvidence,
        ],
        ...,
    ],
    sandbox: SandboxAttestation,
    observations: RuntimeObservationReceipt,
    cleanup: CleanupReceipt,
) -> CertificationCompletionIntent:
    populations, evidence_digests = _evidence_inventory(evidence_pairs)
    if os.path.lexists(path):
        existing = _load_completion_intent(path)
        _validate_completion_intent(
            existing,
            manifest=manifest,
            task_id=task_id,
            attempt_id=attempt_id,
            populations=populations,
            evidence_digests=evidence_digests,
            sandbox=sandbox,
            observations=observations,
            cleanup=cleanup,
        )
        return existing
    candidate = CertificationCompletionIntent(
        certification_id=manifest.certification_ids[task_id],
        release_id=manifest.release_id,
        runtime_bundle_id=manifest.runtime_bundle_ids[task_id],
        task_id=task_id,
        destination=manifest.destinations[task_id],
        attempt_id=attempt_id,
        populations=populations,
        sandbox_attestation_digest=sandbox.attestation_digest,
        observation_digest=observations.observation_digest,
        cleanup_receipt_digest=cleanup.receipt_digest,
        evidence_digests=evidence_digests,
        prepared_at=_utc_now(),
    )
    sealed = candidate.model_copy(
        update={"intent_digest": _intent_digest(candidate)}
    )
    try:
        _publish_json(path, sealed.model_dump(mode="json"))
        return sealed
    except (CertificationLifecycleError, OSError):
        if not os.path.lexists(path):
            raise
        # An identical concurrent preparer may have won the exclusive link.
        existing = _load_completion_intent(path)
        _validate_completion_intent(
            existing,
            manifest=manifest,
            task_id=task_id,
            attempt_id=attempt_id,
            populations=populations,
            evidence_digests=evidence_digests,
            sandbox=sandbox,
            observations=observations,
            cleanup=cleanup,
        )
        return existing


def _verified_completed_attestation(
    *,
    manifest: release_mod.ReleaseManifest,
    task_id: str,
    certification_store: Path | str,
    id_dir: Path,
) -> certification_mod.CertificationAttestation:
    """Load the exact manifest-bound attestation and its persisted evidence."""

    certification_id = manifest.certification_ids[task_id]
    status = certification_mod.certification_status(
        Path(certification_store), certification_id
    )
    if status.state is not certification_mod.CertificationState.CERTIFIED:
        raise CertificationLifecycleError(
            f"runtime certification is {status.state.value}: {status.reason}"
        )
    try:
        attestation = certification_mod.verify_attestation(
            id_dir / certification_mod.ATTESTATION_FILENAME
        )
    except certification_mod.CertificationError as exc:
        raise CertificationLifecycleError(str(exc)) from exc
    expected = (
        certification_id,
        manifest.semantic_release_id,
        manifest.release_id,
        manifest.runtime_bundle_ids[task_id],
        task_id,
        manifest.destinations[task_id],
    )
    actual = (
        attestation.certification_id,
        attestation.semantic_release_id,
        attestation.release_id,
        attestation.runtime_bundle_id,
        attestation.task_id,
        attestation.destination,
    )
    if actual != expected:
        raise CertificationLifecycleError(
            "completed certification attestation does not bind the requested release"
        )
    return attestation


def _attested_attempt_id(
    id_dir: Path,
    attestation: certification_mod.CertificationAttestation,
) -> str:
    """Recover the shared attempt ID from sealed stage evidence."""

    attempts: set[str] = set()
    evidence_root = id_dir / certification_mod.EVIDENCE_DIRNAME
    try:
        for population in attestation.populations:
            stage1 = certification_mod.verify_stage1_evidence(
                evidence_root / population / "stage1.json",
                require_pass=True,
            )
            stage2 = certification_mod.verify_stage2_evidence(
                evidence_root / population / "stage2.json",
                require_pass=True,
            )
            attempts.update((stage1.attempt_id, stage2.attempt_id))
    except certification_mod.CertificationError as exc:
        raise CertificationLifecycleError(str(exc)) from exc
    if len(attempts) != 1:
        raise CertificationLifecycleError(
            "completed certification evidence does not name one lifecycle attempt"
        )
    attempt_id = next(iter(attempts))
    prefix = "certification-attempt-"
    suffix = attempt_id.removeprefix(prefix)
    if (
        not attempt_id.startswith(prefix)
        or len(suffix) != 64
        or any(character not in "0123456789abcdef" for character in suffix)
    ):
        raise CertificationLifecycleError(
            "completed certification evidence names a non-generated attempt id"
        )
    return attempt_id


def _seal_lifecycle_receipt(
    *,
    attestation: certification_mod.CertificationAttestation,
    attempt_id: str,
    sandbox: SandboxAttestation,
    observations: RuntimeObservationReceipt,
    cleanup: CleanupReceipt,
) -> CertificationLifecycleReceipt:
    receipt = CertificationLifecycleReceipt(
        certification_id=attestation.certification_id,
        release_id=attestation.release_id,
        runtime_bundle_id=attestation.runtime_bundle_id,
        task_id=attestation.task_id,
        destination=attestation.destination,
        attempt_id=attempt_id,
        populations=attestation.populations,
        sandbox_attestation_digest=sandbox.attestation_digest,
        observation_digest=observations.observation_digest,
        cleanup_receipt_digest=cleanup.receipt_digest,
        certification_attestation_digest=attestation.attestation_digest,
        evidence_digests=attestation.evidence_digests,
        completed_at=attestation.completed_at,
    )
    return receipt.model_copy(
        update={"lifecycle_digest": _lifecycle_digest(receipt)}
    )


def _publish_lifecycle_idempotently(
    path: Path,
    receipt: CertificationLifecycleReceipt,
) -> CertificationLifecycleReceipt:
    """Publish once, or accept only the exact already-published receipt."""

    try:
        _publish_json(path, receipt.model_dump(mode="json"))
        return receipt
    except (CertificationLifecycleError, OSError) as exc:
        if not os.path.lexists(path):
            raise
        try:
            existing = CertificationLifecycleReceipt.model_validate(
                _read_json(path, label="certification lifecycle receipt")
            )
        except (CertificationLifecycleError, ValueError) as read_exc:
            raise CertificationLifecycleError(
                "an unreadable lifecycle receipt appeared during completion"
            ) from read_exc
        if existing != receipt:
            raise CertificationLifecycleError(
                "a different lifecycle receipt appeared during completion"
            ) from exc
        return existing


def _recover_completed_lifecycle(
    *,
    release_dir: Path | str,
    task_id: str,
    certification_store: Path | str,
    manifest: release_mod.ReleaseManifest,
    id_dir: Path,
) -> tuple[
    certification_mod.CertificationAttestation,
    CertificationLifecycleReceipt,
]:
    """Finish or replay the sidecar transition after attestation publication."""

    attestation = _verified_completed_attestation(
        manifest=manifest,
        task_id=task_id,
        certification_store=certification_store,
        id_dir=id_dir,
    )
    lifecycle_path = id_dir / LIFECYCLE_FILENAME
    if os.path.lexists(lifecycle_path):
        receipt = verify_completed_lifecycle(
            release_dir=release_dir,
            task_id=task_id,
            certification_store=certification_store,
        )
        return attestation, receipt

    attempt_id = _attested_attempt_id(id_dir, attestation)
    lifecycle_dir = _attempt_lifecycle_dir(id_dir, attempt_id)
    intent = _load_completion_intent(
        lifecycle_dir / COMPLETION_INTENT_FILENAME
    )
    sandbox = verify_release_bound_sandbox(
        release_dir,
        task_id,
        lifecycle_dir / SANDBOX_ATTESTATION_FILENAME,
    )
    observations = _verify_observation_receipt(
        lifecycle_dir / OBSERVATIONS_FILENAME,
        manifest=manifest,
        task_id=task_id,
        attempt_id=attempt_id,
    )
    if observations.observed_versions != attestation.observed_versions:
        raise CertificationLifecycleError(
            "runtime observations contradict the final attestation"
        )
    try:
        cleanup = verify_cleanup_receipt(
            _read_json(lifecycle_dir / CLEANUP_FILENAME, label="cleanup receipt"),
            attempt_id=attempt_id,
            destination=manifest.destinations[task_id],
            require_complete=True,
            expected_grader_commands=(
                GRADER_COMMANDS_PER_POPULATION * len(attestation.populations)
            ),
        )
    except ModelCopyError as exc:
        raise CertificationLifecycleError(str(exc)) from exc
    _validate_completion_intent(
        intent,
        manifest=manifest,
        task_id=task_id,
        attempt_id=attempt_id,
        populations=attestation.populations,
        evidence_digests=attestation.evidence_digests,
        sandbox=sandbox,
        observations=observations,
        cleanup=cleanup,
        attestation=attestation,
    )
    sealed = _seal_lifecycle_receipt(
        attestation=attestation,
        attempt_id=attempt_id,
        sandbox=sandbox,
        observations=observations,
        cleanup=cleanup,
    )
    _publish_lifecycle_idempotently(lifecycle_path, sealed)
    verified = verify_completed_lifecycle(
        release_dir=release_dir,
        task_id=task_id,
        certification_store=certification_store,
    )
    return attestation, verified


def complete_runtime_certification(
    *,
    release_dir: Path | str,
    task_id: str,
    certification_store: Path | str,
) -> tuple[
    certification_mod.CertificationAttestation,
    CertificationLifecycleReceipt,
]:
    """Complete verified certification and recover interrupted publication.

    Recovery uses the attempt ID in sealed stage evidence and rechecks every
    lifecycle input.
    """

    try:
        manifest, pending_dir, attempt_id = _pending_context(
            release_dir=release_dir,
            task_id=task_id,
            certification_store=certification_store,
        )
    except CertificationLifecycleError as pending_error:
        # A successful low-level completion intentionally removes pending.json.
        # Resolve the immutable identity without that capability so a retry can
        # distinguish this recoverable state from an ordinary missing begin.
        _, manifest = _verified_manifest(release_dir, task_id)
        pending_dir = _certification_dir(certification_store, manifest, task_id)
        attestation_path = pending_dir / certification_mod.ATTESTATION_FILENAME
        if os.path.lexists(attestation_path):
            return _recover_completed_lifecycle(
                release_dir=release_dir,
                task_id=task_id,
                certification_store=certification_store,
                manifest=manifest,
                id_dir=pending_dir,
            )
        raise pending_error
    attestation_path = pending_dir / certification_mod.ATTESTATION_FILENAME
    if os.path.lexists(attestation_path):
        return _recover_completed_lifecycle(
            release_dir=release_dir,
            task_id=task_id,
            certification_store=certification_store,
            manifest=manifest,
            id_dir=pending_dir,
        )
    lifecycle_dir = _attempt_lifecycle_dir(pending_dir, attempt_id)
    sandbox = verify_release_bound_sandbox(
        release_dir,
        task_id,
        lifecycle_dir / SANDBOX_ATTESTATION_FILENAME,
    )
    observations = _verify_observation_receipt(
        lifecycle_dir / OBSERVATIONS_FILENAME,
        manifest=manifest,
        task_id=task_id,
        attempt_id=attempt_id,
    )
    evidence_pairs = _staged_evidence_pairs(
        pending_dir,
        manifest=manifest,
        task_id=task_id,
        attempt_id=attempt_id,
    )
    try:
        cleanup = verify_cleanup_receipt(
            _read_json(lifecycle_dir / CLEANUP_FILENAME, label="cleanup receipt"),
            attempt_id=attempt_id,
            destination=manifest.destinations[task_id],
            require_complete=True,
            expected_grader_commands=(
                GRADER_COMMANDS_PER_POPULATION * len(evidence_pairs)
            ),
        )
        intent = _prepare_completion_intent(
            lifecycle_dir / COMPLETION_INTENT_FILENAME,
            manifest=manifest,
            task_id=task_id,
            attempt_id=attempt_id,
            evidence_pairs=evidence_pairs,
            sandbox=sandbox,
            observations=observations,
            cleanup=cleanup,
        )
        try:
            attestation = certification_mod.complete_certification(
                pending_dir,
                manifest=manifest,
                task_id=task_id,
                evidence_pairs=evidence_pairs,
                observed_versions=observations.observed_versions,
            )
        except certification_mod.CertificationError:
            # An identical concurrent completion can win between lifecycle
            # verification and acquisition of the pending-capability lock.
            # Reuse it only through the same exact recovery verification.
            if os.path.lexists(attestation_path):
                return _recover_completed_lifecycle(
                    release_dir=release_dir,
                    task_id=task_id,
                    certification_store=certification_store,
                    manifest=manifest,
                    id_dir=pending_dir,
                )
            raise
    except (ModelCopyError, certification_mod.CertificationError) as exc:
        raise CertificationLifecycleError(str(exc)) from exc
    if observations.observed_versions != attestation.observed_versions:
        raise CertificationLifecycleError(
            "runtime observations contradict the final attestation"
        )
    _validate_completion_intent(
        intent,
        manifest=manifest,
        task_id=task_id,
        attempt_id=attempt_id,
        populations=attestation.populations,
        evidence_digests=attestation.evidence_digests,
        sandbox=sandbox,
        observations=observations,
        cleanup=cleanup,
        attestation=attestation,
    )
    sealed = _seal_lifecycle_receipt(
        attestation=attestation,
        attempt_id=attempt_id,
        sandbox=sandbox,
        observations=observations,
        cleanup=cleanup,
    )
    _publish_lifecycle_idempotently(
        pending_dir / LIFECYCLE_FILENAME,
        sealed,
    )
    verified = verify_completed_lifecycle(
        release_dir=release_dir,
        task_id=task_id,
        certification_store=certification_store,
    )
    return attestation, verified


def verify_completed_lifecycle(
    *,
    release_dir: Path | str,
    task_id: str,
    certification_store: Path | str,
) -> CertificationLifecycleReceipt:
    """Contextually verify the lifecycle sidecar and every object it binds."""

    _, manifest = _verified_manifest(release_dir, task_id)
    id_dir = _certification_dir(certification_store, manifest, task_id)
    status = certification_mod.certification_status(
        Path(certification_store), manifest.certification_ids[task_id]
    )
    if status.state is not certification_mod.CertificationState.CERTIFIED:
        raise CertificationLifecycleError(
            f"runtime certification is {status.state.value}: {status.reason}"
        )
    try:
        receipt = CertificationLifecycleReceipt.model_validate(
            _read_json(
                id_dir / LIFECYCLE_FILENAME,
                label="certification lifecycle receipt",
            )
        )
    except ValueError as exc:
        raise CertificationLifecycleError(
            f"certification lifecycle receipt is invalid: {exc}"
        ) from exc
    if not receipt.lifecycle_digest:
        raise CertificationLifecycleError(
            "certification lifecycle receipt is unsealed"
        )
    if receipt.lifecycle_digest != _lifecycle_digest(receipt):
        raise CertificationLifecycleError(
            "certification lifecycle receipt digest does not reproduce"
        )
    try:
        attestation = certification_mod.verify_attestation(
            id_dir / certification_mod.ATTESTATION_FILENAME
        )
    except certification_mod.CertificationError as exc:
        raise CertificationLifecycleError(str(exc)) from exc
    expected_identity = (
        manifest.certification_ids[task_id],
        manifest.release_id,
        manifest.runtime_bundle_ids[task_id],
        task_id,
        manifest.destinations[task_id],
    )
    actual_identity = (
        receipt.certification_id,
        receipt.release_id,
        receipt.runtime_bundle_id,
        receipt.task_id,
        receipt.destination,
    )
    if actual_identity != expected_identity:
        raise CertificationLifecycleError(
            "certification lifecycle receipt does not bind the requested release"
        )
    if (
        receipt.certification_attestation_digest != attestation.attestation_digest
        or receipt.evidence_digests != attestation.evidence_digests
        or receipt.populations != attestation.populations
        or receipt.completed_at != attestation.completed_at
    ):
        raise CertificationLifecycleError(
            "certification lifecycle receipt contradicts the final attestation"
        )
    if receipt.attempt_id != _attested_attempt_id(id_dir, attestation):
        raise CertificationLifecycleError(
            "certification lifecycle receipt names a different evidence attempt"
        )
    lifecycle_dir = _attempt_lifecycle_dir(id_dir, receipt.attempt_id)
    intent_path = lifecycle_dir / COMPLETION_INTENT_FILENAME
    # A final lifecycle seal created before completion intents were introduced
    # remains independently verifiable. Only recovery of a *missing* final
    # seal requires the durable pre-completion intent.
    intent = (
        _load_completion_intent(intent_path)
        if os.path.lexists(intent_path)
        else None
    )
    sandbox = verify_release_bound_sandbox(
        release_dir,
        task_id,
        lifecycle_dir / SANDBOX_ATTESTATION_FILENAME,
    )
    if receipt.sandbox_attestation_digest != sandbox.attestation_digest:
        raise CertificationLifecycleError(
            "certification lifecycle receipt contradicts its sandbox attestation"
        )
    observations = _verify_observation_receipt(
        lifecycle_dir / OBSERVATIONS_FILENAME,
        manifest=manifest,
        task_id=task_id,
        attempt_id=receipt.attempt_id,
    )
    if receipt.observation_digest != observations.observation_digest:
        raise CertificationLifecycleError(
            "certification lifecycle receipt contradicts its runtime observations"
        )
    if observations.observed_versions != attestation.observed_versions:
        raise CertificationLifecycleError(
            "runtime observations contradict the final attestation"
        )
    try:
        cleanup = verify_cleanup_receipt(
            _read_json(lifecycle_dir / CLEANUP_FILENAME, label="cleanup receipt"),
            attempt_id=receipt.attempt_id,
            destination=manifest.destinations[task_id],
            require_complete=True,
            expected_grader_commands=(
                GRADER_COMMANDS_PER_POPULATION * len(attestation.populations)
            ),
        )
    except ModelCopyError as exc:
        raise CertificationLifecycleError(str(exc)) from exc
    if receipt.cleanup_receipt_digest != cleanup.receipt_digest:
        raise CertificationLifecycleError(
            "certification lifecycle receipt contradicts its cleanup receipt"
        )
    if intent is not None:
        _validate_completion_intent(
            intent,
            manifest=manifest,
            task_id=task_id,
            attempt_id=receipt.attempt_id,
            populations=attestation.populations,
            evidence_digests=attestation.evidence_digests,
            sandbox=sandbox,
            observations=observations,
            cleanup=cleanup,
            attestation=attestation,
        )
    return receipt


def certification_lifecycle_status(
    *,
    release_dir: Path | str,
    task_id: str,
    certification_store: Path | str,
) -> dict[str, Any]:
    """Return a report-only state including the cleanup/lifecycle gate."""

    _, manifest = _verified_manifest(release_dir, task_id)
    certification_id = manifest.certification_ids[task_id]
    status = certification_mod.certification_status(
        Path(certification_store), certification_id
    )
    lifecycle_verified = False
    lifecycle_reason = ""
    if status.state is certification_mod.CertificationState.CERTIFIED:
        try:
            verify_completed_lifecycle(
                release_dir=release_dir,
                task_id=task_id,
                certification_store=certification_store,
            )
            lifecycle_verified = True
        except CertificationLifecycleError as exc:
            lifecycle_reason = str(exc)
    return {
        "certification_id": certification_id,
        "state": status.state.value,
        "populations": list(status.populations),
        "reason": status.reason,
        "lifecycle_verified": lifecycle_verified,
        "lifecycle_reason": lifecycle_reason,
    }


def load_run_spec(path: Path | str) -> tuple[CertificationRunSpec, Path]:
    spec_path = Path(path).resolve()
    try:
        spec = CertificationRunSpec.model_validate(
            _read_json(spec_path, label="certification run spec")
        )
    except ValueError as exc:
        raise CertificationLifecycleError(
            f"certification run spec is invalid: {exc}"
        ) from exc
    if not spec.populations:
        raise CertificationLifecycleError(
            "certification run spec has no population evidence"
        )
    names = [entry.population for entry in spec.populations]
    if len(names) != len(set(names)):
        raise CertificationLifecycleError(
            "certification run spec repeats a population"
        )
    return spec, spec_path.parent


def _spec_path(base: Path, raw: str) -> Path:
    path = Path(raw)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _build_and_publish_certified_difficulty(
    *,
    release_dir: Path | str,
    task_id: str,
    certification_store: Path | str,
    difficulty_out: Path | str,
    workspace: Path | str | None,
) -> Any:
    """Build the deterministic promotion and publish-or-confirm it."""

    from elt_taskgen.corpus.certified_difficulty import (
        CertifiedDifficultyError,
        RuntimeCertifiedDifficulty,
        build_runtime_certified_difficulty,
        verify_certified_difficulty,
    )

    try:
        report = build_runtime_certified_difficulty(
            release_dir=Path(release_dir),
            certification_store=Path(certification_store),
            task_id=task_id,
            workspace=Path(workspace).resolve() if workspace is not None else None,
        )
    except CertifiedDifficultyError as exc:
        raise CertificationLifecycleError(str(exc)) from exc
    target = Path(difficulty_out).resolve()
    try:
        _publish_json(target, report.model_dump(mode="json"))
        return report
    except (CertificationLifecycleError, OSError) as exc:
        if not os.path.lexists(target):
            raise
        try:
            existing = RuntimeCertifiedDifficulty.model_validate(
                _read_json(target, label="runtime-certified difficulty report")
            )
            existing = verify_certified_difficulty(existing)
        except (
            CertificationLifecycleError,
            CertifiedDifficultyError,
            ValueError,
        ) as read_exc:
            raise CertificationLifecycleError(
                "an unreadable runtime-certified difficulty report already exists"
            ) from read_exc
        if existing != report:
            raise CertificationLifecycleError(
                "a different runtime-certified difficulty report already exists"
            ) from exc
        return existing


def _finish_certification_promotion(
    *,
    release_dir: Path | str,
    task_id: str,
    certification_store: Path | str,
    difficulty_out: Path | str,
    workspace: Path | str | None,
    attestation: certification_mod.CertificationAttestation,
    lifecycle: CertificationLifecycleReceipt,
) -> tuple[
    certification_mod.CertificationAttestation,
    CertificationLifecycleReceipt,
    Any,
]:
    verified_lifecycle = verify_completed_lifecycle(
        release_dir=release_dir,
        task_id=task_id,
        certification_store=certification_store,
    )
    if verified_lifecycle != lifecycle:
        raise CertificationLifecycleError(
            "published certification lifecycle did not round-trip"
        )
    report = _build_and_publish_certified_difficulty(
        release_dir=release_dir,
        certification_store=certification_store,
        task_id=task_id,
        workspace=workspace,
        difficulty_out=difficulty_out,
    )
    return attestation, lifecycle, report


def finish_runtime_certification(
    *,
    release_dir: Path | str,
    task_id: str,
    certification_store: Path | str,
    run_spec: Path | str,
    difficulty_out: Path | str,
    workspace: Path | str | None = None,
) -> tuple[
    certification_mod.CertificationAttestation,
    CertificationLifecycleReceipt,
    Any,
]:
    """Validate evidence and cleanup, complete certification, and publish difficulty.

    Reject incomplete cleanup or invalid receipts. After attestation publication,
    the sealed completion intent authorizes recovery and the final report must
    verify exactly.
    """

    _, resolved_manifest = _verified_manifest(release_dir, task_id)
    resolved_id_dir = _certification_dir(
        certification_store, resolved_manifest, task_id
    )

    def resume_published_completion():
        attestation, lifecycle = complete_runtime_certification(
            release_dir=release_dir,
            task_id=task_id,
            certification_store=certification_store,
        )
        return _finish_certification_promotion(
            release_dir=release_dir,
            task_id=task_id,
            certification_store=certification_store,
            difficulty_out=difficulty_out,
            workspace=workspace,
            attestation=attestation,
            lifecycle=lifecycle,
        )

    if os.path.lexists(
        resolved_id_dir / certification_mod.ATTESTATION_FILENAME
    ):
        # The only mutation permitted without pending.json is the exact
        # journaled recovery in complete_runtime_certification. A historical
        # low-level attestation has no completion intent and is refused there.
        return resume_published_completion()
    spec, base = load_run_spec(run_spec)
    try:
        manifest, pending_dir, attempt_id = _pending_context(
            release_dir=release_dir,
            task_id=task_id,
            certification_store=certification_store,
        )
    except CertificationLifecycleError:
        # Close the check/read race with another completion process.
        if os.path.lexists(
            resolved_id_dir / certification_mod.ATTESTATION_FILENAME
        ):
            return resume_published_completion()
        raise
    supplied_sandbox = verify_release_bound_sandbox(
        release_dir, task_id, _spec_path(base, spec.sandbox_attestation)
    )
    staged_sandbox = _load_sandbox_attestation(
        _attempt_lifecycle_dir(pending_dir, attempt_id)
        / SANDBOX_ATTESTATION_FILENAME
    )
    if supplied_sandbox != staged_sandbox:
        raise CertificationLifecycleError(
            "run spec sandbox attestation differs from the one bound at begin"
        )
    for entry in spec.populations:
        record_stage1_evidence(
            release_dir=release_dir,
            task_id=task_id,
            certification_store=certification_store,
            population=entry.population,
            execution_receipt=_spec_path(base, entry.stage1_execution),
            evaluator_result=_spec_path(base, entry.stage1_result),
        )
        record_stage2_evidence(
            release_dir=release_dir,
            task_id=task_id,
            certification_store=certification_store,
            population=entry.population,
            execution_receipt=_spec_path(base, entry.stage2_execution),
            evaluator_result=_spec_path(base, entry.stage2_result),
        )
    record_runtime_observations(
        release_dir=release_dir,
        task_id=task_id,
        certification_store=certification_store,
        observed_versions=spec.observed_versions,
    )
    record_certification_cleanup(
        release_dir=release_dir,
        task_id=task_id,
        certification_store=certification_store,
        cleanup_receipt=_spec_path(base, spec.cleanup_receipt),
    )
    attestation, lifecycle = complete_runtime_certification(
        release_dir=release_dir,
        task_id=task_id,
        certification_store=certification_store,
    )
    return _finish_certification_promotion(
        release_dir=release_dir,
        task_id=task_id,
        certification_store=certification_store,
        difficulty_out=difficulty_out,
        workspace=workspace,
        attestation=attestation,
        lifecycle=lifecycle,
    )


__all__ = [
    "CLEANUP_FILENAME",
    "COMPLETION_INTENT_FILENAME",
    "CertificationCompletionIntent",
    "CertificationLifecycleError",
    "CertificationLifecycleReceipt",
    "CertificationRunSpec",
    "GRADER_COMMANDS_PER_POPULATION",
    "LIFECYCLE_DIRNAME",
    "LIFECYCLE_FILENAME",
    "OBSERVATIONS_FILENAME",
    "PopulationEvidenceFiles",
    "RuntimeObservationReceipt",
    "SANDBOX_ATTESTATION_FILENAME",
    "STAGED_EVIDENCE_DIRNAME",
    "begin_runtime_certification",
    "certification_lifecycle_status",
    "complete_runtime_certification",
    "finish_runtime_certification",
    "load_run_spec",
    "mint_unbound_sandbox_attestation",
    "mint_release_bound_sandbox_attestation",
    "publish_immutable_json",
    "record_certification_cleanup",
    "record_runtime_observations",
    "record_stage1_evidence",
    "record_stage2_evidence",
    "verify_completed_lifecycle",
    "verify_release_bound_sandbox",
]

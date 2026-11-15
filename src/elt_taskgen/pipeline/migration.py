"""Revalidate and migrate persisted run identity without rewriting historical evidence."""

from __future__ import annotations
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from elt_taskgen.generation.difficulty_profiles import DEFAULT_GENERATION_PROFILE
from elt_taskgen.engine import (
    EVIDENCE_SUPERSESSION_KEY,
    Engine,
    EngineError,
    ReportRevalidationEvidence,
    ReportSupersession,
    StageName,
    StagePayload,
    VERDICT_FAIL,
    VERDICT_FATAL,
    VERDICT_PASS,
    FINAL_REJECTED,
    validate_author_revalidation_records,
)
from elt_taskgen.models import (
    Origin,
    TaskIR,
    canonical_json,
    readable_json,
    sha256_hex,
    task_from_json,
)
from elt_taskgen.pipeline import generation as _pipeline_generation
from elt_taskgen.pipeline import review as _pipeline_review
from elt_taskgen.pipeline import support as _pipeline_support
from elt_taskgen.pipeline.support import open_engine as _open_engine


@dataclass(frozen=True)
class _ManifestAdapterTransition:
    """One task's exact adapter/source-entry movement between two manifests."""

    task_id: str
    origin: Origin
    authoritative_version: str
    reproduced_version: str
    authoritative_digest: str
    reproduced_digest: str
    authoritative_source_entry_sha256: str
    reproduced_source_entry_sha256: str


@dataclass(frozen=True)
class _PendingConfiguredMigration:
    prior_report: Any
    prior_report_bytes: bytes
    authoritative_selected: Any
    reproduced_selected: Any
    authoritative_pool_sha256: str
    reproduced_pool_sha256: str
    adapter_transitions: tuple[Any, ...] = ()
    authorization: Any | None = None
    authorization_path: Path | None = None
    committed_receipt: Any | None = None
    committed_receipt_path: Path | None = None


class _GeneratorRevalidationTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    intake_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    equivalence_path: str
    equivalence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resumed_existing: Literal[True] = True
    adapter_taskir_rederived: Literal[True] = True
    source_provenance_equivalence_attested: Literal[True] = True
    report_locally_revalidated_and_superseded: bool = False


class _GeneratorEquivalenceCommit(BaseModel):
    """All-roster commit point; no report is suppressed before this exists."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["generator-equivalence-commit-v1"] = (
        "generator-equivalence-commit-v1"
    )
    run_id: str
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_path: str
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordered_task_ids: tuple[str, ...]
    tasks: tuple[_GeneratorRevalidationTask, ...]

    @model_validator(mode="after")
    def _complete_roster(self) -> "_GeneratorEquivalenceCommit":
        if tuple(task.task_id for task in self.tasks) != self.ordered_task_ids:
            raise ValueError("generator equivalence commit roster is not exact")
        if not all(task.source_provenance_equivalence_attested for task in self.tasks):
            raise ValueError("generator equivalence commit has an unattested member")
        return self

    def deterministic_bytes(self) -> bytes:
        return (readable_json(self.model_dump(mode="json")) + "\n").encode("utf-8")

    def evidence_digest(self) -> str:
        return hashlib.sha256(self.deterministic_bytes()).hexdigest()


class _ImplementationEquivalenceCommit(_GeneratorEquivalenceCommit):
    """V2 commit for a generator replay with exact adapter-pin movements.

    The historical v1 model above is intentionally unchanged so its persisted
    deterministic bytes continue to parse and hash exactly as written.
    Adapter transitions live in the content-addressed v2 authorization and
    per-task provenance attestations bound by this commit.
    """

    schema_version: Literal["implementation-equivalence-commit-v2"] = (
        "implementation-equivalence-commit-v2"
    )


class _GeneratorRevalidationReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["configured-generator-revalidation-v1"] = (
        "configured-generator-revalidation-v1"
    )
    run_id: str
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_selected_path: str
    authoritative_pool_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_selected_path: str
    reproduced_pool_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_readiness_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordered_task_ids: tuple[str, ...]
    tasks: tuple[_GeneratorRevalidationTask, ...]
    revalidation_artifact_path: str
    revalidation_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_path: str
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    equivalence_commit_path: str
    equivalence_commit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    superseded_report_ids: tuple[int, ...]
    replacement_report_id: int = Field(default=0, ge=0)
    replacement_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resumed_existing: int
    adapter_taskirs_rederived: int
    source_provenance_equivalence_attested: int
    reports_locally_revalidated_and_superseded: int
    budget_snapshot_before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_snapshot_after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_committed_usd: float
    budget_reserved_usd: float
    budget_uncertain_usd: float

    @model_validator(mode="after")
    def _exact_roster(self) -> "_GeneratorRevalidationReceipt":
        if tuple(task.task_id for task in self.tasks) != self.ordered_task_ids:
            raise ValueError("generator revalidation task order differs from roster")
        if self.resumed_existing != len(self.tasks):
            raise ValueError("resumed_existing does not equal revalidated roster")
        if self.adapter_taskirs_rederived != sum(
            task.adapter_taskir_rederived for task in self.tasks
        ):
            raise ValueError("adapter rederivation count differs from task actions")
        if self.source_provenance_equivalence_attested != sum(
            task.source_provenance_equivalence_attested for task in self.tasks
        ):
            raise ValueError("provenance revalidation count differs from task actions")
        if self.reports_locally_revalidated_and_superseded != sum(
            task.report_locally_revalidated_and_superseded for task in self.tasks
        ):
            raise ValueError("report revalidation count differs from task actions")
        if self.budget_snapshot_before_sha256 != self.budget_snapshot_after_sha256:
            raise ValueError("generator migration changed the durable budget ledger")
        if self.reports_locally_revalidated_and_superseded:
            if (
                len(self.superseded_report_ids) != 1
                or self.replacement_report_id <= self.superseded_report_ids[0]
                or self.replacement_payload_sha256 == "0" * 64
            ):
                raise ValueError("report supersession receipt binding is incomplete")
        elif (
            self.superseded_report_ids
            or self.replacement_report_id
            or self.replacement_payload_sha256 != "0" * 64
        ):
            raise ValueError("receipt claims replacement evidence without revalidation")
        return self

    def deterministic_bytes(self) -> bytes:
        return (readable_json(self.model_dump(mode="json")) + "\n").encode("utf-8")

    def evidence_digest(self) -> str:
        return hashlib.sha256(self.deterministic_bytes()).hexdigest()


class _ImplementationRevalidationReceipt(_GeneratorRevalidationReceipt):
    """V2 configured-run receipt for implementation equivalence.

    Inheriting the established fields keeps report consumers uniform.  The
    distinct schema version prevents a v2 adapter transition from being
    guessed into the narrower historical generator-only contract.
    """

    schema_version: Literal["configured-implementation-revalidation-v2"] = (
        "configured-implementation-revalidation-v2"
    )


_EquivalenceCommit = _GeneratorEquivalenceCommit | _ImplementationEquivalenceCommit
_RevalidationReceipt = _GeneratorRevalidationReceipt | _ImplementationRevalidationReceipt


def _is_implementation_authorization(authorization: Any) -> bool:
    return (
        getattr(authorization, "schema_version", "")
        == "implementation-equivalence-authorization-v2"
    )


def _equivalence_evidence_filename(binding: Any, authorization: Any) -> str:
    suffix = (
        "implementation-equivalence.json"
        if _is_implementation_authorization(authorization)
        else "generator-equivalence.json"
    )
    return (
        f"{binding.intake_content_hash}."
        f"{binding.reproduced_provenance_sha256}.{suffix}"
    )


def _publish_immutable_run_document(
    destination: Path, payload: bytes, *, label: str
) -> Path:
    """Publish exact run evidence once, with no replace semantics."""

    destination = Path(destination).absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    current = destination.parent
    while True:
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise _pipeline_support.CliUsageError(f"{label} store has an unsafe path: {current}")
        if current == current.parent:
            break
        # The run evidence parents have just been checked; walking to the root
        # also prevents an ancestor swap from escaping the expected directory.
        current = current.parent
    try:
        existing = destination.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
            raise _pipeline_support.CliUsageError(f"{label} destination is unsafe: {destination}")
        if destination.read_bytes() != payload:
            raise _pipeline_support.CliUsageError(f"{label} destination contains different bytes")
        return destination
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.is_symlink() or destination.read_bytes() != payload:
                raise _pipeline_support.CliUsageError(f"concurrent {label} publisher wrote different bytes")
        os.chmod(destination, destination.stat().st_mode & 0o555)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _workspace_relative(workspace: Path, path: Path, *, label: str) -> str:
    try:
        return Path(path).absolute().relative_to(Path(workspace).absolute()).as_posix()
    except ValueError:
        raise _pipeline_support.CliUsageError(f"{label} is outside the configured workspace") from None


def _bound_workspace_path(workspace: Path, relative: str, *, label: str) -> Path:
    rel = PurePosixPath(relative)
    if (
        rel.is_absolute()
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in rel.parts)
    ):
        raise _pipeline_support.CliUsageError(f"{label} path is unsafe")
    return Path(workspace).absolute().joinpath(*rel.parts)


def _logical_budget_snapshot(*, workspace: Path, run_id: str, spec) -> dict[str, Any]:
    """Timestamp-free exact ledger state used to prove provider-free migration."""

    if spec.budget_total is None:
        return {"configured": False, "run_id": run_id, "reservations": []}
    from elt_taskgen.review.budget_ledger import initialize

    snapshot = initialize(
        run_id,
        float(spec.budget_total),
        workspace,
        per_task_limit_usd=float(spec.budget_per_task),
    ).snapshot()
    return {
        "configured": True,
        "run_id": snapshot.run_id,
        "total_limit_usd": snapshot.total_limit_usd,
        "per_task_limit_usd": snapshot.per_task_limit_usd,
        "committed_usd": snapshot.committed_usd,
        "reserved_usd": snapshot.reserved_usd,
        "uncertain_usd": snapshot.uncertain_usd,
        "reservation_count": snapshot.reservation_count,
        "committed_count": snapshot.committed_count,
        "reserved_count": snapshot.reserved_count,
        "uncertain_count": snapshot.uncertain_count,
        "released_count": snapshot.released_count,
        "reservations": [
            {
                "call_id": row.call_id,
                "task_id": row.task_id,
                "role": row.role,
                "estimated_usd": row.estimated_usd,
                "actual_usd": row.actual_usd,
                "state": row.state.value,
            }
            for row in sorted(snapshot.reservations, key=lambda item: item.call_id)
        ],
    }


def _prepare_generator_authorization(
    *,
    workspace: Path,
    run_id: str,
    spec,
    pool_path: Path,
    pending: _PendingConfiguredMigration,
    report_revalidation_request=None,
    report_revalidation_request_path: Path | None = None,
):
    """Rebuild the exact roster off-workspace, then seal its transition."""

    from elt_taskgen.ingest_manifest import (
        GeneratorEquivalenceAuthorization,
        GeneratorEquivalenceTaskBinding,
        ImplementationEquivalenceAuthorization,
        ImplementationEquivalenceTaskBinding,
        prepare_five_sources,
    )
    from elt_taskgen.provenance import (
        ImplementationAdapterTransition,
        generator_equivalence_problem,
        implementation_equivalence_problem,
        lineage_root_hash,
        load_current,
    )

    transition_problem, manifest_transitions = _selected_implementation_transition(
        pending.authoritative_selected, pending.reproduced_selected
    )
    if transition_problem:
        raise _pipeline_support.CliUsageError(
            "implementation revalidation refused: " + transition_problem
        )
    if manifest_transitions != pending.adapter_transitions:
        raise _pipeline_support.CliUsageError(
            "implementation transition changed after run-identity preflight"
        )
    implementation_v2 = bool(manifest_transitions)
    if implementation_v2 and (
        report_revalidation_request is not None
        or report_revalidation_request_path is not None
    ):
        raise _pipeline_support.CliUsageError(
            "report revalidation requests are not supported while adapter "
            "implementation pins move"
        )
    transition_by_task = {row.task_id: row for row in manifest_transitions}

    with tempfile.TemporaryDirectory(prefix="elt-taskgen-generator-revalidation-") as temp:
        prepared = prepare_five_sources(
            pending.reproduced_selected,
            manifest_path=pool_path,
            workspace=Path(temp) / "empty-workspace",
            retain_task_local_fatals=True,
            difficulty_profile=DEFAULT_GENERATION_PROFILE.name,
            recipe_selection=spec.generation.recipe_selection(spec.seed),
        )
    expected_ids = tuple(
        entry.expected_task_id
        for entry in pending.reproduced_selected.ordered_entries()
    )
    if tuple(item.task.task_id for item in prepared) != expected_ids:
        raise _pipeline_support.CliUsageError("implementation revalidation rebuilt a different task roster")

    engine = _open_engine(workspace, max_repair_rounds=0)
    bindings = []
    try:
        for item in prepared:
            current_task = engine.load_task(item.task.task_id)
            if lineage_root_hash(current_task) != item.task.content_hash():
                raise _pipeline_support.CliUsageError(
                    f"implementation revalidation changed intake TaskIR for "
                    f"{item.task.task_id!r}"
                )
            authoritative = load_current(
                engine.task_dir(item.task.task_id), task=current_task, required=True
            )
            manifest_transition = transition_by_task.get(item.task.task_id)
            adapter_transition = (
                ImplementationAdapterTransition(
                    pool=manifest_transition.origin,
                    adapter_name=authoritative.source.adapter_name,
                    authoritative_version=(
                        manifest_transition.authoritative_version
                    ),
                    reproduced_version=manifest_transition.reproduced_version,
                    authoritative_digest=(
                        manifest_transition.authoritative_digest
                    ),
                    reproduced_digest=manifest_transition.reproduced_digest,
                    authoritative_source_entry_sha256=(
                        manifest_transition.authoritative_source_entry_sha256
                    ),
                    reproduced_source_entry_sha256=(
                        manifest_transition.reproduced_source_entry_sha256
                    ),
                )
                if manifest_transition is not None
                else None
            )
            problem = (
                implementation_equivalence_problem(
                    authoritative,
                    item.provenance,
                    adapter_transition=adapter_transition,
                )
                if implementation_v2
                else generator_equivalence_problem(authoritative, item.provenance)
            )
            if problem:
                raise _pipeline_support.CliUsageError(
                    f"implementation revalidation changed "
                    f"{item.task.task_id!r}: {problem}"
                )
            if implementation_v2:
                old_entry_digest = authoritative.source.selection_inputs[
                    "ingest_manifest"
                ].digest
                bindings.append(
                    ImplementationEquivalenceTaskBinding(
                        task_id=item.task.task_id,
                        origin=item.origin,
                        authoritative_source_entry_sha256=old_entry_digest,
                        reproduced_source_entry_sha256=item.source_entry_sha256,
                        intake_content_hash=item.task.content_hash(),
                        authoritative_provenance_sha256=(
                            authoritative.evidence_digest()
                        ),
                        reproduced_provenance_sha256=(
                            item.provenance.evidence_digest()
                        ),
                        adapter_transition=adapter_transition,
                    )
                )
            else:
                bindings.append(
                    GeneratorEquivalenceTaskBinding(
                        task_id=item.task.task_id,
                        source_entry_sha256=item.source_entry_sha256,
                        intake_content_hash=item.task.content_hash(),
                        authoritative_provenance_sha256=(
                            authoritative.evidence_digest()
                        ),
                        reproduced_provenance_sha256=(
                            item.provenance.evidence_digest()
                        ),
                    )
                )
    finally:
        engine.close()

    budget_snapshot = _logical_budget_snapshot(
        workspace=workspace, run_id=run_id, spec=spec
    )
    authorization_type = (
        ImplementationEquivalenceAuthorization
        if implementation_v2
        else GeneratorEquivalenceAuthorization
    )
    authorization_fields: dict[str, Any] = dict(
        run_id=run_id,
        config_sha256=spec.fingerprint(),
        prior_readiness_sha256=hashlib.sha256(
            pending.prior_report_bytes
        ).hexdigest(),
        authoritative_selected_sha256=(
            pending.authoritative_selected.manifest_sha256()
        ),
        reproduced_selected_sha256=pending.reproduced_selected.manifest_sha256(),
        authoritative_pool_sha256=pending.authoritative_pool_sha256,
        reproduced_pool_sha256=pending.reproduced_pool_sha256,
        authoritative_generator_sha256=pending.authoritative_selected.generator.sha256,
        reproduced_generator_sha256=pending.reproduced_selected.generator.sha256,
        ordered_task_ids=expected_ids,
        tasks=tuple(bindings),
        budget_snapshot_sha256=sha256_hex(canonical_json(budget_snapshot)),
        budget_snapshot=budget_snapshot,
    )
    if not implementation_v2:
        authorization_fields.update(
            report_revalidation_request_path=(
                _workspace_relative(
                    workspace,
                    report_revalidation_request_path,
                    label="report revalidation request",
                )
                if report_revalidation_request_path is not None
                else ""
            ),
            report_revalidation_request_sha256=(
                report_revalidation_request.evidence_digest()
                if report_revalidation_request is not None
                else ""
            ),
            report_revalidation_request=report_revalidation_request,
        )
    authorization = authorization_type(**authorization_fields)
    destination = (
        workspace
        / "state"
        / "pipeline_runs"
        / run_id
        / "identity_migrations"
        / "authorizations"
        / f"{authorization.evidence_digest()}.json"
    )
    path = _publish_immutable_run_document(
        destination,
        authorization.deterministic_bytes(),
        label="implementation equivalence authorization",
    )
    return authorization, path


def _load_report_revalidation_request(
    path: Path | None,
    *,
    workspace: Path,
    run_id: str,
    spec,
    pending: _PendingConfiguredMigration,
):
    if path is None:
        return None, None
    from elt_taskgen.ingest_manifest import GeneratorReportRevalidationRequest
    from elt_taskgen.pipeline_readiness import _read_bounded_regular_bytes

    source = Path(path).absolute()
    expected_root = (
        workspace
        / "state"
        / "pipeline_runs"
        / run_id
        / "identity_migrations"
        / "requests"
    )
    try:
        source.relative_to(expected_root)
    except ValueError:
        raise _pipeline_support.CliUsageError(
            "report revalidation request must be in this run's canonical request store"
        ) from None
    try:
        payload = _read_bounded_regular_bytes(
            source,
            label="report revalidation request",
            max_bytes=16 * 1024 * 1024,
        )
        request = GeneratorReportRevalidationRequest.model_validate_json(payload)
    except ValueError as exc:
        raise _pipeline_support.CliUsageError(f"report revalidation request is invalid: {exc}") from None
    if payload != request.deterministic_bytes() or source.name != f"{request.evidence_digest()}.json":
        raise _pipeline_support.CliUsageError("report revalidation request is not content-addressed")
    expected = {
        "run_id": run_id,
        "config_sha256": spec.fingerprint(),
        "authoritative_selected_sha256": pending.authoritative_selected.manifest_sha256(),
        "reproduced_selected_sha256": pending.reproduced_selected.manifest_sha256(),
        "authoritative_generator_sha256": pending.authoritative_selected.generator.sha256,
        "reproduced_generator_sha256": pending.reproduced_selected.generator.sha256,
    }
    observed = request.model_dump(mode="json")
    mismatches = [key for key, value in expected.items() if observed[key] != value]
    if mismatches:
        raise _pipeline_support.CliUsageError(
            "report revalidation request differs from this migration: "
            + ", ".join(mismatches)
        )
    return request, source


def _iter_json_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_json_strings(item)


def _regular_json_bytes(path: Path, *, label: str, max_bytes: int = 32 * 1024 * 1024):
    from elt_taskgen.pipeline_readiness import _read_bounded_regular_bytes

    payload = _read_bounded_regular_bytes(path, label=label, max_bytes=max_bytes)
    try:
        return payload, json.loads(payload)
    except (UnicodeError, ValueError) as exc:
        raise _pipeline_support.CliUsageError(f"{label} is not valid JSON: {exc}") from None


def _build_defect_revalidation(
    *,
    workspace: Path,
    run_id: str,
    spec,
    pending: _PendingConfiguredMigration,
    authorization,
    authorization_path: Path,
    equivalence_commit: _GeneratorEquivalenceCommit,
    equivalence_commit_path: Path,
) -> tuple[ReportRevalidationEvidence | None, Path | None, ReportSupersession | None]:
    """Find and locally revalidate the exact obsolete author false positive."""

    from elt_taskgen.review import declarative_prose, prose_fidelity

    request = authorization.report_revalidation_request
    if request is None:
        return None, None, None
    request_path = _bound_workspace_path(
        workspace,
        authorization.report_revalidation_request_path,
        label="report revalidation request",
    )
    request_bytes, _ = _regular_json_bytes(
        request_path, label="report revalidation request"
    )
    if (
        hashlib.sha256(request_bytes).hexdigest()
        != authorization.report_revalidation_request_sha256
        or request_bytes != request.deterministic_bytes()
    ):
        raise _pipeline_support.CliUsageError("authorized report revalidation request changed")
    if request.task_id not in authorization.ordered_task_ids:
        raise _pipeline_support.CliUsageError("report revalidation target is outside the exact roster")

    engine = _open_engine(workspace, max_repair_rounds=0)
    candidates = []
    try:
        for task_id in (request.task_id,):
            task = engine.load_task(task_id)
            fatal = next(
                (
                    row
                    for row in engine.report_history(task_id, StageName.AUTHOR.value)
                    if row.verdict == VERDICT_FATAL
                    and row.content_hash == task.content_hash()
                ),
                None,
            )
            if fatal is None:
                continue
            cause = next(
                (
                    row
                    for row in engine.report_history(task_id, StageName.AUTHOR.value)
                    if row.verdict == VERDICT_FAIL
                    and row.content_hash == task.content_hash()
                    and row.id < fatal.id
                ),
                None,
            )
            if cause is None:
                continue
            try:
                cause_payload = json.loads(cause.payload_json)
                cause_data = cause_payload.get("data") or {}
            except (TypeError, ValueError):
                continue
            error = str(cause_payload.get("error") or "")
            if not (
                cause_data.get("gate") == prose_fidelity.GATE_NAME
                and cause_data.get(_pipeline_review.AUTHOR_SOURCE_KEY) == _pipeline_review.AUTHOR_SOURCE_REVISED
                and "function-call" in error
                and "count" in error.casefold()
            ):
                continue
            candidates.append((task, cause, fatal, cause_data))
        if len(candidates) != 1:
            raise _pipeline_support.CliUsageError("requested obsolete author fatal did not match exactly")
        task, cause, fatal, cause_data = candidates[0]
        prompt = task.solver_prompt or ""
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if not prompt or cause_data.get(_pipeline_review.AUTHOR_PROSE_SHA_KEY) != prompt_sha:
            raise _pipeline_support.CliUsageError("obsolete author report is not bound to stored prose")
        if prompt_sha != request.prose_sha256:
            raise _pipeline_support.CliUsageError("report revalidation request names other prose")
        structural = _pipeline_review._structural_failure_detail(task)
        attack = _pipeline_generation._attack_matrix_failure_detail(engine, task)
        findings = tuple(prose_fidelity.check_prose_fidelity(task))
        if structural is not None or attack is not None or findings:
            raise _pipeline_support.CliUsageError(
                "fixed author validator did not reproduce a fully green local result"
            )

        session_sha = str(cause_data.get(_pipeline_review.AUTHOR_SESSION_SHA_KEY) or "")
        if not re.fullmatch(r"[0-9a-f]{64}", session_sha):
            raise _pipeline_support.CliUsageError("obsolete author report has no valid session digest")
        if session_sha != request.author_session_sha256:
            raise _pipeline_support.CliUsageError("report revalidation request names another session")
        if (
            error != request.expected_old_error
            or hashlib.sha256(error.encode("utf-8")).hexdigest()
            != request.expected_old_error_sha256
        ):
            raise _pipeline_support.CliUsageError("obsolete validator findings differ from request")
        session_path = _bound_workspace_path(
            workspace,
            request.session_summary_path,
            label="author session summary",
        )
        session_bytes, session_document = _regular_json_bytes(
            session_path, label="author session summary"
        )
        if hashlib.sha256(session_bytes).hexdigest() != request.session_summary_sha256:
            raise _pipeline_support.CliUsageError("author session summary differs from request")

        transcript_records = []
        for relative, expected_sha in zip(
            request.transcript_paths, request.transcript_sha256s, strict=True
        ):
            transcript_path = _bound_workspace_path(
                workspace, relative, label="semantic-author transcript"
            )
            transcript_bytes, transcript_document = _regular_json_bytes(
                transcript_path, label="semantic-author transcript"
            )
            if hashlib.sha256(transcript_bytes).hexdigest() != expected_sha:
                raise _pipeline_support.CliUsageError("semantic-author transcript differs from request")
            transcript_records.append((transcript_path, transcript_bytes, transcript_document))
        transcript_path, transcript_bytes, transcript_document = transcript_records[-1]
        session_index_path = _bound_workspace_path(
            workspace, request.session_index_path, label="author session index"
        )
        session_index_bytes, session_index = _regular_json_bytes(
            session_index_path, label="author session index"
        )
        if hashlib.sha256(session_index_bytes).hexdigest() != request.session_index_sha256:
            raise _pipeline_support.CliUsageError("author session index differs from request")
        trajectory_path = _bound_workspace_path(
            workspace, request.trajectory_path, label="author trajectory"
        )
        trajectory_bytes, trajectory = _regular_json_bytes(
            trajectory_path, label="author trajectory"
        )
        if hashlib.sha256(trajectory_bytes).hexdigest() != request.trajectory_sha256:
            raise _pipeline_support.CliUsageError("author trajectory differs from request")
        try:
            record_bindings = validate_author_revalidation_records(
                task_id=task.task_id,
                intake_content_hash=authorization.binding_for(
                    task.task_id
                ).intake_content_hash,
                prompt=prompt,
                session_sha256=session_sha,
                transcript_records=tuple(
                    (
                        _workspace_relative(
                            workspace, path, label="author transcript"
                        ),
                        document,
                    )
                    for path, _payload, document in transcript_records
                ),
                session_index_path=request.session_index_path,
                session_index=session_index,
                trajectory_path=request.trajectory_path,
                trajectory=trajectory,
                session_summary=session_document,
            )
        except (TypeError, ValueError) as exc:
            raise _pipeline_support.CliUsageError(str(exc)) from None
        if (
            session_document.get("drafts")
            != int(cause_data.get(_pipeline_review.AUTHOR_DRAFTS_KEY) or 0)
            or session_document.get("revisions")
            != int(cause_data.get(_pipeline_review.AUTHOR_REVISIONS_KEY) or 0)
            or session_document.get("terminal")
            != str(cause_data.get(_pipeline_review.AUTHOR_SESSION_TERMINAL_KEY) or "")
        ):
            raise _pipeline_support.CliUsageError("author session summary counters differ from report")

        cause_sha = hashlib.sha256(cause.payload_json.encode("utf-8")).hexdigest()
        fatal_sha = hashlib.sha256(fatal.payload_json.encode("utf-8")).hexdigest()
        if (
            task.content_hash() != request.task_content_hash
            or cause.id != request.cause_report_id
            or cause_sha != request.cause_payload_sha256
            or fatal.id != request.target_report_id
            or fatal_sha != request.target_payload_sha256
        ):
            raise _pipeline_support.CliUsageError("requested author report identity or payload changed")
        authorization_sha = authorization.evidence_digest()
        reason = request.reason
        migration_id = sha256_hex(
            canonical_json(
                {
                    "authorization": authorization_sha,
                    "task_id": task.task_id,
                    "content_hash": task.content_hash(),
                    "cause_report_id": cause.id,
                    "cause_payload_sha256": cause_sha,
                    "target_report_id": fatal.id,
                    "target_payload_sha256": fatal_sha,
                    "reason": reason,
                }
            )
        )
        replacement_detail = (
            "persisted authored prose revalidated locally under the corrected "
            "declarative-prose validator; fidelity gate green"
        )
        replacement_data = {
            key: str(value)
            for key, value in cause_data.items()
            if isinstance(value, (str, int, float, bool))
        }
        replacement_data.update(
            {
                _pipeline_review.AUTHOR_PROSE_SHA_KEY: prompt_sha,
                _pipeline_review.AUTHOR_SOURCE_KEY: str(cause_data[_pipeline_review.AUTHOR_SOURCE_KEY]),
                _pipeline_review.AUTHOR_SESSION_SHA_KEY: session_sha,
                "transcript_path": _workspace_relative(
                    workspace, transcript_path, label="author transcript"
                ),
                "transcript_sha256": hashlib.sha256(transcript_bytes).hexdigest(),
                "transcript_manifest": canonical_json(
                    [
                        {
                            "path": _workspace_relative(
                                workspace, path, label="author transcript"
                            ),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                        for path, payload, _ in transcript_records
                    ]
                ),
                "session_summary_path": _workspace_relative(
                    workspace, session_path, label="author session summary"
                ),
                "session_summary_sha256": hashlib.sha256(session_bytes).hexdigest(),
                "session_index_path": request.session_index_path,
                "session_index_sha256": request.session_index_sha256,
                "trajectory_path": request.trajectory_path,
                "trajectory_sha256": request.trajectory_sha256,
                "provider": record_bindings["provider"],
                "model": record_bindings["model"],
                "served_models": record_bindings["served_models"],
                "route": record_bindings["route"],
                "admission": record_bindings["admission"],
                "behavior_sha256": record_bindings["behavior_sha256"],
                "tools_sha256": record_bindings["tools_sha256"],
                "policy_sha256": record_bindings["policy_sha256"],
                "revalidation_reason": reason,
            }
        )
        authorization_rel = _workspace_relative(
            workspace, authorization_path, label="generator authorization"
        )
        validator_identity = {
            "generator_sha256": pending.reproduced_selected.generator.sha256,
            "prose_fidelity_module_sha256": hashlib.sha256(
                Path(prose_fidelity.__file__).read_bytes()
            ).hexdigest(),
            "declarative_prose_module_sha256": hashlib.sha256(
                Path(declarative_prose.__file__).read_bytes()
            ).hexdigest(),
        }
        original_evidence = {
            "prose_sha256": prompt_sha,
            "session_sha256": session_sha,
            "source": str(cause_data[_pipeline_review.AUTHOR_SOURCE_KEY]),
            "drafts": str(cause_data.get(_pipeline_review.AUTHOR_DRAFTS_KEY) or ""),
            "revisions": str(cause_data.get(_pipeline_review.AUTHOR_REVISIONS_KEY) or ""),
            "session_terminal": str(
                cause_data.get(_pipeline_review.AUTHOR_SESSION_TERMINAL_KEY) or ""
            ),
            "transcript_path": replacement_data["transcript_path"],
            "transcript_sha256": replacement_data["transcript_sha256"],
            "session_summary_path": replacement_data["session_summary_path"],
            "session_summary_sha256": replacement_data["session_summary_sha256"],
            "transcript_document_sha256": sha256_hex(
                canonical_json(transcript_document)
            ),
            "transcript_manifest": replacement_data["transcript_manifest"],
            "session_index_path": request.session_index_path,
            "session_index_sha256": request.session_index_sha256,
            "trajectory_path": request.trajectory_path,
            "trajectory_sha256": request.trajectory_sha256,
            "request_path": authorization.report_revalidation_request_path,
            "request_sha256": authorization.report_revalidation_request_sha256,
            "old_error": error,
            "old_error_sha256": request.expected_old_error_sha256,
        }
        task_ir_bytes = (engine.task_dir(task.task_id) / "task_ir.json").read_bytes()
        task_ir_sha = hashlib.sha256(task_ir_bytes).hexdigest()
        archived_task_ir = _publish_immutable_run_document(
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "identity_migrations"
            / "task_ir_snapshots"
            / f"{task_ir_sha}.json",
            task_ir_bytes,
            label="pre-revalidation TaskIR snapshot",
        )
        artifact = ReportRevalidationEvidence(
            migration_id=migration_id,
            authorization_path=authorization_rel,
            authorization_sha256=authorization_sha,
            equivalence_commit_path=_workspace_relative(
                workspace,
                equivalence_commit_path,
                label="generator equivalence commit",
            ),
            equivalence_commit_sha256=equivalence_commit.evidence_digest(),
            run_id=run_id,
            config_sha256=spec.fingerprint(),
            authoritative_selected_sha256=authorization.authoritative_selected_sha256,
            reproduced_selected_sha256=authorization.reproduced_selected_sha256,
            authoritative_generator_sha256=authorization.authoritative_generator_sha256,
            reproduced_generator_sha256=authorization.reproduced_generator_sha256,
            task_id=task.task_id,
            intake_content_hash=authorization.binding_for(task.task_id).intake_content_hash,
            task_content_hash=task.content_hash(),
            archived_task_ir_path=_workspace_relative(
                workspace, archived_task_ir, label="pre-revalidation TaskIR snapshot"
            ),
            archived_task_ir_sha256=task_ir_sha,
            stage=StageName.AUTHOR.value,
            cause_report_id=cause.id,
            cause_payload_sha256=cause_sha,
            target_report_id=fatal.id,
            target_payload_sha256=fatal_sha,
            validator_identity=validator_identity,
            original_evidence=original_evidence,
            deterministic_preconditions=(
                "structural-completeness-green",
                "required-attack-matrix-green",
                "prose-fidelity-zero-findings",
                "persisted-author-session-bound",
                "persisted-author-transcript-exact-prompt-bound",
            ),
            findings=findings,
            replacement_detail=replacement_detail,
            replacement_data=replacement_data,
        )
        artifact_sha = hashlib.sha256(artifact.deterministic_bytes()).hexdigest()
        artifact_path = _publish_immutable_run_document(
            workspace
            / "state"
            / "pipeline_runs"
            / run_id
            / "identity_migrations"
            / "revalidations"
            / f"{artifact_sha}.json",
            artifact.deterministic_bytes(),
            label="local report revalidation",
        )
        supersession = ReportSupersession(
            migration_id=migration_id,
            reason=reason,
            stage=StageName.AUTHOR.value,
            content_hash=task.content_hash(),
            revalidation_path=_workspace_relative(
                workspace, artifact_path, label="local report revalidation"
            ),
            revalidation_sha256=artifact_sha,
            cause_report_id=cause.id,
            cause_payload_sha256=cause_sha,
            target_report_id=fatal.id,
            target_payload_sha256=fatal_sha,
            replacement_detail=replacement_detail,
            replacement_data=tuple(sorted(replacement_data.items())),
        )
        return artifact, artifact_path, supersession
    finally:
        engine.close()


def _commit_generator_equivalences(
    *, workspace: Path, run_id: str, authorization, authorization_path: Path
) -> tuple[_EquivalenceCommit, Path]:
    """Verify every roster attestation and publish the all-or-nothing commit."""

    from elt_taskgen.provenance import (
        GeneratorEquivalenceAttestation,
        ImplementationEquivalenceAttestation,
        load_current,
    )

    implementation_v2 = _is_implementation_authorization(authorization)
    attestation_type = (
        ImplementationEquivalenceAttestation
        if implementation_v2
        else GeneratorEquivalenceAttestation
    )
    engine = _open_engine(workspace, max_repair_rounds=0)
    rows = []
    try:
        for binding in authorization.tasks:
            path = (
                workspace
                / "tasks"
                / binding.task_id
                / "ingest_provenance"
                / _equivalence_evidence_filename(binding, authorization)
            )
            payload, _document = _regular_json_bytes(
                path, label="implementation provenance equivalence"
            )
            try:
                attestation = attestation_type.model_validate_json(payload)
            except ValueError as exc:
                raise _pipeline_support.CliUsageError(
                    f"implementation provenance equivalence is invalid: {exc}"
                ) from None
            current_task = engine.load_task(binding.task_id)
            authoritative = load_current(
                engine.task_dir(binding.task_id),
                task=current_task,
                required=True,
            )
            if (
                payload != attestation.deterministic_bytes()
                or attestation.task_id != binding.task_id
                or attestation.task_content_hash != binding.intake_content_hash
                or attestation.authoritative_evidence_digest
                != binding.authoritative_provenance_sha256
                or authoritative.evidence_digest()
                != binding.authoritative_provenance_sha256
                or attestation.reproduced_provenance.evidence_digest()
                != binding.reproduced_provenance_sha256
                or attestation.reproduced_provenance.source.selection_inputs[
                    "generator_code"
                ].digest
                != authorization.reproduced_generator_sha256
                or (
                    implementation_v2
                    and attestation.adapter_transition
                    != binding.adapter_transition
                )
            ):
                raise _pipeline_support.CliUsageError(
                    "implementation provenance equivalence differs from its exact "
                    f"authorization for {binding.task_id!r}"
                )
            rows.append(
                _GeneratorRevalidationTask(
                    task_id=binding.task_id,
                    intake_content_hash=binding.intake_content_hash,
                    authoritative_provenance_sha256=(
                        binding.authoritative_provenance_sha256
                    ),
                    reproduced_provenance_sha256=(
                        binding.reproduced_provenance_sha256
                    ),
                    equivalence_path=_workspace_relative(
                        workspace,
                        path,
                        label="implementation provenance equivalence",
                    ),
                    equivalence_sha256=hashlib.sha256(payload).hexdigest(),
                ),
            )
    finally:
        engine.close()
    commit_type = (
        _ImplementationEquivalenceCommit
        if implementation_v2
        else _GeneratorEquivalenceCommit
    )
    commit = commit_type(
        run_id=run_id,
        config_sha256=authorization.config_sha256,
        authoritative_selected_sha256=authorization.authoritative_selected_sha256,
        reproduced_selected_sha256=authorization.reproduced_selected_sha256,
        authoritative_generator_sha256=authorization.authoritative_generator_sha256,
        reproduced_generator_sha256=authorization.reproduced_generator_sha256,
        authorization_path=_workspace_relative(
            workspace, authorization_path, label="implementation authorization"
        ),
        authorization_sha256=authorization.evidence_digest(),
        ordered_task_ids=authorization.ordered_task_ids,
        tasks=tuple(rows),
    )
    path = _publish_immutable_run_document(
        workspace
        / "state"
        / "pipeline_runs"
        / run_id
        / "identity_migrations"
        / "equivalence_commits"
        / f"{commit.evidence_digest()}.json",
        commit.deterministic_bytes(),
        label="implementation equivalence commit",
    )
    return commit, path


def _finalize_generator_migration(
    *,
    workspace: Path,
    run_id: str,
    spec,
    pending: _PendingConfiguredMigration,
    authorization,
    authorization_path: Path,
    revalidation_artifact: ReportRevalidationEvidence | None,
    revalidation_artifact_path: Path | None,
    supersession: ReportSupersession | None,
    equivalence_commit: _EquivalenceCommit,
    equivalence_commit_path: Path,
) -> tuple[_RevalidationReceipt, Path]:
    """Confirm all per-task attestations, append one supersession, seal receipt."""

    task_rows = []
    engine = _open_engine(workspace, max_repair_rounds=0)
    try:
        for binding in authorization.tasks:
            path = (
                engine.task_dir(binding.task_id)
                / "ingest_provenance"
                / _equivalence_evidence_filename(binding, authorization)
            )
            payload, _document = _regular_json_bytes(
                path, label="implementation provenance equivalence"
            )
            task_rows.append(
                _GeneratorRevalidationTask(
                    task_id=binding.task_id,
                    intake_content_hash=binding.intake_content_hash,
                    authoritative_provenance_sha256=(
                        binding.authoritative_provenance_sha256
                    ),
                    reproduced_provenance_sha256=(
                        binding.reproduced_provenance_sha256
                    ),
                    equivalence_path=_workspace_relative(
                        workspace,
                        path,
                        label="implementation provenance equivalence",
                    ),
                    equivalence_sha256=hashlib.sha256(payload).hexdigest(),
                    report_locally_revalidated_and_superseded=(
                        supersession is not None
                        and binding.task_id
                        == (revalidation_artifact.task_id if revalidation_artifact else "")
                    ),
                )
            )
        superseded_ids: tuple[int, ...] = ()
        replacement_report_id = 0
        replacement_payload_sha256 = "0" * 64
        if supersession is not None:
            task = engine.load_task(
                revalidation_artifact.task_id if revalidation_artifact else ""
            )
            before = (engine.task_dir(task.task_id) / "task_ir.json").read_bytes()
            engine.supersede_report(task, supersession)
            after = (engine.task_dir(task.task_id) / "task_ir.json").read_bytes()
            if after != before:
                raise _pipeline_support.CliUsageError("report supersession changed TaskIR bytes")
            if engine.final_verdict(task.task_id) == FINAL_REJECTED:
                raise _pipeline_support.CliUsageError("locally revalidated fatal remains active")
            replacement = engine._matching_supersession(task, supersession)
            if replacement is None:
                raise _pipeline_support.CliUsageError("locally revalidated replacement PASS is missing")
            replacement_report_id = replacement.id
            replacement_payload_sha256 = hashlib.sha256(
                replacement.payload_json.encode("utf-8")
            ).hexdigest()
            superseded_ids = (supersession.target_report_id,)
    finally:
        engine.close()

    authoritative_selected_path = (
        workspace
        / "state"
        / "candidate_manifests"
        / f"{authorization.authoritative_selected_sha256}.json"
    )
    reproduced_selected_path = (
        workspace
        / "state"
        / "candidate_manifests"
        / f"{authorization.reproduced_selected_sha256}.json"
    )
    artifact_rel = (
        _workspace_relative(
            workspace, revalidation_artifact_path, label="local report revalidation"
        )
        if revalidation_artifact_path is not None
        else ""
    )
    artifact_sha = (
        hashlib.sha256(revalidation_artifact.deterministic_bytes()).hexdigest()
        if revalidation_artifact is not None
        else "0" * 64
    )
    budget_after = _logical_budget_snapshot(
        workspace=workspace, run_id=run_id, spec=spec
    )
    budget_after_sha = sha256_hex(canonical_json(budget_after))
    if budget_after_sha != authorization.budget_snapshot_sha256:
        raise _pipeline_support.CliUsageError(
            "durable provider budget changed during implementation revalidation"
        )
    receipt_type = (
        _ImplementationRevalidationReceipt
        if _is_implementation_authorization(authorization)
        else _GeneratorRevalidationReceipt
    )
    receipt = receipt_type(
        run_id=run_id,
        config_sha256=authorization.config_sha256,
        authoritative_selected_sha256=authorization.authoritative_selected_sha256,
        authoritative_selected_path=str(authoritative_selected_path),
        authoritative_pool_sha256=authorization.authoritative_pool_sha256,
        authoritative_generator_sha256=authorization.authoritative_generator_sha256,
        reproduced_selected_sha256=authorization.reproduced_selected_sha256,
        reproduced_selected_path=str(reproduced_selected_path),
        reproduced_pool_sha256=authorization.reproduced_pool_sha256,
        reproduced_generator_sha256=authorization.reproduced_generator_sha256,
        prior_readiness_sha256=authorization.prior_readiness_sha256,
        ordered_task_ids=authorization.ordered_task_ids,
        tasks=tuple(task_rows),
        revalidation_artifact_path=artifact_rel,
        revalidation_artifact_sha256=artifact_sha,
        authorization_path=_workspace_relative(
            workspace, authorization_path, label="implementation authorization"
        ),
        authorization_sha256=authorization.evidence_digest(),
        equivalence_commit_path=_workspace_relative(
            workspace,
            equivalence_commit_path,
            label="implementation equivalence commit",
        ),
        equivalence_commit_sha256=equivalence_commit.evidence_digest(),
        superseded_report_ids=superseded_ids,
        replacement_report_id=replacement_report_id,
        replacement_payload_sha256=replacement_payload_sha256,
        resumed_existing=len(task_rows),
        adapter_taskirs_rederived=len(task_rows),
        source_provenance_equivalence_attested=len(task_rows),
        reports_locally_revalidated_and_superseded=int(supersession is not None),
        budget_snapshot_before_sha256=authorization.budget_snapshot_sha256,
        budget_snapshot_after_sha256=budget_after_sha,
        budget_committed_usd=float(budget_after.get("committed_usd", 0.0)),
        budget_reserved_usd=float(budget_after.get("reserved_usd", 0.0)),
        budget_uncertain_usd=float(budget_after.get("uncertain_usd", 0.0)),
    )
    history_path = (
        workspace
        / "state"
        / "pipeline_runs"
        / run_id
        / "readiness_history"
        / f"{authorization.prior_readiness_sha256}.json"
    )
    _publish_immutable_run_document(
        history_path,
        pending.prior_report_bytes,
        label="prior configured readiness",
    )
    receipt_path = _publish_immutable_run_document(
        workspace
        / "state"
        / "pipeline_runs"
        / run_id
        / "identity_migrations"
        / "receipts"
        / f"{receipt.evidence_digest()}.json",
        receipt.deterministic_bytes(),
        label="implementation revalidation receipt",
    )
    return receipt, receipt_path


def _selected_implementation_transition(
    authoritative: Any, reproduced: Any
) -> tuple[str, tuple[_ManifestAdapterTransition, ...]]:
    """Compare selected manifests under the code-only equivalence protocol.

    Generator and uniform per-pool adapter pins may move; every other source
    field must remain exact.
    """

    old_payload = authoritative.model_dump(mode="json")
    new_payload = reproduced.model_dump(mode="json")
    old_generator = dict(old_payload.pop("generator"))
    new_generator = dict(new_payload.pop("generator"))
    old_parent = old_payload.pop("parent_manifest_sha256")
    new_parent = new_payload.pop("parent_manifest_sha256")
    old_payload.pop("sources")
    new_payload.pop("sources")
    if old_payload != new_payload:
        return (
            "task count, catalog, seed, allocation, or independence policy changed",
            (),
        )
    old_generator_sha = str(old_generator.pop("sha256"))
    new_generator_sha = str(new_generator.pop("sha256"))
    old_generator_version = str(old_generator.pop("version"))
    new_generator_version = str(new_generator.pop("version"))
    if old_generator != new_generator:
        return "generator metadata beyond its version/code digest changed", ()
    if old_generator_sha == new_generator_sha:
        return "generator code digest did not move", ()
    if old_parent == new_parent:
        return "parent pool identity did not move", ()

    old_entries = authoritative.ordered_entries()
    new_entries = reproduced.ordered_entries()
    old_ids = tuple(entry.expected_task_id for entry in old_entries)
    new_ids = tuple(entry.expected_task_id for entry in new_entries)
    if old_ids != new_ids:
        return "ordered task ids changed", ()

    transitions: list[_ManifestAdapterTransition] = []
    pins_by_origin: dict[Origin, set[tuple[str, str, str, str]]] = {}
    for old_entry, new_entry in zip(old_entries, new_entries, strict=True):
        old_source = old_entry.model_dump(mode="json")
        new_source = new_entry.model_dump(mode="json")
        old_digest = str(old_source.pop("adapter_digest"))
        new_digest = str(new_source.pop("adapter_digest"))
        old_version = str(old_source.pop("adapter_version"))
        new_version = str(new_source.pop("adapter_version"))
        if old_source != new_source:
            return (
                "selector, source artifacts, revision, license, "
                f"or source policy changed for {old_entry.expected_task_id!r}",
                (),
            )
        origin = Origin(old_entry.pool)
        pins_by_origin.setdefault(origin, set()).add(
            (old_version, old_digest, new_version, new_digest)
        )
        old_source_entry = authoritative.source_entry_sha256(old_entry)
        new_source_entry = reproduced.source_entry_sha256(new_entry)
        if (old_version, old_digest) == (new_version, new_digest):
            if old_source_entry != new_source_entry:
                return (
                    f"source-entry digest moved without an adapter change for "
                    f"{old_entry.expected_task_id!r}",
                    (),
                )
            continue
        transitions.append(
            _ManifestAdapterTransition(
                task_id=old_entry.expected_task_id,
                origin=origin,
                authoritative_version=old_version,
                reproduced_version=new_version,
                authoritative_digest=old_digest,
                reproduced_digest=new_digest,
                authoritative_source_entry_sha256=old_source_entry,
                reproduced_source_entry_sha256=new_source_entry,
            )
        )
    inconsistent = sorted(
        origin.value for origin, pins in pins_by_origin.items() if len(pins) != 1
    )
    if inconsistent:
        return (
            "source entries pin inconsistent adapter transitions for pools "
            f"{inconsistent}",
            (),
        )
    generator_version_transition = (
        old_generator_version,
        new_generator_version,
    )
    entry_version_transitions = {
        (row.authoritative_version, row.reproduced_version)
        for row in transitions
        if row.authoritative_version != row.reproduced_version
    }
    if old_generator_version == new_generator_version:
        if entry_version_transitions:
            return (
                "adapter version changed without the matching generator version",
                (),
            )
    elif (
        entry_version_transitions != {generator_version_transition}
        or any(
            (old_entry.adapter_version, new_entry.adapter_version)
            != generator_version_transition
            for old_entry, new_entry in zip(old_entries, new_entries, strict=True)
        )
    ):
        return (
            "generator version changed without the same adapter version "
            "transition for every selected task",
            (),
        )
    return "", tuple(transitions)


def _pool_reconstructed_at_selected_implementation(
    pool_manifest: Any, selected_manifest: Any
) -> Any:
    """Reconstruct a pool's code pins from a selected-manifest observation.

    A selected roster cannot reveal a historical adapter pin for an omitted
    pool.  Such a pool is deliberately left at the supplied value, causing the
    parent digest check to fail if that unobserved adapter actually moved.
    """

    from elt_taskgen.ingest_manifest import FIVE_ORIGINS

    source_updates: dict[str, tuple[Any, ...]] = {}
    for origin in FIVE_ORIGINS:
        selected_entries = tuple(
            getattr(selected_manifest.sources, origin.value, ())
        )
        pool_entries = tuple(getattr(pool_manifest.sources, origin.value, ()))
        if not selected_entries:
            source_updates[origin.value] = pool_entries
            continue
        selected_pins = {
            (entry.adapter_version, entry.adapter_digest)
            for entry in selected_entries
        }
        current_pins = {
            (entry.adapter_version, entry.adapter_digest)
            for entry in pool_entries
        }
        if len(selected_pins) != 1 or len(current_pins) != 1:
            raise _pipeline_support.CliUsageError(
                f"cannot reconstruct {origin.value} pool from inconsistent "
                "adapter pins"
            )
        version, digest = next(iter(selected_pins))
        source_updates[origin.value] = tuple(
            entry.model_copy(
                update={"adapter_version": version, "adapter_digest": digest}
            )
            for entry in pool_entries
        )
    return pool_manifest.model_copy(
        update={
            "generator": selected_manifest.generator,
            "sources": pool_manifest.sources.model_copy(update=source_updates),
        }
    )


def _historical_committed_supersession_is_intact(
    *,
    engine: Engine,
    task: TaskIR,
    artifact: ReportRevalidationEvidence,
    receipt: _GeneratorRevalidationReceipt,
) -> bool:
    """Verify an inactive supersession as immutable migration history.

    A later semantic identity cannot reactivate or inherit the old pass. Check
    only content-addressed history: archived TaskIR, cause, fatal target,
    replacement row, exact marker, and the forward pass that recorded the live
    identity. Current readiness remains a separate, unmodified gate.
    """

    current_hash = task.content_hash()
    lineage_root = (
        task.revisions[0].content_hash if task.revisions else current_hash
    )
    if (
        task.task_id != artifact.task_id
        or lineage_root != artifact.intake_content_hash
        or current_hash == artifact.task_content_hash
    ):
        return False
    try:
        artifact_bytes = engine._read_supersession_evidence(  # noqa: SLF001
            receipt.revalidation_artifact_path,
            label="committed historical report revalidation",
        )
        parsed_artifact = ReportRevalidationEvidence.model_validate_json(
            artifact_bytes
        )
        archived_bytes = engine._read_supersession_evidence(  # noqa: SLF001
            artifact.archived_task_ir_path,
            label="committed historical archived TaskIR",
        )
        archived_task = task_from_json(archived_bytes.decode("utf-8"))
        archived_root = (
            archived_task.revisions[0].content_hash
            if archived_task.revisions
            else archived_task.content_hash()
        )
        cause = engine._report_by_id(artifact.cause_report_id)  # noqa: SLF001
        target = engine._report_by_id(artifact.target_report_id)  # noqa: SLF001
        replacement = engine._report_by_id(  # noqa: SLF001
            receipt.replacement_report_id
        )
        if cause is None or target is None or replacement is None:
            return False
        expected_common = (
            artifact.task_id,
            artifact.stage,
            artifact.task_content_hash,
        )
        if (
            hashlib.sha256(artifact_bytes).hexdigest()
            != receipt.revalidation_artifact_sha256
            or artifact_bytes != artifact.deterministic_bytes()
            or parsed_artifact != artifact
            or artifact.run_id != receipt.run_id
            or artifact.config_sha256 != receipt.config_sha256
            or artifact.authoritative_selected_sha256
            != receipt.authoritative_selected_sha256
            or artifact.reproduced_selected_sha256
            != receipt.reproduced_selected_sha256
            or artifact.authoritative_generator_sha256
            != receipt.authoritative_generator_sha256
            or artifact.reproduced_generator_sha256
            != receipt.reproduced_generator_sha256
            or artifact.authorization_path != receipt.authorization_path
            or artifact.authorization_sha256 != receipt.authorization_sha256
            or artifact.equivalence_commit_path
            != receipt.equivalence_commit_path
            or artifact.equivalence_commit_sha256
            != receipt.equivalence_commit_sha256
            or receipt.superseded_report_ids != (artifact.target_report_id,)
            or hashlib.sha256(archived_bytes).hexdigest()
            != artifact.archived_task_ir_sha256
            or archived_task.task_id != artifact.task_id
            or archived_task.content_hash() != artifact.task_content_hash
            or archived_root != artifact.intake_content_hash
            or (cause.task_id, cause.stage, cause.content_hash) != expected_common
            or cause.verdict != VERDICT_FAIL
            or hashlib.sha256(cause.payload_json.encode("utf-8")).hexdigest()
            != artifact.cause_payload_sha256
            or (target.task_id, target.stage, target.content_hash)
            != expected_common
            or target.verdict != VERDICT_FATAL
            or hashlib.sha256(target.payload_json.encode("utf-8")).hexdigest()
            != artifact.target_payload_sha256
            or not (cause.id < target.id < replacement.id)
            or (replacement.task_id, replacement.stage, replacement.content_hash)
            != expected_common
            or replacement.verdict != VERDICT_PASS
            or replacement.id != receipt.replacement_report_id
            or hashlib.sha256(replacement.payload_json.encode("utf-8")).hexdigest()
            != receipt.replacement_payload_sha256
        ):
            return False

        marker = engine._supersession_marker(replacement)  # noqa: SLF001
        if marker is None:
            return False
        observed_supersession = engine._supersession_from_marker(  # noqa: SLF001
            marker
        )
        revalidation_reason = artifact.replacement_data.get("revalidation_reason")
        if not revalidation_reason:
            return False
        expected_supersession = ReportSupersession(
            migration_id=artifact.migration_id,
            reason=revalidation_reason,
            stage=artifact.stage,
            content_hash=artifact.task_content_hash,
            revalidation_path=receipt.revalidation_artifact_path,
            revalidation_sha256=receipt.revalidation_artifact_sha256,
            cause_report_id=artifact.cause_report_id,
            cause_payload_sha256=artifact.cause_payload_sha256,
            target_report_id=artifact.target_report_id,
            target_payload_sha256=artifact.target_payload_sha256,
            replacement_detail=artifact.replacement_detail,
            replacement_data=tuple(sorted(artifact.replacement_data.items())),
        )
        expected_payload = StagePayload(
            detail=artifact.replacement_detail,
            data={
                **artifact.replacement_data,
                EVIDENCE_SUPERSESSION_KEY: canonical_json(
                    expected_supersession.as_dict()
                ),
            },
        )
        if (
            observed_supersession != expected_supersession
            or marker != expected_supersession.as_dict()
            or replacement.payload_json
            != canonical_json(expected_payload.model_dump(mode="json"))
        ):
            return False

        # The later identity must have been produced by an ordinary successful
        # execution of this same stage after the replacement.  It may be stale
        # under today's behavior; that is deliberately left for readiness.
        for row in engine.report_history(task.task_id, artifact.stage):
            if (
                row.id > replacement.id
                and row.verdict == VERDICT_PASS
                and row.content_hash == current_hash
                and engine._supersession_marker(row) is None  # noqa: SLF001
            ):
                return True
        return False
    except (EngineError, TypeError, UnicodeError, ValueError):
        return False


def _committed_supersession_or_forward_pass_is_current(
    *,
    engine: Engine,
    task: TaskIR,
    artifact: ReportRevalidationEvidence,
    receipt: _GeneratorRevalidationReceipt,
) -> bool:
    """Whether a committed repair is active or intact historical ancestry."""

    if task.content_hash() == artifact.task_content_hash:
        return engine.has_active_report_supersession(task)
    return _historical_committed_supersession_is_intact(
        engine=engine,
        task=task,
        artifact=artifact,
        receipt=receipt,
    )


def _load_committed_generator_migration(
    *,
    workspace: Path,
    pending: _PendingConfiguredMigration,
    _seen: frozenset[str] = frozenset(),
    _depth: int = 0,
) -> _PendingConfiguredMigration:
    """Revalidate a prior run receipt and authorization for an ordinary resume."""

    from elt_taskgen.ingest_manifest import load_generator_equivalence_authorization
    from elt_taskgen.pipeline_readiness import _read_bounded_regular_bytes

    report = pending.prior_report
    if _depth >= 64:
        raise _pipeline_support.CliUsageError("implementation migration ancestry exceeds 64 receipts")
    if not report.generator_revalidation_receipt or not report.identity_migration_sha256:
        raise _pipeline_support.CliUsageError(
            "active implementation migration has no committed run receipt"
        )
    receipt_path = Path(report.generator_revalidation_receipt).absolute()
    expected_root = (
        workspace
        / "state"
        / "pipeline_runs"
        / report.run_id
        / "identity_migrations"
        / "receipts"
    )
    try:
        receipt_path.relative_to(expected_root)
    except ValueError:
        raise _pipeline_support.CliUsageError(
            "implementation revalidation receipt path is not canonical"
        ) from None
    try:
        receipt_bytes = _read_bounded_regular_bytes(
            receipt_path,
            label="implementation revalidation receipt",
            max_bytes=32 * 1024 * 1024,
        )
        receipt_document = json.loads(receipt_bytes)
        if not isinstance(receipt_document, dict):
            raise ValueError("receipt must be a JSON object")
        receipt_schema = receipt_document.get("schema_version")
        if receipt_schema == "configured-generator-revalidation-v1":
            receipt_type = _GeneratorRevalidationReceipt
        elif receipt_schema == "configured-implementation-revalidation-v2":
            receipt_type = _ImplementationRevalidationReceipt
        else:
            raise ValueError(
                f"unsupported revalidation receipt schema {receipt_schema!r}"
            )
        receipt = receipt_type.model_validate(receipt_document)
    except (UnicodeError, ValueError) as exc:
        raise _pipeline_support.CliUsageError(
            f"implementation revalidation receipt is invalid: {exc}"
        ) from None
    if (
        receipt_bytes != receipt.deterministic_bytes()
        or receipt.evidence_digest() != report.identity_migration_sha256
        or receipt_path.name != f"{receipt.evidence_digest()}.json"
    ):
        raise _pipeline_support.CliUsageError(
            "implementation revalidation receipt is not content-addressed"
        )
    receipt_digest = receipt.evidence_digest()
    if receipt_digest in _seen:
        raise _pipeline_support.CliUsageError(
            "implementation migration ancestry contains a receipt cycle"
        )
    ancestry = _seen | {receipt_digest}
    expected_authoritative_path = (
        workspace
        / "state"
        / "candidate_manifests"
        / f"{pending.authoritative_selected.manifest_sha256()}.json"
    )
    expected_reproduced_path = (
        workspace
        / "state"
        / "candidate_manifests"
        / f"{pending.reproduced_selected.manifest_sha256()}.json"
    )
    if (
        receipt.authoritative_selected_path != str(expected_authoritative_path)
        or receipt.reproduced_selected_path != str(expected_reproduced_path)
        or report.active_selected_manifest_path != str(expected_reproduced_path)
    ):
        raise _pipeline_support.CliUsageError(
            "implementation revalidation selected-manifest paths differ"
        )
    for path, manifest, label in (
        (expected_authoritative_path, pending.authoritative_selected, "authoritative selected manifest"),
        (expected_reproduced_path, pending.reproduced_selected, "reproduced selected manifest"),
    ):
        payload = _read_bounded_regular_bytes(path, label=label, max_bytes=32 * 1024 * 1024)
        if hashlib.sha256(canonical_json(manifest.model_dump(mode="json")).encode("utf-8")).hexdigest() != manifest.manifest_sha256():
            raise _pipeline_support.CliUsageError(f"{label} model digest is inconsistent")
        parsed = type(manifest).model_validate_json(payload)
        if parsed != manifest:
            raise _pipeline_support.CliUsageError(f"{label} bytes changed")
    transition_problem, manifest_transitions = _selected_implementation_transition(
        pending.authoritative_selected, pending.reproduced_selected
    )
    if transition_problem:
        raise _pipeline_support.CliUsageError(
            "committed implementation transition is invalid: "
            + transition_problem
        )
    implementation_v2 = isinstance(receipt, _ImplementationRevalidationReceipt)
    if implementation_v2 != bool(manifest_transitions):
        raise _pipeline_support.CliUsageError(
            "committed receipt schema does not match its adapter transitions"
        )
    if pending.adapter_transitions and pending.adapter_transitions != manifest_transitions:
        raise _pipeline_support.CliUsageError("committed adapter transition set changed")
    expected = {
        "run_id": report.run_id,
        "config_sha256": report.config_sha256,
        "authoritative_selected_sha256": (
            pending.authoritative_selected.manifest_sha256()
        ),
        "reproduced_selected_sha256": pending.reproduced_selected.manifest_sha256(),
        "authoritative_pool_sha256": pending.authoritative_pool_sha256,
        "reproduced_pool_sha256": pending.reproduced_pool_sha256,
        "authoritative_generator_sha256": (
            pending.authoritative_selected.generator.sha256
        ),
        "reproduced_generator_sha256": pending.reproduced_selected.generator.sha256,
    }
    observed = receipt.model_dump(mode="json")
    mismatches = [key for key, value in expected.items() if observed[key] != value]
    if mismatches:
        raise _pipeline_support.CliUsageError(
            "implementation revalidation receipt differs from this run: "
            + ", ".join(mismatches)
        )
    authorization_path = _bound_workspace_path(
        workspace,
        receipt.authorization_path,
        label="implementation authorization",
    )
    authorization = load_generator_equivalence_authorization(
        authorization_path, workspace=workspace
    )
    if (
        _is_implementation_authorization(authorization) != implementation_v2
        or authorization.evidence_digest() != receipt.authorization_sha256
        or authorization.run_id != receipt.run_id
        or authorization.config_sha256 != receipt.config_sha256
        or authorization.ordered_task_ids != receipt.ordered_task_ids
        or authorization.prior_readiness_sha256 != receipt.prior_readiness_sha256
        or authorization.authoritative_selected_sha256
        != receipt.authoritative_selected_sha256
        or authorization.reproduced_selected_sha256
        != receipt.reproduced_selected_sha256
        or (
            authorization.authoritative_pool_sha256
            != receipt.authoritative_pool_sha256
        )
        or authorization.reproduced_pool_sha256 != receipt.reproduced_pool_sha256
        or authorization.authoritative_generator_sha256
        != receipt.authoritative_generator_sha256
        or authorization.reproduced_generator_sha256
        != receipt.reproduced_generator_sha256
        or authorization.budget_snapshot_sha256
        != receipt.budget_snapshot_before_sha256
        or float(authorization.budget_snapshot.get("committed_usd", 0.0))
        != receipt.budget_committed_usd
        or float(authorization.budget_snapshot.get("reserved_usd", 0.0))
        != receipt.budget_reserved_usd
        or float(authorization.budget_snapshot.get("uncertain_usd", 0.0))
        != receipt.budget_uncertain_usd
    ):
        raise _pipeline_support.CliUsageError(
            "committed implementation authorization does not match receipt"
        )
    if implementation_v2:
        transition_by_task = {row.task_id: row for row in manifest_transitions}
        old_entries = {
            entry.expected_task_id: entry
            for entry in pending.authoritative_selected.ordered_entries()
        }
        new_entries = {
            entry.expected_task_id: entry
            for entry in pending.reproduced_selected.ordered_entries()
        }
        for binding in authorization.tasks:
            old_entry = old_entries[binding.task_id]
            new_entry = new_entries[binding.task_id]
            expected_transition = transition_by_task.get(binding.task_id)
            observed_transition = binding.adapter_transition
            mismatch = (
                binding.origin is not Origin(old_entry.pool)
                or binding.authoritative_source_entry_sha256
                != pending.authoritative_selected.source_entry_sha256(old_entry)
                or binding.reproduced_source_entry_sha256
                != pending.reproduced_selected.source_entry_sha256(new_entry)
                or (expected_transition is None) != (observed_transition is None)
            )
            if expected_transition is not None and observed_transition is not None:
                mismatch = mismatch or (
                    observed_transition.pool is not expected_transition.origin
                    or observed_transition.authoritative_version
                    != expected_transition.authoritative_version
                    or observed_transition.reproduced_version
                    != expected_transition.reproduced_version
                    or observed_transition.authoritative_digest
                    != expected_transition.authoritative_digest
                    or observed_transition.reproduced_digest
                    != expected_transition.reproduced_digest
                    or observed_transition.authoritative_source_entry_sha256
                    != expected_transition.authoritative_source_entry_sha256
                    or observed_transition.reproduced_source_entry_sha256
                    != expected_transition.reproduced_source_entry_sha256
                )
            if mismatch:
                raise _pipeline_support.CliUsageError(
                    "committed implementation authorization changed adapter/source "
                    f"binding for {binding.task_id!r}"
                )
    commit_path = _bound_workspace_path(
        workspace,
        receipt.equivalence_commit_path,
        label="implementation equivalence commit",
    )
    commit_bytes = _read_bounded_regular_bytes(
        commit_path,
        label="implementation equivalence commit",
        max_bytes=32 * 1024 * 1024,
    )
    commit_type = (
        _ImplementationEquivalenceCommit
        if implementation_v2
        else _GeneratorEquivalenceCommit
    )
    try:
        commit = commit_type.model_validate_json(commit_bytes)
    except ValueError as exc:
        raise _pipeline_support.CliUsageError(
            f"implementation equivalence commit is invalid: {exc}"
        ) from None
    if (
        commit_bytes != commit.deterministic_bytes()
        or commit.evidence_digest() != receipt.equivalence_commit_sha256
        or commit.authorization_sha256 != authorization.evidence_digest()
        or commit.ordered_task_ids != authorization.ordered_task_ids
        or tuple(
            (row.task_id, row.intake_content_hash, row.equivalence_sha256)
            for row in commit.tasks
        )
        != tuple(
            (row.task_id, row.intake_content_hash, row.equivalence_sha256)
            for row in receipt.tasks
        )
    ):
        raise _pipeline_support.CliUsageError("committed implementation equivalence roster changed")
    verified_commit, verified_commit_path = _commit_generator_equivalences(
        workspace=workspace,
        run_id=receipt.run_id,
        authorization=authorization,
        authorization_path=authorization_path,
    )
    if (
        verified_commit != commit
        or verified_commit_path.absolute() != commit_path.absolute()
    ):
        raise _pipeline_support.CliUsageError(
            "committed implementation equivalence evidence no longer verifies"
        )
    by_id = {task.task_id: task for task in receipt.tasks}
    for binding in authorization.tasks:
        row = by_id.get(binding.task_id)
        if row is None or (
            row.intake_content_hash != binding.intake_content_hash
            or row.authoritative_provenance_sha256
            != binding.authoritative_provenance_sha256
            or row.reproduced_provenance_sha256
            != binding.reproduced_provenance_sha256
        ):
            raise _pipeline_support.CliUsageError(
                f"committed generator task binding changed for {binding.task_id!r}"
            )
        evidence_path = _bound_workspace_path(
            workspace, row.equivalence_path, label="generator provenance equivalence"
        )
        evidence = _read_bounded_regular_bytes(
            evidence_path,
            label="generator provenance equivalence",
            max_bytes=16 * 1024 * 1024,
        )
        if hashlib.sha256(evidence).hexdigest() != row.equivalence_sha256:
            raise _pipeline_support.CliUsageError(
                f"generator provenance equivalence changed for {binding.task_id!r}"
            )
    history_path = (
        workspace
        / "state"
        / "pipeline_runs"
        / report.run_id
        / "readiness_history"
        / f"{receipt.prior_readiness_sha256}.json"
    )
    history = _read_bounded_regular_bytes(
        history_path, label="prior configured readiness", max_bytes=32 * 1024 * 1024
    )
    if hashlib.sha256(history).hexdigest() != receipt.prior_readiness_sha256:
        raise _pipeline_support.CliUsageError("prior configured readiness history changed")
    try:
        historical_report = type(report).model_validate_json(history)
        historical_document = json.loads(history)
    except ValueError as exc:
        raise _pipeline_support.CliUsageError(f"prior configured readiness history is invalid: {exc}") from None
    historical_counts = (
        historical_document.get("counts")
        if isinstance(historical_document, dict)
        else None
    )
    historical_selected_problem = bool(
        isinstance(historical_counts, dict)
        and "selected" in historical_counts
        and historical_report.counts.selected != len(receipt.ordered_task_ids)
    )
    expected_historical_selected_path = (
        workspace
        / "state"
        / "candidate_manifests"
        / f"{receipt.authoritative_selected_sha256}.json"
    )
    if (
        historical_report.run_id != receipt.run_id
        or historical_report.config_sha256 != receipt.config_sha256
        or historical_report.config.fingerprint()
        != historical_report.config_sha256
        or historical_report.source_manifest_sha256
        != receipt.authoritative_selected_sha256
        or historical_report.selected_manifest_path
        != str(expected_historical_selected_path)
        or tuple(task.task_id for task in historical_report.tasks)
        != receipt.ordered_task_ids
        or historical_report.counts.requested != len(receipt.ordered_task_ids)
        or historical_selected_problem
    ):
        raise _pipeline_support.CliUsageError("prior configured readiness history identity differs")

    head = {
        "active_source_manifest_sha256": (
            historical_report.active_source_manifest_sha256
        ),
        "active_selected_manifest_path": (
            historical_report.active_selected_manifest_path
        ),
        "generator_revalidation_receipt": (
            historical_report.generator_revalidation_receipt
        ),
        "identity_migration_sha256": historical_report.identity_migration_sha256,
    }
    present = {name: bool(value) for name, value in head.items()}
    if any(present.values()) and not all(present.values()):
        raise _pipeline_support.CliUsageError(
            "prior configured readiness has a partial implementation migration head"
        )
    if all(present.values()):
        previous_selected_path = (
            workspace
            / "state"
            / "candidate_manifests"
            / f"{historical_report.active_source_manifest_sha256}.json"
        )
        if historical_report.active_selected_manifest_path != str(
            previous_selected_path
        ):
            raise _pipeline_support.CliUsageError(
                "prior implementation migration active selected path is not canonical"
            )
        try:
            previous_selected_bytes = _read_bounded_regular_bytes(
                previous_selected_path,
                label="prior active selected manifest",
                max_bytes=32 * 1024 * 1024,
            )
            previous_selected = type(pending.reproduced_selected).model_validate_json(
                previous_selected_bytes
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise _pipeline_support.CliUsageError(
                f"prior active selected manifest is invalid: {exc}"
            ) from None
        if (
            previous_selected.manifest_sha256()
            != historical_report.active_source_manifest_sha256
        ):
            raise _pipeline_support.CliUsageError("prior active selected manifest digest changed")
        if (
            previous_selected.manifest_sha256()
            == pending.reproduced_selected.manifest_sha256()
        ):
            raise _pipeline_support.CliUsageError(
                "implementation migration ancestry repeats its active head"
            )
        prior_transition_problem, prior_adapter_transitions = (
            _selected_implementation_transition(
            pending.authoritative_selected, previous_selected
            )
        )
        if prior_transition_problem:
            raise _pipeline_support.CliUsageError(
                "prior implementation migration is invalid: "
                + prior_transition_problem
            )
        _load_committed_generator_migration(
            workspace=workspace,
            pending=_PendingConfiguredMigration(
                prior_report=historical_report,
                prior_report_bytes=history,
                authoritative_selected=pending.authoritative_selected,
                reproduced_selected=previous_selected,
                authoritative_pool_sha256=(
                    pending.authoritative_selected.parent_manifest_sha256
                ),
                reproduced_pool_sha256=previous_selected.parent_manifest_sha256,
                adapter_transitions=prior_adapter_transitions,
            ),
            _seen=ancestry,
            _depth=_depth + 1,
        )
    if receipt.superseded_report_ids:
        if not receipt.revalidation_artifact_path:
            raise _pipeline_support.CliUsageError("committed report supersession has no revalidation artifact")
        artifact_path = _bound_workspace_path(
            workspace,
            receipt.revalidation_artifact_path,
            label="local report revalidation",
        )
        artifact_bytes = _read_bounded_regular_bytes(
            artifact_path,
            label="local report revalidation",
            max_bytes=32 * 1024 * 1024,
        )
        try:
            artifact = ReportRevalidationEvidence.model_validate_json(artifact_bytes)
        except ValueError as exc:
            raise _pipeline_support.CliUsageError(f"local report revalidation is invalid: {exc}") from None
        if (
            artifact_bytes != artifact.deterministic_bytes()
            or hashlib.sha256(artifact_bytes).hexdigest()
            != receipt.revalidation_artifact_sha256
            or (artifact.target_report_id,) != receipt.superseded_report_ids
        ):
            raise _pipeline_support.CliUsageError("local report revalidation changed")
        engine = _open_engine(workspace, max_repair_rounds=0)
        try:
            task = engine.load_task(artifact.task_id)
            replacement = engine._report_by_id(receipt.replacement_report_id)
            if (
                replacement is None
                or replacement.task_id != artifact.task_id
                or replacement.stage != artifact.stage
                or replacement.content_hash != artifact.task_content_hash
                or hashlib.sha256(replacement.payload_json.encode("utf-8")).hexdigest()
                != receipt.replacement_payload_sha256
            ):
                raise _pipeline_support.CliUsageError("committed replacement PASS changed")
            if not _committed_supersession_or_forward_pass_is_current(
                engine=engine,
                task=task,
                artifact=artifact,
                receipt=receipt,
            ):
                raise _pipeline_support.CliUsageError(
                    "committed report supersession is neither active nor "
                    "intact historical ancestry"
                )
        finally:
            engine.close()
    return replace(
        pending,
        authorization=authorization,
        authorization_path=authorization_path,
        committed_receipt=receipt,
        committed_receipt_path=receipt_path,
    )


def _assert_configured_run_identity(
    *,
    workspace: Path,
    run_id: str,
    spec,
    pool_manifest,
    selected_manifest,
    reingest: bool,
) -> _PendingConfiguredMigration | None:
    """Validate an ordinary resume or a code-only roster revalidation."""

    from elt_taskgen.ingest_manifest import SelectedSourceIngestManifest
    from elt_taskgen.pipeline_readiness import (
        PipelineReadinessReport,
        _read_bounded_regular_bytes,
    )

    run_root = workspace / "state" / "pipeline_runs" / run_id
    if run_root.is_symlink():
        raise _pipeline_support.CliUsageError(
            f"run id {run_id!r} resolves through an unsafe state symlink; "
            "use a different --run-id"
        )
    if not run_root.exists():
        return None
    report_path = run_root / "readiness.json"
    if not report_path.is_file() or report_path.is_symlink():
        raise _pipeline_support.CliUsageError(
            f"run id {run_id!r} already has state without a safe readiness "
            "identity; use a different --run-id"
        )
    try:
        prior_report_bytes = _read_bounded_regular_bytes(
            report_path, label="prior configured readiness", max_bytes=32 * 1024 * 1024
        )
        prior_report_document = json.loads(prior_report_bytes)
        report = PipelineReadinessReport.model_validate_json(prior_report_bytes)
    except (OSError, UnicodeError, ValueError) as exc:
        raise _pipeline_support.CliUsageError(
            f"run id {run_id!r} has an invalid prior readiness report; use a "
            f"different --run-id: {exc}"
        ) from None
    expected_config = spec.fingerprint()
    expected_selection = selected_manifest.manifest_sha256()
    problems: list[str] = []
    if report.run_id != run_id:
        problems.append(f"embedded run_id is {report.run_id!r}")
    if (
        report.config_sha256 != expected_config
        or report.config.fingerprint() != report.config_sha256
    ):
        problems.append("configuration fingerprint differs")
    if problems:
        raise _pipeline_support.CliUsageError(
            f"run id {run_id!r} is already bound to a different run identity "
            f"({'; '.join(problems)}); use a different --run-id"
        )
    if not spec.resume:
        raise _pipeline_support.CliUsageError(
            f"run id {run_id!r} already exists but resume=false; use a "
            "different --run-id or enable resume"
        )
    head_present = tuple(
        bool(value)
        for value in (
            report.active_source_manifest_sha256,
            report.active_selected_manifest_path,
            report.generator_revalidation_receipt,
            report.identity_migration_sha256,
        )
    )
    if any(head_present) and not all(head_present):
        raise _pipeline_support.CliUsageError(
            "prior readiness has a partial implementation migration head"
        )
    if report.source_manifest_sha256 == expected_selection:
        if any(head_present):
            raise _pipeline_support.CliUsageError("prior readiness has a conflicting active selection")
        return None
    committed_resume = report.active_source_manifest_sha256 == expected_selection
    if not report.active_source_manifest_sha256 and not reingest:
        raise _pipeline_support.CliUsageError(
            f"run id {run_id!r} selected-roster fingerprint differs: bound to "
            f"{report.source_manifest_sha256[:12]}, not {expected_selection[:12]}; "
            "a code-only continuation requires explicit --reingest"
        )

    authoritative_path = (
        workspace
        / "state"
        / "candidate_manifests"
        / f"{report.source_manifest_sha256}.json"
    )
    if str(authoritative_path) != report.selected_manifest_path:
        raise _pipeline_support.CliUsageError(
            "prior readiness does not name its canonical immutable selected manifest"
        )
    try:
        authoritative_bytes = _read_bounded_regular_bytes(
            authoritative_path,
            label="authoritative selected manifest",
            max_bytes=32 * 1024 * 1024,
        )
        authoritative = SelectedSourceIngestManifest.model_validate_json(
            authoritative_bytes
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise _pipeline_support.CliUsageError(
            f"authoritative selected manifest is invalid: {exc}"
        ) from None
    if authoritative.manifest_sha256() != report.source_manifest_sha256:
        raise _pipeline_support.CliUsageError("authoritative selected manifest digest changed")
    authoritative_ids = tuple(
        entry.expected_task_id for entry in authoritative.ordered_entries()
    )
    readiness_ids = tuple(task.task_id for task in report.tasks)
    raw_counts = (
        prior_report_document.get("counts")
        if isinstance(prior_report_document, dict)
        else None
    )
    selected_count_problem = bool(
        isinstance(raw_counts, dict)
        and "selected" in raw_counts
        and report.counts.selected != len(authoritative_ids)
    )
    if (
        readiness_ids != authoritative_ids
        or report.counts.requested != len(authoritative_ids)
        or selected_count_problem
    ):
        raise _pipeline_support.CliUsageError(
            "prior readiness task roster/order/count differs from its "
            "authoritative selected manifest"
        )

    # A run may already have one committed implementation-equivalence head. Validate that
    # immutable receipt before either resuming it or moving the head again.  A
    # later migration remains a direct authoritative->current equivalence for
    # every TaskIR; its prior-readiness digest preserves the intervening head.
    active = None
    if report.active_source_manifest_sha256:
        active_path = (
            workspace
            / "state"
            / "candidate_manifests"
            / f"{report.active_source_manifest_sha256}.json"
        )
        if str(active_path) != report.active_selected_manifest_path:
            raise _pipeline_support.CliUsageError(
                "prior readiness does not name its canonical active selected manifest"
            )
        try:
            active_bytes = _read_bounded_regular_bytes(
                active_path,
                label="active selected manifest",
                max_bytes=32 * 1024 * 1024,
            )
            active = SelectedSourceIngestManifest.model_validate_json(active_bytes)
        except (OSError, UnicodeError, ValueError) as exc:
            raise _pipeline_support.CliUsageError(
                f"active selected manifest is invalid: {exc}"
            ) from None
        if active.manifest_sha256() != report.active_source_manifest_sha256:
            raise _pipeline_support.CliUsageError("active selected manifest digest changed")
        if authoritative_ids != tuple(
            entry.expected_task_id for entry in active.ordered_entries()
        ):
            raise _pipeline_support.CliUsageError(
                "active implementation migration changed ordered task ids"
            )
        active_problem, active_adapter_transitions = (
            _selected_implementation_transition(authoritative, active)
        )
        if active_problem:
            raise _pipeline_support.CliUsageError(
                "active implementation migration is invalid: " + active_problem
            )
        committed = _load_committed_generator_migration(
            workspace=workspace,
            pending=_PendingConfiguredMigration(
                prior_report=report,
                prior_report_bytes=prior_report_bytes,
                authoritative_selected=authoritative,
                reproduced_selected=active,
                authoritative_pool_sha256=authoritative.parent_manifest_sha256,
                reproduced_pool_sha256=active.parent_manifest_sha256,
                adapter_transitions=active_adapter_transitions,
            ),
        )
        if committed_resume:
            return committed

    if not reingest:
        bound = (
            report.active_source_manifest_sha256
            or report.source_manifest_sha256
        )
        raise _pipeline_support.CliUsageError(
            f"run id {run_id!r} selected-roster fingerprint differs: bound to "
            f"{bound[:12]}, not {expected_selection[:12]}; a code-only "
            "continuation requires explicit --reingest"
        )

    transition_problem, adapter_transitions = _selected_implementation_transition(
        authoritative, selected_manifest
    )
    if transition_problem:
        raise _pipeline_support.CliUsageError(
            "implementation revalidation refused: " + transition_problem
        )
    old_parent = authoritative.parent_manifest_sha256
    new_parent = selected_manifest.parent_manifest_sha256
    if selected_manifest.parent_manifest_sha256 != pool_manifest.manifest_sha256():
        raise _pipeline_support.CliUsageError("selected manifest does not bind the supplied pool")
    reconstructed_old_pool = (
        _pool_reconstructed_at_selected_implementation(
            pool_manifest, authoritative
        )
        if adapter_transitions
        else pool_manifest.model_copy(update={"generator": authoritative.generator})
    )
    if reconstructed_old_pool.manifest_sha256() != old_parent:
        raise _pipeline_support.CliUsageError(
            "supplied repinned pool differs from the authoritative pool beyond "
            "its explicitly rederived implementation pins"
        )
    if active is not None:
        active_transition_problem, active_transitions = (
            _selected_implementation_transition(authoritative, active)
        )
        if active_transition_problem:
            raise _pipeline_support.CliUsageError(
                "active implementation migration is invalid: "
                + active_transition_problem
            )
        active_to_selected_problem, active_to_selected_transitions = (
            _selected_implementation_transition(active, selected_manifest)
        )
        if active_to_selected_problem:
            raise _pipeline_support.CliUsageError(
                "implementation revalidation differs from the active head: "
                + active_to_selected_problem
            )
        # The historical active migration can be generator-only while the new
        # pool moves adapter pins relative to that active head.  Reconstruction
        # must therefore use the direct active -> selected transition, not the
        # authoritative -> active transition validated above.
        reconstructed_active_pool = (
            _pool_reconstructed_at_selected_implementation(pool_manifest, active)
            if active_to_selected_transitions
            else pool_manifest.model_copy(update={"generator": active.generator})
        )
        if (
            reconstructed_active_pool.manifest_sha256()
            != active.parent_manifest_sha256
        ):
            raise _pipeline_support.CliUsageError(
                "supplied repinned pool differs from the active migration pool "
                "beyond its explicitly rederived implementation pins"
            )
    pending = _PendingConfiguredMigration(
        prior_report=report,
        prior_report_bytes=prior_report_bytes,
        authoritative_selected=authoritative,
        reproduced_selected=selected_manifest,
        authoritative_pool_sha256=str(old_parent),
        reproduced_pool_sha256=str(new_parent),
        adapter_transitions=adapter_transitions,
    )
    return pending

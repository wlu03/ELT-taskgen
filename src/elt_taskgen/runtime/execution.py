"""Replay solver-owned Stage 1 and Stage 2 working files separately.

These functions execute an already-authored submission.  They do not create
Airbyte resources on behalf of the solver and do not author dbt models:
Terraform in ``elt/`` owns Stage 1; a dbt project in ``elt/`` owns Stage 2.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from elt_taskgen.destinations import (
    DBT_ADAPTER_CONTRACTS,
    DBT_CORE_VERSION,
    Destination,
    destination_contract,
    destination_from_config,
)
from elt_taskgen.runtime.airbyte import AirbyteClient
from elt_taskgen.runtime.process import ProcessFailure, Runner, SubprocessRunner


class ExecutionError(ValueError):
    """A submitted working directory cannot be executed as an ELT task."""


class Stage2PreflightError(ExecutionError):
    """The Stage 2 fail-closed preflight refused the submitted configuration."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"stage2 preflight [{code}]: {message}")
        self.code = code


_MAX_TERRAFORM_STATE_BYTES = 16 * 1024 * 1024
_MAX_DBT_RUN_RESULTS_BYTES = 8 * 1024 * 1024
_MAX_SUBMISSION_TREE_BYTES = 64 * 1024 * 1024
_MAX_SUBMISSION_TREE_FILES = 4096
_MAX_SUBMISSION_TREE_DIRECTORIES = 4096


def _utc_now() -> str:
    """One immutable, unambiguous receipt timestamp."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _read_regular_file(path: Path, *, label: str, limit: int) -> bytes:
    """Read one bounded regular file without following a final symlink."""

    path = Path(path)
    try:
        status = path.lstat()
    except OSError as exc:
        raise ExecutionError(f"{label} is missing or unreadable: {path}") from exc
    if not stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise ExecutionError(f"{label} must be a regular non-symlink file: {path}")
    if status.st_size > limit:
        raise ExecutionError(f"{label} exceeds the {limit}-byte safety bound")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExecutionError(f"{label} is missing or unreadable: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size > limit
            or opened.st_dev != status.st_dev
            or opened.st_ino != status.st_ino
        ):
            raise ExecutionError(f"{label} changed while it was being opened")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1 << 20, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise ExecutionError(f"{label} exceeds the {limit}-byte safety bound")
        finished = os.fstat(descriptor)
        if (
            finished.st_dev != opened.st_dev
            or finished.st_ino != opened.st_ino
            or finished.st_size != opened.st_size
            or finished.st_mtime_ns != opened.st_mtime_ns
            or total != opened.st_size
        ):
            raise ExecutionError(f"{label} changed while it was being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _json_no_duplicates(data: bytes, *, label: str, path: Path) -> Any:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ExecutionError(f"invalid {label}: {path}") from exc


def _walk_regular_tree(
    root: Path,
    *,
    label: str,
    skip_directory: Callable[[Path], bool] | None = None,
) -> tuple[Path, ...]:
    """Enumerate a bounded, symlink-free input tree in canonical order."""

    root = Path(root)
    try:
        root_status = root.lstat()
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise ExecutionError(f"{label} is unreadable: {root}") from exc
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise ExecutionError(f"{label} must be a non-symlink directory: {root}")
    files: list[Path] = []
    pending = [root]
    directory_count = 0
    while pending:
        directory = pending.pop()
        directory_count += 1
        if directory_count > _MAX_SUBMISSION_TREE_DIRECTORIES:
            raise ExecutionError(f"{label} contains too many directories")
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise ExecutionError(f"{label} is unreadable: {directory}") from exc
        child_directories: list[Path] = []
        for entry in entries:
            path = Path(entry.path)
            try:
                status = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ExecutionError(f"{label} entry is unreadable: {path}") from exc
            if stat.S_ISLNK(status.st_mode):
                raise ExecutionError(f"{label} contains a symlink: {path}")
            if stat.S_ISDIR(status.st_mode):
                if skip_directory is None or not skip_directory(path):
                    child_directories.append(path)
                continue
            if not stat.S_ISREG(status.st_mode):
                raise ExecutionError(f"{label} contains a non-regular file: {path}")
            files.append(path)
            if len(files) > _MAX_SUBMISSION_TREE_FILES:
                raise ExecutionError(f"{label} contains too many files")
        # Reverse the push so the lexically first directory is visited first.
        pending.extend(reversed(child_directories))
    return tuple(sorted(files, key=lambda path: path.as_posix()))


def _capture_input_files(
    entries: Mapping[str, Path],
    *,
    label: str,
    initial: Mapping[str, bytes] | None = None,
) -> dict[str, bytes]:
    """Capture each named input once through the bounded O_NOFOLLOW reader."""

    captured = dict(initial or {})
    if len(entries) + len(captured) > _MAX_SUBMISSION_TREE_FILES:
        raise ExecutionError(f"{label} contains too many files")
    total = sum(len(data) for data in captured.values())
    if total > _MAX_SUBMISSION_TREE_BYTES:
        raise ExecutionError(f"{label} exceeds the input-tree safety bound")
    for logical_path, path in sorted(entries.items()):
        if logical_path in captured:
            raise ExecutionError(f"{label} contains duplicate path {logical_path!r}")
        remaining = _MAX_SUBMISSION_TREE_BYTES - total
        if remaining <= 0:
            raise ExecutionError(f"{label} exceeds the input-tree safety bound")
        data = _read_regular_file(path, label=f"{label} file", limit=remaining)
        captured[logical_path] = data
        total += len(data)
    return captured


def _captured_tree_digest(files: Mapping[str, bytes]) -> str:
    """Hash a path/size/content inventory independent of filesystem order."""

    records = [
        {
            "path": path,
            "size_bytes": len(files[path]),
            "sha256": hashlib.sha256(files[path]).hexdigest(),
        }
        for path in sorted(files)
    ]
    canonical = json.dumps(
        records,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _terraform_input_tree_digest(work_dir: Path) -> str:
    """Digest every admitted file visible to Terraform.

    Include the complete workspace except VCS, Python, Terraform, and dbt
    caches plus Terraform state and crash outputs. Credential bytes contribute
    only to the aggregate digest and are never copied into the receipt. Excluded
    runtime outputs are not valid solver inputs.
    """

    work_dir = Path(work_dir)
    elt_dir = work_dir / "elt"

    def skip(path: Path) -> bool:
        if path.name in {".git", "__pycache__", ".terraform"}:
            return True
        try:
            relative = path.relative_to(work_dir)
        except ValueError:
            raise ExecutionError(
                "Terraform submission entry escaped the solver workspace"
            ) from None
        return (
            path.parent == elt_dir
            and (
                path.name in {"target", "logs", "dbt_packages"}
                or path.name.startswith(".elt-taskgen-dbt-target-")
            )
        )

    def is_runtime_output(path: Path) -> bool:
        if path.parent != elt_dir:
            return False
        name = path.name
        return (
            name == ".terraform.tfstate.lock.info"
            or name == "crash.log"
            or (name.startswith("crash.") and name.endswith(".log"))
            or name == "terraform.tfstate"
            or name.startswith("terraform.tfstate.")
        )

    paths = {
        path.relative_to(work_dir).as_posix(): path
        for path in _walk_regular_tree(
            work_dir, label="Terraform submission", skip_directory=skip
        )
        if not is_runtime_output(path)
    }
    return _captured_tree_digest(
        _capture_input_files(paths, label="Terraform submission")
    )


def _harden_terraform_state(elt_dir: Path) -> None:
    """Make every regular Terraform state artifact owner-only.

    Terraform writes state itself and therefore obeys the invoking process's
    umask, which is not a sufficient credential boundary. Run this after each
    command even on failure because a nonzero apply may still have created or
    updated state. Symlinks are ignored rather than followed.
    """

    for path in Path(elt_dir).rglob("terraform.tfstate*"):
        try:
            status = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(status.st_mode):
            path.chmod(0o600)


def _module_connection_resources(
    module: Any, *, fallback_address: str = "root_module"
) -> list[tuple[str, str]]:
    """Extract addressed Airbyte resources from ``terraform show -json``."""

    if not isinstance(module, dict):
        return []
    found: list[tuple[str, str]] = []
    resources = module.get("resources")
    if isinstance(resources, list):
        for index, resource in enumerate(resources):
            if (
                not isinstance(resource, dict)
                or resource.get("type") != "airbyte_connection"
                or resource.get("mode", "managed") != "managed"
            ):
                continue
            values = resource.get("values")
            connection_id = (
                values.get("connection_id") if isinstance(values, dict) else None
            )
            if not isinstance(connection_id, str) or not connection_id.strip():
                raise ExecutionError(
                    "Terraform show state has an Airbyte connection with no "
                    "connection_id"
                )
            if len(connection_id) > 1024 or any(
                ord(char) < 32 for char in connection_id
            ):
                raise ExecutionError(
                    "Terraform show state has an invalid Airbyte connection_id"
                )
            address = resource.get("address")
            if not isinstance(address, str) or not address.strip():
                address = f"{fallback_address}.resources[{index}]"
            if len(address) > 2048 or any(ord(char) < 32 for char in address):
                raise ExecutionError(
                    "Terraform show state has an invalid Airbyte resource address"
                )
            found.append((address, connection_id))
    children = module.get("child_modules")
    if isinstance(children, list):
        for index, child in enumerate(children):
            child_address = (
                child.get("address") if isinstance(child, dict) else None
            )
            if not isinstance(child_address, str) or not child_address.strip():
                child_address = f"{fallback_address}.child_modules[{index}]"
            found.extend(
                _module_connection_resources(
                    child, fallback_address=child_address
                )
            )
    return found


@dataclass(frozen=True)
class _TerraformStateIdentity:
    """Non-secret identity extracted from one exact local state snapshot."""

    connection_ids: tuple[str, ...]
    connection_resources: tuple[tuple[str, str], ...]
    digest: str
    lineage: str
    serial: int


def _state_connection_resources(resources: Any) -> tuple[tuple[str, str], ...]:
    """Return canonical ``(instance address, connection id)`` state entries."""

    if not isinstance(resources, list):
        return ()
    found: list[tuple[str, str]] = []
    for resource_index, resource in enumerate(resources):
        if (
            not isinstance(resource, dict)
            or resource.get("type") != "airbyte_connection"
            or resource.get("mode", "managed") != "managed"
        ):
            continue
        module = resource.get("module")
        if module is not None and (
            not isinstance(module, str)
            or not module.strip()
            or len(module) > 1536
            or any(ord(char) < 32 for char in module)
        ):
            raise ExecutionError(
                "Terraform state has an invalid Airbyte connection module address"
            )
        name = resource.get("name")
        if (
            isinstance(name, str)
            and name.strip()
            and len(name) <= 256
            and not any(ord(char) < 32 for char in name)
        ):
            base = f"airbyte_connection.{name}"
        elif name is not None:
            raise ExecutionError(
                "Terraform state has an invalid Airbyte connection resource name"
            )
        else:
            # Compatibility with old/minimal state writers.  The positional
            # address remains exact for the captured bytes, but certified
            # Terraform state normally supplies the real resource name.
            base = f"airbyte_connection.<resource-{resource_index}>"
        if module:
            base = f"{module}.{base}"
        instances = resource.get("instances")
        if not isinstance(instances, list):
            raise ExecutionError(
                f"Terraform state Airbyte connection {base} has no instance list"
            )
        for instance_index, instance in enumerate(instances):
            if not isinstance(instance, dict):
                raise ExecutionError(
                    f"Terraform state Airbyte connection {base} has an invalid instance"
                )
            attributes = instance.get("attributes")
            if instance.get("status") == "tainted" or "deposed" in instance:
                raise ExecutionError(
                    f"Terraform state Airbyte connection {base} is not current"
                )
            connection_id = (
                attributes.get("connection_id")
                if isinstance(attributes, dict)
                else None
            )
            if not isinstance(connection_id, str) or not connection_id.strip():
                raise ExecutionError(
                    f"Terraform state Airbyte connection {base} has no connection_id"
                )
            if len(connection_id) > 1024 or any(
                ord(char) < 32 for char in connection_id
            ):
                raise ExecutionError(
                    f"Terraform state Airbyte connection {base} has an invalid "
                    "connection_id"
                )
            if "index_key" in instance:
                try:
                    index = json.dumps(
                        instance["index_key"],
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                except (TypeError, ValueError) as exc:
                    raise ExecutionError(
                        f"Terraform state Airbyte connection {base} has an "
                        "invalid index"
                    ) from exc
                address = f"{base}[{index}]"
            elif len(instances) > 1:
                # Real Terraform state provides index_key for repeated
                # resources.  Keep minimal legacy fixtures unambiguous.
                address = f"{base}[{instance_index}]"
            else:
                address = base
            found.append((address, connection_id))
    canonical = tuple(sorted(found))
    addresses = [address for address, _ in canonical]
    ids = [connection_id for _, connection_id in canonical]
    if len(addresses) != len(set(addresses)):
        raise ExecutionError(
            "Terraform state contains duplicate Airbyte connection resource addresses"
        )
    if len(ids) != len(set(ids)):
        raise ExecutionError(
            "Terraform state reuses one Airbyte connection id across resources"
        )
    return canonical


def _terraform_state_identity(state_path: Path) -> _TerraformStateIdentity:
    """Parse non-secret identity and hash the exact same bounded state bytes."""

    state_path = Path(state_path)
    data = _read_regular_file(
        state_path,
        label="Terraform state",
        limit=_MAX_TERRAFORM_STATE_BYTES,
    )
    state = _json_no_duplicates(
        data,
        label="Terraform state",
        path=state_path,
    )
    if not isinstance(state, dict):
        raise ExecutionError(f"invalid Terraform state: {state_path}")
    resource_entries = list(_state_connection_resources(state.get("resources")))
    # ``connection_ids`` historically also accepted ``terraform show -json``;
    # retain that bounded shape and give every entry a stable address too.
    values = state.get("values")
    if isinstance(values, dict):
        resource_entries.extend(
            _module_connection_resources(values.get("root_module"))
        )
    resources = tuple(sorted(resource_entries))
    addresses = [address for address, _ in resources]
    ids = [connection_id for _, connection_id in resources]
    if len(addresses) != len(set(addresses)):
        raise ExecutionError(
            "Terraform state contains duplicate Airbyte connection resource addresses"
        )
    if len(ids) != len(set(ids)):
        raise ExecutionError(
            "Terraform state reuses one Airbyte connection id across resources"
        )
    found = tuple(ids)

    lineage = state.get("lineage")
    if not isinstance(lineage, str):
        lineage = ""
    serial = state.get("serial")
    if isinstance(serial, bool) or not isinstance(serial, int) or serial < 0:
        serial = -1
    return _TerraformStateIdentity(
        connection_ids=found,
        connection_resources=resources,
        digest=hashlib.sha256(data).hexdigest(),
        lineage=lineage,
        serial=serial,
    )


def connection_ids(state_path: Path) -> tuple[str, ...]:
    """Read every Airbyte connection id from a Terraform state document."""

    return _terraform_state_identity(state_path).connection_ids


def _validated_sync_maps(
    connection_ids: tuple[str, ...],
    raw_statuses: Any,
    raw_jobs: Any,
) -> tuple[dict[str, str], dict[str, int]]:
    """Validate exact coverage and one positive job identity per connection."""

    if not connection_ids or len(connection_ids) != len(set(connection_ids)):
        raise ExecutionError(
            "Airbyte sync receipt requires distinct Terraform connection ids"
        )
    try:
        statuses = dict(raw_statuses)
        raw_job_ids = dict(raw_jobs)
    except (TypeError, ValueError) as exc:
        raise ExecutionError("Airbyte sync receipt maps are invalid") from exc
    expected = set(connection_ids)
    if set(statuses) != expected or set(raw_job_ids) != expected:
        raise ExecutionError(
            "Airbyte sync receipt does not exactly cover Terraform connections"
        )
    job_ids: dict[str, int] = {}
    seen: set[int] = set()
    for connection_id in connection_ids:
        value = raw_job_ids[connection_id]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ExecutionError(
                "Airbyte sync receipt job ids must be positive integers"
            )
        if value in seen:
            raise ExecutionError(
                "Airbyte sync receipt reused one job id for distinct connections"
            )
        seen.add(value)
        job_ids[connection_id] = value
    return statuses, job_ids


def _validated_sync_receipt(
    connection_ids: tuple[str, ...], receipt: Any
) -> tuple[dict[str, str], dict[str, int]]:
    try:
        statuses = receipt.statuses
        job_ids = receipt.job_ids
    except AttributeError as exc:
        raise ExecutionError("Airbyte sync receipt has no status/job maps") from exc
    return _validated_sync_maps(connection_ids, statuses, job_ids)


def _validate_workspace_connections(
    airbyte: AirbyteClient,
    workspace_id: str,
    expected_ids: tuple[str, ...],
) -> None:
    records = airbyte.list_connections(workspace_id)
    actual: dict[str, dict[str, Any]] = {}
    for record in records:
        raw_id = record.get("connectionId") or record.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            raise ExecutionError("Airbyte workspace returned a connection with no id")
        if raw_id in actual:
            raise ExecutionError("Airbyte workspace returned a duplicate connection id")
        actual[raw_id] = record
    expected = set(expected_ids)
    if set(actual) != expected:
        missing = sorted(expected - set(actual))
        unexpected = sorted(set(actual) - expected)
        raise ExecutionError(
            "Terraform/Airbyte workspace connection mismatch: "
            f"missing={missing}, unexpected={unexpected}"
        )
    for connection_id, record in actual.items():
        schedule = record.get("schedule")
        schedule_type = record.get("scheduleType") or record.get("schedule_type")
        if isinstance(schedule, dict):
            schedule_type = (
                schedule.get("scheduleType")
                or schedule.get("schedule_type")
                or schedule.get("type")
                or schedule_type
            )
        if schedule_type is not None and str(schedule_type).casefold() != "manual":
            raise ExecutionError(
                f"Airbyte connection {connection_id} must use a manual schedule"
            )


@dataclass(frozen=True)
class Stage1Execution:
    connection_ids: tuple[str, ...]
    statuses: dict[str, str]
    job_ids: dict[str, int] = field(default_factory=dict)
    terraform_state_digest: str = ""
    runner_image: str = ""
    execution_started_at: str = ""
    execution_completed_at: str = ""
    terraform_input_tree_digest: str = ""
    workspace_dir: Path | None = None
    terraform_state_path: Path | None = None
    terraform_state_lineage: str = ""
    terraform_state_serial: int = -1
    terraform_connection_resources: tuple[tuple[str, str], ...] = ()


def validate_stage1_execution_receipt(
    execution: Stage1Execution,
) -> tuple[str, ...]:
    """Validate a Stage 1 receipt against current inputs and Terraform state.

    Each connection must have a distinct positive Airbyte job ID with a
    recorded ``succeeded`` status. This check requires no Airbyte credential.
    """

    if execution.workspace_dir is None or execution.terraform_state_path is None:
        raise ExecutionError(
            "Stage 1 execution receipt has no workspace/state provenance"
        )
    workspace = Path(execution.workspace_dir)
    state_path = Path(execution.terraform_state_path)
    try:
        workspace_status = workspace.lstat()
        canonical_workspace = workspace.resolve(strict=True)
    except OSError as exc:
        raise ExecutionError("Stage 1 submitted workspace is unreadable") from exc
    if (
        not workspace.is_absolute()
        or workspace != canonical_workspace
        or stat.S_ISLNK(workspace_status.st_mode)
        or not stat.S_ISDIR(workspace_status.st_mode)
    ):
        raise ExecutionError(
            "Stage 1 submitted workspace is not one canonical regular directory"
        )
    elt_dir = workspace / "elt"
    try:
        elt_status = elt_dir.lstat()
    except OSError as exc:
        raise ExecutionError("Stage 1 Terraform directory is unreadable") from exc
    if stat.S_ISLNK(elt_status.st_mode) or not stat.S_ISDIR(elt_status.st_mode):
        raise ExecutionError("Stage 1 Terraform directory is not a regular directory")
    expected_state_path = elt_dir / "terraform.tfstate"
    if state_path != expected_state_path:
        raise ExecutionError(
            "Stage 1 receipt names a state outside the submitted workspace"
        )

    identity = _terraform_state_identity(expected_state_path)
    if not identity.connection_ids or not identity.connection_resources:
        raise ExecutionError(
            "Stage 1 Terraform state has no exact Airbyte connection resource roster"
        )
    lineage = execution.terraform_state_lineage
    serial = execution.terraform_state_serial
    if (
        not isinstance(lineage, str)
        or not lineage.strip()
        or len(lineage) > 256
        or any(ord(char) < 32 for char in lineage)
        or isinstance(serial, bool)
        or not isinstance(serial, int)
        or serial < 0
    ):
        raise ExecutionError(
            "Stage 1 execution receipt has invalid Terraform lineage/serial"
        )
    try:
        receipt_resources = tuple(
            tuple(value) for value in execution.terraform_connection_resources
        )
        receipt_ids = tuple(execution.connection_ids)
    except TypeError as exc:
        raise ExecutionError(
            "Stage 1 execution receipt has an invalid connection roster"
        ) from exc
    if (
        identity.digest != execution.terraform_state_digest
        or identity.lineage != lineage
        or identity.serial != serial
        or identity.connection_resources != receipt_resources
        or identity.connection_ids != receipt_ids
    ):
        raise ExecutionError(
            "Stage 1 execution receipt disagrees with its Terraform state"
        )
    current_input_digest = _terraform_input_tree_digest(workspace)
    if current_input_digest != execution.terraform_input_tree_digest:
        raise ExecutionError(
            "Terraform submission identity changed after the captured execution"
        )

    statuses, job_ids = _validated_sync_maps(
        receipt_ids, execution.statuses, execution.job_ids
    )
    if any(
        not isinstance(status, str) or status.casefold() != "succeeded"
        for status in statuses.values()
    ):
        raise ExecutionError(
            "Stage 1 execution receipt has a non-successful Airbyte job"
        )
    # Assert the normalized map too, so exotic Mapping implementations cannot
    # smuggle a second view into downstream evidence creation.
    if job_ids != execution.job_ids or statuses != execution.statuses:
        raise ExecutionError("Stage 1 execution receipt maps are not canonical")
    return receipt_ids


#: Session-override profile keys the certified matrix does not cover; a
#: submitted profile carrying one fails the preflight closed.
_FORBIDDEN_SESSION_KEYS: dict[Destination, tuple[str, ...]] = {
    Destination.SNOWFLAKE: ("session_parameters",),
    Destination.DATABRICKS: ("session_properties",),
    Destination.REDSHIFT: ("search_path",),
}

#: Profile output keys the preflight verifies statically; a Jinja template in
#: any of them (or in a forbidden session key) cannot be verified and fails
#: closed.
_INSPECTED_OUTPUT_KEYS = ("type", "database", "dbname", "schema", "catalog")

#: Snowflake profiles may read only these scoped credential variables.
_SNOWFLAKE_ENV_PROFILE_FIELDS = {
    "account": "ELT_TASKGEN_SNOWFLAKE_ACCOUNT",
    "user": "ELT_TASKGEN_SNOWFLAKE_USER",
    "password": "ELT_TASKGEN_SNOWFLAKE_PASSWORD",
    "role": "ELT_TASKGEN_SNOWFLAKE_ROLE",
    "warehouse": "ELT_TASKGEN_SNOWFLAKE_WAREHOUSE",
}


@dataclass(frozen=True)
class Stage2Preflight:
    destination: Destination
    dbt_core_version: str
    adapter_version: str
    profile_name: str
    target_name: str
    namespace: str
    physical_container: str = ""


@dataclass(frozen=True)
class Stage2Execution:
    project_dir: Path
    profiles_dir: Path
    preflight: Stage2Preflight | None = None
    run_results_path: Path | None = None
    dbt_invocation_id: str = ""
    dbt_run_results_digest: str = ""
    runner_image: str = ""
    execution_started_at: str = ""
    execution_completed_at: str = ""
    dbt_input_tree_digest: str = ""
    dbt_expected_model_ids: tuple[str, ...] = ()
    dbt_observed_model_ids: tuple[str, ...] = ()


_DBT_RESOURCE_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("model-paths", ("models",)),
    ("seed-paths", ("seeds",)),
    ("snapshot-paths", ("snapshots",)),
    ("analysis-paths", ("analyses",)),
    ("test-paths", ("tests",)),
    ("macro-paths", ("macros",)),
)


def _dbt_submission_identity(
    project: Path, profiles: Path
) -> tuple[str, tuple[str, ...]]:
    """Capture the deterministic dbt input inventory and expected model ids."""

    project_file = project / "dbt_project.yml"
    project_bytes = _read_regular_file(
        project_file,
        label="dbt_project.yml",
        limit=_MAX_SUBMISSION_TREE_BYTES,
    )
    try:
        project_data = yaml.safe_load(project_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ExecutionError("dbt_project.yml is invalid") from exc
    if not isinstance(project_data, dict):
        raise ExecutionError("dbt_project.yml is not a mapping")
    project_name = project_data.get("name")
    if not isinstance(project_name, str) or not project_name.strip():
        raise ExecutionError("dbt_project.yml declares no project name")

    paths: dict[str, Path] = {}
    model_paths: set[Path] = set()
    skipped_directories = {".git", ".terraform", "target", "logs"}

    def skip_generated(path: Path) -> bool:
        # Only top-level harness/dbt outputs are ignored.  A submitted model
        # tree may legitimately contain a nested directory named ``target``;
        # skipping by basename anywhere would leave executable SQL unbound.
        return path.parent == project and (
            path.name in skipped_directories
            or path.name.startswith(".elt-taskgen-dbt-target-")
        )

    def project_directory(raw: str, *, setting: str) -> Path:
        requested = Path(raw)
        if requested.is_absolute():
            raise ExecutionError(f"dbt {setting} must be project-relative")
        # abspath normalizes ``..`` without resolving symlinks; the bounded
        # walker below can therefore still reject every symlink it encounters.
        candidate = Path(os.path.abspath(project / requested))
        try:
            candidate.relative_to(project)
        except ValueError:
            raise ExecutionError(f"dbt {setting} escaped the project") from None
        return candidate

    for setting, defaults in _DBT_RESOURCE_PATHS:
        configured = project_data.get(setting, defaults)
        if not isinstance(configured, (list, tuple)) or not all(
            isinstance(value, str) and value for value in configured
        ):
            raise ExecutionError(f"dbt_project.yml {setting} must be a string list")
        for raw in configured:
            resource_root = project_directory(raw, setting=setting)
            resource_files = _walk_regular_tree(
                resource_root,
                label=f"dbt {setting}",
                skip_directory=skip_generated,
            )
            for path in resource_files:
                relative = path.relative_to(project).as_posix()
                paths.setdefault(f"project/{relative}", path)
                if setting == "model-paths" and path.suffix.casefold() == ".sql":
                    model_paths.add(path)

    packages_path = project_data.get("packages-install-path", "dbt_packages")
    if not isinstance(packages_path, str) or not packages_path:
        raise ExecutionError(
            "dbt_project.yml packages-install-path must be a string"
        )
    installed_packages = project_directory(
        packages_path, setting="packages-install-path"
    )
    for path in _walk_regular_tree(
        installed_packages,
        label="dbt installed packages",
        skip_directory=skip_generated,
    ):
        paths.setdefault(
            f"project/{path.relative_to(project).as_posix()}", path
        )

    for name in (
        "packages.yml",
        "dependencies.yml",
        "package-lock.yml",
        "selectors.yml",
    ):
        path = project / name
        try:
            status = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ExecutionError(f"dbt submission file is unreadable: {path}") from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise ExecutionError(
                f"dbt submission file must be regular and non-symlink: {path}"
            )
        paths[f"project/{name}"] = path

    profiles_file = profiles / "profiles.yml"
    paths["profiles/profiles.yml"] = profiles_file
    # A valid project may deliberately use ``model-paths: ['.']``.  The
    # project document was already captured above; do not count/read it twice.
    paths.pop("project/dbt_project.yml", None)
    captured = _capture_input_files(
        paths,
        label="dbt submission",
        initial={"project/dbt_project.yml": project_bytes},
    )

    model_names: dict[str, Path] = {}
    for path in sorted(model_paths, key=lambda item: item.as_posix()):
        model_name = path.stem
        if model_name in model_names:
            raise ExecutionError(
                "dbt submission contains duplicate model name "
                f"{model_name!r}"
            )
        model_names[model_name] = path
    expected = tuple(
        sorted(f"model.{project_name}.{model_name}" for model_name in model_names)
    )
    return _captured_tree_digest(captured), expected


def _dbt_run_results(
    target_dir: Path, *, expected_model_ids: tuple[str, ...]
) -> tuple[Path, str, str, tuple[str, ...]]:
    """Validate and identify a fresh dbt ``run_results.json`` artifact."""

    target_dir = Path(target_dir)
    try:
        target_status = target_dir.lstat()
        target_resolved = target_dir.resolve(strict=True)
        target_resolved.relative_to(target_dir.parent.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ExecutionError("dbt target directory escaped the submitted project") from exc
    if not stat.S_ISDIR(target_status.st_mode) or stat.S_ISLNK(target_status.st_mode):
        raise ExecutionError("dbt target directory is not a regular directory")
    path = target_dir / "run_results.json"
    data = _read_regular_file(
        path,
        label="dbt run_results.json",
        limit=_MAX_DBT_RUN_RESULTS_BYTES,
    )
    payload = _json_no_duplicates(data, label="dbt run_results.json", path=path)
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    invocation_id = metadata.get("invocation_id") if isinstance(metadata, dict) else None
    if (
        not isinstance(invocation_id, str)
        or not invocation_id.strip()
        or len(invocation_id) > 256
        or any(ord(char) < 32 for char in invocation_id)
    ):
        raise ExecutionError(
            "dbt run_results.json has no valid metadata.invocation_id"
        )
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results:
        raise ExecutionError("dbt run_results.json has no model results")
    actual_model_ids: list[str] = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise ExecutionError(
                f"dbt run_results.json result {index} is not an object"
            )
        status_value = result.get("status")
        if (
            not isinstance(status_value, str)
            or status_value.strip().casefold() != "success"
        ):
            raise ExecutionError(
                f"dbt run_results.json result {index} did not succeed"
            )
        unique_id = result.get("unique_id")
        if not isinstance(unique_id, str) or not unique_id.strip():
            raise ExecutionError(
                f"dbt run_results.json result {index} has no unique_id"
            )
        actual_model_ids.append(unique_id)
    if len(set(actual_model_ids)) != len(actual_model_ids):
        raise ExecutionError("dbt run_results.json contains duplicate model results")
    if set(actual_model_ids) != set(expected_model_ids):
        missing = sorted(set(expected_model_ids) - set(actual_model_ids))
        unexpected = sorted(set(actual_model_ids) - set(expected_model_ids))
        raise ExecutionError(
            "dbt run_results.json model roster mismatch: "
            f"missing={missing}, unexpected={unexpected}"
        )
    observed_model_ids = tuple(sorted(actual_model_ids))
    return (
        path,
        invocation_id,
        hashlib.sha256(data).hexdigest(),
        observed_model_ids,
    )


def validate_stage2_execution_receipt(
    execution: Stage2Execution,
) -> tuple[str, ...]:
    """Revalidate a Stage 2 receipt against the harness contract.

    Require a fresh regular ``run_results.json``, unchanged input-tree identity,
    and exactly one successful result for each submitted model.
    """

    if execution.run_results_path is None:
        raise ExecutionError("Stage 2 execution captured no dbt run artifact")
    project = Path(execution.project_dir)
    profiles = Path(execution.profiles_dir)
    try:
        project_status = project.lstat()
        project_resolved = project.resolve(strict=True)
    except OSError as exc:
        raise ExecutionError("Stage 2 submitted project is unreadable") from exc
    if stat.S_ISLNK(project_status.st_mode) or not stat.S_ISDIR(
        project_status.st_mode
    ):
        raise ExecutionError("Stage 2 submitted project is not a regular directory")

    artifact = Path(execution.run_results_path)
    target_dir = artifact.parent
    target_prefix = ".elt-taskgen-dbt-target-"
    if (
        artifact.name != "run_results.json"
        or not target_dir.name.startswith(target_prefix)
        or len(target_dir.name) == len(target_prefix)
    ):
        raise ExecutionError(
            "dbt run_results.json did not come from a fresh harness target"
        )
    try:
        if target_dir.parent.resolve(strict=True) != project_resolved:
            raise ExecutionError(
                "dbt run_results.json did not come from the submitted project"
            )
    except OSError as exc:
        raise ExecutionError("dbt harness target is unreadable") from exc

    current_digest, current_expected = _dbt_submission_identity(project, profiles)
    expected = tuple(execution.dbt_expected_model_ids)
    observed_receipt = tuple(execution.dbt_observed_model_ids)
    if not expected or expected != tuple(sorted(set(expected))):
        raise ExecutionError(
            "Stage 2 execution receipt has no canonical expected model roster"
        )
    if current_digest != execution.dbt_input_tree_digest or current_expected != expected:
        raise ExecutionError(
            "dbt submission identity changed after the captured execution"
        )
    if observed_receipt != tuple(sorted(set(observed_receipt))):
        raise ExecutionError(
            "Stage 2 execution receipt has a non-canonical observed model roster"
        )

    path, invocation_id, digest, observed = _dbt_run_results(
        target_dir, expected_model_ids=expected
    )
    try:
        same_path = path.resolve(strict=True) == artifact.resolve(strict=True)
    except OSError:
        same_path = False
    if not same_path:
        raise ExecutionError("Stage 2 execution receipt names a different dbt artifact")
    if (
        invocation_id != execution.dbt_invocation_id
        or digest != execution.dbt_run_results_digest
        or observed != observed_receipt
    ):
        raise ExecutionError(
            "Stage 2 execution receipt disagrees with its dbt run artifact"
        )
    return observed


def _namespace_match(found: Any, expected: str) -> bool:
    """Trimmed, case-insensitive comparison of one profile namespace field.

    Deliberately NOT identifier-resolution certification: quoted-identifier
    and exact-case semantics stay with the destination evaluators. This check
    only refuses a profile that names a different namespace outright.
    """
    return (
        isinstance(found, str)
        and found.strip().casefold() == expected.strip().casefold()
    )


def _fixed_env_reference(value: Any, name: str) -> bool:
    if not isinstance(value, str):
        return False
    return re.fullmatch(
        r"\{\{\s*env_var\(\s*(['\"])"
        + re.escape(name)
        + r"\1\s*\)\s*\}\}",
        value.strip(),
    ) is not None


def stage2_preflight(
    work_dir: Path,
    project: Path,
    profiles: Path,
    *,
    runner: Runner,
    dbt: tuple[str, ...] = ("dbt",),
    target: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Stage2Preflight:
    """Validate the dbt runtime before executing models.

    Check workspace identity, pinned versions, profile shape and adapter,
    attempt namespace, and session overrides. Failures raise coded
    :class:`Stage2PreflightError` before ``dbt run``.
    """
    config_path = work_dir / "config.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise Stage2PreflightError(
            "config", f"installed workspace config is unreadable: {config_path}"
        ) from exc
    if not isinstance(config, dict):
        raise Stage2PreflightError(
            "config", f"installed workspace config is not a mapping: {config_path}"
        )
    try:
        destination = destination_from_config(config)
    except ValueError as exc:
        raise Stage2PreflightError("config", str(exc)) from None
    contract = destination_contract(destination)
    destination_values = (config.get(contract.config_section) or {}).get("config")
    if not isinstance(destination_values, dict):
        raise Stage2PreflightError(
            "config",
            f"installed config has no {contract.config_section}.config mapping",
        )
    namespace = destination_values.get(contract.logical_namespace_field)
    if not isinstance(namespace, str) or not namespace.strip():
        raise Stage2PreflightError(
            "config",
            f"installed config {contract.config_section}.config."
            f"{contract.logical_namespace_field} must be a non-empty string",
        )
    if contract.fixed_schema is not None and not _namespace_match(
        destination_values.get("schema"), contract.fixed_schema
    ):
        raise Stage2PreflightError(
            "config",
            f"installed config {contract.config_section}.config.schema must "
            f"name the fixed schema {contract.fixed_schema!r}",
        )
    container: str | None = None
    if contract.physical_container_field is not None:
        raw_container = destination_values.get(contract.physical_container_field)
        if not isinstance(raw_container, str) or not raw_container.strip():
            raise Stage2PreflightError(
                "config",
                f"installed config {contract.config_section}.config."
                f"{contract.physical_container_field} must be injected at "
                "installation time",
            )
        container = raw_container

    adapter_name, adapter_pin = DBT_ADAPTER_CONTRACTS[destination]
    try:
        version_result = runner.run(
            (*dbt, "--version"), cwd=project, env=env
        )
    except ProcessFailure as exc:
        raise Stage2PreflightError(
            "dbt-version", "dbt --version could not be executed"
        ) from exc
    core_match = re.search(r"installed:\s*(\d[\w.+-]*)", version_result.stdout)
    if core_match is None:
        raise Stage2PreflightError(
            "dbt-version", "dbt --version reported no installed core version"
        )
    core_version = core_match.group(1)
    if core_version != DBT_CORE_VERSION:
        raise Stage2PreflightError(
            "dbt-version",
            f"dbt-core must be {DBT_CORE_VERSION}, found {core_version}",
        )
    plugin_match = re.search(
        rf"^\s*-\s*{re.escape(adapter_name)}:\s*(\d[\w.+-]*)",
        version_result.stdout,
        re.MULTILINE,
    )
    if plugin_match is None:
        raise Stage2PreflightError(
            "dbt-version",
            f"dbt --version lists no {adapter_name} adapter plugin",
        )
    adapter_version = plugin_match.group(1)
    if adapter_version != adapter_pin:
        raise Stage2PreflightError(
            "dbt-version",
            f"dbt-{adapter_name} must be {adapter_pin}, found {adapter_version}",
        )

    try:
        project_data = yaml.safe_load(
            (project / "dbt_project.yml").read_text(encoding="utf-8")
        )
    except (OSError, yaml.YAMLError) as exc:
        raise Stage2PreflightError(
            "profile", "dbt_project.yml is unreadable"
        ) from exc
    profile_name = (
        project_data.get("profile") if isinstance(project_data, dict) else None
    )
    if not isinstance(profile_name, str) or not profile_name:
        raise Stage2PreflightError(
            "profile", "dbt_project.yml declares no profile name"
        )
    profiles_path = profiles / "profiles.yml"
    try:
        profiles_data = yaml.safe_load(profiles_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise Stage2PreflightError(
            "profile", f"profiles.yml is missing or unreadable: {profiles_path}"
        ) from exc
    if not isinstance(profiles_data, dict):
        raise Stage2PreflightError("profile", "profiles.yml is not a mapping")
    profile = profiles_data.get(profile_name)
    if not isinstance(profile, dict):
        raise Stage2PreflightError(
            "profile", f"profiles.yml has no {profile_name!r} profile"
        )
    target_name = target or profile.get("target")
    if not isinstance(target_name, str) or not target_name:
        raise Stage2PreflightError(
            "profile", f"profile {profile_name!r} declares no target"
        )
    outputs = profile.get("outputs")
    output = outputs.get(target_name) if isinstance(outputs, dict) else None
    if not isinstance(output, dict):
        raise Stage2PreflightError(
            "profile",
            f"profile {profile_name!r} has no output for target {target_name!r}",
        )
    for key in (*_INSPECTED_OUTPUT_KEYS, *_FORBIDDEN_SESSION_KEYS[destination]):
        value = output.get(key)
        if isinstance(value, str) and "{{" in value:
            raise Stage2PreflightError(
                "profile",
                f"profile output {key!r} is templated and cannot be verified "
                "statically; write the installed literal value",
            )
    if output.get("type") != adapter_name:
        raise Stage2PreflightError(
            "profile",
            f"profile output type must be {adapter_name!r}, "
            f"found {output.get('type')!r}",
        )

    if destination is Destination.SNOWFLAKE:
        invalid_connection_fields = sorted(
            field
            for field, env_name in _SNOWFLAKE_ENV_PROFILE_FIELDS.items()
            if not _fixed_env_reference(output.get(field), env_name)
        )
        if invalid_connection_fields:
            raise Stage2PreflightError(
                "profile",
                "Snowflake profile connection fields must use the fixed "
                "ELT_TASKGEN_SNOWFLAKE_* environment bindings; invalid fields: "
                f"{invalid_connection_fields}",
            )

    def require_namespace(field: str, expected: str) -> None:
        found = output.get(field)
        if not _namespace_match(found, expected):
            raise Stage2PreflightError(
                "namespace",
                f"profile output {field!r} must name the installed attempt "
                f"namespace {expected!r}, found {found!r}",
            )

    if destination is Destination.SNOWFLAKE:
        require_namespace("database", namespace)
        if contract.fixed_schema is not None:
            require_namespace("schema", contract.fixed_schema)
    elif destination is Destination.DATABRICKS:
        require_namespace("schema", namespace)
        if container is not None:
            require_namespace("catalog", container)
    else:
        require_namespace("schema", namespace)
        if container is not None:
            require_namespace("dbname", container)

    forbidden_present = sorted(
        key for key in _FORBIDDEN_SESSION_KEYS[destination] if key in output
    )
    if forbidden_present:
        raise Stage2PreflightError(
            "session",
            "certified runs use default session semantics; session overrides "
            f"are not certified ({forbidden_present})",
        )
    return Stage2Preflight(
        destination=destination,
        dbt_core_version=core_version,
        adapter_version=adapter_version,
        profile_name=profile_name,
        target_name=target_name,
        namespace=namespace,
        physical_container=str(container or ""),
    )


def run_stage1_submission(
    work_dir: Path,
    airbyte: AirbyteClient,
    *,
    runner: Runner | None = None,
    terraform: str = "terraform",
    workspace_id: str | None = None,
    poll_interval: float = 10.0,
    timeout: float = 3600.0,
) -> Stage1Execution:
    """Apply submitted Terraform, trigger its connections, and wait exactly.

    This is the executable equivalent of upstream ELT-Bench Stage 1 steps 1-8.
    The Airbyte client follows exact job IDs returned by the trigger calls, so
    a historical success for the same connection cannot produce a false pass.
    """
    work_dir = Path(work_dir).resolve()
    elt_dir = work_dir / "elt"
    main_tf = elt_dir / "main.tf"
    if not main_tf.is_file():
        raise ExecutionError(f"submitted Stage 1 has no {main_tf}")
    runner = runner or SubprocessRunner()
    execution_started_at = _utc_now()
    try:
        runner.run((terraform, "init", "-input=false"), cwd=elt_dir)
    finally:
        _harden_terraform_state(elt_dir)
    # Capture after init (which can materialize/update the provider lock) and
    # immediately before apply: this is the canonical input tree consumed by
    # the state-producing command.
    terraform_input_tree_digest = _terraform_input_tree_digest(work_dir)
    try:
        # A task can declare several independent Airbyte resources. Serialize
        # their provider calls so concurrent schema discovery cannot exhaust a
        # single-node control plane before Stage 1 has created its state.
        runner.run(
            (
                terraform,
                "apply",
                "-input=false",
                "-auto-approve",
                "-parallelism=1",
            ),
            cwd=elt_dir,
        )
    finally:
        _harden_terraform_state(elt_dir)
    if _terraform_input_tree_digest(work_dir) != terraform_input_tree_digest:
        raise ExecutionError("Terraform submission changed during apply")
    state_path = elt_dir / "terraform.tfstate"
    state_identity = _terraform_state_identity(state_path)
    ids = state_identity.connection_ids
    if not ids:
        raise ExecutionError("submitted Terraform created no Airbyte connections")
    if workspace_id is not None:
        if not str(workspace_id):
            raise ExecutionError("Airbyte workspace id cannot be empty")
        _validate_workspace_connections(airbyte, str(workspace_id), ids)
    receipt_method = getattr(airbyte, "trigger_and_wait_receipt", None)
    if callable(receipt_method):
        receipt = receipt_method(
            ids, poll_interval=poll_interval, timeout=timeout
        )
        statuses, job_ids = _validated_sync_receipt(ids, receipt)
    else:
        # Compatibility for small duck-typed clients. Such an execution cannot
        # create certification evidence because its job-id map remains empty.
        statuses = airbyte.trigger_and_wait(
            ids, poll_interval=poll_interval, timeout=timeout
        )
        job_ids = {}
    execution = Stage1Execution(
        connection_ids=ids,
        statuses=statuses,
        job_ids=job_ids,
        terraform_state_digest=state_identity.digest,
        runner_image=str(getattr(runner, "image", "")),
        execution_started_at=execution_started_at,
        execution_completed_at=_utc_now(),
        terraform_input_tree_digest=terraform_input_tree_digest,
        workspace_dir=work_dir,
        terraform_state_path=state_path,
        terraform_state_lineage=state_identity.lineage,
        terraform_state_serial=state_identity.serial,
        terraform_connection_resources=state_identity.connection_resources,
    )
    # A receipt-bearing Airbyte client is the provenance/certification path.
    # Re-read both workspace and state after the last sync completed, just as
    # Stage 2 re-reads its submitted tree and artifact before returning.
    if job_ids:
        validate_stage1_execution_receipt(execution)
    return execution


def rerun_stage1_syncs(
    work_dir: Path,
    airbyte: AirbyteClient,
    *,
    workspace_id: str,
    poll_interval: float = 10.0,
    timeout: float = 3600.0,
) -> Stage1Execution:
    """Trigger a second sync from validated Terraform state.

    This probes ``full_refresh_append`` without rerunning Terraform. The same
    connection IDs must remain the complete attempt workspace; trigger each
    once and follow its returned job ID.
    """

    root = Path(work_dir).resolve()
    execution_started_at = _utc_now()
    _harden_terraform_state(root / "elt")
    elt_dir = root / "elt"
    terraform_input_tree_digest = _terraform_input_tree_digest(root)
    state_path = elt_dir / "terraform.tfstate"
    state_identity = _terraform_state_identity(state_path)
    ids = state_identity.connection_ids
    if not ids:
        raise ExecutionError("submitted Terraform created no Airbyte connections")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise ExecutionError("Airbyte workspace id cannot be empty")
    _validate_workspace_connections(airbyte, workspace_id, ids)
    receipt_method = getattr(airbyte, "trigger_and_wait_receipt", None)
    if callable(receipt_method):
        receipt = receipt_method(
            ids, poll_interval=poll_interval, timeout=timeout
        )
        statuses, job_ids = _validated_sync_receipt(ids, receipt)
    else:
        statuses = airbyte.trigger_and_wait(
            ids, poll_interval=poll_interval, timeout=timeout
        )
        job_ids = {}
    execution = Stage1Execution(
        connection_ids=ids,
        statuses=statuses,
        job_ids=job_ids,
        terraform_state_digest=state_identity.digest,
        execution_started_at=execution_started_at,
        execution_completed_at=_utc_now(),
        terraform_input_tree_digest=terraform_input_tree_digest,
        workspace_dir=root,
        terraform_state_path=state_path,
        terraform_state_lineage=state_identity.lineage,
        terraform_state_serial=state_identity.serial,
        terraform_connection_resources=state_identity.connection_resources,
    )
    if job_ids:
        validate_stage1_execution_receipt(execution)
    return execution


def run_stage2_submission(
    work_dir: Path,
    *,
    runner: Runner | None = None,
    dbt: tuple[str, ...] = ("dbt",),
    project_dir: Path | None = None,
    profiles_dir: Path | None = None,
    target: str | None = None,
    env: Mapping[str, str] | None = None,
    capture_provenance: bool = False,
) -> Stage2Execution:
    """Run an already-authored dbt project for the Transform phase."""
    work_dir = Path(work_dir).resolve(strict=True)

    def workspace_path(value: Path | None, *, default: Path, label: str) -> Path:
        requested = Path(value) if value is not None else default
        if not requested.is_absolute():
            requested = work_dir / requested
        resolved = requested.resolve()
        try:
            resolved.relative_to(work_dir)
        except ValueError:
            raise ExecutionError(
                f"submitted Stage 2 {label} must remain inside the solver workspace"
            ) from None
        return resolved

    project = workspace_path(
        project_dir,
        default=work_dir / "elt",
        label="project directory",
    )
    profiles = workspace_path(
        profiles_dir,
        default=project,
        label="profiles directory",
    )
    if not (project / "dbt_project.yml").is_file():
        raise ExecutionError(f"submitted Stage 2 has no {project / 'dbt_project.yml'}")
    if not profiles.is_dir():
        raise ExecutionError(
            f"submitted Stage 2 profiles directory does not exist: {profiles}"
        )
    if not dbt:
        raise ExecutionError("dbt command cannot be empty")
    runner = runner or SubprocessRunner()
    execution_started_at = _utc_now()
    dbt_input_tree_digest, expected_model_ids = _dbt_submission_identity(
        project, profiles
    )
    preflight = stage2_preflight(
        work_dir,
        project,
        profiles,
        runner=runner,
        dbt=dbt,
        target=target,
        env=env,
    )
    # The Docker runner mounts ``work_dir`` at /workspace. Host-absolute paths
    # are meaningless inside that container, while paths relative to ``cwd``
    # work for both the Docker and plain subprocess runners.
    profiles_arg = Path(os.path.relpath(profiles, start=project)).as_posix()
    command = [
        *dbt,
        "run",
        "--project-dir",
        ".",
        "--profiles-dir",
        profiles_arg,
    ]
    if target:
        command.extend(("--target", target))
    target_dir: Path | None = None
    if capture_provenance:
        target_dir = Path(
            tempfile.mkdtemp(prefix=".elt-taskgen-dbt-target-", dir=project)
        )
        target_dir.chmod(0o700)
        command.extend(
            (
                "--target-path",
                Path(os.path.relpath(target_dir, start=project)).as_posix(),
            )
        )
    try:
        runner.run(tuple(command), cwd=project, env=env)
        post_digest, post_model_ids = _dbt_submission_identity(project, profiles)
        if (
            post_digest != dbt_input_tree_digest
            or post_model_ids != expected_model_ids
        ):
            raise ExecutionError("dbt submission changed during execution")
        if target_dir is None:
            return Stage2Execution(
                project_dir=project,
                profiles_dir=profiles,
                preflight=preflight,
                runner_image=str(getattr(runner, "image", "")),
                execution_started_at=execution_started_at,
                execution_completed_at=_utc_now(),
                dbt_input_tree_digest=dbt_input_tree_digest,
                dbt_expected_model_ids=expected_model_ids,
            )
        (
            run_results_path,
            invocation_id,
            run_results_digest,
            observed_model_ids,
        ) = _dbt_run_results(target_dir, expected_model_ids=expected_model_ids)
        receipt = Stage2Execution(
            project_dir=project,
            profiles_dir=profiles,
            preflight=preflight,
            run_results_path=run_results_path,
            dbt_invocation_id=invocation_id,
            dbt_run_results_digest=run_results_digest,
            runner_image=str(getattr(runner, "image", "")),
            execution_started_at=execution_started_at,
            execution_completed_at=_utc_now(),
            dbt_input_tree_digest=dbt_input_tree_digest,
            dbt_expected_model_ids=expected_model_ids,
            dbt_observed_model_ids=observed_model_ids,
        )
        validate_stage2_execution_receipt(receipt)
        return receipt
    except BaseException:
        if target_dir is not None:
            shutil.rmtree(target_dir, ignore_errors=True)
        raise

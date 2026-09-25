"""Process-compliance report for live-warehouse agent runs.

The stage-1 and stage-2 rewards score warehouse content only, matching upstream
ELT-Bench.  This module reports whether the agent also followed the process
rules the reward does not check:

* every Airbyte connection in the task workspace is declared in the agent's
  Terraform state (no connections created through the REST API);
* every expected raw table is fed by a declared connection;
* the agent's Terraform passes the offline intent compiler's policy checks;
* nothing outside ``elt/`` was created or changed in the agent's mount.

The report does not change the reward. A driver may gate on ``compliant``.
``DEFAULT_IGNORED_TOP_LEVEL`` names the runner-owned output directories at the
top of a mount; ``_SKIPPED_DIR_NAMES`` names build caches skipped at any depth.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_IGNORED_TOP_LEVEL = ("claude", "openhands", "codex", "swe", "spider", "logs")

_SKIPPED_DIR_NAMES = frozenset({"__pycache__", ".terraform", ".git", ".dbt", "target", "dbt_packages"})

_CONNECTION_ID = re.compile(r"^[0-9a-fA-F-]{36}$")


class ProcessComplianceError(RuntimeError):
    """A malformed input the check cannot interpret."""


@dataclass(frozen=True)
class TerraformIntentReport:
    reward: float | None
    error_codes: tuple[str, ...]
    policy_violations: tuple[str, ...]
    harness_error: str | None
    terraform_files: tuple[str, ...]


@dataclass(frozen=True)
class ProcessComplianceResult:
    compliant: bool
    violations: tuple[str, ...]
    terraform_state_path: str | None
    state_connections: dict[str, tuple[str, ...]]
    workspace_connections: dict[str, tuple[str, ...]]
    api_only_connections: tuple[str, ...]
    stale_state_connections: tuple[str, ...]
    expected_tables: tuple[str, ...]
    uncovered_tables: tuple[str, ...]
    files_outside_elt: tuple[str, ...]
    terraform_intent: TerraformIntentReport | None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _stream_tables(streams: Any, prefix: Any) -> tuple[str, ...]:
    """Destination table names a connection's selected streams produce."""
    names: list[str] = []
    prefix_text = prefix if isinstance(prefix, str) else ""
    if isinstance(streams, list):
        for stream in streams:
            if isinstance(stream, Mapping):
                name = stream.get("name")
                if isinstance(name, str) and name:
                    names.append(prefix_text + name)
    return tuple(sorted(set(names)))


def find_terraform_state(elt_dir: Path) -> Path | None:
    """Locate the agent's ``terraform.tfstate`` under ``elt/``.

    ``elt/terraform.tfstate`` wins; otherwise the first state file found in a
    subdirectory, skipping provider caches.  Symlinks are never followed.
    """
    elt_dir = Path(elt_dir)
    direct = elt_dir / "terraform.tfstate"
    if direct.is_file() and not direct.is_symlink():
        return direct
    candidates: list[Path] = []
    for path in sorted(elt_dir.rglob("terraform.tfstate")):
        if path.is_symlink() or not path.is_file():
            continue
        if any(part in _SKIPPED_DIR_NAMES for part in path.relative_to(elt_dir).parts):
            continue
        candidates.append(path)
    return candidates[0] if candidates else None


def terraform_state_connections(state_path: Path) -> dict[str, tuple[str, ...]]:
    """Map each ``airbyte_connection`` in a Terraform state to its tables."""
    try:
        payload = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProcessComplianceError(f"unreadable Terraform state: {state_path}") from exc
    resources = payload.get("resources") if isinstance(payload, Mapping) else None
    if not isinstance(resources, list):
        raise ProcessComplianceError(f"Terraform state has no resources list: {state_path}")
    connections: dict[str, tuple[str, ...]] = {}
    for resource in resources:
        if not isinstance(resource, Mapping) or resource.get("type") != "airbyte_connection":
            continue
        instances = resource.get("instances")
        if not isinstance(instances, list):
            continue
        for instance in instances:
            attributes = instance.get("attributes") if isinstance(instance, Mapping) else None
            if not isinstance(attributes, Mapping):
                continue
            connection_id = attributes.get("connection_id") or attributes.get("id")
            if not isinstance(connection_id, str) or not _CONNECTION_ID.match(connection_id):
                continue
            configurations = attributes.get("configurations")
            streams = configurations.get("streams") if isinstance(configurations, Mapping) else None
            connections[connection_id] = _stream_tables(streams, attributes.get("prefix"))
    return connections


def workspace_connection_streams(
    connections: Iterable[Mapping[str, Any]],
) -> dict[str, tuple[str, ...]]:
    """Map each connection an Airbyte workspace listing returns to its tables."""
    result: dict[str, tuple[str, ...]] = {}
    for connection in connections:
        if not isinstance(connection, Mapping):
            continue
        connection_id = connection.get("connectionId") or connection.get("connection_id")
        if not isinstance(connection_id, str) or not connection_id:
            continue
        configurations = connection.get("configurations")
        streams = configurations.get("streams") if isinstance(configurations, Mapping) else None
        result[connection_id] = _stream_tables(streams, connection.get("prefix"))
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def changed_files_outside_elt(
    agent_dir: Path,
    inputs_dir: Path,
    *,
    ignore_top_level: Sequence[str] = DEFAULT_IGNORED_TOP_LEVEL,
) -> tuple[str, ...]:
    """Files under the agent mount, outside ``elt/``, that are new or changed.

    ``inputs_dir`` is the task directory the runner copied into the mount.  A
    file present there but missing from the mount is not reported: runners
    deliberately withhold credential files.
    """
    agent_dir = Path(agent_dir)
    inputs_dir = Path(inputs_dir)
    ignored = set(ignore_top_level) | {"elt"}
    changed: list[str] = []
    for path in sorted(agent_dir.rglob("*")):
        relative = path.relative_to(agent_dir)
        if not relative.parts or relative.parts[0] in ignored:
            continue
        if any(part in _SKIPPED_DIR_NAMES for part in relative.parts):
            continue
        if path.is_symlink():
            changed.append(str(relative))
            continue
        if not path.is_file():
            continue
        original = inputs_dir / relative
        if not original.is_file() or original.is_symlink():
            changed.append(str(relative))
            continue
        if _sha256(original) != _sha256(path):
            changed.append(str(relative))
    return tuple(changed)


def terraform_files(elt_dir: Path) -> tuple[Path, ...]:
    """The agent's ``.tf`` files directly under ``elt/``, ``main.tf`` first."""
    elt_dir = Path(elt_dir)
    files = [
        path
        for path in sorted(elt_dir.glob("*.tf"))
        if path.is_file() and not path.is_symlink()
    ]
    files.sort(key=lambda path: (path.name != "main.tf", path.name))
    return tuple(files)


def terraform_intent_report(
    release_dir: Path,
    task_id: str,
    elt_dir: Path,
    *,
    destination: str | None = None,
) -> TerraformIntentReport:
    """Run the offline Terraform intent compiler over the agent's ``.tf`` files.

    The compiler reads one ``main.tf``; an agent that split its configuration
    across several files is graded on their concatenation, ``main.tf`` first.
    """
    from elt_taskgen.training.package import WorkspacePackageError, load_workspace_package
    from elt_taskgen.training.terraform_intent import (
        TERRAFORM_POLICY_VIOLATION_CODES,
        TerraformIntentHarnessError,
        evaluate_terraform_intent,
    )

    files = terraform_files(elt_dir)
    names = tuple(path.name for path in files)
    if not files:
        return TerraformIntentReport(
            reward=0.0,
            error_codes=("terraform_missing",),
            policy_violations=(),
            harness_error=None,
            terraform_files=names,
        )
    try:
        package = load_workspace_package(Path(release_dir), task_id, destination=destination)
    except WorkspacePackageError as exc:
        return TerraformIntentReport(None, (), (), f"package: {exc}", names)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            combined = Path(tmp) / "main.tf"
            combined.write_text(
                "\n\n".join(path.read_text(encoding="utf-8") for path in files),
                encoding="utf-8",
            )
            evaluation = evaluate_terraform_intent(package, combined)
    except TerraformIntentHarnessError as exc:
        return TerraformIntentReport(None, (), (), f"harness: {exc}", names)
    except (OSError, UnicodeError) as exc:
        return TerraformIntentReport(None, (), (), f"read: {exc}", names)
    codes = tuple(
        code.value if hasattr(code, "value") else str(code)
        for code in evaluation.error_codes
    )
    policy = tuple(
        code.value for code in TERRAFORM_POLICY_VIOLATION_CODES if code.value in codes
    )
    return TerraformIntentReport(
        reward=float(evaluation.reward),
        error_codes=codes,
        policy_violations=policy,
        harness_error=None,
        terraform_files=names,
    )


def evaluate_process_compliance(
    *,
    agent_dir: Path,
    inputs_dir: Path,
    expected_tables: Iterable[str],
    workspace_connections: Mapping[str, tuple[str, ...]],
    terraform_intent: TerraformIntentReport | None,
    ignore_top_level: Sequence[str] = DEFAULT_IGNORED_TOP_LEVEL,
) -> ProcessComplianceResult:
    """Combine the four checks into one report."""
    agent_dir = Path(agent_dir)
    elt_dir = agent_dir / "elt"
    violations: list[str] = []

    state_path = find_terraform_state(elt_dir) if elt_dir.is_dir() else None
    state_connections: dict[str, tuple[str, ...]] = {}
    if state_path is None:
        violations.append("terraform_state_missing")
    else:
        state_connections = terraform_state_connections(state_path)

    api_only = tuple(sorted(set(workspace_connections) - set(state_connections)))
    stale = tuple(sorted(set(state_connections) - set(workspace_connections)))
    if api_only:
        violations.append("connections_outside_terraform")

    expected = tuple(sorted({name for name in expected_tables if isinstance(name, str)}))
    covered = {
        table.casefold()
        for connection_id, tables in state_connections.items()
        if connection_id in workspace_connections
        for table in tables
    }
    uncovered = tuple(name for name in expected if name.casefold() not in covered)
    if uncovered:
        violations.append("expected_tables_not_fed_by_terraform_connections")

    outside = changed_files_outside_elt(agent_dir, inputs_dir, ignore_top_level=ignore_top_level)
    if outside:
        violations.append("files_outside_elt")

    if terraform_intent is not None:
        if terraform_intent.policy_violations:
            violations.append("terraform_policy_violation")
        if "terraform_missing" in terraform_intent.error_codes:
            violations.append("terraform_missing")

    return ProcessComplianceResult(
        compliant=not violations,
        violations=tuple(violations),
        terraform_state_path=str(state_path) if state_path else None,
        state_connections=dict(state_connections),
        workspace_connections=dict(workspace_connections),
        api_only_connections=api_only,
        stale_state_connections=stale,
        expected_tables=expected,
        uncovered_tables=uncovered,
        files_outside_elt=outside,
        terraform_intent=terraform_intent,
    )


__all__ = [
    "DEFAULT_IGNORED_TOP_LEVEL",
    "ProcessComplianceError",
    "ProcessComplianceResult",
    "TerraformIntentReport",
    "changed_files_outside_elt",
    "evaluate_process_compliance",
    "find_terraform_state",
    "terraform_files",
    "terraform_intent_report",
    "terraform_state_connections",
    "workspace_connection_streams",
]

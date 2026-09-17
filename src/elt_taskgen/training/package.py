"""Verified private package loading for ``workspace-v1`` attempts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from elt_taskgen.destinations import (
    Destination,
    destination_contract,
    destination_from_config,
)
from elt_taskgen.export import eltbench
from elt_taskgen.export.eltbench import (
    PRIVATE_AIRBYTE_CONNECTOR_CONTRACT,
    build_data_model,
    public_documentation,
    schema_csv,
)
from elt_taskgen.models import canonical_json
from elt_taskgen.semantic.package import SemanticPackage, load_semantic_package


class WorkspacePackageError(RuntimeError):
    """A combined release cannot safely back a workspace-v1 attempt."""


@dataclass(frozen=True)
class WorkspacePackage:
    """Hash-bound public task plus evaluator-private replay inputs."""

    semantic: SemanticPackage
    public_dir: Path
    documentation_path: Path
    destination: Destination
    logical_namespace: str
    airbyte_contract_path: Path
    airbyte_contract: Mapping[str, Any]
    airbyte_contract_sha256: str
    public_tree_sha256: str
    public_static_sha256: str

    @property
    def release_dir(self) -> Path:
        return self.semantic.release_dir

    @property
    def manifest(self):
        return self.semantic.manifest

    @property
    def task(self):
        return self.semantic.task

    @property
    def gold(self):
        return self.semantic.gold

    @property
    def task_id(self) -> str:
        return self.semantic.task.task_id

    def source_root(self, population):
        return self.semantic.source_root(population)


def _read_yaml_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise WorkspacePackageError(f"{label} is missing") from None
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise WorkspacePackageError(f"{label} is not readable safe YAML") from exc
    if not isinstance(payload, dict):
        raise WorkspacePackageError(f"{label} must be a YAML object")
    return payload


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise WorkspacePackageError(f"{label} is missing") from None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspacePackageError(f"{label} is not readable JSON") from exc
    if not isinstance(payload, dict):
        raise WorkspacePackageError(f"{label} must be a JSON object")
    return payload


def _deep_freeze(value: Any) -> Any:
    """Recursively freeze parsed package metadata after it is hash-bound."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def public_tree_digest(root: Path, *, exclude_elt: bool = False) -> str:
    """Byte identity for a regular, symlink-free public task tree."""

    if root.is_symlink() or not root.is_dir():
        raise WorkspacePackageError("public task is missing or is a symlink")
    records: list[dict[str, Any]] = []

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = path.relative_to(root)
            # Exclude solver-owned elt/ from the bounded static-integrity walk.
            if exclude_elt and relative.parts[0] == "elt":
                continue
            if path.is_symlink():
                raise WorkspacePackageError("public task contains a symlink")
            if path.is_dir():
                records.append({"kind": "directory", "path": relative.as_posix()})
                visit(path)
                continue
            if not path.is_file():
                raise WorkspacePackageError("public task contains a non-regular file")
            content = path.read_bytes()
            records.append(
                {
                    "kind": "file",
                    "path": relative.as_posix(),
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )

    try:
        visit(root)
    except OSError as exc:
        raise WorkspacePackageError("public task could not be hashed") from exc
    return hashlib.sha256(canonical_json(records).encode("utf-8")).hexdigest()


def _validate_public_semantics(public_dir: Path, semantic: SemanticPackage) -> Path:
    try:
        eltbench.assert_public_runtime_shape(public_dir)
    except ValueError as exc:
        raise WorkspacePackageError(str(exc)) from exc

    top_level_documentation = public_dir / "documentation.md"
    if top_level_documentation.exists() or top_level_documentation.is_symlink():
        raise WorkspacePackageError(
            "combined workspace must use documentation/README.md, not documentation.md"
        )
    documentation_path = public_dir / "documentation" / "README.md"
    try:
        documentation = documentation_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise WorkspacePackageError("public documentation/README.md is missing") from None
    except (OSError, UnicodeError) as exc:
        raise WorkspacePackageError("public documentation/README.md is unreadable") from exc
    expected_documentation = public_documentation(semantic.task)
    if expected_documentation not in documentation:
        raise WorkspacePackageError(
            "public documentation/README.md does not contain the TaskIR specification"
        )

    schemas_dir = public_dir / "schemas"
    if not schemas_dir.is_dir() or schemas_dir.is_symlink():
        raise WorkspacePackageError("public schemas directory is missing")
    expected_schema_names = {f"{table.name}.csv" for table in semantic.task.tables}
    actual_schema_names = {
        path.name for path in schemas_dir.iterdir() if path.is_file()
    }
    if actual_schema_names != expected_schema_names:
        raise WorkspacePackageError("public schemas do not exactly cover source tables")
    for table in semantic.task.tables:
        path = schemas_dir / f"{table.name}.csv"
        try:
            actual = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise WorkspacePackageError(f"public schema {path.name!r} is unreadable") from exc
        if actual != schema_csv(table):
            raise WorkspacePackageError(
                f"public schema {path.name!r} does not match the private TaskIR"
            )

    actual_model = _read_yaml_object(public_dir / "data_model.yaml", "data_model.yaml")
    if actual_model != build_data_model(semantic.task):
        raise WorkspacePackageError("public data_model.yaml does not match the private TaskIR")
    return documentation_path


def _validate_airbyte_contract(
    payload: Mapping[str, Any],
    *,
    destination: Destination,
    expected_source_keys: Mapping[str, str],
    logical_namespace: str,
) -> None:
    if payload.get("schema_version") != "1.0":
        raise WorkspacePackageError("private Airbyte contract has an unsupported schema")
    provider = payload.get("terraform_provider")
    if not isinstance(provider, Mapping) or provider.get("source") != "airbytehq/airbyte":
        raise WorkspacePackageError("private Airbyte contract has the wrong provider")
    if provider.get("version") != "0.6.5":
        raise WorkspacePackageError("private Airbyte contract has the wrong provider version")
    destination_payload = payload.get("destination")
    if not isinstance(destination_payload, Mapping):
        raise WorkspacePackageError("private Airbyte contract has no destination")
    contract = destination_contract(destination)
    if (
        destination_payload.get("key") != destination.value
        or destination_payload.get("definition_id") != contract.definition_id
        or destination_payload.get("connector_version") != contract.connector_version
    ):
        raise WorkspacePackageError("private Airbyte destination contract drifted")
    destination_configuration = destination_payload.get("configuration")
    if not isinstance(destination_configuration, Mapping):
        raise WorkspacePackageError("private Airbyte destination configuration is malformed")
    if (
        destination_configuration.get(contract.logical_namespace_field)
        != logical_namespace
    ):
        raise WorkspacePackageError(
            "private Airbyte destination namespace disagrees with public config"
        )
    if (
        contract.fixed_schema is not None
        and destination_configuration.get("schema") != contract.fixed_schema
    ):
        raise WorkspacePackageError("private Airbyte destination schema drifted")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise WorkspacePackageError("private Airbyte contract has no sources")
    selected: list[str] = []
    for source in sources:
        if not isinstance(source, Mapping):
            raise WorkspacePackageError("private Airbyte source contract is malformed")
        source_key = source.get("key")
        if not isinstance(source_key, str):
            raise WorkspacePackageError("private Airbyte source has no stable key")
        connection = source.get("connection")
        if not isinstance(connection, Mapping):
            raise WorkspacePackageError("private Airbyte source has no connection")
        configurations = connection.get("configurations")
        streams = (
            configurations.get("streams")
            if isinstance(configurations, Mapping) else None
        )
        if not isinstance(streams, list) or not streams:
            raise WorkspacePackageError("private Airbyte source has no selected streams")
        for stream in streams:
            if not isinstance(stream, Mapping) or not isinstance(stream.get("name"), str):
                raise WorkspacePackageError("private Airbyte stream is malformed")
            if stream.get("sync_mode") != "full_refresh_append":
                raise WorkspacePackageError("private Airbyte stream has unsupported sync mode")
            stream_name = str(stream["name"])
            if expected_source_keys.get(stream_name) != source_key:
                raise WorkspacePackageError(
                    "private Airbyte source backend disagrees with TaskIR"
                )
            selected.append(stream_name)
        if connection.get("namespace_definition") != "destination":
            raise WorkspacePackageError("private Airbyte source has wrong namespace behavior")
    if (
        len(selected) != len(set(selected))
        or set(selected) != set(expected_source_keys)
    ):
        raise WorkspacePackageError(
            "private Airbyte streams do not exactly cover source tables"
        )


def load_workspace_package(
    release_dir: Path,
    task_id: str,
    *,
    verify: bool = True,
) -> WorkspacePackage:
    """Load a verified schema-3 task for independently versioned workspace replay."""

    try:
        semantic = load_semantic_package(release_dir, task_id, verify=verify)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspacePackageError("combined release verification failed") from exc
    public_dir = semantic.release_dir / "public" / task_id
    if not public_dir.is_dir() or public_dir.is_symlink():
        raise WorkspacePackageError("combined public task directory is missing")
    documentation_path = _validate_public_semantics(public_dir, semantic)

    config = _read_yaml_object(public_dir / "config.yaml", "config.yaml")
    try:
        destination = destination_from_config(config)
        contract = destination_contract(destination)
        values = config[contract.config_section]["config"]
        logical_namespace = str(values[contract.logical_namespace_field])
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkspacePackageError("public destination configuration is invalid") from exc
    if not logical_namespace:
        raise WorkspacePackageError("public destination namespace is empty")
    manifest_destination = semantic.manifest.destinations.get(task_id)
    if manifest_destination != destination.value:
        raise WorkspacePackageError("manifest and public destination disagree")

    airbyte_contract_path = (
        semantic.release_dir
        / "private"
        / task_id
        / "answer_key"
        / PRIVATE_AIRBYTE_CONNECTOR_CONTRACT
    )
    airbyte_contract = _read_json_object(
        airbyte_contract_path,
        "private Airbyte connector contract",
    )
    backend_source_key = {
        "postgres": lambda table: "postgres",
        "mongodb": lambda table: "mongodb",
        "rest": lambda table: "custom_api",
        "s3": lambda table: "aws_s3",
        "files": lambda table: f"file_{table}",
    }
    expected_source_keys = {
        table.name: backend_source_key[
            semantic.task.backend_for(table.name).backend.value
        ](table.name)
        for table in semantic.task.tables
    }
    _validate_airbyte_contract(
        airbyte_contract,
        destination=destination,
        expected_source_keys=expected_source_keys,
        logical_namespace=logical_namespace,
    )
    airbyte_contract_sha256 = hashlib.sha256(
        canonical_json(airbyte_contract).encode("utf-8")
    ).hexdigest()
    return WorkspacePackage(
        semantic=semantic,
        public_dir=public_dir,
        documentation_path=documentation_path,
        destination=destination,
        logical_namespace=logical_namespace,
        airbyte_contract_path=airbyte_contract_path,
        airbyte_contract=_deep_freeze(airbyte_contract),
        airbyte_contract_sha256=airbyte_contract_sha256,
        public_tree_sha256=public_tree_digest(public_dir),
        public_static_sha256=public_tree_digest(public_dir, exclude_elt=True),
    )


__all__ = [
    "WorkspacePackage",
    "WorkspacePackageError",
    "load_workspace_package",
    "public_tree_digest",
]

"""Compile candidate Terraform into deterministic Airbyte intent offline.

Parsing invokes no Terraform, provider, Airbyte, or network service. Connections
and selected streams define source identity; resource labels do not. Invalid
private contracts raise ``TerraformIntentHarnessError`` while candidate errors
return stable public codes.
"""

from __future__ import annotations

import json

import atexit
import io
import os
import pickle
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import hcl2  # noqa: F401 — parsing runs in a child of THIS interpreter; fail here if absent
from lark import Tree

from elt_taskgen.destinations import Destination
from elt_taskgen.training.contract import MAX_WORKSPACE_FILE_BYTES, WorkspaceErrorCode
from elt_taskgen.training.package import WorkspacePackage
from elt_taskgen.training.workspace import (
    WorkspaceLifecycleError,
    _read_bounded_regular_file,
)


#: Largest candidate ``main.tf`` the compiler reads: the seal's per-file cap,
#: so a file the seal admits is never refused here for size alone.
MAX_MAIN_TF_BYTES = MAX_WORKSPACE_FILE_BYTES
#: Wall-clock deadline for parsing one candidate ``main.tf`` in its spawned,
#: killable worker (SoT T2: 10 s ``compile_terraform_intent``); a 2 MiB HCL
#: file can otherwise hold Lark for minutes (threat model A18).
TERRAFORM_PARSE_DEADLINE_SECONDS = 10.0
#: Worker-start deadline. It ends before candidate parse timing begins, so a
#: cold or failed start is a harness fault rather than ``PARSE_TIMEOUT``.
TERRAFORM_PARSE_WORKER_STARTUP_DEADLINE_SECONDS = 60.0
#: The one byte a worker writes after ``import hcl2`` and its warm-up parse
#: (the first parse builds the Lark parser) and before its first read of a
#: request; the supervisor arms no candidate deadline before it.
_PARSE_WORKER_READY = b"K"
#: The bounded-read refusals that are the CANDIDATE's ``PARSE``: an oversized
#: or symlinked ``main.tf`` is the artifact's doing. Every other refusal from
#: the reader (``WORKSPACE_NON_REGULAR_FILE`` for an EACCES/EIO/ENOENT-race
#: ``OSError``) is the harness failing to read a regular file it already
#: stat'ed, and is a harness fault.
_CANDIDATE_READ_CODES = frozenset(
    {
        WorkspaceErrorCode.WORKSPACE_SIZE_LIMIT,
        WorkspaceErrorCode.WORKSPACE_SYMLINK,
        WorkspaceErrorCode.WORKSPACE_DIGEST_MISMATCH,
    }
)
#: OS memory envelope for the parse worker (best-effort rlimit, Linux-hard).
TERRAFORM_PARSE_WORKER_RSS_LIMIT_MB = 1024
#: A payload the worker could not have held is a forged length claim.
MAX_PARSE_PAYLOAD_BYTES = TERRAFORM_PARSE_WORKER_RSS_LIMIT_MB * 1024 * 1024

AIRBYTE_PROVIDER_SOURCE = "airbytehq/airbyte"
AIRBYTE_PROVIDER_VERSION = "0.6.5"
ALLOWED_DATA_SOURCE_TYPES: frozenset[str] = frozenset()

_SOURCE_TYPE_TO_KIND: Mapping[str, str] = {
    "airbyte_source_postgres": "postgres",
    "airbyte_source_mongodb_v2": "mongodb",
    "airbyte_source_custom": "custom_api",
    "airbyte_source_s3": "aws_s3",
    "airbyte_source_file": "file",
}
_DESTINATION_TYPE_TO_KIND: Mapping[str, str] = {
    "airbyte_destination_snowflake": "snowflake",
    "airbyte_destination_databricks": "databricks",
    "airbyte_destination_redshift": "redshift",
}
_ALLOWED_RESOURCE_TYPES = frozenset(
    {*_SOURCE_TYPE_TO_KIND, *_DESTINATION_TYPE_TO_KIND, "airbyte_connection"}
)
_ALLOWED_FUNCTIONS = frozenset(
    {
        # The original custom connector uses jsonencode({}).  The remaining
        # functions are deterministic scalar normalization only; none reads
        # process, filesystem, or network state.
        "jsonencode",
        "lower",
        "upper",
        "trimspace",
        "tostring",
        "tonumber",
    }
)
_EXTERNAL_FUNCTIONS = frozenset(
    {
        "abspath",
        "file",
        "filebase64",
        "filebase64sha256",
        "fileexists",
        "fileset",
        "pathexpand",
        "plantimestamp",
        "templatefile",
        "timestamp",
        "uuid",
        "uuidv5",
    }
)
_DYNAMIC_TREE_NODES = frozenset(
    {
        "for_object_expr",
        "for_tuple_expr",
        "full_splat_expr_term",
        "index_expr_term",
        "legacy_index",
        "splat",
    }
)
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "access_key_id",
        "access_token",
        "api_key",
        "aws_access_key_id",
        "aws_secret_access_key",
        "bearer_token",
        "client_id",
        "client_secret",
        "password",
        "personal_access_token",
        "private_key",
        "private_key_password",
        "refresh_token",
        "secret",
        "secret_access_key",
        "secret_id",
        "service_account_json",
        "shared_key",
        "ssh_key",
        "sas_token",
        "token",
        "tunnel_user_password",
    }
)
_SOURCE_REQUIRED_CONFIGURATION: Mapping[str, frozenset[str]] = {
    "postgres": frozenset(
        {"host", "port", "database", "username", "password", "schemas"}
    ),
    "mongodb": frozenset({"database_config"}),
    "custom_api": frozenset(),
    "aws_s3": frozenset({"bucket", "streams"}),
    "file": frozenset({"dataset_name", "format", "provider", "url"}),
}


class TerraformIntentErrorCode(str, Enum):
    """Stable public failures that do not include private expected values."""

    PARSE = "terraform_parse_error"
    #: The candidate's HCL held the parser past its worker deadline: stopped,
    #: scored as a candidate failure, never a hang (roadmap Phase 0.A).
    PARSE_TIMEOUT = "terraform_parse_timeout"
    UNSUPPORTED_BLOCK = "terraform_unsupported_block"
    PROVIDER_FORBIDDEN = "terraform_provider_forbidden"
    PROVIDER_CONTRACT = "terraform_provider_contract"
    MODULE_FORBIDDEN = "terraform_module_forbidden"
    DATA_SOURCE_FORBIDDEN = "terraform_data_source_forbidden"
    PROVISIONER_FORBIDDEN = "terraform_provisioner_forbidden"
    EXEC_FORBIDDEN = "terraform_exec_forbidden"
    EXTERNAL_ACCESS = "terraform_external_access"
    DYNAMIC_CONSTRUCT = "terraform_dynamic_construct"
    HARDCODED_CREDENTIAL = "terraform_hardcoded_credential"
    DUPLICATE_RESOURCE = "terraform_duplicate_resource"
    UNRESOLVED_REFERENCE = "terraform_unresolved_reference"
    DEPENDENCY_CYCLE = "terraform_dependency_cycle"
    SOURCE_CONTRACT = "terraform_source_contract"
    DESTINATION_CONTRACT = "terraform_destination_contract"
    WORKSPACE_CONTRACT = "terraform_workspace_contract"
    CONNECTION_CONTRACT = "terraform_connection_contract"
    DUPLICATE_CONNECTION = "terraform_duplicate_connection"
    STREAM_CONTRACT = "terraform_stream_contract"
    SYNC_MODE = "terraform_sync_mode"
    NAMESPACE_CONTRACT = "terraform_namespace_contract"

    @property
    def policy_violation(self) -> bool:
        """Whether the failure is unsafe rather than merely incorrect."""

        return self in TERRAFORM_POLICY_VIOLATION_CODES


TERRAFORM_POLICY_VIOLATION_CODES: frozenset[TerraformIntentErrorCode] = frozenset(
    {
        TerraformIntentErrorCode.PROVIDER_FORBIDDEN,
        TerraformIntentErrorCode.MODULE_FORBIDDEN,
        TerraformIntentErrorCode.DATA_SOURCE_FORBIDDEN,
        TerraformIntentErrorCode.PROVISIONER_FORBIDDEN,
        TerraformIntentErrorCode.EXEC_FORBIDDEN,
        TerraformIntentErrorCode.EXTERNAL_ACCESS,
        TerraformIntentErrorCode.DYNAMIC_CONSTRUCT,
        TerraformIntentErrorCode.HARDCODED_CREDENTIAL,
    }
)


class TerraformIntentError(RuntimeError):
    """A candidate-authored HCL failure safe to turn into a policy label."""

    def __init__(self, code: TerraformIntentErrorCode):
        self.code = code
        super().__init__(code.value)


class TerraformIntentHarnessError(RuntimeError):
    """Private package/compiler defect; never convert this into agent reward."""


@dataclass(frozen=True, order=True)
class TerraformSelectedStream:
    source_key: str
    connector_kind: str
    stream_name: str
    sync_mode: str


@dataclass(frozen=True, order=True)
class TerraformSourceIntent:
    source_key: str
    connector_kind: str
    streams: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class TerraformDestinationIntent:
    kind: str
    logical_namespace: str
    physical_container: str
    schema: str
    warehouse_intent: bool = False
    http_path_intent: bool = False
    # Candidate-derived only. The pinned Databricks 4.0.2 destination has no
    # Volume name/path setting: it manages temporary Unity Catalog Volume use
    # internally. CREATE VOLUME permission is provisioned and certified by the
    # real-runtime harness, not expressed by solver Terraform.
    volume_intent: bool = False
    s3_staging_intent: bool = False


@dataclass(frozen=True, order=True)
class TerraformConnectionIntent:
    source_key: str
    destination_kind: str
    streams: tuple[tuple[str, str], ...]
    namespace_definition: str


@dataclass(frozen=True, order=True)
class TerraformDependencyEdge:
    node: str
    depends_on: str


@dataclass(frozen=True)
class TerraformIntentGraph:
    sources: tuple[TerraformSourceIntent, ...]
    destination: TerraformDestinationIntent
    connections: tuple[TerraformConnectionIntent, ...]
    dependency_edges: tuple[TerraformDependencyEdge, ...]

    @property
    def selected_streams(self) -> tuple[TerraformSelectedStream, ...]:
        return tuple(
            TerraformSelectedStream(
                source_key=source.source_key,
                connector_kind=source.connector_kind,
                stream_name=name,
                sync_mode=mode,
            )
            for source in self.sources
            for name, mode in source.streams
        )


@dataclass(frozen=True)
class TerraformIntentEvaluation:
    """One exact Terraform reward head and its public diagnostics."""

    reward: float
    graph: TerraformIntentGraph | None
    error_codes: tuple[TerraformIntentErrorCode, ...] = ()

    def __post_init__(self) -> None:
        if self.reward not in {0.0, 1.0}:
            raise ValueError("Terraform intent reward must be exactly zero or one")
        if (self.reward == 1.0) != (self.graph is not None and not self.error_codes):
            raise ValueError("Terraform intent evaluation fields disagree")

    @property
    def valid(self) -> bool:
        return self.reward == 1.0

    @property
    def policy_violation(self) -> bool:
        return any(code.policy_violation for code in self.error_codes)


@dataclass(frozen=True)
class _Resource:
    resource_type: str
    label: str
    body: Mapping[str, Any]

    @property
    def address(self) -> str:
        return f"{self.resource_type}.{self.label}"


def _candidate_error(code: TerraformIntentErrorCode) -> None:
    raise TerraformIntentError(code)


def _mapping(value: Any, code: TerraformIntentErrorCode) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _candidate_error(code)
    return value


def _sequence(value: Any, code: TerraformIntentErrorCode) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _candidate_error(code)
    return value


def _private_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TerraformIntentHarnessError("private Airbyte contract is malformed")
    return value


def _private_sequence(value: Any) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TerraformIntentHarnessError("private Airbyte contract is malformed")
    return value


def _flatten_labeled_blocks(value: Any) -> list[tuple[str, Mapping[str, Any]]]:
    """Flatten python-hcl2's ``[{label: body}, ...]`` representation."""

    blocks = _sequence(value, TerraformIntentErrorCode.PARSE)
    flattened: list[tuple[str, Mapping[str, Any]]] = []
    for raw in blocks:
        outer = _mapping(raw, TerraformIntentErrorCode.PARSE)
        if len(outer) != 1:
            _candidate_error(TerraformIntentErrorCode.PARSE)
        label, body = next(iter(outer.items()))
        if not isinstance(label, str):
            _candidate_error(TerraformIntentErrorCode.PARSE)
        flattened.append(
            (label, _mapping(body, TerraformIntentErrorCode.PARSE))
        )
    return flattened


def _resources(document: Mapping[str, Any]) -> tuple[_Resource, ...]:
    result: list[_Resource] = []
    seen: set[str] = set()
    for raw in document.get("resource", []):
        outer = _mapping(raw, TerraformIntentErrorCode.PARSE)
        if len(outer) != 1:
            _candidate_error(TerraformIntentErrorCode.PARSE)
        resource_type, labels = next(iter(outer.items()))
        for label, body in _mapping(
            labels, TerraformIntentErrorCode.PARSE
        ).items():
            if not isinstance(resource_type, str) or not isinstance(label, str):
                _candidate_error(TerraformIntentErrorCode.PARSE)
            resource = _Resource(
                resource_type,
                label,
                _mapping(body, TerraformIntentErrorCode.PARSE),
            )
            if resource.address in seen:
                _candidate_error(TerraformIntentErrorCode.DUPLICATE_RESOURCE)
            seen.add(resource.address)
            result.append(resource)
    return tuple(result)


def _identifier(tree: Tree) -> str:
    if tree.data != "identifier" or len(tree.children) != 1:
        return ""
    return str(tree.children[0])


def _literal_string(tree: Tree) -> str:
    if tree.data != "string":
        return ""
    values: list[str] = []
    for token in tree.scan_values(lambda item: not isinstance(item, Tree)):
        values.append(str(token))
    return "".join(values)


def _block_identity(block: Tree) -> tuple[str, tuple[str, ...]]:
    if block.data != "block" or not block.children:
        return "", ()
    block_type = _identifier(block.children[0]) if isinstance(block.children[0], Tree) else ""
    labels: list[str] = []
    for child in block.children[1:]:
        if not isinstance(child, Tree) or child.data == "body":
            break
        if child.data == "string":
            labels.append(_literal_string(child))
        elif child.data == "identifier":
            labels.append(_identifier(child))
    return block_type, tuple(labels)


def _traversal(tree: Tree) -> tuple[str, ...] | None:
    """Return one simple HCL attribute traversal from a parsed expression."""

    if tree.data == "identifier":
        name = _identifier(tree)
        return (name,) if name else None
    if tree.data == "expr_term" and len(tree.children) == 1:
        child = tree.children[0]
        return _traversal(child) if isinstance(child, Tree) else None
    if tree.data != "get_attr_expr_term" or len(tree.children) != 2:
        return None
    base = tree.children[0]
    attribute = tree.children[1]
    if not isinstance(base, Tree) or not isinstance(attribute, Tree):
        return None
    base_parts = _traversal(base)
    names = list(attribute.find_data("identifier"))
    if base_parts is None or len(names) != 1:
        return None
    name = _identifier(names[0])
    return (*base_parts, name) if name else None


def _collect_traversals(tree: Tree) -> tuple[tuple[str, ...], ...]:
    found: list[tuple[str, ...]] = []

    def visit(node: Tree) -> None:
        if node.data == "get_attr_expr_term":
            value = _traversal(node)
            if value is not None:
                found.append(value)
                return
        for child in node.children:
            if isinstance(child, Tree):
                visit(child)

    visit(tree)
    return tuple(found)


def _resource_dependencies_from_tree(tree: Tree) -> dict[str, set[str]]:
    dependencies: dict[str, set[str]] = {}
    for block in tree.find_data("block"):
        block_type, labels = _block_identity(block)
        if block_type != "resource" or len(labels) != 2:
            continue
        address = f"{labels[0]}.{labels[1]}"
        dependencies.setdefault(address, set())
        body = next(
            (
                child
                for child in block.children
                if isinstance(child, Tree) and child.data == "body"
            ),
            None,
        )
        if body is None:
            continue
        for traversal in _collect_traversals(body):
            if traversal and traversal[0].startswith("airbyte_") and len(traversal) >= 2:
                dependencies[address].add(".".join(traversal[:2]))
    return dependencies


def _validate_document_security(
    document: Mapping[str, Any], tree: Tree, resources: tuple[_Resource, ...]
) -> tuple[dict[str, Mapping[str, Any]], dict[str, set[str]]]:
    if "module" in document:
        _candidate_error(TerraformIntentErrorCode.MODULE_FORBIDDEN)
    if "data" in document:
        data_types = {label for label, _ in _flatten_labeled_blocks(document["data"])}
        if not data_types.issubset(ALLOWED_DATA_SOURCE_TYPES):
            _candidate_error(TerraformIntentErrorCode.DATA_SOURCE_FORBIDDEN)
    allowed_top_level = {"terraform", "provider", "resource", "variable"}
    if set(document) - allowed_top_level:
        _candidate_error(TerraformIntentErrorCode.UNSUPPORTED_BLOCK)

    for node in tree.iter_subtrees_topdown():
        node_name = str(node.data)
        if node_name in _DYNAMIC_TREE_NODES:
            _candidate_error(TerraformIntentErrorCode.DYNAMIC_CONSTRUCT)
        if node_name == "function_call":
            first = node.children[0] if node.children else None
            name = _identifier(first) if isinstance(first, Tree) else ""
            if name in _EXTERNAL_FUNCTIONS:
                _candidate_error(TerraformIntentErrorCode.EXTERNAL_ACCESS)
            if name not in _ALLOWED_FUNCTIONS:
                _candidate_error(TerraformIntentErrorCode.DYNAMIC_CONSTRUCT)

    variables: dict[str, Mapping[str, Any]] = {}
    for name, body in _flatten_labeled_blocks(document.get("variable", [])):
        if name in variables:
            _candidate_error(TerraformIntentErrorCode.DUPLICATE_RESOURCE)
        variables[name] = body

    declared_addresses = {resource.address for resource in resources}
    dependencies = _resource_dependencies_from_tree(tree)
    for traversal in _collect_traversals(tree):
        if not traversal:
            continue
        root = traversal[0]
        if root == "var":
            if len(traversal) != 2 or traversal[1] not in variables:
                _candidate_error(TerraformIntentErrorCode.UNRESOLVED_REFERENCE)
            continue
        if root.startswith("airbyte_"):
            if len(traversal) < 2 or ".".join(traversal[:2]) not in declared_addresses:
                _candidate_error(TerraformIntentErrorCode.UNRESOLVED_REFERENCE)
            continue
        _candidate_error(TerraformIntentErrorCode.UNRESOLVED_REFERENCE)

    for resource in resources:
        if resource.resource_type not in _ALLOWED_RESOURCE_TYPES:
            _candidate_error(TerraformIntentErrorCode.PROVIDER_FORBIDDEN)
        if "count" in resource.body or "for_each" in resource.body or "dynamic" in resource.body:
            _candidate_error(TerraformIntentErrorCode.DYNAMIC_CONSTRUCT)
        provisioners = resource.body.get("provisioner")
        if provisioners is not None:
            encoded = str(provisioners)
            if "local-exec" in encoded or "remote-exec" in encoded:
                _candidate_error(TerraformIntentErrorCode.EXEC_FORBIDDEN)
            _candidate_error(TerraformIntentErrorCode.PROVISIONER_FORBIDDEN)

    _validate_providers(document, variables)
    _validate_credentials(document, variables)
    _validate_dependency_graph(declared_addresses, dependencies)
    return variables, dependencies


def _validate_providers(
    document: Mapping[str, Any], variables: Mapping[str, Mapping[str, Any]]
) -> None:
    terraform_blocks = document.get("terraform")
    if not isinstance(terraform_blocks, list) or len(terraform_blocks) != 1:
        _candidate_error(TerraformIntentErrorCode.PROVIDER_CONTRACT)
    terraform = _mapping(terraform_blocks[0], TerraformIntentErrorCode.PROVIDER_CONTRACT)
    if "backend" in terraform or "cloud" in terraform:
        _candidate_error(TerraformIntentErrorCode.EXTERNAL_ACCESS)
    permitted = {"required_providers", "required_version"}
    if set(terraform) - permitted:
        _candidate_error(TerraformIntentErrorCode.PROVIDER_CONTRACT)
    required_blocks = terraform.get("required_providers")
    if not isinstance(required_blocks, list) or len(required_blocks) != 1:
        _candidate_error(TerraformIntentErrorCode.PROVIDER_CONTRACT)
    required = _mapping(
        required_blocks[0], TerraformIntentErrorCode.PROVIDER_CONTRACT
    )
    if set(required) != {"airbyte"}:
        _candidate_error(TerraformIntentErrorCode.PROVIDER_FORBIDDEN)
    airbyte = _mapping(required["airbyte"], TerraformIntentErrorCode.PROVIDER_CONTRACT)
    if (
        airbyte.get("source") != AIRBYTE_PROVIDER_SOURCE
        or airbyte.get("version") != AIRBYTE_PROVIDER_VERSION
    ):
        _candidate_error(TerraformIntentErrorCode.PROVIDER_CONTRACT)

    seen = 0
    for raw in document.get("provider", []):
        provider = _mapping(raw, TerraformIntentErrorCode.PARSE)
        if set(provider) != {"airbyte"}:
            _candidate_error(TerraformIntentErrorCode.PROVIDER_FORBIDDEN)
        body = _mapping(provider["airbyte"], TerraformIntentErrorCode.PROVIDER_CONTRACT)
        allowed_attributes = {
            "server_url",
            "client_id",
            "client_secret",
            "username",
            "password",
        }
        if set(body) - allowed_attributes:
            _candidate_error(TerraformIntentErrorCode.PROVIDER_CONTRACT)
        for field in ("client_id", "client_secret", "username", "password"):
            value = body.get(field)
            if value is None or value == "":
                continue
            variable = _exact_variable_reference(value)
            if variable is None or variable not in variables:
                _candidate_error(TerraformIntentErrorCode.HARDCODED_CREDENTIAL)
        seen += 1
    if seen > 1:
        _candidate_error(TerraformIntentErrorCode.DUPLICATE_RESOURCE)


def _exact_variable_reference(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("${") or not value.endswith("}"):
        return None
    content = value[2:-1]
    parts = content.split(".")
    if len(parts) != 2 or parts[0] != "var" or not all(
        part.replace("_", "a").replace("-", "a").isalnum() for part in parts
    ):
        return None
    return parts[1]


def _walk_items(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key), child
            yield from _walk_items(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk_items(child)


def _validate_credentials(
    document: Mapping[str, Any], variables: Mapping[str, Mapping[str, Any]]
) -> None:
    used_secret_variables: set[str] = set()
    for key, value in _walk_items(
        {
            "provider": document.get("provider", []),
            "resource": document.get("resource", []),
        }
    ):
        if key not in _SENSITIVE_FIELD_NAMES:
            continue
        if value is None or value == "":
            continue
        variable = _exact_variable_reference(value)
        if variable is None or variable not in variables:
            _candidate_error(TerraformIntentErrorCode.HARDCODED_CREDENTIAL)
        used_secret_variables.add(variable)
    for variable in used_secret_variables:
        if "default" in variables[variable]:
            _candidate_error(TerraformIntentErrorCode.HARDCODED_CREDENTIAL)


def _validate_dependency_graph(
    addresses: set[str], dependencies: Mapping[str, set[str]]
) -> None:
    for address, referenced in dependencies.items():
        if address not in addresses or not referenced.issubset(addresses):
            _candidate_error(TerraformIntentErrorCode.UNRESOLVED_REFERENCE)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(address: str) -> None:
        if address in visiting:
            _candidate_error(TerraformIntentErrorCode.DEPENDENCY_CYCLE)
        if address in visited:
            return
        visiting.add(address)
        for dependency in dependencies.get(address, set()):
            visit(dependency)
        visiting.remove(address)
        visited.add(address)

    for address in sorted(addresses):
        visit(address)


def _expected_source_kind(source_key: str) -> str:
    if source_key.startswith("file_") and len(source_key) > len("file_"):
        return "file"
    if source_key in {"postgres", "mongodb", "custom_api", "aws_s3"}:
        return source_key
    raise TerraformIntentHarnessError("private source routing is unsupported")


def expected_terraform_graph(package: WorkspacePackage) -> TerraformIntentGraph:
    """Derive the canonical graph from a verified private connector contract."""

    contract = _private_mapping(package.airbyte_contract)
    if "workspace_id" not in contract or not isinstance(
        contract.get("workspace_id"), str
    ):
        raise TerraformIntentHarnessError("private workspace contract is malformed")
    provider = _private_mapping(contract.get("terraform_provider"))
    if (
        provider.get("source") != AIRBYTE_PROVIDER_SOURCE
        or provider.get("version") != AIRBYTE_PROVIDER_VERSION
    ):
        raise TerraformIntentHarnessError("private provider contract drifted")
    raw_destination = _private_mapping(contract.get("destination"))
    destination_kind = raw_destination.get("key")
    if not isinstance(destination_kind, str) or destination_kind not in {
        item.value for item in Destination
    }:
        raise TerraformIntentHarnessError("private destination contract is malformed")
    if destination_kind != package.destination.value:
        raise TerraformIntentHarnessError("private destination identity drifted")
    destination_config = _private_mapping(raw_destination.get("configuration"))
    destination = _expected_destination_intent(
        destination_kind, package.logical_namespace, destination_config
    )

    sources: list[TerraformSourceIntent] = []
    connections: list[TerraformConnectionIntent] = []
    edges: list[TerraformDependencyEdge] = []
    seen_keys: set[str] = set()
    for raw_source in _private_sequence(contract.get("sources")):
        source = _private_mapping(raw_source)
        source_key = source.get("key")
        if not isinstance(source_key, str) or source_key in seen_keys:
            raise TerraformIntentHarnessError("private source identity is malformed")
        seen_keys.add(source_key)
        kind = _expected_source_kind(source_key)
        connection = _private_mapping(source.get("connection"))
        if connection.get("namespace_definition") != "destination":
            raise TerraformIntentHarnessError("private namespace contract drifted")
        configurations = _private_mapping(connection.get("configurations"))
        streams: list[tuple[str, str]] = []
        for raw_stream in _private_sequence(configurations.get("streams")):
            stream = _private_mapping(raw_stream)
            name = stream.get("name")
            mode = stream.get("sync_mode")
            if not isinstance(name, str) or mode != "full_refresh_append":
                raise TerraformIntentHarnessError("private stream contract drifted")
            streams.append((name, mode))
        normalized_streams = tuple(sorted(streams))
        if not normalized_streams or len(normalized_streams) != len(set(normalized_streams)):
            raise TerraformIntentHarnessError("private stream coverage is malformed")
        sources.append(TerraformSourceIntent(source_key, kind, normalized_streams))
        connections.append(
            TerraformConnectionIntent(
                source_key,
                destination_kind,
                normalized_streams,
                "destination",
            )
        )
        connection_node = f"connection:{source_key}->{destination_kind}"
        edges.extend(
            [
                TerraformDependencyEdge(connection_node, f"source:{source_key}"),
                TerraformDependencyEdge(
                    connection_node, f"destination:{destination_kind}"
                ),
            ]
        )
    if not sources:
        raise TerraformIntentHarnessError("private source contract is empty")
    return TerraformIntentGraph(
        sources=tuple(sorted(sources)),
        destination=destination,
        connections=tuple(sorted(connections)),
        dependency_edges=tuple(sorted(edges)),
    )


def _expected_destination_intent(
    kind: str, logical_namespace: str, configuration: Mapping[str, Any]
) -> TerraformDestinationIntent:
    _validate_private_destination_shape(kind, logical_namespace, configuration)
    schema = configuration.get("schema")
    database = configuration.get("database")
    if not isinstance(schema, str) or not isinstance(database, str):
        raise TerraformIntentHarnessError("private destination namespace is malformed")
    if kind == "snowflake":
        if database != logical_namespace or schema != "AIRBYTE_SCHEMA":
            raise TerraformIntentHarnessError("private Snowflake namespace drifted")
        return TerraformDestinationIntent(
            kind=kind,
            logical_namespace=logical_namespace,
            physical_container=database,
            schema=schema,
            warehouse_intent=True,
        )
    if schema != logical_namespace:
        raise TerraformIntentHarnessError("private destination namespace drifted")
    physical = database or "runtime_injected"
    if kind == "databricks":
        return TerraformDestinationIntent(
            kind=kind,
            logical_namespace=logical_namespace,
            physical_container=physical,
            schema=schema,
            http_path_intent=True,
        )
    if kind == "redshift":
        return TerraformDestinationIntent(
            kind=kind,
            logical_namespace=logical_namespace,
            physical_container=physical,
            schema=schema,
            s3_staging_intent=True,
        )
    raise TerraformIntentHarnessError("private destination kind is unsupported")


def _validate_private_destination_shape(
    kind: str, logical_namespace: str, configuration: Mapping[str, Any]
) -> None:
    """Fail label-ineligible when the sealed connector shape has drifted."""

    if kind == "snowflake":
        required = {
            "host",
            "role",
            "warehouse",
            "database",
            "schema",
            "username",
            "number_data_type",
            "credentials",
        }
        credentials = _private_mapping(configuration.get("credentials"))
        if (
            set(configuration) != required
            or configuration.get("database") != logical_namespace
            or configuration.get("schema") != "AIRBYTE_SCHEMA"
            or configuration.get("number_data_type") != "NUMBER(38,9)"
            or credentials.get("auth_type") != "Username and Password"
            or set(credentials) != {"auth_type", "password"}
        ):
            raise TerraformIntentHarnessError(
                "private Snowflake destination contract drifted"
            )
        runtime_fields = (
            configuration.get("host"),
            configuration.get("role"),
            configuration.get("warehouse"),
            configuration.get("username"),
            credentials.get("password"),
        )
    elif kind == "databricks":
        required = {
            "accept_terms",
            "authentication",
            "database",
            "hostname",
            "http_path",
            "port",
            "purge_staging_data",
            "schema",
        }
        authentication = _private_mapping(configuration.get("authentication"))
        if (
            set(configuration) != required
            or configuration.get("schema") != logical_namespace
            or configuration.get("accept_terms") is not True
            or configuration.get("port") != "443"
            or configuration.get("purge_staging_data") is not True
            or set(authentication) != {"auth_type", "client_id", "secret"}
            or authentication.get("auth_type") != "OAUTH"
        ):
            raise TerraformIntentHarnessError(
                "private Databricks destination contract drifted"
            )
        runtime_fields = (
            configuration.get("database"),
            configuration.get("hostname"),
            configuration.get("http_path"),
            authentication.get("client_id"),
            authentication.get("secret"),
        )
    elif kind == "redshift":
        required = {
            "database",
            "drop_cascade",
            "host",
            "password",
            "port",
            "schema",
            "uploading_method",
            "username",
        }
        upload = _private_mapping(configuration.get("uploading_method"))
        upload_required = {
            "access_key_id",
            "method",
            "purge_staging_data",
            "s3_bucket_name",
            "s3_bucket_path",
            "s3_bucket_region",
            "secret_access_key",
        }
        port = configuration.get("port")
        if (
            set(configuration) != required
            or configuration.get("schema") != logical_namespace
            or configuration.get("drop_cascade") is not False
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
            or set(upload) != upload_required
            or upload.get("method") != "S3 Staging"
            or upload.get("purge_staging_data") is not True
            or upload.get("s3_bucket_path") != f"elt-bench/{logical_namespace}"
        ):
            raise TerraformIntentHarnessError(
                "private Redshift destination contract drifted"
            )
        runtime_fields = (
            configuration.get("database"),
            configuration.get("host"),
            configuration.get("password"),
            configuration.get("username"),
            upload.get("access_key_id"),
            upload.get("s3_bucket_name"),
            upload.get("s3_bucket_region"),
            upload.get("secret_access_key"),
        )
    else:
        raise TerraformIntentHarnessError("private destination kind is unsupported")
    if any(not isinstance(value, str) for value in runtime_fields):
        raise TerraformIntentHarnessError(
            "private destination runtime configuration is malformed"
        )


def _exact_resource_reference(value: Any, attribute: str) -> str:
    if not isinstance(value, str) or not value.startswith("${") or not value.endswith("}"):
        _candidate_error(TerraformIntentErrorCode.UNRESOLVED_REFERENCE)
    parts = value[2:-1].split(".")
    if len(parts) != 3 or parts[2] != attribute or not all(parts[:2]):
        _candidate_error(TerraformIntentErrorCode.UNRESOLVED_REFERENCE)
    return ".".join(parts[:2])


def _intent_present(
    value: Any, variables: Mapping[str, Mapping[str, Any]] | set[str]
) -> bool:
    if isinstance(value, str) and value and not value.startswith("${"):
        return True
    variable = _exact_variable_reference(value)
    return variable is not None and variable in variables


def _runtime_variable(
    value: Any,
    variables: Mapping[str, Mapping[str, Any]] | set[str],
    code: TerraformIntentErrorCode,
) -> str:
    """Return one harness-injected variable, rejecting candidate-owned values.

    Runtime inputs are intentionally not represented by literal values in the
    normalized graph.  A default would still let the candidate choose the
    execution value, so runtime variables must be declared without one.
    """

    variable = _exact_variable_reference(value)
    if variable is None or variable not in variables:
        _candidate_error(code)
    if isinstance(variables, Mapping) and "default" in variables[variable]:
        _candidate_error(code)
    return variable


def _exact_keys(
    value: Mapping[str, Any],
    required: frozenset[str],
    code: TerraformIntentErrorCode,
) -> None:
    if set(value) != required:
        _candidate_error(code)


def _validate_workspace_bindings(
    sources: Mapping[str, _Resource],
    destinations: Mapping[str, _Resource],
    variables: Mapping[str, Mapping[str, Any]],
) -> None:
    """Require one declaration-backed workspace id across all endpoints."""

    workspace_variables = {
        _runtime_variable(
            resource.body.get("workspace_id"),
            variables,
            TerraformIntentErrorCode.WORKSPACE_CONTRACT,
        )
        for resource in (*sources.values(), *destinations.values())
    }
    if len(workspace_variables) != 1:
        _candidate_error(TerraformIntentErrorCode.WORKSPACE_CONTRACT)


def _validate_definition_id(body: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    expected_id = expected.get("definition_id")
    actual = body.get("definition_id")
    if isinstance(expected_id, str) and expected_id:
        if actual != expected_id:
            _candidate_error(TerraformIntentErrorCode.SOURCE_CONTRACT)
    elif not _intent_present(actual, set()):
        # Empty private ids (declarative REST definitions) must be supplied by
        # a declared runtime variable.  Declaration validation happens later.
        if _exact_variable_reference(actual) is None:
            _candidate_error(TerraformIntentErrorCode.SOURCE_CONTRACT)


def _candidate_streams(body: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    configurations = _mapping(
        body.get("configurations"), TerraformIntentErrorCode.CONNECTION_CONTRACT
    )
    streams: list[tuple[str, str]] = []
    for raw in _sequence(
        configurations.get("streams"), TerraformIntentErrorCode.STREAM_CONTRACT
    ):
        stream = _mapping(raw, TerraformIntentErrorCode.STREAM_CONTRACT)
        name = stream.get("name")
        mode = stream.get("sync_mode")
        if not isinstance(name, str) or not name:
            _candidate_error(TerraformIntentErrorCode.STREAM_CONTRACT)
        if mode != "full_refresh_append":
            _candidate_error(TerraformIntentErrorCode.SYNC_MODE)
        streams.append((name, mode))
    normalized = tuple(sorted(streams))
    if not normalized or len(normalized) != len(set(normalized)):
        _candidate_error(TerraformIntentErrorCode.STREAM_CONTRACT)
    return normalized


def _validate_source_configuration(
    kind: str,
    body: Mapping[str, Any],
    streams: tuple[tuple[str, str], ...],
    expected_source: Mapping[str, Any],
) -> None:
    expected_config = _private_mapping(expected_source.get("configuration"))
    configuration = body.get("configuration")
    if kind == "custom_api":
        _validate_custom_api_configuration(configuration, expected_config)
        return
    config = _mapping(
        _decode_jsonencode(configuration), TerraformIntentErrorCode.SOURCE_CONTRACT
    )
    if not _SOURCE_REQUIRED_CONFIGURATION[kind].issubset(config):
        _candidate_error(TerraformIntentErrorCode.SOURCE_CONTRACT)
    stream_names = {name for name, _ in streams}
    if kind == "postgres":
        # Passwords are deliberately variable-only and excluded here.  Every
        # routing-bearing field must still agree with the trusted contract so
        # the local loader cannot launder a connection aimed at another DB.
        for field in ("host", "port", "database", "username"):
            if config.get(field) != expected_config.get(field):
                _candidate_error(TerraformIntentErrorCode.SOURCE_CONTRACT)
        # Normalize private tuples and HCL lists before comparing the same
        # routing intent.
        actual_schemas = config.get("schemas")
        expected_schemas = expected_config.get("schemas")
        if (
            not isinstance(actual_schemas, Sequence)
            or isinstance(actual_schemas, (str, bytes, bytearray))
            or tuple(actual_schemas) != tuple(_private_sequence(expected_schemas))
        ):
            _candidate_error(TerraformIntentErrorCode.SOURCE_CONTRACT)
        return
    if kind == "mongodb":
        actual_connection, actual_databases = _mongodb_route(config)
        expected_database_config = _private_mapping(
            expected_config.get("database_config")
        )
        expected_connection = expected_database_config.get("connection_string")
        expected_databases = expected_database_config.get("databases")
        if (
            actual_connection != expected_connection
            or actual_databases != tuple(_private_sequence(expected_databases))
        ):
            _candidate_error(TerraformIntentErrorCode.SOURCE_CONTRACT)
        return
    if kind == "aws_s3":
        for field in ("bucket", "endpoint", "region_name"):
            if config.get(field) != expected_config.get(field):
                _candidate_error(TerraformIntentErrorCode.SOURCE_CONTRACT)
        actual_streams = _normalized_s3_streams(config.get("streams"))
        expected_streams = _normalized_s3_streams(expected_config.get("streams"))
        if (
            {name for name, _, _ in actual_streams} != stream_names
            or actual_streams != expected_streams
        ):
            _candidate_error(TerraformIntentErrorCode.SOURCE_CONTRACT)
        return
    if kind == "file":
        if config.get("dataset_name") not in stream_names:
            _candidate_error(TerraformIntentErrorCode.SOURCE_CONTRACT)
        expected_provider = _private_mapping(expected_config.get("provider"))
        actual_provider = _mapping(
            config.get("provider"), TerraformIntentErrorCode.SOURCE_CONTRACT
        )
        provider_ok = (
            actual_provider == expected_provider
            or (
                expected_provider == {"storage": "HTTPS"}
                and set(actual_provider) == {"https_public_web"}
                and actual_provider["https_public_web"] == {}
            )
        )
        if (
            config.get("dataset_name") != expected_config.get("dataset_name")
            or config.get("format") != expected_config.get("format")
            or config.get("url") != expected_config.get("url")
            or not provider_ok
        ):
            _candidate_error(TerraformIntentErrorCode.SOURCE_CONTRACT)


def _validate_custom_api_configuration(
    actual: Any, expected: Mapping[str, Any]
) -> None:
    """Normalize the certified declarative REST runtime configuration.

    REST endpoint/pagination semantics live in the separately hash-bound
    declarative manifest.  The current provider resource therefore has an
    exactly empty runtime configuration, represented by original ELT-Bench as
    ``jsonencode({})``.  Fail as a harness defect if a future private contract
    starts carrying runtime fields until an explicit normalizer is certified.
    """

    if expected:
        raise TerraformIntentHarnessError(
            "private custom API runtime configuration is outside the certified shape"
        )
    if actual == "${jsonencode({})}" or actual == {}:
        return
    _candidate_error(TerraformIntentErrorCode.SOURCE_CONTRACT)


_JSONENCODE_PREFIX = "${jsonencode("
_JSONENCODE_SUFFIX = ")}"


def _decode_jsonencode(value: Any) -> Any:
    """The mapping a ``jsonencode({...})`` body encodes, or ``value`` itself.

    The HCL parser keeps the call as the text ``${jsonencode(<json>)}`` with
    the object already serialized as JSON, so the body is recovered by
    decoding that JSON. Anything else is returned unchanged.
    """
    if (
        isinstance(value, str)
        and value.startswith(_JSONENCODE_PREFIX)
        and value.endswith(_JSONENCODE_SUFFIX)
    ):
        try:
            return json.loads(value[len(_JSONENCODE_PREFIX) : -len(_JSONENCODE_SUFFIX)])
        except ValueError:
            return value
    return value


def _mongodb_route(config: Mapping[str, Any]) -> tuple[Any, tuple[Any, ...]]:
    database_config = _mapping(
        config.get("database_config"), TerraformIntentErrorCode.SOURCE_CONTRACT
    )
    # The original typed provider shape wraps the self-managed settings; the
    # pinned private connector JSON is already normalized.  Both describe the
    # same route and therefore compile to one representation.
    if "self_managed_replica_set" in database_config:
        self_managed = _mapping(
            database_config["self_managed_replica_set"],
            TerraformIntentErrorCode.SOURCE_CONTRACT,
        )
        return self_managed.get("connection_string"), (
            self_managed.get("database"),
        )
    databases = database_config.get("databases")
    if not isinstance(databases, Sequence) or isinstance(
        databases, (str, bytes, bytearray)
    ):
        _candidate_error(TerraformIntentErrorCode.SOURCE_CONTRACT)
    return database_config.get("connection_string"), tuple(databases)


def _normalized_s3_streams(value: Any) -> tuple[tuple[str, tuple[str, ...], str], ...]:
    normalized: list[tuple[str, tuple[str, ...], str]] = []
    for raw in _sequence(value, TerraformIntentErrorCode.SOURCE_CONTRACT):
        stream = _mapping(raw, TerraformIntentErrorCode.SOURCE_CONTRACT)
        name = stream.get("name")
        globs = stream.get("globs")
        if not isinstance(name, str):
            _candidate_error(TerraformIntentErrorCode.SOURCE_CONTRACT)
        glob_values = tuple(
            str(item)
            for item in _sequence(globs, TerraformIntentErrorCode.SOURCE_CONTRACT)
        )
        format_value = _mapping(
            stream.get("format"), TerraformIntentErrorCode.SOURCE_CONTRACT
        )
        if "filetype" in format_value:
            filetype = format_value.get("filetype")
        else:
            format_keys = [key for key in format_value if key.endswith("_format")]
            filetype = (
                format_keys[0][: -len("_format")]
                if len(format_keys) == 1
                else None
            )
        if not isinstance(filetype, str):
            _candidate_error(TerraformIntentErrorCode.SOURCE_CONTRACT)
        normalized.append((name, glob_values, filetype))
    if len(normalized) != len(set(normalized)):
        _candidate_error(TerraformIntentErrorCode.SOURCE_CONTRACT)
    return tuple(sorted(normalized))


def _candidate_destination(
    resource: _Resource,
    expected: TerraformDestinationIntent,
    expected_payload: Mapping[str, Any],
    variables: Mapping[str, Mapping[str, Any]],
) -> TerraformDestinationIntent:
    kind = _DESTINATION_TYPE_TO_KIND[resource.resource_type]
    if kind != expected.kind:
        _candidate_error(TerraformIntentErrorCode.DESTINATION_CONTRACT)
    if resource.body.get("definition_id") != expected_payload.get("definition_id"):
        _candidate_error(TerraformIntentErrorCode.DESTINATION_CONTRACT)
    config = _mapping(
        resource.body.get("configuration"),
        TerraformIntentErrorCode.DESTINATION_CONTRACT,
    )
    if config.get("schema") != expected.schema:
        _candidate_error(TerraformIntentErrorCode.NAMESPACE_CONTRACT)
    expected_config = _private_mapping(expected_payload.get("configuration"))
    actual_database = config.get("database")
    if expected.kind == "snowflake":
        _exact_keys(
            config,
            frozenset(
                {
                    "host",
                    "role",
                    "warehouse",
                    "database",
                    "schema",
                    "number_data_type",
                    "credentials",
                    "username",
                }
            ),
            TerraformIntentErrorCode.DESTINATION_CONTRACT,
        )
        if actual_database != expected.physical_container:
            _candidate_error(TerraformIntentErrorCode.NAMESPACE_CONTRACT)
        if config.get("number_data_type") != expected_config.get("number_data_type"):
            _candidate_error(TerraformIntentErrorCode.DESTINATION_CONTRACT)
        # Provider 0.6.5 (ELT-Bench's pinned version, see its
        # documentation/destination_snowflake.md and example/retails/main.tf)
        # takes `username` as a required top-level configuration attribute;
        # only the password sits under `credentials.username_and_password`.
        # A submission that nests the username there fails `terraform apply`.
        for field in ("host", "role", "warehouse", "username"):
            _runtime_variable(
                config.get(field), variables, TerraformIntentErrorCode.DESTINATION_CONTRACT
            )
        private_credentials = _private_mapping(expected_config.get("credentials"))
        if private_credentials.get("auth_type") != "Username and Password":
            raise TerraformIntentHarnessError(
                "private Snowflake authentication contract drifted"
            )
        credentials = _mapping(
            config.get("credentials"), TerraformIntentErrorCode.DESTINATION_CONTRACT
        )
        _exact_keys(
            credentials,
            frozenset({"username_and_password"}),
            TerraformIntentErrorCode.DESTINATION_CONTRACT,
        )
        username_password = _mapping(
            credentials.get("username_and_password"),
            TerraformIntentErrorCode.DESTINATION_CONTRACT,
        )
        _exact_keys(
            username_password,
            frozenset({"password"}),
            TerraformIntentErrorCode.DESTINATION_CONTRACT,
        )
        _runtime_variable(
            username_password.get("password"),
            variables,
            TerraformIntentErrorCode.DESTINATION_CONTRACT,
        )
        return expected
    if expected.kind == "databricks":
        _exact_keys(
            config,
            frozenset(
                {
                    "accept_terms",
                    "authentication",
                    "database",
                    "hostname",
                    "http_path",
                    "port",
                    "purge_staging_data",
                    "schema",
                }
            ),
            TerraformIntentErrorCode.DESTINATION_CONTRACT,
        )
        if (
            config.get("accept_terms") is not True
            or config.get("port") != expected_config.get("port")
            or config.get("purge_staging_data") is not True
        ):
            _candidate_error(TerraformIntentErrorCode.DESTINATION_CONTRACT)
        for field in ("database", "hostname", "http_path"):
            _runtime_variable(
                config.get(field), variables, TerraformIntentErrorCode.DESTINATION_CONTRACT
            )
        private_authentication = _private_mapping(
            expected_config.get("authentication")
        )
        if private_authentication.get("auth_type") != "OAUTH":
            raise TerraformIntentHarnessError(
                "private Databricks authentication contract drifted"
            )
        authentication = _mapping(
            config.get("authentication"), TerraformIntentErrorCode.DESTINATION_CONTRACT
        )
        _exact_keys(
            authentication,
            frozenset({"oauth"}),
            TerraformIntentErrorCode.DESTINATION_CONTRACT,
        )
        oauth = _mapping(
            authentication.get("oauth"), TerraformIntentErrorCode.DESTINATION_CONTRACT
        )
        _exact_keys(
            oauth,
            frozenset({"client_id", "secret"}),
            TerraformIntentErrorCode.DESTINATION_CONTRACT,
        )
        for field in ("client_id", "secret"):
            _runtime_variable(
                oauth.get(field),
                variables,
                TerraformIntentErrorCode.DESTINATION_CONTRACT,
            )
        return expected
    _exact_keys(
        config,
        frozenset(
            {
                "database",
                "drop_cascade",
                "host",
                "password",
                "port",
                "schema",
                "uploading_method",
                "username",
            }
        ),
        TerraformIntentErrorCode.DESTINATION_CONTRACT,
    )
    if (
        config.get("drop_cascade") is not False
        or config.get("port") != expected_config.get("port")
    ):
        _candidate_error(TerraformIntentErrorCode.DESTINATION_CONTRACT)
    for field in ("database", "host", "username", "password"):
        _runtime_variable(
            config.get(field), variables, TerraformIntentErrorCode.DESTINATION_CONTRACT
        )
    upload_wrapper = _mapping(
        config.get("uploading_method"),
        TerraformIntentErrorCode.DESTINATION_CONTRACT,
    )
    # Provider 0.6.5 requires Redshift's upload strategy under the
    # `awss3_staging` branch, not the connector API's flat shape.
    _exact_keys(
        upload_wrapper,
        frozenset({"awss3_staging"}),
        TerraformIntentErrorCode.DESTINATION_CONTRACT,
    )
    upload = _mapping(
        upload_wrapper["awss3_staging"],
        TerraformIntentErrorCode.DESTINATION_CONTRACT,
    )
    required_staging = {
        "access_key_id",
        "secret_access_key",
        "s3_bucket_name",
        "s3_bucket_path",
        "s3_bucket_region",
        "purge_staging_data",
    }
    _exact_keys(
        upload,
        frozenset(required_staging),
        TerraformIntentErrorCode.DESTINATION_CONTRACT,
    )
    if upload.get("purge_staging_data") is not True:
        _candidate_error(TerraformIntentErrorCode.DESTINATION_CONTRACT)
    expected_upload = _private_mapping(expected_config.get("uploading_method"))
    for field in ("s3_bucket_path",):
        if upload.get(field) != expected_upload.get(field):
            _candidate_error(TerraformIntentErrorCode.DESTINATION_CONTRACT)
    expected_region = expected_upload.get("s3_bucket_region")
    if isinstance(expected_region, str) and expected_region:
        if upload.get("s3_bucket_region") != expected_region:
            _candidate_error(TerraformIntentErrorCode.DESTINATION_CONTRACT)
    else:
        _runtime_variable(
            upload.get("s3_bucket_region"),
            variables,
            TerraformIntentErrorCode.DESTINATION_CONTRACT,
        )
    for field in ("access_key_id", "secret_access_key", "s3_bucket_name"):
        _runtime_variable(
            upload.get(field), variables, TerraformIntentErrorCode.DESTINATION_CONTRACT
        )
    return expected


def _compile_candidate_graph(
    package: WorkspacePackage,
    document: Mapping[str, Any],
    tree: Tree,
    expected: TerraformIntentGraph,
) -> TerraformIntentGraph:
    resources = _resources(document)
    variables, raw_dependencies = _validate_document_security(
        document, tree, resources
    )
    _validate_provider_endpoint(package, document, variables)
    source_resources = {
        resource.address: resource
        for resource in resources
        if resource.resource_type in _SOURCE_TYPE_TO_KIND
    }
    destination_resources = {
        resource.address: resource
        for resource in resources
        if resource.resource_type in _DESTINATION_TYPE_TO_KIND
    }
    connection_resources = [
        resource for resource in resources if resource.resource_type == "airbyte_connection"
    ]
    _validate_workspace_bindings(source_resources, destination_resources, variables)
    if len(destination_resources) != 1:
        _candidate_error(TerraformIntentErrorCode.DESTINATION_CONTRACT)
    destination_address, destination_resource = next(iter(destination_resources.items()))
    expected_destination_payload = _private_mapping(
        package.airbyte_contract.get("destination")
    )
    destination = _candidate_destination(
        destination_resource,
        expected.destination,
        expected_destination_payload,
        variables,
    )

    private_sources = {
        str(_private_mapping(raw).get("key")): _private_mapping(raw)
        for raw in _private_sequence(package.airbyte_contract.get("sources"))
    }
    expected_by_signature: dict[
        tuple[str, tuple[tuple[str, str], ...]], list[TerraformSourceIntent]
    ] = {}
    definition_kinds: dict[str, str] = {}
    for source in expected.sources:
        expected_by_signature.setdefault(
            (source.connector_kind, source.streams), []
        ).append(source)
        if source.connector_kind != "custom_api":
            private_definition = private_sources.get(source.source_key, {}).get(
                "definition_id"
            )
            if isinstance(private_definition, str) and private_definition:
                definition_kinds[private_definition] = source.connector_kind

    matched_sources: list[TerraformSourceIntent] = []
    matched_connections: list[TerraformConnectionIntent] = []
    matched_by_address: dict[str, str] = {}
    connection_by_address: dict[str, str] = {}
    used_source_keys: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for connection_resource in connection_resources:
        body = connection_resource.body
        source_address = _exact_resource_reference(body.get("source_id"), "source_id")
        actual_destination_address = _exact_resource_reference(
            body.get("destination_id"), "destination_id"
        )
        if source_address not in source_resources or actual_destination_address != destination_address:
            _candidate_error(TerraformIntentErrorCode.CONNECTION_CONTRACT)
        pair = (source_address, actual_destination_address)
        if pair in seen_pairs:
            _candidate_error(TerraformIntentErrorCode.DUPLICATE_CONNECTION)
        seen_pairs.add(pair)
        if body.get("namespace_definition") != "destination":
            _candidate_error(TerraformIntentErrorCode.NAMESPACE_CONTRACT)
        streams = _candidate_streams(body)
        source_resource = source_resources[source_address]
        kind = _SOURCE_TYPE_TO_KIND[source_resource.resource_type]
        if kind == "custom_api":
            # The generic resource is also the only shape that applies for a
            # connector whose current spec the provider's typed resource
            # cannot express (source-mongodb-v2 takes a `databases` list the
            # typed resource lacks). Such a source names the connector by its
            # literal definition id, which the private contract also carries.
            kind = definition_kinds.get(
                str(source_resource.body.get("definition_id")), kind
            )
        matches = [
            item
            for item in expected_by_signature.get((kind, streams), [])
            if item.source_key not in used_source_keys
        ]
        if len(matches) != 1:
            _candidate_error(TerraformIntentErrorCode.STREAM_CONTRACT)
        source_intent = matches[0]
        used_source_keys.add(source_intent.source_key)
        matched_by_address[source_address] = source_intent.source_key
        connection_by_address[connection_resource.address] = source_intent.source_key
        private_source = private_sources.get(source_intent.source_key)
        if private_source is None:
            raise TerraformIntentHarnessError("private source lookup failed")
        _validate_definition_id(source_resource.body, private_source)
        definition_value = source_resource.body.get("definition_id")
        definition_variable = _exact_variable_reference(definition_value)
        if definition_variable is not None and definition_variable not in variables:
            _candidate_error(TerraformIntentErrorCode.UNRESOLVED_REFERENCE)
        _validate_source_configuration(
            kind, source_resource.body, streams, private_source
        )
        matched_sources.append(source_intent)
        matched_connections.append(
            TerraformConnectionIntent(
                source_key=source_intent.source_key,
                destination_kind=destination.kind,
                streams=streams,
                namespace_definition="destination",
            )
        )
    if (
        len(connection_resources) != len(expected.connections)
        or set(source_resources) != set(matched_by_address)
        or used_source_keys != {source.source_key for source in expected.sources}
    ):
        _candidate_error(TerraformIntentErrorCode.CONNECTION_CONTRACT)

    semantic_address: dict[str, str] = {
        destination_address: f"destination:{destination.kind}"
    }
    semantic_address.update(
        {
            address: f"source:{source_key}"
            for address, source_key in matched_by_address.items()
        }
    )
    semantic_address.update(
        {
            address: f"connection:{source_key}->{destination.kind}"
            for address, source_key in connection_by_address.items()
        }
    )
    edges: set[TerraformDependencyEdge] = set()
    for address, dependencies in raw_dependencies.items():
        if address not in semantic_address:
            continue
        for dependency in dependencies:
            if dependency not in semantic_address:
                _candidate_error(TerraformIntentErrorCode.CONNECTION_CONTRACT)
            edges.add(
                TerraformDependencyEdge(
                    semantic_address[address], semantic_address[dependency]
                )
            )
    graph = TerraformIntentGraph(
        sources=tuple(sorted(matched_sources)),
        destination=destination,
        connections=tuple(sorted(matched_connections)),
        dependency_edges=tuple(sorted(edges)),
    )
    if graph != expected:
        _candidate_error(TerraformIntentErrorCode.CONNECTION_CONTRACT)
    return graph


def _validate_provider_endpoint(
    package: WorkspacePackage,
    document: Mapping[str, Any],
    variables: set[str],
) -> None:
    provider_configuration = _private_mapping(
        _private_mapping(package.airbyte_contract.get("terraform_provider")).get(
            "configuration", {}
        )
    )
    expected_server_url = provider_configuration.get("server_url", "")
    for raw in document.get("provider", []):
        body = _mapping(
            _mapping(raw, TerraformIntentErrorCode.PROVIDER_CONTRACT).get("airbyte"),
            TerraformIntentErrorCode.PROVIDER_CONTRACT,
        )
        if "server_url" not in body:
            continue
        actual = body.get("server_url")
        if isinstance(expected_server_url, str) and expected_server_url and actual == expected_server_url:
            continue
        if _intent_present(actual, variables) and _exact_variable_reference(actual) is not None:
            continue
        _candidate_error(TerraformIntentErrorCode.PROVIDER_CONTRACT)


#: Isolated ``hcl2`` worker using a length-prefixed stdin/stdout protocol.
#: One worker is reused per parent and replaced after death or timeout.
_HCL_PARSE_WORKER_SOURCE = """
import pickle, struct, sys
try:
    import resource
    limit = int(sys.argv[1])
    for name in ("RLIMIT_AS", "RLIMIT_DATA"):
        rlim = getattr(resource, name, None)
        if rlim is None:
            continue
        try:
            _soft, hard = resource.getrlimit(rlim)
            new_hard = limit if hard == resource.RLIM_INFINITY or hard > limit else hard
            resource.setrlimit(rlim, (min(limit, new_hard), new_hard))
        except (ValueError, OSError):
            pass
except ImportError:
    pass
import hcl2
from lark.exceptions import LarkError
# Warm up: the first parse builds the Lark parser (hundreds of ms cold).
# That is harness start-up, never the candidate's parse time, so it happens
# before the ready marker.
hcl2.loads("a = 1\\n")
hcl2.parses("a = 1\\n")
stdin = sys.stdin.buffer
stdout = sys.stdout.buffer
stdout.write(b'K')
stdout.flush()
def read_exact(count):
    chunks = []
    while count:
        chunk = stdin.read(count)
        if not chunk:
            return None
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)
while True:
    header = read_exact(4)
    if header is None:
        break
    body = read_exact(struct.unpack(">I", header)[0])
    if body is None:
        break
    try:
        text = body.decode("utf-8")
        document = hcl2.loads(text)
        tree = hcl2.parses(text)
        payload = pickle.dumps((document, tree), protocol=pickle.HIGHEST_PROTOCOL)
    except (LarkError, RuntimeError, ValueError, MemoryError, UnicodeError):
        stdout.write(b"E")
    else:
        stdout.write(b"R" + struct.pack(">Q", len(payload)) + payload)
    stdout.flush()
"""


def _kill_worker_group(process: subprocess.Popen) -> None:
    """SIGKILL the worker's session (it was started in its own), then the
    worker itself as a backstop, and reap it."""
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        process.wait(timeout=5.0)
    except (subprocess.TimeoutExpired, OSError):
        pass
    for stream in (process.stdin, process.stdout):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


class _WorkerDeadline(Exception):
    """Raised inside the reader when the per-call deadline passes."""


#: The only globals the worker's ``(document, tree)`` payload may name:
#: hcl2's document is plain containers and scalars (no globals at all) and
#: the Lark tree is ``Tree``/``Meta`` nodes with ``Token`` leaves.  The
#: worker ran UNTRUSTED text, so its pickle is not trusted either: any other
#: global is a forged payload and is refused before it is constructed.
_PARSE_PAYLOAD_GLOBALS = frozenset(
    {("lark.tree", "Tree"), ("lark.tree", "Meta"), ("lark.lexer", "Token")}
)


class _ParsePayloadUnpickler(pickle.Unpickler):
    """Unpickler restricted to the parse payload's three Lark types."""

    def find_class(self, module: str, name: str) -> Any:
        if (module, name) in _PARSE_PAYLOAD_GLOBALS:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(
            f"parse worker payload names forbidden global {module}.{name}"
        )


def _load_parse_payload(payload: bytes) -> tuple[Mapping[str, Any], Tree]:
    """Decode one worker payload under the restricted unpickler."""
    document, tree = _ParsePayloadUnpickler(io.BytesIO(payload)).load()
    return document, tree


class _HclParseWorker:
    """One killable HCL parse worker per parent process.

    Two clocks. A fresh worker's start (spawn, ``import hcl2``) runs on the
    HARNESS clock, ``startup_deadline_seconds``: it must write its ready
    marker in time or the start is a ``TerraformIntentHarnessError``. Every
    call is then bounded by its own CANDIDATE deadline, armed only once the
    worker is ready; on that deadline the worker's process group is killed
    (never waited on) and the next call starts a fresh worker. Calls are
    serialized by a lock.
    """

    def __init__(
        self,
        *,
        startup_deadline_seconds: float = TERRAFORM_PARSE_WORKER_STARTUP_DEADLINE_SECONDS,
    ) -> None:
        if not startup_deadline_seconds > 0.0:
            raise ValueError("startup_deadline_seconds must be positive")
        self._startup_deadline_seconds = float(startup_deadline_seconds)
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None

    def _start(self) -> subprocess.Popen:
        limit_bytes = TERRAFORM_PARSE_WORKER_RSS_LIMIT_MB * 1024 * 1024
        try:
            return subprocess.Popen(
                [sys.executable, "-I", "-c", _HCL_PARSE_WORKER_SOURCE, str(limit_bytes)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise TerraformIntentHarnessError(
                "terraform intent parse worker could not be started"
            ) from exc

    def _discard(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            _kill_worker_group(process)

    def _await_ready(self, process: subprocess.Popen) -> None:
        """Block until the fresh worker has written its ready marker, under
        the HARNESS start-up deadline. A late, dead or malformed start is a
        harness fault — never the candidate's ``PARSE_TIMEOUT``."""
        deadline = time.monotonic() + self._startup_deadline_seconds
        try:
            marker = self._read_exact(process, 1, deadline)
        except _WorkerDeadline:
            raise TerraformIntentHarnessError(
                "terraform intent parse worker did not become ready in time"
            ) from None
        if marker != _PARSE_WORKER_READY:
            raise TerraformIntentHarnessError(
                "terraform intent parse worker returned malformed output"
            )

    def shutdown(self) -> None:
        with self._lock:
            self._discard()

    @staticmethod
    def _read_exact(process: subprocess.Popen, count: int, deadline: float) -> bytes:
        stream = process.stdout
        assert stream is not None
        fd = stream.fileno()
        chunks: list[bytes] = []
        remaining = count
        while remaining:
            wait = deadline - time.monotonic()
            if wait <= 0.0:
                raise _WorkerDeadline
            ready, _w, _x = select.select([fd], [], [], min(wait, 0.25))
            if not ready:
                if process.poll() is not None:
                    raise TerraformIntentHarnessError(
                        "terraform intent parse worker failed"
                    )
                continue
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                raise TerraformIntentHarnessError("terraform intent parse worker failed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def parse(
        self, text: str, *, deadline_seconds: float
    ) -> tuple[Mapping[str, Any], Tree]:
        if not deadline_seconds > 0.0:
            raise ValueError("deadline_seconds must be positive")
        body = text.encode("utf-8")
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._discard()
                self._process = self._start()
                try:
                    self._await_ready(self._process)
                except TerraformIntentHarnessError:
                    self._discard()
                    raise
            process = self._process
            assert process.stdin is not None
            # The CANDIDATE's clock starts here: the worker is imported and
            # idle, so nothing but its parse of ``text`` is measured.
            deadline = time.monotonic() + deadline_seconds
            try:
                try:
                    process.stdin.write(struct.pack(">I", len(body)) + body)
                    process.stdin.flush()
                except (BrokenPipeError, OSError) as exc:
                    raise TerraformIntentHarnessError(
                        "terraform intent parse worker failed"
                    ) from exc
                kind = self._read_exact(process, 1, deadline)
                if kind == b"E":
                    raise TerraformIntentError(TerraformIntentErrorCode.PARSE)
                if kind != b"R":
                    raise TerraformIntentHarnessError(
                        "terraform intent parse worker returned malformed output"
                    )
                (length,) = struct.unpack(">Q", self._read_exact(process, 8, deadline))
                if length > MAX_PARSE_PAYLOAD_BYTES:
                    raise TerraformIntentHarnessError(
                        "terraform intent parse worker returned malformed output"
                    )
                payload = self._read_exact(process, length, deadline)
                # Decode while the worker is still ours to discard: a worker
                # that answered with a malformed or forged payload is never
                # reused, exactly like one that missed its deadline.
                try:
                    document, tree = _load_parse_payload(payload)
                except (
                    pickle.UnpicklingError,
                    EOFError,
                    ValueError,
                    TypeError,
                    AttributeError,
                    IndexError,
                ) as exc:
                    raise TerraformIntentHarnessError(
                        "terraform intent parse worker returned malformed output"
                    ) from exc
            except _WorkerDeadline:
                self._discard()
                raise TerraformIntentError(TerraformIntentErrorCode.PARSE_TIMEOUT) from None
            except TerraformIntentHarnessError:
                self._discard()
                raise
        if not isinstance(document, Mapping) or not isinstance(tree, Tree):
            raise TerraformIntentError(TerraformIntentErrorCode.PARSE)
        return document, tree


_PARSE_WORKER = _HclParseWorker()
atexit.register(_PARSE_WORKER.shutdown)


def _parse_hcl_in_worker(
    text: str, *, deadline_seconds: float
) -> tuple[Mapping[str, Any], Tree]:
    """Parse ``text`` in the killable worker under ``deadline_seconds``.

    A parse failure is the candidate's ``PARSE``; a deadline is the
    candidate's ``PARSE_TIMEOUT`` (the worker's process group is killed,
    never waited on). The worker's own start is bounded separately by
    ``TERRAFORM_PARSE_WORKER_STARTUP_DEADLINE_SECONDS``: a slow start, like
    an unexplained worker death, is a harness fault, never a candidate code.
    """
    return _PARSE_WORKER.parse(text, deadline_seconds=deadline_seconds)


def _read_candidate_main_tf(main_tf_path: Path) -> str:
    """Bounded, no-follow read of the candidate ``main.tf`` (seal caps).

    The CANDIDATE's ``PARSE``: a missing, non-regular, symlinked, oversized
    or non-UTF-8 ``main.tf``. The HARNESS's fault: a regular file the reader
    could not stat or read (EACCES, EIO, an ENOENT race — the reader's
    ``WORKSPACE_NON_REGULAR_FILE`` for any non-ELOOP ``OSError``), which
    says nothing about the artifact and must never score 0.0.
    """
    try:
        if main_tf_path.is_symlink() or not main_tf_path.is_file():
            _candidate_error(TerraformIntentErrorCode.PARSE)
    except OSError as exc:
        raise TerraformIntentHarnessError(
            "candidate main.tf could not be inspected"
        ) from exc
    try:
        content = _read_bounded_regular_file(
            main_tf_path, max_bytes=MAX_MAIN_TF_BYTES, label="candidate main.tf"
        )
    except WorkspaceLifecycleError as exc:
        if exc.code in _CANDIDATE_READ_CODES:
            raise TerraformIntentError(TerraformIntentErrorCode.PARSE) from exc
        raise TerraformIntentHarnessError(
            "candidate main.tf could not be read"
        ) from exc
    except OSError as exc:
        raise TerraformIntentHarnessError(
            "candidate main.tf could not be read"
        ) from exc
    try:
        return content.decode("utf-8")
    except UnicodeError as exc:
        raise TerraformIntentError(TerraformIntentErrorCode.PARSE) from exc


def compile_terraform_intent(
    package: WorkspacePackage,
    main_tf_path: Path,
    *,
    parse_deadline_seconds: float = TERRAFORM_PARSE_DEADLINE_SECONDS,
) -> TerraformIntentGraph:
    """Parse and exactly validate one candidate ``elt/main.tf`` offline.

    The read is bounded to the seal's per-file cap and the HCL parse runs in
    a spawned, killable worker under ``parse_deadline_seconds`` (10 s by
    default, armed only once the worker has reported ready — its start is
    the harness's own bound); the private package never enters the worker.
    """

    expected = expected_terraform_graph(package)
    text = _read_candidate_main_tf(main_tf_path)
    document, tree = _parse_hcl_in_worker(
        text, deadline_seconds=parse_deadline_seconds
    )
    return _compile_candidate_graph(package, document, tree, expected)


def evaluate_terraform_intent(
    package: WorkspacePackage, main_tf_path: Path
) -> TerraformIntentEvaluation:
    """Return the exact ``terraform_contract`` head for a candidate artifact.

    Private contract failures intentionally propagate as
    :class:`TerraformIntentHarnessError`, making them label-ineligible.
    """

    try:
        graph = compile_terraform_intent(package, main_tf_path)
    except TerraformIntentError as exc:
        return TerraformIntentEvaluation(
            reward=0.0,
            graph=None,
            error_codes=(exc.code,),
        )
    return TerraformIntentEvaluation(reward=1.0, graph=graph)


__all__ = [
    "AIRBYTE_PROVIDER_SOURCE",
    "AIRBYTE_PROVIDER_VERSION",
    "ALLOWED_DATA_SOURCE_TYPES",
    "TERRAFORM_PARSE_DEADLINE_SECONDS",
    "TERRAFORM_PARSE_WORKER_STARTUP_DEADLINE_SECONDS",
    "TERRAFORM_POLICY_VIOLATION_CODES",
    "TerraformConnectionIntent",
    "TerraformDependencyEdge",
    "TerraformDestinationIntent",
    "TerraformIntentError",
    "TerraformIntentErrorCode",
    "TerraformIntentEvaluation",
    "TerraformIntentGraph",
    "TerraformIntentHarnessError",
    "TerraformSelectedStream",
    "TerraformSourceIntent",
    "compile_terraform_intent",
    "evaluate_terraform_intent",
    "expected_terraform_graph",
]

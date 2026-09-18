"""Run candidate dbt projects against attempt-local DuckDB state.

Parse the declared destination dialect, enforce the portable subset, and
rewrite only a private execution copy. Trusted failures raise
``DbtTrustedFailure`` and produce no label; candidate failures return stable
public codes.
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlglot import exp, parse, tokenize

from elt_taskgen.destinations import Destination
from elt_taskgen.models import PopulationName, Row, canonical_json
from elt_taskgen.sql_identifiers import quote_sql_identifier, quote_sql_path
from elt_taskgen.training.local_sync import (
    LocalSyncExecution,
    LocalSyncHarnessError,
    raw_state_immutable,
    shared_schema_mart_names,
    verify_raw_state,
)
from elt_taskgen.training.package import WorkspacePackage
from elt_taskgen.verification import upstream_eval
from elt_taskgen.verification.strict_diagnostic import strict_cell

if TYPE_CHECKING:
    from elt_taskgen.training.namespace import NamespaceProjection

try:
    import resource
except ImportError:  # pragma: no cover - Windows workers use the outer sandbox
    resource = None  # type: ignore[assignment]


DBT_DUCKDB_RUNTIME_SCHEMA_VERSION = "dbt-duckdb-runtime-v1"
DBT_DUCKDB_CORE_VERSION = "1.10.22"
DBT_DUCKDB_ADAPTER_VERSION = "1.10.0"
DBT_DUCKDB_ENGINE_VERSION = "1.4.5"
DBT_DUCKDB_PYTHON_MAJOR_MINOR = (3, 9)
DBT_DUCKDB_UV_LOCK_SHA256 = (
    "92129f2f0ec8b66b04116e8b2c378c1576adbd8287c86e8a99beea13f5d2e9db"
)
DBT_DUCKDB_INSTALLED_DISTRIBUTIONS_SHA256 = (
    "d389472818eca7afd2bbdc73de31ef9c8fde0707dc6a3096cfcb6ad76143222a"
)
DBT_DUCKDB_RUNTIME_MANIFEST_SHA256 = (
    "0fa9adbb840e0cbeb0308e0ce16dc37900de3d162af5d12db6d547f9c3a5d7fc"
)
DBT_COMPATIBILITY_SUBSET_VERSION = "portable-dbt-sql-v4"
DBT_PROFILE_NAME = "elt_taskgen"
DBT_PROFILE_TARGET = "local"

MAX_DBT_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_DBT_PROJECT_YAML_BYTES = 512 * 1024
MAX_DBT_SQL_BYTES = 2 * 1024 * 1024

_SAFE_PROJECT_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")
_RESERVED_JINJA_PREFIX = "__elt_taskgen_jinja_"


class DbtFailureClass(str, Enum):
    """The local dbt label boundary, aligned with workspace-v1."""

    POLICY_FAILURE = "policy_failure"
    POLICY_VIOLATION = "policy_violation"
    TASK_DEFECT = "task_defect"
    HARNESS_DEFECT = "harness_defect"
    TRANSIENT_INFRASTRUCTURE = "transient_infrastructure"

    @property
    def label_eligible(self) -> bool:
        return self in {self.POLICY_FAILURE, self.POLICY_VIOLATION}


class DbtRuntimeVerificationScope(str, Enum):
    """Whether the interpreter is an exact worker or a development fixture."""

    EXACT_ENVIRONMENT = "exact_environment"
    REQUIRED_CLOSURE_ONLY = "required_closure_only"


class DbtErrorCode(str, Enum):
    """Stable public codes; messages and private values never enter results."""

    PROJECT_INVALID = "dbt_project_invalid"
    UNSAFE_ARTIFACT = "dbt_unsafe_artifact"
    COMPATIBILITY_UNSUPPORTED = "dbt_compatibility_unsupported"
    PARSE_FAILED = "dbt_parse_failed"
    GRAPH_INVALID = "dbt_graph_invalid"
    COMPILE_FAILED = "dbt_compile_failed"
    RUN_FAILED = "dbt_run_failed"
    TIMEOUT = "dbt_timeout"
    OUTPUT_LIMIT = "dbt_output_limit"
    MART_MISSING = "dbt_mart_missing"
    MART_SCHEMA_INVALID = "dbt_mart_schema_invalid"
    MART_KEY_INVALID = "dbt_mart_key_invalid"
    RAW_MUTATED = "dbt_raw_mutated"
    RUNTIME_UNAVAILABLE = "dbt_runtime_unavailable"
    RUNTIME_LOCK_INVALID = "dbt_runtime_lock_invalid"
    RUNTIME_VERSION_MISMATCH = "dbt_runtime_version_mismatch"
    INPUT_DATABASE_INVALID = "dbt_input_database_invalid"
    GOLD_MISSING = "dbt_gold_missing"
    EVALUATOR_INVALID = "dbt_evaluator_invalid"
    HARNESS_INTERNAL = "dbt_harness_internal"


_FAILURE_CLASS_BY_CODE: Mapping[DbtErrorCode, DbtFailureClass] = {
    DbtErrorCode.PROJECT_INVALID: DbtFailureClass.POLICY_FAILURE,
    DbtErrorCode.COMPATIBILITY_UNSUPPORTED: DbtFailureClass.POLICY_FAILURE,
    DbtErrorCode.PARSE_FAILED: DbtFailureClass.POLICY_FAILURE,
    DbtErrorCode.GRAPH_INVALID: DbtFailureClass.POLICY_FAILURE,
    DbtErrorCode.COMPILE_FAILED: DbtFailureClass.POLICY_FAILURE,
    DbtErrorCode.RUN_FAILED: DbtFailureClass.POLICY_FAILURE,
    DbtErrorCode.TIMEOUT: DbtFailureClass.POLICY_FAILURE,
    DbtErrorCode.OUTPUT_LIMIT: DbtFailureClass.POLICY_FAILURE,
    DbtErrorCode.MART_MISSING: DbtFailureClass.POLICY_FAILURE,
    DbtErrorCode.MART_SCHEMA_INVALID: DbtFailureClass.POLICY_FAILURE,
    DbtErrorCode.MART_KEY_INVALID: DbtFailureClass.POLICY_FAILURE,
    DbtErrorCode.UNSAFE_ARTIFACT: DbtFailureClass.POLICY_VIOLATION,
    DbtErrorCode.RAW_MUTATED: DbtFailureClass.POLICY_VIOLATION,
    DbtErrorCode.GOLD_MISSING: DbtFailureClass.TASK_DEFECT,
    DbtErrorCode.EVALUATOR_INVALID: DbtFailureClass.TASK_DEFECT,
    DbtErrorCode.RUNTIME_UNAVAILABLE: DbtFailureClass.HARNESS_DEFECT,
    DbtErrorCode.RUNTIME_LOCK_INVALID: DbtFailureClass.HARNESS_DEFECT,
    DbtErrorCode.RUNTIME_VERSION_MISMATCH: DbtFailureClass.HARNESS_DEFECT,
    DbtErrorCode.INPUT_DATABASE_INVALID: DbtFailureClass.HARNESS_DEFECT,
    DbtErrorCode.HARNESS_INTERNAL: DbtFailureClass.HARNESS_DEFECT,
}


class DbtTrustedFailure(RuntimeError):
    """A task/harness failure that is never converted into candidate zero."""

    def __init__(self, code: DbtErrorCode):
        classification = _FAILURE_CLASS_BY_CODE[code]
        if classification.label_eligible:
            raise ValueError("DbtTrustedFailure requires a label-ineligible code")
        super().__init__(code.value)
        self.code = code
        self.classification = classification


class DbtPolicyFailure(ValueError):
    """Internal short-circuit for a measured candidate failure."""

    def __init__(self, code: DbtErrorCode):
        classification = _FAILURE_CLASS_BY_CODE[code]
        if not classification.label_eligible:
            raise ValueError("DbtPolicyFailure requires a label-eligible code")
        super().__init__(code.value)
        self.code = code
        self.classification = classification


class DbtRunnerLimits(BaseModel):
    """Per-command and artifact bounds; the outer worker supplies isolation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_timeout_seconds: float = Field(default=90.0, ge=1.0, le=3600.0)
    runtime_probe_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    memory_limit_mb: int = Field(default=2048, ge=256, le=1_048_576)
    process_limit: int = Field(default=32, ge=1, le=4096)
    max_command_output_bytes: int = Field(
        default=2 * 1024 * 1024, ge=4096, le=128 * 1024 * 1024
    )
    max_state_bytes: int = Field(
        default=128 * 1024 * 1024, ge=1024 * 1024, le=4 * 1024 * 1024 * 1024
    )
    max_result_rows_per_mart: int = Field(default=100_000, ge=1, le=10_000_000)
    max_result_bytes_per_mart: int = Field(
        default=16 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024
    )


class DbtRuntimeIdentity(BaseModel):
    """Path-free identity of the verified local runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = DBT_DUCKDB_RUNTIME_SCHEMA_VERSION
    python_version: str
    uv_lock_sha256: str
    installed_distributions_sha256: str
    manifest_sha256: str
    runtime_identity_sha256: str
    verification_scope: DbtRuntimeVerificationScope
    scoring_eligible: bool
    extra_distribution_count: int = Field(ge=0)
    packages: Mapping[str, str]

    @model_validator(mode="after")
    def _scope_is_honest(self) -> "DbtRuntimeIdentity":
        eligible = self.verification_scope is DbtRuntimeVerificationScope.EXACT_ENVIRONMENT
        if self.scoring_eligible is not eligible:
            raise ValueError("only an exact runtime environment is scoring eligible")
        if eligible and self.extra_distribution_count != 0:
            raise ValueError("an exact runtime cannot contain extra distributions")
        return self


class DbtCommandEvidence(BaseModel):
    """Bounded command evidence; raw dbt logs stay evaluator-private."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    succeeded: bool
    return_code: int
    elapsed_ms: int = Field(ge=0)
    output_sha256: str
    output_bytes: int = Field(ge=0)
    output_truncated: bool = False


class DbtMartEvidence(BaseModel):
    """Public-safe evidence for one required mart."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mart: str
    persistent_table: bool
    exact_columns: bool
    unique_key: bool
    matched_gold: bool


class DbtRunResult(BaseModel):
    """One population's local dbt outcome on the candidate EL state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str = "artifact_workflow_proxy"
    compatibility_subset: str = DBT_COMPATIBILITY_SUBSET_VERSION
    runtime: DbtRuntimeIdentity
    dbt_project: float = Field(ge=0.0, le=1.0)
    mart_reward: float = Field(ge=0.0, le=1.0)
    raw_immutable: bool
    mart_scores: Mapping[str, bool]
    marts: Mapping[str, DbtMartEvidence]
    commands: tuple[DbtCommandEvidence, ...] = ()
    error_codes: tuple[DbtErrorCode, ...] = ()
    failure_class: DbtFailureClass | None = None
    raw_before_sha256: str
    raw_after_sha256: str

    @model_validator(mode="after")
    def _consistent_failure(self) -> "DbtRunResult":
        if self.error_codes:
            classes = {_FAILURE_CLASS_BY_CODE[code] for code in self.error_codes}
            expected = (
                DbtFailureClass.POLICY_VIOLATION
                if DbtFailureClass.POLICY_VIOLATION in classes
                else DbtFailureClass.POLICY_FAILURE
            )
            if self.failure_class is not expected:
                raise ValueError("dbt failure_class disagrees with error_codes")
        elif self.failure_class is not None:
            raise ValueError("dbt failure_class requires an error code")
        if set(self.mart_scores) != set(self.marts):
            raise ValueError("mart_scores and mart evidence must have identical keys")
        for name, evidence in self.marts.items():
            if evidence.mart != name or evidence.matched_gold != self.mart_scores[name]:
                raise ValueError("mart evidence identity or score is inconsistent")
        if self.raw_immutable != (self.raw_before_sha256 == self.raw_after_sha256):
            raise ValueError("raw_immutable disagrees with raw fingerprints")
        return self


@dataclass(frozen=True)
class DbtRuntimeConfig:
    """Trusted provisioning inputs; paths never appear in the public result."""

    python: Path
    manifest: Path
    verification_scope: DbtRuntimeVerificationScope = (
        DbtRuntimeVerificationScope.EXACT_ENVIRONMENT
    )


@dataclass(frozen=True)
class _CommandRun:
    evidence: DbtCommandEvidence
    output_path: Path
    timed_out: bool


@dataclass(frozen=True)
class _JinjaToken:
    placeholder: str
    source: str
    kind: str


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.YAMLError("duplicate mapping key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _canonical_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _sha256_file(path: Path, *, limit: int | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if limit is not None and size > limit:
                    raise DbtPolicyFailure(DbtErrorCode.OUTPUT_LIMIT)
                digest.update(chunk)
    except DbtPolicyFailure:
        raise
    except OSError as exc:
        raise DbtTrustedFailure(DbtErrorCode.HARNESS_INTERNAL) from exc
    return digest.hexdigest(), size


def _sha256_runtime_file(path: Path, *, limit: int) -> tuple[str, int]:
    """Hash a harness-owned runtime artifact without emitting candidate codes."""

    if path.is_symlink() or not path.is_file():
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_LOCK_INVALID)
    try:
        return _sha256_file(path, limit=limit)
    except (DbtPolicyFailure, DbtTrustedFailure) as exc:
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_LOCK_INVALID) from exc


def _load_runtime_contract(
    config: DbtRuntimeConfig,
) -> tuple[dict[str, str], dict[str, Any]]:
    manifest = Path(config.manifest)
    manifest_sha, manifest_size = _sha256_runtime_file(manifest, limit=64 * 1024)
    if manifest_size == 0 or manifest_sha != DBT_DUCKDB_RUNTIME_MANIFEST_SHA256:
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_LOCK_INVALID)
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_LOCK_INVALID) from exc
    expected_keys = {
        "schema_version",
        "python_requires",
        "uv_lock",
        "uv_lock_sha256",
        "installed_distributions",
        "installed_distributions_sha256",
        "dbt_core",
        "dbt_duckdb",
        "duckdb",
        "network_required_at_execution",
        "profile_owner",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_LOCK_INVALID)
    if (
        payload.get("schema_version") != DBT_DUCKDB_RUNTIME_SCHEMA_VERSION
        or payload.get("python_requires") != "==3.9.*"
        or payload.get("uv_lock") != "uv.lock"
        or payload.get("uv_lock_sha256") != DBT_DUCKDB_UV_LOCK_SHA256
        or payload.get("installed_distributions") != "installed-distributions.json"
        or payload.get("installed_distributions_sha256")
        != DBT_DUCKDB_INSTALLED_DISTRIBUTIONS_SHA256
        or payload.get("dbt_core") != DBT_DUCKDB_CORE_VERSION
        or payload.get("dbt_duckdb") != DBT_DUCKDB_ADAPTER_VERSION
        or payload.get("duckdb") != DBT_DUCKDB_ENGINE_VERSION
        or payload.get("network_required_at_execution") is not False
        or payload.get("profile_owner") != "harness"
    ):
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_LOCK_INVALID)
    lock_path = manifest.parent / str(payload["uv_lock"])
    distributions_path = manifest.parent / str(payload["installed_distributions"])
    if (
        lock_path.parent.resolve() != manifest.parent.resolve()
        or distributions_path.parent.resolve() != manifest.parent.resolve()
    ):
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_LOCK_INVALID)
    lock_sha, _ = _sha256_runtime_file(lock_path, limit=4 * 1024 * 1024)
    distributions_sha, _ = _sha256_runtime_file(
        distributions_path, limit=1024 * 1024
    )
    if (
        lock_sha != DBT_DUCKDB_UV_LOCK_SHA256
        or distributions_sha != DBT_DUCKDB_INSTALLED_DISTRIBUTIONS_SHA256
    ):
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_LOCK_INVALID)
    try:
        decoded = json.loads(distributions_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_LOCK_INVALID) from exc
    if not isinstance(decoded, dict) or not decoded:
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_LOCK_INVALID)
    distributions: dict[str, str] = {}
    for raw_name, raw_version in decoded.items():
        if not isinstance(raw_name, str) or not isinstance(raw_version, str):
            raise DbtTrustedFailure(DbtErrorCode.RUNTIME_LOCK_INVALID)
        name = _canonical_distribution_name(raw_name)
        if raw_name != name or name in distributions or not raw_version:
            raise DbtTrustedFailure(DbtErrorCode.RUNTIME_LOCK_INVALID)
        distributions[name] = raw_version
    roots = {
        "dbt-core": DBT_DUCKDB_CORE_VERSION,
        "dbt-duckdb": DBT_DUCKDB_ADAPTER_VERSION,
        "duckdb": DBT_DUCKDB_ENGINE_VERSION,
    }
    if any(distributions.get(name) != version for name, version in roots.items()):
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_LOCK_INVALID)
    return distributions, payload


_RUNTIME_PROBE = r"""
import importlib.metadata as metadata
import json
import platform
import re
import sys
def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()
versions = {}
duplicates = []
for distribution in metadata.distributions():
    raw_name = distribution.metadata.get("Name")
    if not raw_name:
        continue
    name = canonical(raw_name)
    if name in versions and versions[name] != distribution.version:
        duplicates.append(name)
    versions[name] = distribution.version
print(json.dumps({
    "implementation": platform.python_implementation(),
    "python": platform.python_version(),
    "version_info": list(sys.version_info[:3]),
    "packages": versions,
    "duplicates": sorted(set(duplicates)),
}, sort_keys=True, separators=(",", ":")))
"""


def verify_dbt_runtime(
    config: DbtRuntimeConfig,
    *,
    limits: DbtRunnerLimits | None = None,
) -> DbtRuntimeIdentity:
    """Fail closed unless the supplied interpreter exactly matches the lock."""

    bounds = limits or DbtRunnerLimits()
    expected_distributions, _ = _load_runtime_contract(config)
    python = Path(config.python)
    # Keep the virtualenv launcher path; resolving its `python3` symlink probes
    # the base interpreter without `pyvenv.cfg`. Runtime config is harness-owned.
    if not python.exists() or not python.is_file() or not os.access(python, os.X_OK):
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_UNAVAILABLE)
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "DO_NOT_TRACK": "1",
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    try:
        proc = subprocess.run(
            [str(python), "-c", _RUNTIME_PROBE],
            env=env,
            capture_output=True,
            text=True,
            timeout=bounds.runtime_probe_timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_UNAVAILABLE) from exc
    if proc.returncode != 0 or len(proc.stdout.encode("utf-8")) > 1024 * 1024:
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_UNAVAILABLE)
    try:
        observed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_UNAVAILABLE) from exc
    version_info = observed.get("version_info")
    installed = observed.get("packages")
    if (
        observed.get("implementation") != "CPython"
        or not isinstance(version_info, list)
        or tuple(version_info[:2]) != DBT_DUCKDB_PYTHON_MAJOR_MINOR
        or not isinstance(installed, dict)
        or observed.get("duplicates") != []
        or any(
            installed.get(name) != version
            for name, version in expected_distributions.items()
        )
    ):
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_VERSION_MISMATCH)
    extras = sorted(set(installed) - set(expected_distributions))
    scope = DbtRuntimeVerificationScope(config.verification_scope)
    if scope is DbtRuntimeVerificationScope.EXACT_ENVIRONMENT and extras:
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_VERSION_MISMATCH)
    runtime_identity_sha256 = hashlib.sha256(
        canonical_json(
            {
                "schema_version": DBT_DUCKDB_RUNTIME_SCHEMA_VERSION,
                "python": observed["python"],
                "installed_distributions": expected_distributions,
                "uv_lock_sha256": DBT_DUCKDB_UV_LOCK_SHA256,
                "installed_distributions_sha256": (
                    DBT_DUCKDB_INSTALLED_DISTRIBUTIONS_SHA256
                ),
                "manifest_sha256": DBT_DUCKDB_RUNTIME_MANIFEST_SHA256,
            }
        ).encode("utf-8")
    ).hexdigest()
    return DbtRuntimeIdentity(
        python_version=str(observed["python"]),
        uv_lock_sha256=DBT_DUCKDB_UV_LOCK_SHA256,
        installed_distributions_sha256=(
            DBT_DUCKDB_INSTALLED_DISTRIBUTIONS_SHA256
        ),
        manifest_sha256=DBT_DUCKDB_RUNTIME_MANIFEST_SHA256,
        runtime_identity_sha256=runtime_identity_sha256,
        verification_scope=scope,
        scoring_eligible=(scope is DbtRuntimeVerificationScope.EXACT_ENVIRONMENT),
        extra_distribution_count=len(extras),
        packages=dict(sorted(expected_distributions.items())),
    )


def _read_bounded_text(path: Path, limit: int, code: DbtErrorCode) -> str:
    try:
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
        if status.st_size > limit:
            raise DbtPolicyFailure(code)
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
    except DbtPolicyFailure:
        raise
    except OSError as exc:
        raise DbtPolicyFailure(code) from exc
    if len(content) > limit:
        raise DbtPolicyFailure(code)
    try:
        return content.decode("utf-8")
    except UnicodeError as exc:
        raise DbtPolicyFailure(code) from exc


def _load_unique_yaml(path: Path, *, limit: int = MAX_DBT_PROJECT_YAML_BYTES) -> Any:
    text = _read_bounded_text(path, limit, DbtErrorCode.PROJECT_INVALID)
    try:
        return yaml.load(text, Loader=_UniqueSafeLoader)
    except yaml.YAMLError as exc:
        raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID) from exc


def _walk_project_files(project: Path) -> tuple[Path, ...]:
    if project.is_symlink() or not project.is_dir():
        raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
    found: list[Path] = []
    pending = [project]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if entry.is_symlink():
                        raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
                    relative = PurePosixPath(path.relative_to(project).as_posix())
                    if any(part in {".", "..", ""} for part in relative.parts):
                        raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        found.append(path)
                    else:
                        raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
        except DbtPolicyFailure:
            raise
        except OSError as exc:
            raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID) from exc
    return tuple(sorted(found))


def _contains_hook(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower().replace("_", "-").lstrip("+")
            if normalized in {"pre-hook", "post-hook", "on-run-start", "on-run-end"}:
                return True
            if _contains_hook(child):
                return True
    elif isinstance(value, list):
        return any(_contains_hook(child) for child in value)
    return False


_HARNESS_OWNED_PROJECT_PATH_KEYS = frozenset(
    {
        # L1 permits only the conventional project-local models/ resource root.
        "analysis-paths",
        "asset-paths",
        "data-paths",  # deprecated alias retained fail-closed
        "docs-paths",
        "function-paths",
        "macro-paths",
        "seed-paths",
        "snapshot-paths",
        "source-paths",  # deprecated alias retained fail-closed
        "test-paths",
        "unit-test-paths",
        # dbt writes these trees or resolves dependencies through them.  The
        # scorer supplies target/log/profile paths and admits no packages.
        "clean-targets",
        "log-path",
        "packages-install-path",
        "profiles-dir",
        "project-dir",
        "state",
        "target-path",
    }
)
_PATH_CONTROL_KEY_NAMES = frozenset(
    {
        "external-location",
        "external-root",
        "file",
        "filename",
        "location",
        "path",
    }
)


def _normalized_project_key(value: object) -> str:
    return str(value).strip().casefold().replace("_", "-").lstrip("+")


def _is_path_control_key(value: object) -> bool:
    """Recognize path controls, including unreviewed path-bearing extensions."""

    normalized = _normalized_project_key(value)
    return (
        normalized in _HARNESS_OWNED_PROJECT_PATH_KEYS
        or normalized in _PATH_CONTROL_KEY_NAMES
        or normalized.endswith(("-path", "-paths", "-dir", "-dirs", "-location", "-locations"))
    )


def _validate_no_path_controls(value: Any) -> None:
    """Reject path routing anywhere in candidate-owned project configuration."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if _is_path_control_key(key):
                raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
            _validate_no_path_controls(child)
    elif isinstance(value, list):
        for child in value:
            _validate_no_path_controls(child)


def _validate_project_path_controls(project_doc: Mapping[str, Any]) -> None:
    """Confine discovery and generated state to harness-owned paths.

    Only the exact default ``models/`` root is accepted; redirection, traversal,
    backslashes, absolute paths, and symlinks are rejected.
    """

    model_paths = project_doc.get("model-paths")
    if model_paths is not None:
        if model_paths != ["models"]:
            raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)

    for key, child in project_doc.items():
        if key == "model-paths":
            continue
        if _is_path_control_key(key):
            raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
        _validate_no_path_controls(child)


def _literal(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant) and isinstance(
        node.value, (str, int, float, bool, type(None))
    ):
        return node.value
    raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)


def _parse_jinja_expression(source: str, index: int) -> _JinjaToken:
    expression = source[2:-2].strip()
    try:
        node = ast.parse(expression, mode="eval").body
    except SyntaxError as exc:
        raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID) from exc
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
    name = node.func.id
    if any(keyword.arg is None for keyword in node.keywords):
        raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
    args = tuple(_literal(item) for item in node.args)
    kwargs = {str(item.arg): _literal(item.value) for item in node.keywords}
    if name == "config":
        allowed = {"materialized", "alias"}
        if args or set(kwargs) - allowed:
            raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
        if kwargs.get("materialized", "table") != "table":
            raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
        if "alias" in kwargs and not isinstance(kwargs["alias"], str):
            raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
        return _JinjaToken("", source, "config")
    if name == "source":
        if len(args) != 2 or kwargs or not all(isinstance(item, str) for item in args):
            raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
    elif name == "ref":
        if len(args) != 1 or kwargs or not isinstance(args[0], str):
            raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
    else:
        raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
    placeholder = f"{_RESERVED_JINJA_PREFIX}{index:04d}__"
    return _JinjaToken(placeholder, source, name)


def _mask_jinja(sql: str) -> tuple[str, tuple[_JinjaToken, ...]]:
    if _RESERVED_JINJA_PREFIX in sql:
        raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
    output: list[str] = []
    tokens: list[_JinjaToken] = []
    cursor = 0
    while cursor < len(sql):
        start = sql.find("{", cursor)
        if start < 0:
            output.append(sql[cursor:])
            break
        output.append(sql[cursor:start])
        if sql.startswith("{{", start):
            end = sql.find("}}", start + 2)
            if end < 0:
                raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
            source = sql[start : end + 2]
            token = _parse_jinja_expression(source, len(tokens))
            tokens.append(token)
            if token.kind != "config":
                output.append('"' + token.placeholder + '"')
            cursor = end + 2
            continue
        # Blocks, comments, and unmatched braces materially expand the Jinja
        # execution surface.  portable-dbt-sql-v1 intentionally excludes them.
        raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
    return "".join(output), tuple(tokens)


_MUTATING_SQL_NODES = tuple(
    node
    for node in (
        getattr(exp, name, None)
        for name in (
            "Alter",
            "Attach",
            "Call",
            "Command",
            "Copy",
            "Create",
            "Delete",
            "Detach",
            "Drop",
            "Execute",
            "Export",
            "Grant",
            "Insert",
            "LoadData",
            "Merge",
            "Pragma",
            "Revoke",
            "Set",
            "Transaction",
            "Update",
            "Use",
        )
    )
    if isinstance(node, type)
)

_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "current_date",
        "current_timestamp",
        "current_time",
        "current_user",
        "delta_scan",
        "getenv",
        "glob",
        "httpfs",
        "iceberg_scan",
        "nextval",
        "now",
        "parquet_scan",
        "postgres_scan",
        "random",
        "read_blob",
        "read_csv",
        "read_csv_auto",
        "read_json",
        "read_json_auto",
        "read_ndjson",
        "read_parquet",
        "sqlite_scan",
        "uuid",
    }
)

_FORBIDDEN_STATEMENT_PREFIXES = frozenset(
    {
        "ALTER",
        "ATTACH",
        "CALL",
        "COPY",
        "CREATE",
        "DELETE",
        "DETACH",
        "DROP",
        "EXECUTE",
        "EXPORT",
        "GRANT",
        "INSERT",
        "INSTALL",
        "LOAD",
        "MERGE",
        "PRAGMA",
        "REVOKE",
        "SET",
        "UPDATE",
        "USE",
    }
)

_ALLOWED_ANONYMOUS_FUNCTIONS = frozenset(
    {
        "bool_and",
        "bool_or",
        "datediff",
        "dateadd",
        "json_extract_path_text",
        "regexp_replace",
        "split_part",
    }
)

# Closed portable SQL registry; unknown or unmeasured constructs fail closed.
_ALLOWED_AST_NODE_NAMES = frozenset(
    {
        "Add",
        "Alias",
        "And",
        "Anonymous",
        "Avg",
        "Between",
        "Boolean",
        "Case",
        "Cast",
        "Ceil",
        "Coalesce",
        "Column",
        "Concat",
        "Count",
        "CTE",
        "DataType",
        "DataTypeParam",
        "DateAdd",
        "DateDiff",
        "DateTrunc",
        "DenseRank",
        "Distinct",
        "Div",
        "DPipe",
        "EQ",
        "Except",
        "Extract",
        "Filter",
        "Floor",
        "From",
        "Group",
        "GT",
        "GTE",
        "Having",
        "Identifier",
        "If",
        "ILike",
        "In",
        "Intersect",
        "Is",
        "JSONExtract",
        "JSONExtractScalar",
        "JSONPath",
        "JSONPathKey",
        "JSONPathRoot",
        "JSONPathSubscript",
        "Join",
        "Lag",
        "LastValue",
        "Lead",
        "Length",
        "Like",
        "Limit",
        "Literal",
        "Lower",
        "LT",
        "LTE",
        "Max",
        "MD5",
        "Min",
        "Mod",
        "Mul",
        "NEQ",
        "Neg",
        "Not",
        "Null",
        "Nullif",
        "Offset",
        "Or",
        "Order",
        "Ordered",
        "Paren",
        "Pow",
        "Qualify",
        "Rank",
        "RegexpLike",
        "RegexpReplace",
        "Round",
        "RowNumber",
        "Select",
        "Star",
        "Sub",
        "Subquery",
        "Substring",
        "Sum",
        "Table",
        "TableAlias",
        "TimestampTrunc",
        "TimeStrToTime",
        "TimeToStr",
        "ToChar",
        "Trim",
        "TryCast",
        "Union",
        "When",
        "Where",
        "Window",
        "WindowSpec",
        "With",
    }
)

#: Closed temporal-unit vocabulary, checked for both identifier and string
#: forms. Unmeasured week, epoch, and sub-second units remain excluded.
_ALLOWED_TIME_UNITS = frozenset(
    {"YEAR", "QUARTER", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND"}
)
_TIME_UNIT_SLOTS: dict[type[exp.Expression], str] = {
    exp.DateTrunc: "unit",
    exp.TimestampTrunc: "unit",
    exp.Extract: "this",
    exp.DateDiff: "unit",
}
_TEXT_DATA_TYPES = frozenset({exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR})
#: Types with destination-specific text rendering. FLOAT/DOUBLE and TIMESTAMP
#: casts are unsafe for hashes; measured INTEGER and DECIMAL casts remain allowed.
_DESTINATION_SPECIFIC_TEXT_SOURCES = frozenset(
    {
        exp.DataType.Type.DOUBLE,
        exp.DataType.Type.FLOAT,
        exp.DataType.Type.TIMESTAMP,
        exp.DataType.Type.TIMESTAMPTZ,
        exp.DataType.Type.TIMESTAMPNTZ,
        exp.DataType.Type.TIMESTAMPLTZ,
        exp.DataType.Type.DATETIME,
    }
)
#: Functions whose extra arguments sqlglot silently DROPS at parse time
#: (Snowflake DATE_TRUNC(part, x, zone)): arity is checked on the tokens.
_MAX_TOKEN_ARITY = {"DATE_TRUNC": 2, "TRUNC": 2, "DATE_PART": 2, "DATEDIFF": 3, "DATEADD": 3}
_LENGTH_TYPE_TOKENS = frozenset({"VARCHAR", "CHAR", "CHARACTER", "TEXT", "STRING", "NVARCHAR", "BPCHAR"})


def _unit_value(node: exp.Expression | None) -> str | None:
    if isinstance(node, exp.Var):
        return str(node.this).upper()
    if isinstance(node, exp.Literal) and node.is_string:
        return str(node.this).upper()
    return None


def _is_admitted_time_unit(node: exp.Expression) -> bool:
    """A `Var` passes the flat registry check only as the unit of a temporal
    function; its value is judged by `_temporal_units_problem`."""
    if not isinstance(node, exp.Var):
        return False
    slot = _TIME_UNIT_SLOTS.get(type(node.parent))
    return slot is not None and node.arg_key == slot


def _temporal_units_problem(tree: exp.Expression, destination: Destination) -> bool:
    """Return whether temporal units fall outside the closed portable contract."""
    for node in tree.walk():
        if isinstance(node, exp.DateAdd) and node.args.get("unit") is not None:
            return True
        slot = _TIME_UNIT_SLOTS.get(type(node))
        if slot is None:
            continue
        unit = node.args.get(slot)
        if unit is None:
            continue
        if isinstance(node, exp.DateDiff) and destination is not Destination.SNOWFLAKE:
            return True
        value = _unit_value(unit)
        if value is None or value not in _ALLOWED_TIME_UNITS:
            return True
    return False


def _token_shape_problem(tokens: list, destination: Destination) -> bool:
    """Checks on the lexical stream for what the parser discards: extra
    arguments to temporal functions and MAX character lengths."""
    upper = [str(token.text).upper() for token in tokens]
    for index, text in enumerate(upper):
        if text == "MD5" and index + 1 < len(upper) and upper[index + 1] == "(":
            # Length-limited MD5 casts can hash different values across engines.
            depth = 0
            span_end = len(upper)
            for offset, follow in enumerate(upper[index + 1 :], start=index + 1):
                if follow == "(":
                    depth += 1
                elif follow == ")":
                    depth -= 1
                    if depth == 0:
                        span_end = offset
                        break
            span = upper[index + 2 : span_end]
            for inner, word in enumerate(span[:-1]):
                if word not in _LENGTH_TYPE_TOKENS or span[inner + 1] != "(":
                    continue
                bare_redshift_text = (
                    destination is Destination.REDSHIFT
                    and word == "VARCHAR"
                    and inner + 3 < len(span)
                    and span[inner + 2] == "MAX"
                    and span[inner + 3] == ")"
                )
                if not bare_redshift_text:
                    return True
        if text in _MAX_TOKEN_ARITY and index + 1 < len(upper) and upper[index + 1] == "(":
            depth, args = 0, 1
            for follow in upper[index + 1 :]:
                if follow == "(":
                    depth += 1
                elif follow == ")":
                    depth -= 1
                    if depth == 0:
                        break
                elif follow == "," and depth == 1:
                    args += 1
            if args > _MAX_TOKEN_ARITY[text]:
                return True
        if (
            text in _LENGTH_TYPE_TOKENS
            and index + 3 < len(upper)
            and upper[index + 1] == "("
            and upper[index + 2] == "MAX"
            and upper[index + 3] == ")"
        ):
            if not (destination is Destination.REDSHIFT and text == "VARCHAR"):
                return True
    return False


def _is_admitted_redshift_varchar_max(
    node: exp.Expression, destination: Destination
) -> bool:
    """Admit Redshift ``VARCHAR(MAX)`` for compatibility folding to ``TEXT``."""
    if destination is not Destination.REDSHIFT or not isinstance(node, exp.Var):
        return False
    if str(node.this).upper() != "MAX":
        return False
    param = node.parent
    data_type = param.parent if isinstance(param, exp.DataTypeParam) else None
    return (
        isinstance(data_type, exp.DataType)
        and data_type.this is exp.DataType.Type.VARCHAR
        and len(data_type.expressions) == 1
    )


def _unsafe_text_cast(node: exp.Expression, unsafe_columns: frozenset[str]) -> bool:
    """Return whether a text cast has a statically destination-specific operand."""
    if not isinstance(node, exp.Cast):
        return False
    target = node.args.get("to")
    if not (isinstance(target, exp.DataType) and target.this in _TEXT_DATA_TYPES):
        return False
    # The whole operand subtree is inspected: ROUND(CAST(x AS DOUBLE)) and
    # CAST(a AS DOUBLE) / CAST(b AS DOUBLE) render per destination exactly as a
    # bare double does.
    for inner in node.this.walk():
        if isinstance(inner, exp.Cast):
            target_type = inner.args.get("to")
            if (
                isinstance(target_type, exp.DataType)
                and target_type.this in _DESTINATION_SPECIFIC_TEXT_SOURCES
            ):
                return True
        if isinstance(inner, exp.Column) and inner.name.casefold() in unsafe_columns:
            return True
    return False


#: Boolean-valued nodes cannot be portably cast to text on Redshift.
_BOOLEAN_VALUED_NODES = (
    exp.Boolean,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Is,
    exp.In,
    exp.Like,
    exp.ILike,
    exp.RegexpLike,
    exp.Between,
    exp.And,
    exp.Or,
    exp.Not,
)


def _unwrap_parens(node: exp.Expression) -> exp.Expression:
    """Strip redundant parentheses, which carry no type of their own.

    Without this the rule below saw only a Cast's direct child, so a
    parenthesis hid every comparison from it.
    """

    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _boolean_text_cast(node: exp.Expression, destination: Destination) -> bool:
    if destination is not Destination.REDSHIFT or not isinstance(node, exp.Cast):
        return False
    target = node.args.get("to")
    if not (isinstance(target, exp.DataType) and target.this in _TEXT_DATA_TYPES):
        return False
    operand = _unwrap_parens(node.this)
    if isinstance(operand, _BOOLEAN_VALUED_NODES):
        return True
    if isinstance(operand, exp.Cast):
        inner = operand.args.get("to")
        return isinstance(inner, exp.DataType) and inner.this is exp.DataType.Type.BOOLEAN
    return False


#: Redshift requires an explicit frame when one of these carries an ORDER BY.
#: ROW_NUMBER, RANK, DENSE_RANK, LAG and LEAD do not, and stay admitted.
_FRAME_REQUIRING_WINDOW_FUNCTIONS = (
    exp.Sum,
    exp.Avg,
    exp.Count,
    exp.Min,
    exp.Max,
    exp.FirstValue,
    exp.LastValue,
    exp.NthValue,
)


def _window_problem(node: exp.Expression, destination: Destination) -> bool:
    """Window shapes a destination refuses to compile.

    Databricks and Redshift both reject a DISTINCT window aggregate. Snowflake
    runs it and agrees, so it stays admitted there.
    """

    if not isinstance(node, exp.Window):
        return False
    function = node.this
    if destination in {Destination.DATABRICKS, Destination.REDSHIFT} and any(
        isinstance(child, exp.Distinct) for child in function.walk()
    ):
        return True
    if destination is not Destination.REDSHIFT:
        return False
    if not isinstance(function, _FRAME_REQUIRING_WINDOW_FUNCTIONS):
        return False
    if not node.args.get("order"):
        return False
    return node.args.get("spec") is None


def _operand_is_timestamp(node: exp.Expression, timestamp_columns: frozenset[str]) -> bool:
    """Statically certain timestamp operands: an explicit cast, or a source
    column the TaskIR declares as a timestamp."""
    if isinstance(node, exp.Cast):
        target = node.args.get("to")
        return isinstance(target, exp.DataType) and target.this in {
            exp.DataType.Type.TIMESTAMP,
            exp.DataType.Type.TIMESTAMPTZ,
            exp.DataType.Type.TIMESTAMPNTZ,
            exp.DataType.Type.TIMESTAMPLTZ,
            exp.DataType.Type.DATETIME,
        }
    return isinstance(node, exp.Column) and node.name.casefold() in timestamp_columns


def _md5_argument_is_text(
    node: exp.MD5, json_columns: frozenset[str], destination: Destination
) -> bool:
    """Admit MD5 only for literals or explicit unbounded text casts of non-JSON data."""
    argument = node.this
    if isinstance(argument, exp.Literal):
        return argument.is_string
    if type(argument) is not exp.Cast:
        return False
    target = argument.args.get("to")
    if not isinstance(target, exp.DataType) or target.this not in _TEXT_DATA_TYPES:
        return False
    if target.expressions and not _is_admitted_redshift_varchar_max(
        target.expressions[0].this if isinstance(target.expressions[0], exp.DataTypeParam) else None,
        destination,
    ):
        # A length-limited cast hashes a truncated value on the destination;
        # Redshift's bare text type VARCHAR(MAX) is not a length limit.
        return False
    operand = argument.this
    if isinstance(operand, (exp.JSONExtract, exp.JSONExtractScalar)):
        return False
    if isinstance(operand, exp.Column) and operand.name.casefold() in json_columns:
        return False
    return True


def _validate_decimal_casts(tree: exp.Expression) -> None:
    for cast in tree.find_all(exp.Cast):
        target = cast.args.get("to")
        if not isinstance(target, exp.DataType) or target.this is not exp.DataType.Type.DECIMAL:
            continue
        params = target.expressions
        if not params:
            continue
        values: list[int] = []
        for param in params:
            literal = param.this if isinstance(param, exp.DataTypeParam) else None
            if not isinstance(literal, exp.Literal) or not literal.is_number:
                raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
            values.append(int(literal.this))
        precision = values[0]
        scale = values[1] if len(values) > 1 else 0
        if precision < 1 or precision > 38 or scale < 0 or scale > 9 or scale > precision:
            raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)


_DATE_FORMATS = {
    "YYYY-MM": "%Y-%m",
    "YYYY-MM-DD": "%Y-%m-%d",
    "YYYY-MM-DD HH24:MI:SS": "%Y-%m-%d %H:%M:%S",
    "%Y-%m": "%Y-%m",
    "%Y-%m-%d": "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S": "%Y-%m-%d %H:%M:%S",
    # sqlglot 30 marks Databricks strict datetime patterns this way.
    "%Y-%mstrict": "%Y-%m",
    "%Y-%m-%dstrict": "%Y-%m-%d",
}


def _json_path(*keys: str) -> exp.JSONPath:
    return exp.JSONPath(
        expressions=[
            exp.JSONPathRoot(),
            *(exp.JSONPathKey(this=key) for key in keys),
        ]
    )


def _redshift_super_scalar(
    value: exp.Column,
    json_columns: frozenset[str],
) -> exp.JSONExtractScalar | None:
    """Translate only TaskIR-proven SUPER navigation, never an ordinary dot."""

    table = value.table
    database = value.db
    leaf = value.name
    if table and table.casefold() in json_columns:
        base = exp.Column(
            this=exp.Identifier(this=table),
            table=(
                exp.Identifier(this=database)
                if database
                else None
            ),
        )
        keys = (leaf,)
    elif database and database.casefold() in json_columns:
        base = exp.Column(this=exp.Identifier(this=database))
        keys = (table, leaf)
    else:
        return None
    return exp.JSONExtractScalar(this=base, expression=_json_path(*keys))


def _compatibility_transform(
    node: exp.Expression,
    *,
    destination: Destination,
    json_columns: frozenset[str],
    timestamp_columns: frozenset[str] = frozenset(),
) -> exp.Expression:
    if (
        destination is Destination.REDSHIFT
        and isinstance(node, exp.DataType)
        and node.this is exp.DataType.Type.VARCHAR
        and len(node.expressions) == 1
        and isinstance(node.expressions[0], exp.DataTypeParam)
        and isinstance(node.expressions[0].this, exp.Var)
        and str(node.expressions[0].this.this).upper() == "MAX"
    ):
        # Redshift's bare text type; DuckDB has no TEXT(MAX).
        return exp.DataType(this=exp.DataType.Type.TEXT)
    if isinstance(node, (exp.DateTrunc, exp.TimestampTrunc)) and (
        # Day-or-coarser truncation returns incompatible types across engines.
        destination is Destination.REDSHIFT
        or (
            destination is Destination.DATABRICKS
            and _operand_is_timestamp(node.this, timestamp_columns)
        )
    ):
        return exp.Cast(this=node, to=exp.DataType(this=exp.DataType.Type.TIMESTAMP))
    if (
        destination is Destination.REDSHIFT
        and isinstance(node, exp.RegexpReplace)
        and node.args.get("single_replace")
    ):
        # Redshift and DuckDB use different default REGEXP_REPLACE scopes.
        replaced = node.copy()
        # sqlglot's Redshift reader marks the call single-replace; clearing the
        # flag makes the DuckDB generator emit the global form.
        replaced.set("single_replace", False)
        return replaced
    if isinstance(node, exp.Cast):
        target = node.args.get("to")
        value = node.this
        if (
            isinstance(target, exp.DataType)
            and target.this in {exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR}
            and isinstance(value, exp.JSONExtract)
        ):
            return exp.JSONExtractScalar(
                this=value.this.copy(),
                expression=value.expression.copy(),
            )
        if (
            destination is Destination.REDSHIFT
            and isinstance(target, exp.DataType)
            and target.this in {exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR}
            and isinstance(value, exp.Column)
        ):
            extracted = _redshift_super_scalar(value, json_columns)
            if extracted is not None:
                return extracted
    if isinstance(node, exp.ToChar) and node.args.get("format") is not None:
        format_node = node.args["format"]
        if not isinstance(format_node, exp.Literal) or not format_node.is_string:
            raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
        replacement = _DATE_FORMATS.get(str(format_node.this))
        if replacement is None:
            raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
        return exp.TimeToStr(
            this=node.this.copy(), format=exp.Literal.string(replacement)
        )
    if isinstance(node, exp.TimeToStr):
        format_node = node.args.get("format")
        if isinstance(format_node, exp.Literal) and format_node.is_string:
            replacement = _DATE_FORMATS.get(str(format_node.this))
            if replacement is None:
                raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
            node.set("format", exp.Literal.string(replacement))
    return node


def rewrite_model_sql(
    sql: str,
    destination: Destination,
    *,
    json_columns: frozenset[str] = frozenset(),
    float_or_temporal_columns: frozenset[str] = frozenset(),
    timestamp_columns: frozenset[str] = frozenset(),
) -> str:
    """Rewrite one declared dialect into the closed portable DuckDB subset.

    Rewrite Redshift dot navigation only for TaskIR-declared JSON roots.
    """

    masked, tokens = _mask_jinja(sql)
    resolved_destination = Destination(destination)
    dialect = resolved_destination.value
    normalized_json_columns = frozenset(name.casefold() for name in json_columns)
    normalized_unsafe_columns = frozenset(
        name.casefold() for name in float_or_temporal_columns
    )
    normalized_timestamp_columns = frozenset(
        name.casefold() for name in timestamp_columns
    )
    try:
        lexical_tokens = tokenize(masked, read=dialect)
        if (
            lexical_tokens
            and lexical_tokens[0].text.upper() in _FORBIDDEN_STATEMENT_PREFIXES
        ):
            raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
        statements = parse(masked, read=dialect)
    except DbtPolicyFailure:
        raise
    except Exception as exc:  # noqa: BLE001 - stable code leaves this frame
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED) from exc
    if len(statements) != 1:
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    tree = statements[0]
    if any(isinstance(node, _MUTATING_SQL_NODES) for node in tree.walk()):
        raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
    if not isinstance(tree, exp.Query):
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    unsupported_nodes = {
        type(node).__name__
        for node in tree.walk()
        if type(node).__name__ not in _ALLOWED_AST_NODE_NAMES
        and not _is_admitted_time_unit(node)
        and not _is_admitted_redshift_varchar_max(node, resolved_destination)
    }
    if unsupported_nodes:
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    if _temporal_units_problem(tree, resolved_destination):
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    if resolved_destination is Destination.REDSHIFT and any(
        isinstance(node, exp.Avg) for node in tree.walk()
    ):
        # Refuse Redshift AVG when operand type cannot prove portable division.
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    if _token_shape_problem(list(lexical_tokens), resolved_destination):
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    if any(
        not _md5_argument_is_text(node, normalized_json_columns, resolved_destination)
        for node in tree.find_all(exp.MD5)
    ):
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    if any(_unsafe_text_cast(node, normalized_unsafe_columns) for node in tree.walk()):
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    if any(_boolean_text_cast(node, resolved_destination) for node in tree.walk()):
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    if any(_window_problem(node, resolved_destination) for node in tree.walk()):
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    if resolved_destination is Destination.REDSHIFT and any(
        isinstance(node, exp.Filter) for node in tree.walk()
    ):
        # Redshift has no aggregate FILTER (WHERE ...) clause.
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    if resolved_destination is Destination.DATABRICKS and any(
        isinstance(node, exp.ToChar) for node in tree.walk()
    ):
        # Spark refuses TO_CHAR's datetime pattern outright.
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    cte_names = {
        cte.alias_or_name.casefold()
        for cte in tree.find_all(exp.CTE)
        if cte.alias_or_name
    }
    relation_tokens = {
        token.placeholder.casefold()
        for token in tokens
        if token.kind in {"source", "ref"}
    }
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
        # Physical relation spelling is never portable and bypasses the dbt
        # graph. Only source()/ref() placeholders and query-local CTEs exist in
        # the admitted subset.
        if table.name.casefold() not in relation_tokens | cte_names:
            raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
    for function in tree.find_all(exp.Func):
        name = function.sql_name().lower()
        if name in _FORBIDDEN_FUNCTIONS:
            raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
        if isinstance(function, exp.Anonymous):
            anonymous_name = function.name.lower()
            if anonymous_name in _FORBIDDEN_FUNCTIONS:
                raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
            if anonymous_name not in _ALLOWED_ANONYMOUS_FUNCTIONS:
                raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    nondeterministic_types = tuple(
        item
        for item in (
            getattr(exp, "CurrentDate", None),
            getattr(exp, "CurrentTime", None),
            getattr(exp, "CurrentTimestamp", None),
            getattr(exp, "Rand", None),
            getattr(exp, "Uuid", None),
        )
        if isinstance(item, type)
    )
    if nondeterministic_types and any(
        isinstance(node, nondeterministic_types) for node in tree.walk()
    ):
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    nondeterministic_aggregates = tuple(
        item
        for item in (
            getattr(exp, "ArrayAgg", None),
            getattr(exp, "GroupConcat", None),
        )
        if isinstance(item, type)
    )
    if nondeterministic_aggregates and any(
        isinstance(node, nondeterministic_aggregates) for node in tree.walk()
    ):
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    for window in tree.find_all(exp.Window):
        function = window.this
        if isinstance(function, (exp.RowNumber, exp.Rank, exp.DenseRank)) and not window.args.get("order"):
            raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    _validate_decimal_casts(tree)
    try:
        rewritten = tree.transform(
            lambda node: _compatibility_transform(
                node,
                destination=resolved_destination,
                json_columns=normalized_json_columns,
                timestamp_columns=normalized_timestamp_columns,
            )
        ).sql(dialect="duckdb")
    except DbtPolicyFailure:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED) from exc
    config_tokens = [token.source for token in tokens if token.kind == "config"]
    for token in tokens:
        if token.kind == "config":
            continue
        candidates = (
            '"' + token.placeholder + '"',
            token.placeholder,
            token.placeholder.upper(),
        )
        replaced = False
        for candidate in candidates:
            if candidate in rewritten:
                rewritten = rewritten.replace(candidate, token.source)
                replaced = True
        if not replaced:
            raise DbtPolicyFailure(DbtErrorCode.COMPATIBILITY_UNSUPPORTED)
    prefix = "\n".join(config_tokens)
    return (prefix + "\n" if prefix else "") + rewritten + "\n"


def _logical_source_names(sources_doc: Mapping[str, Any]) -> tuple[str, ...]:
    sources = sources_doc.get("sources")
    if not isinstance(sources, list) or not sources:
        raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
    names: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
        if any(key in source for key in ("loader", "external", "meta")):
            raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
        tables = source.get("tables")
        if not isinstance(tables, list) or not tables:
            raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
        for table in tables:
            if not isinstance(table, dict) or not isinstance(table.get("name"), str):
                raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
            if table.get("identifier", table["name"]) != table["name"]:
                raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
            if any(key in table for key in ("external", "meta")):
                raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
            names.append(str(table["name"]))
    return tuple(names)


def _validate_declared_source_namespace(
    sources_doc: Mapping[str, Any],
    namespace: "NamespaceProjection",
) -> None:
    """Validate cloud intent before projecting the private execution copy."""

    expected_container = (
        namespace.logical_catalog
        if namespace.destination is Destination.DATABRICKS
        else namespace.logical_database
    )
    for source in sources_doc["sources"]:
        declared_schema = source.get("schema")
        if (
            not isinstance(declared_schema, str)
            or declared_schema.casefold() != namespace.logical_schema.casefold()
        ):
            raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
        declared_database = source.get("database")
        declared_catalog = source.get("catalog")
        if (
            expected_container is not None
            and declared_database is None
            and declared_catalog is None
        ):
            raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
        for declared in (declared_database, declared_catalog):
            if declared is None:
                continue
            if (
                expected_container is None
                or not isinstance(declared, str)
                or declared.casefold() != expected_container.casefold()
            ):
                raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
        if (
            declared_database is not None
            and declared_catalog is not None
            and str(declared_database).casefold() != str(declared_catalog).casefold()
        ):
            raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)


def _prepare_execution_project(
    candidate_elt: Path,
    execution_project: Path,
    *,
    package: WorkspacePackage,
    namespace: "NamespaceProjection",
) -> str:
    files = _walk_project_files(candidate_elt)
    relative_names = {path.relative_to(candidate_elt).as_posix() for path in files}
    if "dbt_project.yml" not in relative_names or "models/sources.yml" not in relative_names:
        raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
    forbidden_names = {
        "packages.yml",
        "dependencies.yml",
        "package-lock.yml",
        "profiles.yml",
    }
    if any(path.name in forbidden_names for path in files):
        raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
    if any(
        part in {"macros", "seeds", "snapshots", "analyses"}
        for path in files
        for part in path.relative_to(candidate_elt).parts
    ):
        raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
    if any(path.suffix.lower() == ".py" for path in files):
        raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)

    project_doc = _load_unique_yaml(candidate_elt / "dbt_project.yml")
    if not isinstance(project_doc, dict):
        raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
    project_name = project_doc.get("name")
    if not isinstance(project_name, str) or _SAFE_PROJECT_NAME_RE.fullmatch(project_name) is None:
        raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
    if project_doc.get("config-version") != 2:
        raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
    declared_profile = project_doc.get("profile")
    if declared_profile not in (None, DBT_PROFILE_NAME):
        raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
    _validate_project_path_controls(project_doc)
    if _contains_hook(project_doc):
        raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)

    sql_paths = [path for path in files if path.suffix.lower() == ".sql"]
    if not sql_paths or any("models" not in path.relative_to(candidate_elt).parts for path in sql_paths):
        raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
    yaml_paths = [path for path in files if path.suffix.lower() in {".yml", ".yaml"}]
    for path in yaml_paths:
        doc = _load_unique_yaml(path)
        if path.name != "dbt_project.yml":
            _validate_no_path_controls(doc)
        if _contains_hook(doc):
            raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)

    sources_path = candidate_elt / "models" / "sources.yml"
    sources_doc = _load_unique_yaml(sources_path)
    if not isinstance(sources_doc, dict) or sources_doc.get("version") != 2:
        raise DbtPolicyFailure(DbtErrorCode.PROJECT_INVALID)
    selected = _logical_source_names(sources_doc)
    _validate_declared_source_namespace(sources_doc, namespace)
    expected = {table.name for table in package.task.tables}
    if len(selected) != len(set(selected)) or set(selected) != expected:
        raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)

    shutil.copytree(candidate_elt, execution_project, symlinks=False)
    rewritten_sources = json.loads(json.dumps(sources_doc))
    for source in rewritten_sources["sources"]:
        source.pop("database", None)
        source.pop("catalog", None)
        source["schema"] = namespace.raw_schema
    (execution_project / "models" / "sources.yml").write_text(
        yaml.safe_dump(rewritten_sources, sort_keys=True), encoding="utf-8"
    )
    json_columns = frozenset(
        column.name
        for table in package.task.tables
        for column in table.columns
        if column.type.value == "json"
    )
    float_or_temporal_columns = frozenset(
        column.name
        for table in package.task.tables
        for column in table.columns
        if column.type.value in {"float", "timestamp"}
    )
    timestamp_columns = frozenset(
        column.name
        for table in package.task.tables
        for column in table.columns
        if column.type.value == "timestamp"
    )
    for source_path in sql_paths:
        relative = source_path.relative_to(candidate_elt)
        sql = _read_bounded_text(source_path, MAX_DBT_SQL_BYTES, DbtErrorCode.PROJECT_INVALID)
        rewritten = rewrite_model_sql(
            sql,
            package.destination,
            json_columns=json_columns,
            float_or_temporal_columns=float_or_temporal_columns,
            timestamp_columns=timestamp_columns,
        )
        (execution_project / relative).write_text(rewritten, encoding="utf-8")
    return project_name


def _quote_identifier(value: str) -> str:
    return quote_sql_identifier(value, dialect="duckdb", force=True)


def _relation_sql(parts: Sequence[str]) -> str:
    return quote_sql_path(
        (str(part) for part in parts if str(part)),
        dialect="duckdb",
        force=True,
    )


def _raw_relation(namespace: "NamespaceProjection", table: str) -> str:
    relation = namespace.raw_relation(table)
    if isinstance(relation, str):
        return relation
    if isinstance(relation, Sequence):
        return _relation_sql(tuple(str(part) for part in relation))
    raise DbtTrustedFailure(DbtErrorCode.INPUT_DATABASE_INVALID)


def _mart_relation(namespace: "NamespaceProjection", mart: str) -> str:
    relation = namespace.mart_relation(mart)
    if isinstance(relation, str):
        return relation
    if isinstance(relation, Sequence):
        return _relation_sql(tuple(str(part) for part in relation))
    raise DbtTrustedFailure(DbtErrorCode.INPUT_DATABASE_INVALID)


def _fingerprint_physical_raw_state(
    database_path: Path,
    namespace: "NamespaceProjection",
    *,
    ignored_tables: frozenset[str] = frozenset(),
) -> str:
    """Digest every raw table, column, type, and value independent of order.

    Include metadata and unexpected tables; this proves immutability, not EL
    correctness.

    ``ignored_tables`` (casefolded) is the marts a destination materializes
    INTO the raw schema, which is Databricks and Redshift: their raw landing
    schema is the dbt target schema, so a mart is a new table there and would
    otherwise read as a raw mutation. It is empty for Snowflake, and a mart can
    never be named after a raw table, so no raw table is ever excluded; the
    digest of the raw tables themselves is unchanged.
    """

    if database_path.is_symlink() or not database_path.is_file():
        raise DbtTrustedFailure(DbtErrorCode.INPUT_DATABASE_INVALID)
    table_fingerprints: dict[str, str] = {}
    try:
        connection = duckdb.connect(str(database_path), read_only=True)
        try:
            listed = connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE lower(table_schema) = lower(?) "
                "AND table_type = 'BASE TABLE' "
                "ORDER BY lower(table_name), table_name",
                [namespace.raw_schema],
            ).fetchall()
            table_names = [str(row[0]) for row in listed]
            if len({name.casefold() for name in table_names}) != len(table_names):
                raise DbtTrustedFailure(DbtErrorCode.INPUT_DATABASE_INVALID)
            for table_name in table_names:
                if table_name.casefold() in ignored_tables:
                    continue
                relation = _relation_sql((namespace.raw_schema, table_name))
                cursor = connection.execute("SELECT * FROM " + relation)
                columns = tuple(
                    (str(item[0]), str(item[1])) for item in (cursor.description or ())
                )
                count = 0
                modulus = 1 << 256
                sum_one = 0
                sum_two = 0
                xor_value = 0
                while True:
                    row = cursor.fetchone()
                    if row is None:
                        break
                    count += 1
                    row_digest = hashlib.sha256(
                        canonical_json([list(strict_cell(value)) for value in row]).encode(
                            "utf-8"
                        )
                    ).digest()
                    number = int.from_bytes(row_digest, "big")
                    sum_one = (sum_one + number) % modulus
                    sum_two = (sum_two + number * number) % modulus
                    xor_value ^= number
                table_fingerprints[table_name] = hashlib.sha256(
                    canonical_json(
                        {
                            "columns": columns,
                            "row_count": count,
                            "sum": f"{sum_one:064x}",
                            "sum_squares": f"{sum_two:064x}",
                            "xor": f"{xor_value:064x}",
                        }
                    ).encode("utf-8")
                ).hexdigest()
        finally:
            connection.close()
    except DbtTrustedFailure:
        raise
    except Exception as exc:  # noqa: BLE001 - trusted database boundary
        raise DbtTrustedFailure(DbtErrorCode.INPUT_DATABASE_INVALID) from exc
    return hashlib.sha256(
        canonical_json(table_fingerprints).encode("utf-8")
    ).hexdigest()


def _post_dbt_raw_state(
    package: WorkspacePackage,
    execution: LocalSyncExecution,
):
    try:
        return verify_raw_state(package, execution)
    except LocalSyncHarnessError as exc:
        raise DbtTrustedFailure(DbtErrorCode.INPUT_DATABASE_INVALID) from exc


def _write_profile(
    profiles_dir: Path,
    database_path: Path,
    namespace: "NamespaceProjection",
) -> None:
    profiles_dir.mkdir(parents=True, exist_ok=False)
    profile = {
        DBT_PROFILE_NAME: {
            "target": DBT_PROFILE_TARGET,
            "outputs": {
                DBT_PROFILE_TARGET: {
                    "type": "duckdb",
                    "path": str(database_path),
                    "schema": namespace.dbt_target_schema,
                    "threads": 1,
                    # A Databricks CAST(x AS TIMESTAMP) renders as TIMESTAMPTZ;
                    # EXTRACT and DATE_TRUNC over it must not depend on the
                    # grading host's clock.
                    "settings": {"TimeZone": "UTC"},
                }
            },
        }
    }
    (profiles_dir / "profiles.yml").write_text(
        yaml.safe_dump(profile, sort_keys=True), encoding="utf-8"
    )


def _resource_preexec(limits: DbtRunnerLimits):
    def configure() -> None:
        if resource is None:
            return
        try:
            resource.setrlimit(
                resource.RLIMIT_FSIZE,
                (limits.max_state_bytes, limits.max_state_bytes),
            )
            resource.setrlimit(
                resource.RLIMIT_CORE,
                (0, 0),
            )
            if sys.platform.startswith("linux"):
                memory = limits.memory_limit_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
                if hasattr(resource, "RLIMIT_NPROC"):
                    resource.setrlimit(
                        resource.RLIMIT_NPROC,
                        (limits.process_limit, limits.process_limit),
                    )
        except (OSError, ValueError):
            os._exit(125)

    return configure


def _bounded_tree_size(root: Path, limit: int) -> int:
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                        if total > limit:
                            raise DbtPolicyFailure(DbtErrorCode.OUTPUT_LIMIT)
                    else:
                        raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
        except DbtPolicyFailure:
            raise
        except OSError as exc:
            raise DbtTrustedFailure(DbtErrorCode.HARNESS_INTERNAL) from exc
    return total


def _command_environment(state_dir: Path, profiles_dir: Path, target_dir: Path, log_dir: Path) -> dict[str, str]:
    temp_dir = state_dir / "tmp"
    cache_dir = state_dir / "cache"
    temp_dir.mkdir(exist_ok=True)
    cache_dir.mkdir(exist_ok=True)
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(temp_dir),
        "XDG_CACHE_HOME": str(cache_dir),
        "PYTHONNOUSERSITE": "1",
        "DBT_PROFILES_DIR": str(profiles_dir),
        "DBT_TARGET_PATH": str(target_dir),
        "DBT_LOG_PATH": str(log_dir),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        "DO_NOT_TRACK": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "",
    }


def _run_command(
    python: Path,
    command: str,
    *,
    project_dir: Path,
    profiles_dir: Path,
    target_dir: Path,
    log_dir: Path,
    state_dir: Path,
    limits: DbtRunnerLimits,
) -> _CommandRun:
    output_path = state_dir / f"{command}.output.log"
    args = [
        str(python.absolute()),
        "-m",
        "dbt.cli.main",
        command,
        "--no-partial-parse",
        "--profile",
        DBT_PROFILE_NAME,
        "--target",
        DBT_PROFILE_TARGET,
        "--project-dir",
        str(project_dir),
        "--profiles-dir",
        str(profiles_dir),
    ]
    if command == "run":
        args.append("--full-refresh")
    started = time.monotonic()
    timed_out = False
    return_code = -1
    try:
        with output_path.open("wb") as output:
            process = subprocess.Popen(
                args,
                cwd=str(project_dir),
                env=_command_environment(
                    state_dir, profiles_dir, target_dir, log_dir
                ),
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=_resource_preexec(limits) if os.name == "posix" else None,
            )
            try:
                return_code = process.wait(timeout=limits.command_timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    process.kill()
                return_code = process.wait(timeout=5)
    except OSError as exc:
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_UNAVAILABLE) from exc
    elapsed = max(0, round((time.monotonic() - started) * 1000))
    digest, output_size = _sha256_file(output_path)
    truncated = output_size > limits.max_command_output_bytes
    evidence = DbtCommandEvidence(
        command=command,
        succeeded=(return_code == 0 and not timed_out and not truncated),
        return_code=return_code,
        elapsed_ms=elapsed,
        output_sha256=digest,
        output_bytes=min(output_size, limits.max_command_output_bytes),
        output_truncated=truncated,
    )
    return _CommandRun(evidence=evidence, output_path=output_path, timed_out=timed_out)


def _load_manifest(target_dir: Path) -> dict[str, Any]:
    path = target_dir / "manifest.json"
    text = _read_bounded_text(path, MAX_DBT_MANIFEST_BYTES, DbtErrorCode.GRAPH_INVALID)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID) from exc
    if not isinstance(payload, dict):
        raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
    return payload


def _validate_manifest_graph(
    manifest: Mapping[str, Any],
    *,
    package: WorkspacePackage,
    namespace: "NamespaceProjection",
    project_name: str,
) -> None:
    metadata = manifest.get("metadata")
    if not isinstance(metadata, Mapping) or metadata.get("adapter_type") != "duckdb":
        raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
    nodes = manifest.get("nodes")
    sources = manifest.get("sources")
    if not isinstance(nodes, Mapping) or not isinstance(sources, Mapping):
        raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
    source_names = []
    for source in sources.values():
        if not isinstance(source, Mapping) or source.get("resource_type") != "source":
            raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
        identifier = source.get("identifier") or source.get("name")
        if not isinstance(identifier, str):
            raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
        if str(source.get("schema", "")).casefold() != namespace.raw_schema.casefold():
            raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
        source_names.append(identifier)
    expected_sources = {table.name for table in package.task.tables}
    if len(source_names) != len(set(source_names)) or set(source_names) != expected_sources:
        raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)

    model_by_id = {
        str(node_id): node
        for node_id, node in nodes.items()
        if isinstance(node, Mapping)
        and node.get("resource_type") == "model"
        and node.get("package_name") == project_name
    }
    source_ids = {
        str(source_id)
        for source_id, source in sources.items()
        if isinstance(source, Mapping)
    }
    aliases: dict[str, Mapping[str, Any]] = {}
    ids_by_alias: dict[str, str] = {}
    for node_id, node in model_by_id.items():
        if node.get("language", "sql") != "sql":
            raise DbtPolicyFailure(DbtErrorCode.UNSAFE_ARTIFACT)
        alias = node.get("alias") or node.get("name")
        if not isinstance(alias, str) or alias.casefold() in aliases:
            raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
        aliases[alias.casefold()] = node
        ids_by_alias[alias.casefold()] = node_id
    required_ids: set[str] = set()
    for mart in package.task.marts:
        node = aliases.get(mart.name.casefold())
        if node is None:
            raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
        required_ids.add(ids_by_alias[mart.name.casefold()])
        config = node.get("config")
        if not isinstance(config, Mapping) or config.get("materialized") != "table":
            raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
        if str(node.get("schema", "")).casefold() != namespace.mart_schema.casefold():
            raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
        depends_on = node.get("depends_on")
        if not isinstance(depends_on, Mapping) or not isinstance(depends_on.get("nodes", []), list):
            raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)

    reachable_models = set(required_ids)
    for required_id in required_ids:
        pending = [required_id]
        visited: set[str] = set()
        saw_source = False
        while pending:
            node_id = pending.pop()
            if node_id in visited:
                continue
            visited.add(node_id)
            node = model_by_id.get(node_id)
            if node is None:
                raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
            dependency = node.get("depends_on")
            dependency_nodes = (
                dependency.get("nodes") if isinstance(dependency, Mapping) else None
            )
            if not isinstance(dependency_nodes, list):
                raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
            for dependency_id in dependency_nodes:
                if not isinstance(dependency_id, str):
                    raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
                if dependency_id in source_ids:
                    saw_source = True
                elif dependency_id in model_by_id:
                    reachable_models.add(dependency_id)
                    pending.append(dependency_id)
                else:
                    # Tests, exposures, packages, and unresolved graph objects
                    # are outside portable-dbt-sql-v1.
                    raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
        if not saw_source:
            # A constant mart or direct physical table reference cannot prove
            # it transformed the candidate EL state.
            raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)
    if set(model_by_id) != reachable_models:
        # Extra models expand the execution surface and are accepted only when
        # they are a transitive ref() dependency of a required mart.
        raise DbtPolicyFailure(DbtErrorCode.GRAPH_INVALID)


def _read_evaluator_sql(package: WorkspacePackage, mart_name: str) -> str:
    path = (
        package.release_dir
        / "private"
        / package.task_id
        / "answer_key"
        / "evaluation"
        / "sql"
        / f"{mart_name}.sql"
    )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DbtTrustedFailure(DbtErrorCode.EVALUATOR_INVALID) from exc
    if not text.strip() or len(text.encode("utf-8")) > MAX_DBT_SQL_BYTES:
        raise DbtTrustedFailure(DbtErrorCode.EVALUATOR_INVALID)
    try:
        statements = parse(text, read="duckdb")
    except Exception as exc:  # noqa: BLE001
        raise DbtTrustedFailure(DbtErrorCode.EVALUATOR_INVALID) from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise DbtTrustedFailure(DbtErrorCode.EVALUATOR_INVALID)
    if any(isinstance(node, _MUTATING_SQL_NODES) for node in statements[0].walk()):
        raise DbtTrustedFailure(DbtErrorCode.EVALUATOR_INVALID)
    return text


def _read_reference_sql(package: WorkspacePackage, mart_name: str) -> str:
    """Read one trusted DuckDB reference query for evidence preflight."""

    path = (
        package.release_dir
        / "private"
        / package.task_id
        / "answer_key"
        / "reference"
        / f"{mart_name}.sql"
    )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DbtTrustedFailure(DbtErrorCode.GOLD_MISSING) from exc
    if not text.strip() or len(text.encode("utf-8")) > MAX_DBT_SQL_BYTES:
        raise DbtTrustedFailure(DbtErrorCode.GOLD_MISSING)
    try:
        statements = parse(text, read="duckdb")
    except Exception as exc:  # noqa: BLE001 - private task evidence boundary
        raise DbtTrustedFailure(DbtErrorCode.GOLD_MISSING) from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise DbtTrustedFailure(DbtErrorCode.GOLD_MISSING)
    if any(isinstance(node, _MUTATING_SQL_NODES) for node in statements[0].walk()):
        raise DbtTrustedFailure(DbtErrorCode.GOLD_MISSING)
    return text.rstrip().removesuffix(";")


def _validate_private_evidence(
    package: WorkspacePackage,
    population: PopulationName,
    gold_csvs: Mapping[str, str],
    *,
    namespace: "NamespaceProjection",
    state_dir: Path,
    limits: DbtRunnerLimits,
) -> Mapping[str, str]:
    """Validate private gold and evaluators against an attempt-local oracle copy.

    Build reference marts and require evaluator output to match frozen gold
    before candidate execution.
    """

    oracle = (
        package.release_dir
        / "private"
        / package.task_id
        / "oracle"
        / f"{population.value}.duckdb"
    )
    if oracle.is_symlink() or not oracle.is_file():
        raise DbtTrustedFailure(DbtErrorCode.GOLD_MISSING)
    preflight = state_dir / "private-evidence.duckdb"
    try:
        shutil.copyfile(oracle, preflight)
        connection = duckdb.connect(str(preflight))
        try:
            mart_schema = namespace.mart_schema
            connection.execute(
                "CREATE SCHEMA " + _quote_identifier(mart_schema)
            )
            evaluators: dict[str, str] = {}
            for mart in package.task.marts:
                gold_text = gold_csvs.get(mart.name)
                if not isinstance(gold_text, str):
                    raise DbtTrustedFailure(DbtErrorCode.GOLD_MISSING)
                try:
                    gold_columns, _gold_rows = upstream_eval.parse_canonical_csv(
                        gold_text
                    )
                except ValueError as exc:
                    raise DbtTrustedFailure(DbtErrorCode.GOLD_MISSING) from exc
                expected_columns = tuple(column.name for column in mart.columns)
                if tuple(name.casefold() for name in gold_columns) != tuple(
                    name.casefold() for name in expected_columns
                ):
                    raise DbtTrustedFailure(DbtErrorCode.GOLD_MISSING)

                reference_sql = _read_reference_sql(package, mart.name)
                relation = _relation_sql((mart_schema, mart.name))
                try:
                    connection.execute(
                        "CREATE TABLE " + relation + " AS " + reference_sql
                    )
                except Exception as exc:  # noqa: BLE001 - private evidence
                    raise DbtTrustedFailure(DbtErrorCode.GOLD_MISSING) from exc

                evaluator_sql = _read_evaluator_sql(package, mart.name)
                evaluators[mart.name] = evaluator_sql
                try:
                    columns, rows = _fetch_bounded_rows(
                        connection, evaluator_sql, limits=limits
                    )
                except DbtPolicyFailure as exc:
                    raise DbtTrustedFailure(DbtErrorCode.EVALUATOR_INVALID) from exc
                if (
                    tuple(name.casefold() for name in columns)
                    != tuple(name.casefold() for name in expected_columns)
                    or not _unique_key(rows, mart.key_columns)
                    or not upstream_eval.compare_mart(
                        gold_text,
                        rows,
                        mart,
                        actual_columns=columns,
                    )
                ):
                    raise DbtTrustedFailure(DbtErrorCode.GOLD_MISSING)
            return dict(sorted(evaluators.items()))
        finally:
            connection.close()
    except DbtTrustedFailure:
        raise
    except Exception as exc:  # noqa: BLE001 - private evidence boundary
        raise DbtTrustedFailure(DbtErrorCode.HARNESS_INTERNAL) from exc
    finally:
        try:
            preflight.unlink(missing_ok=True)
            Path(str(preflight) + ".wal").unlink(missing_ok=True)
        except OSError as exc:
            raise DbtTrustedFailure(DbtErrorCode.HARNESS_INTERNAL) from exc


def _fetch_bounded_rows(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    *,
    limits: DbtRunnerLimits,
) -> tuple[tuple[str, ...], list[Row]]:
    try:
        cursor = connection.execute(sql)
        columns = tuple(str(item[0]) for item in (cursor.description or ()))
        rows: list[Row] = []
        total_bytes = 0
        while True:
            raw = cursor.fetchone()
            if raw is None:
                break
            if len(rows) >= limits.max_result_rows_per_mart:
                raise DbtPolicyFailure(DbtErrorCode.OUTPUT_LIMIT)
            total_bytes += sum(len(str(value).encode("utf-8")) for value in raw)
            if total_bytes > limits.max_result_bytes_per_mart:
                raise DbtPolicyFailure(DbtErrorCode.OUTPUT_LIMIT)
            normalized = tuple(
                value.isoformat()
                if isinstance(value, (dt.datetime, dt.date, dt.time))
                else value
                for value in raw
            )
            rows.append(dict(zip(columns, normalized)))
        return columns, rows
    except DbtPolicyFailure:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DbtPolicyFailure(DbtErrorCode.RUN_FAILED) from exc


def _table_is_persistent(
    connection: duckdb.DuckDBPyConnection,
    namespace: "NamespaceProjection",
    mart_name: str,
) -> bool:
    rows = connection.execute(
        "SELECT table_type FROM information_schema.tables "
        "WHERE lower(table_schema) = lower(?) AND lower(table_name) = lower(?)",
        [namespace.mart_schema, mart_name],
    ).fetchall()
    return len(rows) == 1 and str(rows[0][0]).upper() == "BASE TABLE"


def _physical_mart_columns(
    connection: duckdb.DuckDBPyConnection,
    namespace: "NamespaceProjection",
    mart_name: str,
) -> tuple[str, ...]:
    relation = _mart_relation(namespace, mart_name)
    try:
        description = connection.execute("DESCRIBE SELECT * FROM " + relation).fetchall()
    except Exception as exc:  # noqa: BLE001
        raise DbtPolicyFailure(DbtErrorCode.MART_SCHEMA_INVALID) from exc
    return tuple(str(row[0]) for row in description)


def _unique_key(rows: Sequence[Row], columns: tuple[str, ...]) -> bool:
    if not columns:
        return False
    if not rows:
        return True
    lower = {name.casefold(): name for name in rows[0]}
    resolved = [lower.get(name.casefold()) for name in columns]
    if any(name is None for name in resolved):
        return False
    keys: set[tuple[Any, ...]] = set()
    for row in rows:
        key = tuple(row[name] for name in resolved if name is not None)
        if any(value is None for value in key):
            return False
        typed_key = tuple(strict_cell(value) for value in key)
        if typed_key in keys:
            return False
        keys.add(typed_key)
    return True


def _empty_result(
    package: WorkspacePackage,
    runtime: DbtRuntimeIdentity,
    raw_before: str,
    raw_after: str,
    commands: Sequence[DbtCommandEvidence],
    errors: Sequence[DbtErrorCode],
) -> DbtRunResult:
    all_errors = list(errors)
    if raw_before != raw_after:
        all_errors.append(DbtErrorCode.RAW_MUTATED)
    error_tuple = tuple(dict.fromkeys(all_errors))
    classes = {_FAILURE_CLASS_BY_CODE[code] for code in error_tuple}
    failure_class = (
        DbtFailureClass.POLICY_VIOLATION
        if DbtFailureClass.POLICY_VIOLATION in classes
        else DbtFailureClass.POLICY_FAILURE
    )
    marts = {
        mart.name: DbtMartEvidence(
            mart=mart.name,
            persistent_table=False,
            exact_columns=False,
            unique_key=False,
            matched_gold=False,
        )
        for mart in package.task.marts
    }
    return DbtRunResult(
        runtime=runtime,
        dbt_project=0.0,
        mart_reward=0.0,
        raw_immutable=(raw_before == raw_after),
        mart_scores={name: False for name in marts},
        marts=marts,
        commands=tuple(commands),
        error_codes=error_tuple,
        failure_class=failure_class,
        raw_before_sha256=raw_before,
        raw_after_sha256=raw_after,
    )


def run_dbt_project(
    package: WorkspacePackage,
    population: PopulationName | str,
    *,
    attempt_dir: Path,
    sync_execution: LocalSyncExecution,
    runtime_config: DbtRuntimeConfig,
    limits: DbtRunnerLimits | None = None,
) -> DbtRunResult:
    """Run and score dbt on authenticated state using a private candidate copy."""

    bounds = limits or DbtRunnerLimits()
    runtime = verify_dbt_runtime(runtime_config, limits=bounds)
    if not runtime.scoring_eligible:
        # A non-pinned interpreter may run tests but cannot emit training labels.
        raise DbtTrustedFailure(DbtErrorCode.RUNTIME_VERSION_MISMATCH)
    try:
        population_name = PopulationName(population)
    except ValueError as exc:
        raise DbtTrustedFailure(DbtErrorCode.GOLD_MISSING) from exc
    gold_csvs = package.gold.stage2_csv.get(population_name.value)
    if not isinstance(gold_csvs, Mapping) or set(gold_csvs) != {
        mart.name for mart in package.task.marts
    }:
        raise DbtTrustedFailure(DbtErrorCode.GOLD_MISSING)

    attempt = Path(attempt_dir).resolve(strict=True)
    candidate_elt = attempt / "elt"
    if (
        sync_execution.task_id != package.task_id
        or sync_execution.task_content_hash != package.task.content_hash()
        or sync_execution.population != population_name.value
    ):
        raise DbtTrustedFailure(DbtErrorCode.INPUT_DATABASE_INVALID)
    database = Path(sync_execution.database_path).resolve(strict=True)
    namespace = sync_execution.namespace
    try:
        database.relative_to(attempt)
    except ValueError as exc:
        raise DbtTrustedFailure(DbtErrorCode.INPUT_DATABASE_INVALID) from exc
    ignored_tables = shared_schema_mart_names(package, namespace)
    raw_before = _fingerprint_physical_raw_state(
        database, namespace, ignored_tables=ignored_tables
    )

    state_dir = attempt / ".workspace-runtime" / "dbt"
    try:
        state_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise DbtTrustedFailure(DbtErrorCode.HARNESS_INTERNAL) from exc
    project_dir = state_dir / "project"
    profiles_dir = state_dir / "profiles"
    target_dir = state_dir / "target"
    log_dir = state_dir / "logs"
    target_dir.mkdir()
    log_dir.mkdir()
    commands: list[DbtCommandEvidence] = []
    try:
        evaluator_sql_by_mart = _validate_private_evidence(
            package,
            population_name,
            gold_csvs,
            namespace=namespace,
            state_dir=state_dir,
            limits=bounds,
        )
        project_name = _prepare_execution_project(
            candidate_elt,
            project_dir,
            package=package,
            namespace=namespace,
        )
        _write_profile(profiles_dir, database, namespace)
    except DbtPolicyFailure as failure:
        _post_dbt_raw_state(package, sync_execution)
        raw_after = _fingerprint_physical_raw_state(
            database, namespace, ignored_tables=ignored_tables
        )
        return _empty_result(
            package, runtime, raw_before, raw_after, commands, [failure.code]
        )
    except DbtTrustedFailure:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DbtTrustedFailure(DbtErrorCode.HARNESS_INTERNAL) from exc

    for command, failure_code in (
        ("parse", DbtErrorCode.PARSE_FAILED),
        ("compile", DbtErrorCode.COMPILE_FAILED),
        ("run", DbtErrorCode.RUN_FAILED),
    ):
        command_run = _run_command(
            runtime_config.python,
            command,
            project_dir=project_dir,
            profiles_dir=profiles_dir,
            target_dir=target_dir,
            log_dir=log_dir,
            state_dir=state_dir,
            limits=bounds,
        )
        commands.append(command_run.evidence)
        try:
            _bounded_tree_size(state_dir, bounds.max_state_bytes)
        except DbtPolicyFailure as failure:
            _post_dbt_raw_state(package, sync_execution)
            raw_after = _fingerprint_physical_raw_state(
                database, namespace, ignored_tables=ignored_tables
            )
            return _empty_result(
                package,
                runtime,
                raw_before,
                raw_after,
                commands,
                [failure.code],
            )
        if command_run.timed_out:
            _post_dbt_raw_state(package, sync_execution)
            raw_after = _fingerprint_physical_raw_state(
                database, namespace, ignored_tables=ignored_tables
            )
            return _empty_result(
                package, runtime, raw_before, raw_after, commands, [DbtErrorCode.TIMEOUT]
            )
        if command_run.evidence.output_truncated:
            _post_dbt_raw_state(package, sync_execution)
            raw_after = _fingerprint_physical_raw_state(
                database, namespace, ignored_tables=ignored_tables
            )
            return _empty_result(
                package,
                runtime,
                raw_before,
                raw_after,
                commands,
                [DbtErrorCode.OUTPUT_LIMIT],
            )
        if not command_run.evidence.succeeded:
            _post_dbt_raw_state(package, sync_execution)
            raw_after = _fingerprint_physical_raw_state(
                database, namespace, ignored_tables=ignored_tables
            )
            return _empty_result(
                package, runtime, raw_before, raw_after, commands, [failure_code]
            )
        if command == "parse":
            try:
                _validate_manifest_graph(
                    _load_manifest(target_dir),
                    package=package,
                    namespace=namespace,
                    project_name=project_name,
                )
            except DbtPolicyFailure as failure:
                _post_dbt_raw_state(package, sync_execution)
                raw_after = _fingerprint_physical_raw_state(
                database, namespace, ignored_tables=ignored_tables
            )
                return _empty_result(
                    package,
                    runtime,
                    raw_before,
                    raw_after,
                    commands,
                    [failure.code],
                )

    raw_after_state = _post_dbt_raw_state(package, sync_execution)
    raw_after = _fingerprint_physical_raw_state(
        database, namespace, ignored_tables=ignored_tables
    )
    error_codes: list[DbtErrorCode] = []
    immutable = (
        raw_before == raw_after
        and raw_state_immutable(sync_execution.raw_state, raw_after_state)
    )
    if not immutable:
        error_codes.append(DbtErrorCode.RAW_MUTATED)
    mart_evidence: dict[str, DbtMartEvidence] = {}
    mart_scores: dict[str, bool] = {}
    try:
        connection = duckdb.connect(str(database), read_only=True)
        try:
            for mart in package.task.marts:
                persistent = _table_is_persistent(connection, namespace, mart.name)
                if not persistent:
                    error_codes.append(DbtErrorCode.MART_MISSING)
                    evidence = DbtMartEvidence(
                        mart=mart.name,
                        persistent_table=False,
                        exact_columns=False,
                        unique_key=False,
                        matched_gold=False,
                    )
                    mart_evidence[mart.name] = evidence
                    mart_scores[mart.name] = False
                    continue
                expected_columns = tuple(column.name for column in mart.columns)
                physical_columns = _physical_mart_columns(
                    connection, namespace, mart.name
                )
                physical_exact = tuple(
                    name.casefold() for name in physical_columns
                ) == tuple(name.casefold() for name in expected_columns)
                evaluator_sql = evaluator_sql_by_mart[mart.name]
                columns, rows = _fetch_bounded_rows(
                    connection, evaluator_sql, limits=bounds
                )
                exact_columns = physical_exact and (
                    tuple(name.casefold() for name in columns)
                    == tuple(name.casefold() for name in expected_columns)
                )
                unique_key = _unique_key(rows, mart.key_columns)
                if not exact_columns:
                    error_codes.append(DbtErrorCode.MART_SCHEMA_INVALID)
                if not unique_key:
                    error_codes.append(DbtErrorCode.MART_KEY_INVALID)
                matched = (
                    exact_columns
                    and unique_key
                    and upstream_eval.compare_mart(
                        str(gold_csvs[mart.name]),
                        rows,
                        mart,
                        actual_columns=columns,
                    )
                )
                evidence = DbtMartEvidence(
                    mart=mart.name,
                    persistent_table=True,
                    exact_columns=exact_columns,
                    unique_key=unique_key,
                    matched_gold=matched,
                )
                mart_evidence[mart.name] = evidence
                mart_scores[mart.name] = matched
        finally:
            connection.close()
    except DbtPolicyFailure as failure:
        error_codes.append(failure.code)
        for mart in package.task.marts:
            mart_evidence.setdefault(
                mart.name,
                DbtMartEvidence(
                    mart=mart.name,
                    persistent_table=False,
                    exact_columns=False,
                    unique_key=False,
                    matched_gold=False,
                ),
            )
            mart_scores.setdefault(mart.name, False)
    except DbtTrustedFailure:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DbtTrustedFailure(DbtErrorCode.HARNESS_INTERNAL) from exc

    error_tuple = tuple(dict.fromkeys(error_codes))
    failure_class: DbtFailureClass | None = None
    if error_tuple:
        classes = {_FAILURE_CLASS_BY_CODE[code] for code in error_tuple}
        failure_class = (
            DbtFailureClass.POLICY_VIOLATION
            if DbtFailureClass.POLICY_VIOLATION in classes
            else DbtFailureClass.POLICY_FAILURE
        )
    mart_reward = (
        sum(mart_scores.values()) / len(package.task.marts)
        if package.task.marts
        else 0.0
    )
    return DbtRunResult(
        runtime=runtime,
        dbt_project=1.0,
        mart_reward=mart_reward,
        raw_immutable=immutable,
        mart_scores=dict(sorted(mart_scores.items())),
        marts=dict(sorted(mart_evidence.items())),
        commands=tuple(commands),
        error_codes=error_tuple,
        failure_class=failure_class,
        raw_before_sha256=raw_before,
        raw_after_sha256=raw_after,
    )


__all__ = [
    "DBT_COMPATIBILITY_SUBSET_VERSION",
    "DBT_DUCKDB_ADAPTER_VERSION",
    "DBT_DUCKDB_CORE_VERSION",
    "DBT_DUCKDB_ENGINE_VERSION",
    "DBT_DUCKDB_INSTALLED_DISTRIBUTIONS_SHA256",
    "DBT_DUCKDB_RUNTIME_MANIFEST_SHA256",
    "DBT_DUCKDB_RUNTIME_SCHEMA_VERSION",
    "DBT_DUCKDB_UV_LOCK_SHA256",
    "DbtCommandEvidence",
    "DbtErrorCode",
    "DbtFailureClass",
    "DbtMartEvidence",
    "DbtPolicyFailure",
    "DbtRunResult",
    "DbtRunnerLimits",
    "DbtRuntimeConfig",
    "DbtRuntimeIdentity",
    "DbtRuntimeVerificationScope",
    "DbtTrustedFailure",
    "rewrite_model_sql",
    "run_dbt_project",
    "verify_dbt_runtime",
]

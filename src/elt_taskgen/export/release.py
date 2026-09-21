"""Freeze accepted tasks into an immutable release.

Each release contains one public end-to-end task and private EL/T evidence,
source populations under populations/<population>/rendered/, and DuckDB
oracles. Oracle identity comes from the BATTERY-CERTIFIED census: the digest
the transform battery recorded for that population, not whatever the shipped
warehouse happens to hash to, so a warehouse nobody certified cannot be frozen.
Dependency drift is rejected unless ``allow_unlocked_env`` records it. This
module verifies acceptance but does not decide it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Protocol, Sequence, runtime_checkable

import yaml
from pydantic import BaseModel, ConfigDict, Field

import elt_taskgen
from elt_taskgen import runtime_matrix
from elt_taskgen.destinations import (
    destination_contract,
    destination_from_config,
)
from elt_taskgen.export import eltbench as eltbench_mod
from elt_taskgen.export.eltbench import (
    CENSUS_VERSION,
    EL_SOURCES_REL_TEMPLATE,
    REWARD_MANIFEST,
    SOURCES_SERVING_MANIFEST,
    WAREHOUSE_CENSUS_EVIDENCE_REL,
    WAREHOUSE_CENSUS_KIND,
    WAREHOUSE_DIRNAME,
    assert_public_runtime_shape,
    assert_public_runtime_tree_clean,
    assert_source_identifiers,
    census_digest,
    warehouse_census,
)
from elt_taskgen.models import (
    DifficultyMeasurement,
    Origin,
    RLVR_TASK_VARIANTS,
    TaskIR,
    TaskVariant,
    canonical_json,
    readable_json,
    sha256_hex,
    task_from_json,
    validate_task_id_segment,
    variant_task_id,
)
from elt_taskgen.package_resources import resource_path
from elt_taskgen.provenance import (
    IngestProvenance,
    RELEASE_PROVENANCE_DIRNAME,
    RELEASE_PROVENANCE_FILENAME,
    load_current as load_current_provenance,
    release_provenance_rel,
    validate_task_binding,
)
from elt_taskgen.runtime.attestation import SandboxAttestation
from elt_taskgen.semantic_contract import SEMANTIC_SCORER_VERSION

GENERATOR_VERSION = getattr(elt_taskgen, "__version__", "0.0.0")

#: Default SHA-256 byte checksum, including all files in legacy manifests.
BYTE_CHECKSUM_KIND = "sha256"

#: Only DuckDB files may use a census checksum instead of a byte checksum.
WAREHOUSE_SUFFIX = ".duckdb"

#: Versioned census checksum for DuckDB data.
_WAREHOUSE_CHECKSUM_KIND_PREFIX = "duckdb-census/"
WAREHOUSE_CHECKSUM_KIND = f"{_WAREHOUSE_CHECKSUM_KIND_PREFIX}{CENSUS_VERSION}"

#: Flat-file header explaining the exemption to whoever runs `shasum -c`.
#: `sha256sum`/`shasum -c` ignore '#' lines, so the census entries are recorded
#: as comments there and pinned for real in the manifest.
_CHECKSUM_FILE_HEADER = (
    "# sha256sum-format checksums for this release.",
    "# Every entry below is a BYTE hash and verifies with `shasum -c`.",
    f"# {WAREHOUSE_SUFFIX} warehouses are NOT byte-pinned: DuckDB storage bytes",
    "# differ between two clean builds of identical data. They are pinned by",
    "# warehouse census instead (listed below, authoritative copy in",
    "# release_manifest.json: checksum_kinds + warehouse_census).",
)


#: Manifest schema written by the current freezer; older manifests remain
#: readable and verifiable under their own recorded rules.
RELEASE_SCHEMA_VERSION = "3.5"

#: Development permits local iteration; certified requires publication evidence.
DEVELOPMENT_RELEASE_MODE = "development"
CERTIFIED_RELEASE_MODE = "certified"
RELEASE_MODES = frozenset({DEVELOPMENT_RELEASE_MODE, CERTIFIED_RELEASE_MODE})

#: Root-level isolation evidence for a certified release. It is byte-pinned in
#: ``checksums`` and its schema-level seal is pinned separately in the manifest.
SANDBOX_ATTESTATION_FILENAME = "sandbox_attestation.json"

#: Private empirical solver evidence and its size limit.
DIFFICULTY_REPORT_REL = Path("reports") / "difficulty.json"
_MAX_DIFFICULTY_REPORT_BYTES = 4 * 1024 * 1024
_MAX_SANDBOX_ATTESTATION_BYTES = 1 * 1024 * 1024

#: First schema with a private, hash-bound semantic task description.
SEMANTIC_PACKAGE_MIN_SCHEMA = "3.2"
#: Schema 3.4 refuses packages whose pinned source/gold bytes cannot be used
#: by the strict canonical warehouse-parity path.  Older manifests retain
#: their original verification semantics.
SEMANTIC_PORTABILITY_MIN_SCHEMA = "3.4"
SEMANTIC_DIRNAME = "semantic"
SEMANTIC_TASK_IR_FILENAME = "task_ir.json"
SEMANTIC_TASK_IR_REL = f"{SEMANTIC_DIRNAME}/{SEMANTIC_TASK_IR_FILENAME}"

#: Schema-3 release identity. Internal evidence still records extract_load and
#: transform separately, while exactly one combined parent task is public.
COMBINED_CORPUS_PROFILE = "eltbench_end_to_end"
COMBINED_PUBLIC_LAYOUT = "combined"
LEGACY_SPLIT_PUBLIC_LAYOUT = "split_variants"

#: First schema whose rules include the serving surface.
_SERVING_SURFACE_MIN_SCHEMA = "2.1"

#: These prefixes separate semantic, runtime, and certification identities;
#: legacy `release_id` remains release-wide, and layer changes rotate dependents.
SEMANTIC_ID_PREFIX = "semantic-"
RUNTIME_BUNDLE_ID_PREFIX = "runtime-"
CERTIFICATION_ID_PREFIX = "certification-"

#: First schema that records layered identities.
_LAYERED_IDENTITY_MIN_SCHEMA = "3.3"

#: Schema 3.4 fixes the identity chain. Retain 3.3 algorithms only to verify
#: historical manifests; new certification requires the corrected chain.
_CHAINED_IDENTITY_MIN_SCHEMA = "3.4"

#: First schema with immutable source selector, revision, and digest evidence.
_SOURCE_PROVENANCE_MIN_SCHEMA = "3.5"

#: Runtime packages whose versions are recorded and drift-checked: everything
#: that can change a digest, a plan or a comparison.
_PINNED_RUNTIME_PACKAGES = (
    "duckdb",
    "sqlglot",
    "pydantic",
    "pyyaml",
    "python-hcl2",
    "lark",
)


def _scorer_version() -> str:
    """The single reward implementation's version (verification/gates.py).

    Falls back to a traceable sentinel, never a pass, if gates is a scaffold.
    """
    from elt_taskgen.verification import gates

    return str(getattr(gates, "SCORER_VERSION", "0.0.0+unversioned"))


def _roster_digest() -> str:
    """Identity of the gate roster the shipped batteries were measured against."""
    from elt_taskgen.verification import gates

    return str(getattr(gates, "ROSTER_DIGEST", ""))


def battery_roster(variant: TaskVariant) -> tuple[str, ...]:
    """The gate names a battery for `variant` must cover.

    Recorded in the manifest so a release alone answers "which gates certified
    this unit?" — scorer version alone cannot distinguish roster sizes.
    """
    from elt_taskgen.verification import variant_battery as battery_mod

    return tuple(battery_mod.gate_roster(TaskVariant(variant)))


def repo_lock_path() -> Path:
    """Path to the shipped uv.lock in a checkout or installed distribution."""
    return resource_path("uv.lock")


def runtime_versions() -> dict[str, str]:
    """The interpreter + dependency versions this process is running.

    Provenance, not a gate: every determinism claim here is a SAME-ENVIRONMENT
    claim, so a release must record which environment that was.
    """
    import platform
    from importlib.metadata import PackageNotFoundError, version

    versions: dict[str, str] = {
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
    }
    for name in _PINNED_RUNTIME_PACKAGES:
        try:
            versions[name] = version(name)
        except PackageNotFoundError:  # pragma: no cover - dependency is required
            versions[name] = "missing"
    return dict(sorted(versions.items()))


def locked_versions(lock_path: Path | None = None) -> dict[str, str]:
    """package -> version pinned by uv.lock, for the runtime dependencies.

    Empty when the lock is absent/unreadable; the CALLER decides what that
    means (`environment_drift` never reports it as clean).
    """
    import tomllib

    path = Path(lock_path) if lock_path is not None else repo_lock_path()
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    wanted = set(_PINNED_RUNTIME_PACKAGES)
    return {
        str(pkg.get("name")): str(pkg.get("version"))
        for pkg in data.get("package", [])
        if isinstance(pkg, dict) and str(pkg.get("name")) in wanted
    }


def environment_drift(lock_path: Path | None = None) -> dict[str, tuple[str, str]]:
    """package -> (installed, locked) for every runtime dependency that differs.

    Empty means the installed environment IS the locked one. A MISSING lock is
    never silently clean — it reports `{'uv.lock': ('missing', '')}`.
    """
    path = Path(lock_path) if lock_path is not None else repo_lock_path()
    locked = locked_versions(path)
    if not locked:
        return {"uv.lock": ("missing", "")}
    installed = runtime_versions()
    drift: dict[str, tuple[str, str]] = {}
    for name in _PINNED_RUNTIME_PACKAGES:
        want = locked.get(name)
        got = installed.get(name, "missing")
        if want is None:
            drift[name] = (got, "unpinned")
        elif want != got:
            drift[name] = (got, want)
    return dict(sorted(drift.items()))


@runtime_checkable
class EngineLike(Protocol):
    """Structural view of engine.Engine (owned by another builder)."""

    workspace: Path

    def load_task(self, task_id: str) -> TaskIR: ...
    def latest_report(self, task_id: str, stage: str) -> Any | None: ...
    def final_verdict(self, task_id: str) -> str: ...


@runtime_checkable
class SelectionLike(Protocol):
    """Structural view of corpus.selection.SelectionResult."""

    train: tuple[str, ...]
    val: tuple[str, ...]
    variants: dict[str, tuple[str, ...]]
    empirical_required: bool
    task_content_hashes: dict[str, str]
    difficulty_measurements: dict[str, str]


class VariantAcceptance(BaseModel):
    """How thoroughly ONE graded unit was validated, as recorded in the manifest.

    `gates_applicable` is the ROSTER size; `gates_recorded`/`gates_passed` are
    what the battery actually did, so a battery that ran fewer gates than its
    roster is visible in the shipped artifact, not only in the workspace.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    variant: str = Field(min_length=1)
    variant_task_id: str = Field(min_length=1)
    accepted: bool
    shipped: bool
    #: Roster size for this variant (gates that MUST be present).
    gates_applicable: int = Field(ge=0)
    gates_recorded: int = Field(ge=0)
    gates_passed: int = Field(ge=0)
    failing_gates: tuple[str, ...] = ()
    missing_gates: tuple[str, ...] = ()
    #: 'task-level' | 'variant-local' | '' (no failure). See design R5.
    classification: str = ""
    #: Ledger stage carrying this battery ('gates', 'gates_extract_load', ...).
    battery_stage: str = Field(min_length=1)
    #: Empty when accepted; otherwise why this unit is not in the release.
    refusal_reason: str = ""


class WarehouseCensusRecord(BaseModel):
    """Reproducible identity for one shipped DuckDB warehouse.

    Row counts and order-independent row-multiset digests identify the data
    independently of storage layout. Per-relation details locate divergence
    and must agree with the aggregate digest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: eltbench.CENSUS_VERSION at freeze time. Digests are comparable only
    #: within one version; a mismatch is refused, never silently accepted.
    census_version: str = Field(min_length=1)
    #: The pinned value; equals checksums[rel] for this warehouse.
    census_digest: str = Field(min_length=1)
    row_counts: dict[str, int]
    row_digests: dict[str, str]
    #: Catalog digest covering macros, views, constraints, and comments.
    catalog_digest: str = ""


class ReleaseManifest(BaseModel):
    """Frozen record of exactly what was released, traceable to code versions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Missing in historical manifests. Defaults deliberately identify those
    # records as legacy rather than silently re-labelling them as EL/T.
    schema_version: str = "1.0"
    corpus_profile: str = "legacy_full"
    #: Solver-facing directory layout. Defaults to the historical two-unit
    #: shape so schema-1/2 manifests continue to mean what they recorded.
    public_layout: str = LEGACY_SPLIT_PUBLIC_LAYOUT
    #: parent task_id -> agent-facing destination. Empty means the manifest
    #: predates multi-destination schema 3.1.
    destinations: dict[str, str] = Field(default_factory=dict)
    #: parent task_id -> Airbyte destination image tag expected at bootstrap.
    #: ``legacy-unpinned`` is retained only when reading/freezing against an
    #: older destination contract that genuinely has no version pin.
    destination_connector_versions: dict[str, str] = Field(default_factory=dict)
    #: Explicit release policy. Historical manifests parse as development and
    #: therefore are not retroactively represented as certified.
    release_mode: Literal["development", "certified"] = DEVELOPMENT_RELEASE_MODE
    #: Schema-level seal of ``sandbox_attestation.json``. Empty is required for
    #: development releases; certified releases require a 64-hex digest.
    sandbox_attestation_digest: str = ""
    release_id: str = Field(min_length=1)
    tasks: dict[str, str]                  # task_id -> TaskIR content hash
    #: task_id -> sha256(canonical_json(parsed DifficultyMeasurement)). Exact
    #: file bytes are independently covered by ``checksums``.
    difficulty_measurements: dict[str, str] = Field(default_factory=dict)
    splits: dict[str, str]                 # task_id -> 'train'|'val'
    families: dict[str, str]               # task_id -> family_id
    licenses: dict[str, str]               # task_id -> license
    #: Immutable source identity by task; empty in pre-3.5 manifests.
    source_provenance: dict[str, IngestProvenance] = Field(default_factory=dict)
    #: task_id -> canonical parsed-record digest. The sidecar's exact bytes are
    #: independently pinned by ``checksums``.
    source_provenance_digests: dict[str, str] = Field(default_factory=dict)
    checksums: dict[str, str]              # rel_path -> pinned digest (public/ + private/)
    #: Non-byte checksum rules by relative path; empty in legacy manifests.
    checksum_kinds: dict[str, str] = Field(default_factory=dict)
    #: rel_path -> census evidence for every census-pinned warehouse. The
    #: exemption is visible here, never implied.
    warehouse_census: dict[str, WarehouseCensusRecord] = Field(default_factory=dict)
    #: parent task_id -> the two internal certification phases represented in
    #: this release. They do not name schema-3 public directories.
    variants: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    #: parent task_id -> acceptance records for EL and T only.
    variant_acceptance: dict[str, tuple[VariantAcceptance, ...]] = Field(
        default_factory=dict
    )
    #: variant_task_id -> refusal reason (R7: a stage rejection is recorded,
    #: never silent).
    rejected_variants: dict[str, str] = Field(default_factory=dict)
    #: parent task_id -> population -> release-relative rendered source root,
    #: the roots the EL reward is computed against. Empty in a 2.0 manifest,
    #: which is exactly how a stale release is recognised.
    el_sources: dict[str, dict[str, str]] = Field(default_factory=dict)
    #: parent task_id -> population -> proven relation between that population
    #: and primary (today only 'rearrangement_of:primary'). A trainer
    #: aggregating per-population rewards can dedupe instead of double-counting.
    population_relations: dict[str, dict[str, str]] = Field(default_factory=dict)
    #: variant -> the gate roster that certified it, and the roster identity.
    #: A manifest without them predates roster identity (legacy 1.0.0 scorer).
    gate_rosters: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    roster_digest: str = ""
    #: Interpreter + dependency versions that FROZE this release, and any
    #: drift from uv.lock at that moment (recorded, never hidden).
    environment: dict[str, str] = Field(default_factory=dict)
    environment_drift: dict[str, str] = Field(default_factory=dict)
    scorer_version: str = Field(min_length=1)
    #: Version of the private combined-task DuckDB execution contract. Empty
    #: only in manifests older than schema 3.2.
    semantic_scorer_version: str = ""
    #: Schema-3.3+ layered identities. The EMPTY defaults identify a pre-3.3
    #: manifest, which is never retroactively judged by them.
    #: Destination-independent identity of what the release measures.
    semantic_release_id: str = ""
    #: parent task_id -> runtime bundle identity. Per-destination by
    #: construction: each shipped task binds exactly one destination.
    runtime_bundle_ids: dict[str, str] = Field(default_factory=dict)
    #: parent task_id -> certification identity for the recorded matrix.
    certification_ids: dict[str, str] = Field(default_factory=dict)
    #: Frozen execution matrix for each shipped destination.
    certification_matrix: dict[str, dict[str, str]] = Field(default_factory=dict)
    generator_version: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _report_field(report: Any, name: str) -> Any:
    """Read a field from a ReportRow-like object (attribute or mapping)."""
    if isinstance(report, dict):
        return report.get(name)
    return getattr(report, name, None)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_release_relative_path(value: str) -> str:
    """Return one canonical, portable path that cannot leave a release root."""

    if not isinstance(value, str) or not value:
        raise ValueError("path must be a non-empty string")
    if value.startswith("/"):
        raise ValueError("absolute paths are forbidden")
    if "\\" in value:
        raise ValueError("backslashes are forbidden path separators")
    parts = value.split("/")
    if any(not part for part in parts):
        raise ValueError("empty path segments are forbidden")
    for part in parts:
        try:
            validate_task_id_segment(part)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsafe path segment {part!r}: {exc}") from exc
    return "/".join(parts)


def _manifest_path_problems(manifest: ReleaseManifest) -> tuple[str, ...]:
    """Find every manifest-controlled segment/path before it is ever joined."""

    problems: list[str] = []
    task_id_maps = (
        "tasks",
        "destinations",
        "destination_connector_versions",
        "difficulty_measurements",
        "splits",
        "families",
        "licenses",
        "source_provenance",
        "source_provenance_digests",
        "variants",
        "variant_acceptance",
        "rejected_variants",
        "el_sources",
        "population_relations",
        "runtime_bundle_ids",
        "certification_ids",
    )
    for field_name in task_id_maps:
        for task_id in getattr(manifest, field_name):
            try:
                validate_task_id_segment(task_id)
            except (TypeError, ValueError) as exc:
                problems.append(f"{field_name} key {task_id!r}: {exc}")

    for field_name in ("checksums", "checksum_kinds", "warehouse_census"):
        for rel in getattr(manifest, field_name):
            try:
                _validate_release_relative_path(rel)
            except ValueError as exc:
                problems.append(f"{field_name} path {rel!r}: {exc}")

    for task_id, population_roots in manifest.el_sources.items():
        for population, rel in population_roots.items():
            try:
                _validate_release_relative_path(rel)
            except ValueError as exc:
                problems.append(
                    f"el_sources[{task_id!r}][{population!r}] path {rel!r}: {exc}"
                )
    return tuple(problems)


def _release_tree_problems(release_dir: Path) -> tuple[tuple[str, str], ...]:
    """Reject links and special nodes without following them during discovery."""

    problems: list[tuple[str, str]] = []
    walk_errors: list[OSError] = []

    def on_error(error: OSError) -> None:
        walk_errors.append(error)

    for current, dirnames, filenames in os.walk(
        release_dir, topdown=True, followlinks=False, onerror=on_error
    ):
        current_path = Path(current)
        traversable_dirs: list[str] = []
        for name in sorted(dirnames):
            path = current_path / name
            rel = path.relative_to(release_dir).as_posix()
            try:
                metadata = path.lstat()
            except OSError as exc:
                problems.append((rel, f"cannot inspect filesystem entry: {exc}"))
                continue
            if stat.S_ISLNK(metadata.st_mode):
                problems.append((rel, "symbolic links are forbidden in releases"))
            elif not stat.S_ISDIR(metadata.st_mode):
                problems.append((rel, "non-directory node appears in directory inventory"))
            else:
                traversable_dirs.append(name)
        # Be explicit even though followlinks=False: never descend through a
        # link that os.walk classified as a directory on this platform.
        dirnames[:] = traversable_dirs

        for name in sorted(filenames):
            path = current_path / name
            rel = path.relative_to(release_dir).as_posix()
            try:
                metadata = path.lstat()
            except OSError as exc:
                problems.append((rel, f"cannot inspect filesystem entry: {exc}"))
                continue
            if stat.S_ISLNK(metadata.st_mode):
                problems.append((rel, "symbolic links are forbidden in releases"))
            elif not stat.S_ISREG(metadata.st_mode):
                problems.append((rel, "only regular files are permitted in releases"))

    for error in walk_errors:
        path = Path(error.filename) if error.filename else release_dir
        try:
            rel = path.relative_to(release_dir).as_posix()
        except ValueError:
            rel = str(path)
        problems.append((rel, f"cannot walk release directory: {error}"))
    return tuple(problems)


def _assert_safe_release_input_tree(root: Path, *, label: str) -> None:
    """Refuse links/special nodes before copytree can dereference host paths."""

    try:
        metadata = root.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unreadable: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{label} must not be a symbolic link: {root}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a directory: {root}")
    problems = _release_tree_problems(root)
    if problems:
        rel, detail = problems[0]
        suffix = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
        raise ValueError(f"{label} contains unsafe entry {rel!r}: {detail}{suffix}")


def _is_sha256(value: str) -> bool:
    """Whether ``value`` is one lowercase hexadecimal SHA-256 digest."""

    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _difficulty_report_rel(task_id: str) -> str:
    return (Path("private") / task_id / DIFFICULTY_REPORT_REL).as_posix()


def _difficulty_measurement_digest(measurement: DifficultyMeasurement) -> str:
    """Canonical evidence digest (formatting-independent)."""

    return sha256_hex(canonical_json(measurement.model_dump(mode="json")))


def _validate_difficulty_measurement(
    measurement: DifficultyMeasurement,
    *,
    task_id: str,
    content_hash: str,
    require_empirical: bool,
    expected_campaign_fingerprint: str | None = None,
) -> None:
    """Require current evidence; certified releases additionally need empirical."""

    if measurement.task_id != task_id:
        raise ValueError(
            f"difficulty evidence for task {task_id!r} names "
            f"{measurement.task_id!r}"
        )
    if measurement.task_content_hash != content_hash:
        raise ValueError(
            f"difficulty evidence for task {task_id!r} is stale: binds "
            f"{measurement.task_content_hash[:12]}, current {content_hash[:12]}"
        )
    empirical = measurement.empirical
    if empirical is None:
        if require_empirical:
            raise ValueError(
                f"difficulty evidence for task {task_id!r} is structural-only; "
                "a certified release requires empirical solver evidence"
            )
        return
    # One predicate governs selection, direct CLI release, concurrent pipeline
    # release and this final freezer boundary; do not let those definitions of
    # a complete empirical campaign drift apart.
    from elt_taskgen.corpus.selection import empirical_evidence_problem

    problem = empirical_evidence_problem(
        measurement,
        expected_campaign_fingerprint=expected_campaign_fingerprint,
    )
    if problem is not None:
        raise ValueError(
            f"empirical difficulty for task {task_id!r} is invalid: {problem}"
        )


def _stage_difficulty_measurement(
    workspace: Path,
    private_root: Path,
    task: TaskIR,
    *,
    required: bool,
    expected_campaign_fingerprint: str | None = None,
) -> str | None:
    """Validate and copy one workspace difficulty report, returning its seal.

    A development release may omit this report or bind a current structural-only
    measurement for backwards compatibility. Certified releases require a
    complete empirical measurement; malformed or stale evidence is never
    silently omitted in either mode.
    """

    source = workspace / "tasks" / task.task_id / DIFFICULTY_REPORT_REL
    if not source.exists():
        if source.is_symlink():
            raise ValueError(
                f"task {task.task_id!r} difficulty evidence is a dangling symlink: "
                f"{source}"
            )
        if required:
            raise ValueError(
                f"task {task.task_id!r} has no empirical difficulty evidence at "
                f"{source}; run calibration at the current content hash"
            )
        return None
    if source.is_symlink() or source.parent.is_symlink():
        raise ValueError(
            f"task {task.task_id!r} difficulty evidence must be a regular, "
            f"non-symlink file: {source}"
        )
    metadata = source.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(
            f"task {task.task_id!r} difficulty evidence is not a regular file: "
            f"{source}"
        )
    if metadata.st_size > _MAX_DIFFICULTY_REPORT_BYTES:
        raise ValueError(
            f"task {task.task_id!r} difficulty evidence exceeds "
            f"{_MAX_DIFFICULTY_REPORT_BYTES} bytes"
        )
    raw = source.read_bytes()
    if len(raw) > _MAX_DIFFICULTY_REPORT_BYTES:
        raise ValueError(
            f"task {task.task_id!r} difficulty evidence grew beyond "
            f"{_MAX_DIFFICULTY_REPORT_BYTES} bytes while being read"
        )
    try:
        measurement = DifficultyMeasurement.model_validate_json(raw)
    except ValueError as exc:
        raise ValueError(
            f"task {task.task_id!r} difficulty evidence is invalid: {exc}"
        ) from exc
    _validate_difficulty_measurement(
        measurement,
        task_id=task.task_id,
        content_hash=task.content_hash(),
        require_empirical=required,
        expected_campaign_fingerprint=expected_campaign_fingerprint,
    )
    destination = private_root / task.task_id / DIFFICULTY_REPORT_REL
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    return _difficulty_measurement_digest(measurement)


def _warehouse_record(path: Path) -> WarehouseCensusRecord:
    """Census one shipped warehouse into its manifest record (read-only)."""
    census = warehouse_census(path)
    tables = census["tables"]
    return WarehouseCensusRecord(
        census_version=CENSUS_VERSION,
        census_digest=census["census_digest"],
        row_counts={name: int(t["row_count"]) for name, t in sorted(tables.items())},
        row_digests={name: str(t["row_digest"]) for name, t in sorted(tables.items())},
        catalog_digest=str(census["catalog_digest"]),
    )


def _record_census_digest(record: WarehouseCensusRecord) -> str:
    """Recompute the roll-up from a record's own per-relation detail.

    Catches a manifest edited in one place but not the other: digest, table
    detail and catalog digest must agree before any is compared to disk. A
    current-version record with no catalog digest is itself inconsistent, so it
    is refused rather than folded in as ''.
    """
    if set(record.row_counts) != set(record.row_digests):
        raise ValueError("census record row_counts/row_digests cover different relations")
    if record.census_version == CENSUS_VERSION and not record.catalog_digest:
        raise ValueError(
            f"census record at version {CENSUS_VERSION} carries no catalog "
            "digest (macros/views/comments would be unpinned)"
        )
    return census_digest(
        {
            name: {"row_count": record.row_counts[name], "row_digest": record.row_digests[name]}
            for name in record.row_counts
        },
        record.catalog_digest,
    )


def _tree_checksums(
    root: Path, base: Path
) -> tuple[dict[str, str], dict[str, str], dict[str, WarehouseCensusRecord]]:
    """Return pinned digests for regular files below ``root``.

    DuckDB warehouses use census digests; all other files use byte digests.
    Return ``(checksums, kinds, censuses)``, with ``kinds`` containing only
    non-byte pins.
    """
    checksums: dict[str, str] = {}
    kinds: dict[str, str] = {}
    censuses: dict[str, WarehouseCensusRecord] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        if path.suffix == WAREHOUSE_SUFFIX:
            record = _warehouse_record(path)
            checksums[rel] = record.census_digest
            kinds[rel] = WAREHOUSE_CHECKSUM_KIND
            censuses[rel] = record
        else:
            checksums[rel] = _file_sha256(path)
    return checksums, kinds, censuses


def _require_nonempty_dir(path: Path, what: str) -> None:
    if not path.is_dir() or not any(path.iterdir()):
        raise ValueError(f"{what} missing or empty: {path}")


def _verify_required_units(engine: EngineLike, task: TaskIR) -> dict[str, VariantAcceptance]:
    """Require exactly the independently accepted EL and T task units."""
    records = variant_acceptance(engine, task)
    refused = [records[v.value] for v in RLVR_TASK_VARIANTS if not records[v.value].accepted]
    if refused:
        detail = "; ".join(
            f"{r.variant}: {r.refusal_reason}" for r in refused
        )
        raise ValueError(
            f"task {task.task_id!r}: both EL and T certification phases are "
            f"required; {detail}"
        )
    return records


def _verify_release_selection(
    engine: EngineLike, task: TaskIR, selection: SelectionLike
) -> None:
    """Verify the selection and final task verdict while holding task locks."""

    currency = getattr(engine, "report_is_current", None)
    select_report = None
    if callable(currency):
        # Check every earlier stage for invalidation after the selection.
        # Import locally to keep lightweight engine test doubles working.
        from elt_taskgen.engine import STAGE_ORDER, StageName

        for stage in STAGE_ORDER:
            if stage is StageName.RELEASE:
                break
            row = engine.latest_report(task.task_id, stage.value)
            current, why = currency(task, stage, row)
            if not current:
                raise ValueError(
                    f"task {task.task_id!r}: stage {stage.value!r} is not "
                    f"current at release: {why}"
                )
            if stage is StageName.SELECT:
                select_report = row

        if select_report is None:  # pragma: no cover - loop check above refused
            raise ValueError(
                f"task {task.task_id!r}: no current selection report"
            )
        dump_selection = getattr(selection, "model_dump", None)
        if not callable(dump_selection):
            raise ValueError(
                "release selection cannot be serialized for evidence binding"
            )
        try:
            selected_payload = json.loads(
                str(_report_field(select_report, "payload_json"))
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(
                f"task {task.task_id!r}: selection report is unreadable"
            ) from exc
        if selected_payload != dump_selection(mode="json"):
            raise ValueError(
                f"task {task.task_id!r}: current selection report does not "
                "match the cohort being frozen"
            )

    # SELECT is the last per-task stage, so it carries the verdict release
    # fails closed on. Checked unconditionally: an engine without
    # `report_is_current` skipped the loop above and has proved nothing yet.
    if select_report is None:
        select_report = engine.latest_report(task.task_id, "select")
    if select_report is None:
        raise ValueError(
            f"task {task.task_id!r}: no final selection report (fail closed)"
        )
    verdict = _report_field(select_report, "verdict")
    if verdict != "pass":
        raise ValueError(
            f"task {task.task_id!r}: latest selection verdict is {verdict!r}, "
            "not 'pass'"
        )
    bound = _report_field(select_report, "content_hash")
    current = task.content_hash()
    if bound != current:
        raise ValueError(
            f"task {task.task_id!r}: selection is bound to stale content hash "
            f"{str(bound)[:12]} (current {current[:12]})"
        )
    final_verdict = getattr(engine, "final_verdict", None)
    if not callable(final_verdict):
        raise ValueError(
            "release engine cannot prove the final task verdict (fail closed)"
        )
    final = str(final_verdict(task.task_id))
    if final != "accepted":
        raise ValueError(
            f"task {task.task_id!r}: final verdict is {final!r}, not 'accepted'"
        )


#: Exactly two task units ship. FULL remains a legacy diagnostic view only.
_SHIPPABLE_VARIANTS = RLVR_TASK_VARIANTS


def variant_acceptance(engine: EngineLike, task: TaskIR) -> dict[str, VariantAcceptance]:
    """Read per-variant acceptance from the ledger.

    A variant is accepted only when its complete battery passed at its own
    ledger stage for the current parent hash. Acceptance is not inherited
    across variants.
    """
    from elt_taskgen.engine import variant_gate_stage
    from elt_taskgen.verification import variant_battery as battery_mod

    current = task.content_hash()
    out: dict[str, VariantAcceptance] = {}
    for variant in RLVR_TASK_VARIANTS:
        stage = variant_gate_stage(variant).value
        vid = variant_task_id(task.task_id, variant)
        roster = battery_mod.gate_roster(variant)
        base = {
            "variant": variant.value,
            "variant_task_id": vid,
            "gates_applicable": len(roster),
            "battery_stage": stage,
        }

        def refuse(reason: str) -> VariantAcceptance:
            # Only measured gate failures receive an R5 classification.
            return VariantAcceptance(
                accepted=False,
                shipped=False,
                gates_recorded=0,
                gates_passed=0,
                missing_gates=roster,
                classification="",
                refusal_reason=reason,
                **base,
            )

        report = engine.latest_report(task.task_id, stage)
        if report is None:
            out[variant.value] = refuse(
                f"no {stage!r} battery in the ledger (fail closed)"
            )
            continue
        if _report_field(report, "verdict") != "pass":
            out[variant.value] = refuse(
                f"latest {stage!r} battery verdict is "
                f"{_report_field(report, 'verdict')!r}, not 'pass'"
            )
            continue
        bound = _report_field(report, "content_hash")
        if bound != current:
            out[variant.value] = refuse(
                f"{stage!r} battery is bound to stale content hash "
                f"{str(bound)[:12]} (current {current[:12]}); re-run it"
            )
            continue
        payload = _report_field(report, "payload_json")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                payload = None
        if not isinstance(payload, dict):
            out[variant.value] = refuse(
                f"{stage!r} battery payload is unreadable (fail closed)"
            )
            continue
        if payload.get("task_id") not in (vid, None):
            out[variant.value] = refuse(
                f"{stage!r} battery reports task_id {payload.get('task_id')!r}, "
                f"expected the variant id {vid!r}"
            )
            continue
        summary = battery_mod.summarize_payload(payload, variant)
        failing = tuple(summary["failing_gates"])
        missing = tuple(summary["missing_gates"])
        accepted = bool(summary["accepted"])
        recorded = battery_mod.recorded_roster(payload)
        claimed = [name for name in missing if name in recorded]
        if claimed and payload.get("accepted"):
            # Acceptance cannot include gates that were not run.
            raise ValueError(
                f"task {task.task_id!r} variant {variant.value!r}: battery "
                f"claims acceptance but never ran {claimed} — an "
                "acceptance that skipped gates is not an acceptance; re-run "
                "the battery, do not release"
            )
        staleness = battery_mod.roster_staleness(payload, variant)
        if staleness:
            # An older scorer or roster requires a new battery run.
            out[variant.value] = refuse(
                f"{staleness}; re-run validate-el/validate-t "
                "(offline, at the same content hash)"
            )
            continue
        reason = ""
        if not accepted:
            parts = []
            if failing:
                parts.append("failing gate(s): " + ", ".join(failing))
            if missing:
                parts.append("gate(s) never run: " + ", ".join(missing))
            if summary.get("reason"):
                parts.append(str(summary["reason"]))
            reason = "; ".join(parts) or "battery did not accept this variant"
        out[variant.value] = VariantAcceptance(
            accepted=accepted,
            shipped=False,  # decided by _ship_combined_task
            gates_recorded=int(summary["gates_recorded"]),
            gates_passed=int(summary["gates_passed"]),
            failing_gates=failing,
            missing_gates=missing,
            classification="" if accepted else str(summary["classification"]),
            refusal_reason=reason,
            **base,
        )
    return out


def _ship_combined_task(
    workspace: Path,
    task: TaskIR,
    public_root: Path,
    private_root: Path,
    acceptance: dict[str, VariantAcceptance],
) -> tuple[tuple[str, ...], dict[str, VariantAcceptance]]:
    """Ship a public parent task after both internal batteries pass.

    Internal variant trees remain private validation evidence. Copy the parent
    bundle once, keep stage reward records private, and retain transform
    warehouses only as private DuckDB oracles.
    """
    tid = task.task_id
    task_root = workspace / "tasks" / tid
    variants_root = workspace / "tasks" / tid / "variants"
    src_public = task_root / "task"
    _require_nonempty_dir(src_public, f"task {tid!r} combined public bundle")
    assert_public_runtime_shape(src_public)
    try:
        public_config = yaml.safe_load(
            (src_public / "config.yaml").read_text(encoding="utf-8")
        )
        if not isinstance(public_config, dict):
            raise ValueError("config root is not an object")
        public_destination = destination_from_config(public_config)
        public_contract = destination_contract(public_destination)
        destination_section = public_config[public_contract.config_section]["config"]
        logical_namespace = destination_section[
            public_contract.logical_namespace_field
        ]
    except (KeyError, OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(
            f"task {tid!r}: cannot resolve the public destination contract"
        ) from exc
    expected_namespace = eltbench_mod.database_name(task)
    if logical_namespace != expected_namespace:
        raise ValueError(
            f"task {tid!r}: public {public_destination.value} namespace does "
            "not match the TaskIR-derived namespace"
        )
    shipped: list[str] = []
    acceptance = dict(acceptance)
    if not variants_root.is_dir():
        raise ValueError(
            f"task {tid!r}: required EL/T bundle directory is missing: {variants_root}"
        )

    known = {v.value for v in TaskVariant}
    unknown = sorted(
        p.name for p in variants_root.iterdir() if p.is_dir() and p.name not in known
    )
    if unknown:
        raise ValueError(
            f"task {tid!r}: unknown variant dirs under variants/: {unknown}"
        )

    oracle_source: Path | None = None
    for variant in _SHIPPABLE_VARIANTS:
        vdir = variants_root / variant.value
        record = acceptance[variant.value]
        if not record.accepted:
            raise ValueError(
                f"task {tid!r} variant {variant.value!r}: both EL and T "
                "certification phases are required before the combined task "
                f"can ship, but this battery refused it ({record.refusal_reason})"
            )
        if not vdir.exists():
            raise ValueError(
                f"task {tid!r} variant {variant.value!r}: battery ACCEPTED this "
                f"variant at the current hash but no bundle exists at {vdir}; "
                "re-run the variant gate stage, do not release a phantom accept"
            )
        vid = variant_task_id(tid, variant)
        _require_nonempty_dir(
            vdir / "task", f"task {tid!r} variant {variant.value!r} evidence bundle"
        )
        reward_path = vdir / REWARD_MANIFEST
        if not reward_path.is_file():
            raise ValueError(
                f"task {tid!r} variant {variant.value!r}: missing {REWARD_MANIFEST}"
            )
        reward = _read_json(reward_path)
        if not isinstance(reward, dict):
            raise ValueError(
                f"task {tid!r} variant {variant.value!r}: malformed "
                f"{REWARD_MANIFEST}"
            )
        reward = dict(reward)
        reward["public_task_id"] = tid
        reward["scope"] = "private_stage_evidence"
        reward["runtime"] = f"{public_destination.value}_warehouse_state"
        reward["path_base"] = (
            "answer_key/, populations/, and oracle/ resolve under "
            "private/<parent_task_id>/; the one solver-facing bundle is "
            "public/<parent_task_id>/"
        )
        # These keys described the retired JSON/DuckDB solver submission
        # surface. The underlying semantic comparators remain useful after a
        # Snowflake collector supplies counts or mart rows.
        reward.pop("load_plan_root", None)
        reward.pop("warehouse_base", None)
        if variant is TaskVariant.TRANSFORM:
            populations = sorted((reward.get("gold") or {}).keys())
            reward["oracle"] = {
                pop: f"oracle/{pop}.duckdb" for pop in populations
            }
            reward.pop("warehouse", None)

        dst_private = private_root / vid
        dst_private.mkdir(parents=True, exist_ok=True)
        (dst_private / REWARD_MANIFEST).write_text(
            canonical_json(reward) + "\n", encoding="utf-8"
        )
        if variant is TaskVariant.TRANSFORM:
            candidate = vdir / "task" / WAREHOUSE_DIRNAME
            if candidate.is_dir():
                oracle_source = candidate
        shipped.append(variant.value)
        acceptance[variant.value] = record.model_copy(update={"shipped": True})

    dst_public = public_root / tid
    shutil.copytree(src_public, dst_public)
    # Old workspace exports may still carry development fixtures. Source data
    # belongs to benchmark-managed service deployment under private/, never to
    # the solver-facing task contract.
    public_sources = dst_public / "sources"
    if public_sources.is_dir():
        shutil.rmtree(public_sources)
    _mirror_root_destination(dst_public)
    _refresh_runtime_documentation(task, dst_public)
    assert_public_runtime_shape(dst_public)
    assert_public_runtime_tree_clean(task, dst_public)

    if oracle_source is not None:
        oracle_dest = private_root / tid / "oracle"
        if oracle_dest.exists():
            raise ValueError(f"task {tid!r}: duplicate private oracle destination")
        shutil.copytree(oracle_source, oracle_dest)
    return tuple(shipped), acceptance



def _refresh_runtime_documentation(task: TaskIR, public_dir: Path) -> None:
    """Rewrite `documentation/` from the packaged upstream reference set.

    Runs before the release pins its checksums, so a package built with the
    older summaries ships the current references. The task specification stays
    appended to README.md.
    """
    from elt_taskgen.export.eltbench import (
        _runtime_documentation,
        public_documentation,
    )

    documentation = public_dir / "documentation"
    if not documentation.is_dir():
        return
    config_path = public_dir / "config.yaml"
    if not config_path.is_file():
        return
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    destination = next(
        (name for name in ("snowflake", "databricks", "redshift") if name in config),
        None,
    )
    if destination is None:
        return
    for name, content in sorted(_runtime_documentation(destination).items()):
        if name == "README.md":
            content = content.rstrip() + "\n\n" + public_documentation(task)
        (documentation / name).write_text(content, encoding="utf-8")


def _mirror_root_destination(public_dir: Path) -> None:
    """Give the ROOT destination a `destinations/<name>/` directory too.

    A package built before this rule ships the root destination only at the
    bundle root, so `destinations/` lists the other two and a reader cannot
    tell which warehouse the root uses. The release copies that tree and then
    pins its own checksums, so the mirror is added here and covered by them.
    Bundles that ship a single destination have no `destinations/` directory
    and are left alone.
    """
    from elt_taskgen.export.eltbench import (
        EXTRA_DESTINATIONS_DIRNAME,
        write_root_destination,
    )

    extras = public_dir / EXTRA_DESTINATIONS_DIRNAME
    if not extras.is_dir():
        return
    config_path = public_dir / "config.yaml"
    if not config_path.is_file():
        return
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    for name in ("snowflake", "databricks", "redshift"):
        if name in config:
            if not (extras / name).is_dir():
                write_root_destination(public_dir, name, config)
            return


def _read_json(path: Path) -> Any:
    """Parse one JSON file, or None when it is absent/unreadable."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _el_reward_path(workspace: Path, task: TaskIR) -> Path:
    """Where the EXTRACT_LOAD unit's reward manifest lives in the workspace."""
    return (
        Path(workspace)
        / "tasks"
        / task.task_id
        / "variants"
        / TaskVariant.EXTRACT_LOAD.value
        / REWARD_MANIFEST
    )


def _el_reward_manifest(workspace: Path, task: TaskIR) -> dict[str, Any]:
    """The EXTRACT_LOAD unit's reward manifest from the workspace (or {})."""
    payload = _read_json(_el_reward_path(workspace, task))
    return payload if isinstance(payload, dict) else {}


#: Per-population stage-1 answer key inside answer_key/gold/<population>/.
#: Its presence is what makes a population GRADED for the EL unit, so it is
#: also what the EL reward manifest is reconciled against at freeze.
_STAGE1_COUNTS_FILENAME = "stage1_counts.json"


def _answer_key_gold_populations(workspace: Path, task: TaskIR) -> set[str]:
    """Populations the frozen answer key can grade stage 1 on.

    Empty when the workspace carries no gold — a stand-in tree, which certifies
    nothing and therefore contradicts nothing.
    """
    gold_root = Path(workspace) / "tasks" / task.task_id / "answer_key" / "gold"
    if not gold_root.is_dir():
        return set()
    return {
        child.name
        for child in gold_root.iterdir()
        if child.is_dir() and (child / _STAGE1_COUNTS_FILENAME).is_file()
    }


def _ship_el_sources(
    workspace: Path, task: TaskIR, private_root: Path, el_reward: dict[str, Any]
) -> dict[str, str]:
    """Copy every graded extract/load source into the private release.

    Derive the required population set from frozen gold rather than
    ``reward.json``. Reject missing or incomplete rendered roots. Skip legacy
    workspaces that contain no frozen gold.
    """
    from elt_taskgen.reference.solution import find_rendered_artifact

    tid = task.task_id
    expected = el_reward.get("expected")
    named = set(expected) if isinstance(expected, dict) else set()
    graded = _answer_key_gold_populations(workspace, task)
    ungraded = sorted(graded - named)
    if ungraded:
        raise ValueError(
            f"task {tid!r}: the answer key holds frozen gold for population(s) "
            f"{ungraded} that the EL unit's {REWARD_MANIFEST} does not name in "
            "'expected', so the __el unit would ship no graded source root for "
            "them and could never be scored on them; re-run validate-el"
        )
    if not named:
        return {}
    declared = el_reward.get("sources")
    shipped: dict[str, str] = {}
    for pop in sorted(named):
        rel = EL_SOURCES_REL_TEMPLATE.format(pop=pop)
        if isinstance(declared, dict) and pop in declared and declared[pop] != rel:
            raise ValueError(
                f"task {tid!r}: EL reward declares source root "
                f"{declared[pop]!r} for population {pop!r}, but the release "
                f"layout is {rel!r}"
            )
        src = Path(workspace) / "tasks" / tid / rel
        if not src.is_dir() or not any(src.iterdir()):
            raise ValueError(
                f"task {tid!r}: EL reward names population {pop!r} but "
                f"{src} is missing/empty — the __el unit would be unscorable "
                "on that population"
            )
        dst = private_root / tid / rel
        shutil.copytree(src, dst)
        for table in task.tables:
            # The shipped tree must be the GRADED surface, not a lookalike:
            # the same resolver the loader and the gates use must find every
            # source table's artifact in it.
            find_rendered_artifact(task, dst, table.name)
        shipped[pop] = f"private/{tid}/{rel}"
    return shipped


def _certified_warehouse_census(engine: EngineLike, task: TaskIR) -> dict[str, str]:
    """Return each population's transform-certified census digest.

    Require both the hash-bound ``reports/warehouse_census.json`` and the
    ``warehouses-load`` evidence on the transform ledger row. Do not census the
    staging copy because it may have changed after validation.
    """
    from elt_taskgen.engine import variant_gate_stage

    workspace = Path(engine.workspace)
    tid = task.task_id
    path = workspace / "tasks" / tid / WAREHOUSE_CENSUS_EVIDENCE_REL
    record = _read_json(path)
    if not isinstance(record, dict):
        raise ValueError(
            f"task {tid!r}: the T unit is being released but no readable "
            f"battery-certified census exists at {path} — a shipped warehouse "
            "nobody certified must not be released (re-run validate-t)"
        )
    if record.get("kind") != WAREHOUSE_CENSUS_KIND:
        raise ValueError(
            f"task {tid!r}: warehouse census evidence is kind "
            f"{record.get('kind')!r}, not {WAREHOUSE_CENSUS_KIND!r}"
        )
    if record.get("task_id") != tid:
        raise ValueError(
            f"task {tid!r}: warehouse census evidence names task "
            f"{record.get('task_id')!r}"
        )
    current = task.content_hash()
    if record.get("task_content_hash") != current:
        raise ValueError(
            f"task {tid!r}: warehouse census evidence is bound to content hash "
            f"{str(record.get('task_content_hash'))[:12]} (current "
            f"{current[:12]}); re-run validate-t"
        )
    recorded_version = str(record.get("census_version") or "1")
    if recorded_version != CENSUS_VERSION:
        raise ValueError(
            f"task {tid!r}: warehouse census evidence was recorded under census "
            f"version {recorded_version!r}, current is {CENSUS_VERSION!r}; "
            "digests are per-version — re-run validate-t"
        )
    populations = record.get("populations")
    if not isinstance(populations, dict) or not populations:
        raise ValueError(
            f"task {tid!r}: warehouse census evidence names no populations"
        )
    certified = {
        str(pop): str((entry or {}).get("census_digest") or "")
        for pop, entry in populations.items()
    }
    empty = sorted(pop for pop, digest in certified.items() if not digest)
    if empty:
        raise ValueError(
            f"task {tid!r}: warehouse census evidence carries no digest for "
            f"population(s) {empty}"
        )

    stage = variant_gate_stage(TaskVariant.TRANSFORM).value
    report = engine.latest_report(tid, stage)
    payload = _report_field(report, "payload_json") if report is not None else None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            payload = None
    evidence: dict[str, Any] = {}
    if isinstance(payload, dict):
        for entry in payload.get("gates") or []:
            if isinstance(entry, dict) and entry.get("gate") == "warehouses-load":
                candidate = entry.get("evidence")
                evidence = candidate if isinstance(candidate, dict) else {}
                break
    if not evidence:
        raise ValueError(
            f"task {tid!r}: the {stage!r} battery records no 'warehouses-load' "
            "census evidence, so the certificate on disk is bound to nothing; "
            "re-run validate-t"
        )
    for pop, digest in sorted(certified.items()):
        full = evidence.get(f"census_digest:{pop}")
        prefix = evidence.get(f"census:{pop}")
        if isinstance(full, str) and full:
            if full != digest:
                raise ValueError(
                    f"task {tid!r} population {pop!r}: certificate "
                    f"{digest[:16]} is not bound to the ledger battery "
                    f"evidence {full[:16]}; re-run validate-t"
                )
        elif isinstance(prefix, str) and prefix:
            if not digest.startswith(prefix):
                raise ValueError(
                    f"task {tid!r} population {pop!r}: certificate "
                    f"{digest[:16]} is not bound to the ledger battery "
                    f"evidence {prefix}; re-run validate-t"
                )
        else:
            raise ValueError(
                f"task {tid!r} population {pop!r}: the {stage!r} battery "
                "records no census evidence for this population; re-run "
                "validate-t"
            )
    return certified


def _reconcile_shipped_warehouses(
    engine: EngineLike,
    tasks: Mapping[str, TaskIR],
    censuses: Mapping[str, WarehouseCensusRecord],
    variants_by_task: Mapping[str, Sequence[str]],
) -> None:
    """Match every frozen private oracle to its transform certification.

    Support the legacy public layout during migration. Certification records,
    not the presence of oracle files, determine required coverage.
    """
    by_task: dict[str, dict[str, str]] = {}
    for rel in sorted(censuses):
        parts = rel.split("/")
        parent: str | None = None
        if len(parts) == 4 and parts[0] == "private" and parts[2] == "oracle":
            parent = parts[1] if parts[1] in tasks else None
        elif len(parts) == 4 and parts[0] == "public" and parts[2] == WAREHOUSE_DIRNAME:
            unit_id = parts[1]
            parent = next(
                (
                    tid
                    for tid in tasks
                    if unit_id == variant_task_id(tid, TaskVariant.TRANSFORM)
                ),
                None,
            )
        else:
            raise ValueError(
                f"unexpected DuckDB artifact in the release at {rel!r}: "
                "schema-3 permits it only at "
                "private/<task_id>/oracle/<population>.duckdb"
            )
        pop = parts[3][: -len(".duckdb")]
        if parent is None:
            raise ValueError(
                f"DuckDB artifact {rel!r} belongs to no released parent task"
            )
        by_task.setdefault(parent, {})[pop] = rel

    # Reconciled even when it ships zero warehouses (see the docstring).
    # `_ship_combined_task` has run, so `variants_by_task` names the certified
    # internal phases represented by the staging tree.
    workspace = Path(engine.workspace)
    reconciled = set(by_task)
    for tid, shipped_variants in variants_by_task.items():
        if TaskVariant.TRANSFORM.value not in tuple(shipped_variants):
            continue
        if (workspace / "tasks" / tid / WAREHOUSE_CENSUS_EVIDENCE_REL).is_file():
            reconciled.add(tid)

    for tid in sorted(reconciled):
        shipped = by_task.get(tid, {})
        certified = _certified_warehouse_census(engine, tasks[tid])
        missing = sorted(set(certified) - set(shipped))
        if missing:
            raise ValueError(
                f"task {tid!r}: the certified census covers population(s) "
                f"{missing} but the private oracle ships no warehouse for "
                "them — a certified population that freezes nothing leaves "
                "the release "
                "unsolvable; re-run validate-t"
            )
        for pop, rel in sorted(shipped.items()):
            want = certified.get(pop)
            if not want:
                raise ValueError(
                    f"task {tid!r}: shipped warehouse {rel!r} has no certified "
                    "census (a shipped warehouse nobody certified); re-run "
                    "validate-t"
                )
            got = censuses[rel].census_digest
            if got != want:
                raise ValueError(
                    f"task {tid!r} population {pop!r}: shipped warehouse "
                    f"census {got[:16]} differs from the certified census "
                    f"{want[:16]} — the bytes being frozen are not the bytes "
                    "the T battery measured; re-run validate-t"
                )


def _config_source_identifiers(path: Path) -> tuple[set[str], set[str]]:
    """(databases, buckets) declared by one config.yaml."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"unreadable config at {path}: {exc}") from exc
    if not isinstance(data, dict):
        return set(), set()
    databases: set[str] = set()
    for section in ("snowflake", "postgres", "mongodb"):
        block = data.get(section)
        if isinstance(block, dict):
            name = (block.get("config") or {}).get("database")
            if isinstance(name, str) and name:
                databases.add(name)
    buckets: set[str] = set()
    s3 = data.get("aws_s3")
    if isinstance(s3, dict):
        for entry in s3.get("data") or []:
            s3_path = str((entry or {}).get("path") or "")
            if s3_path.startswith("s3://"):
                buckets.add(s3_path[len("s3://") :].split("/", 1)[0])
    return databases, buckets


def _assert_release_source_identifiers(root: Path) -> list[str]:
    """Refuse a release whose configs name un-provisionable identifiers.

    An over-long name passes offline and then cannot be stood up upstream:
    LocalStack rejects the bucket and Postgres truncates the database, aliasing
    a whole family onto one database. Returns the config paths checked.
    """
    checked: list[str] = []
    for config_path in sorted(root.rglob("config.yaml")):
        databases, buckets = _config_source_identifiers(config_path)
        rel = config_path.relative_to(root).as_posix()
        for db in sorted(databases):
            try:
                assert_source_identifiers(db)
            except ValueError as exc:
                raise ValueError(f"source identifier limits: {rel}: {exc}") from exc
        for bucket in sorted(buckets):
            if len(bucket) > eltbench_mod.S3_BUCKET_MAX_LEN:
                raise ValueError(
                    f"source identifier limits: {rel}: S3 bucket {bucket!r} is "
                    f"{len(bucket)} characters, over the "
                    f"{eltbench_mod.S3_BUCKET_MAX_LEN}-character maximum"
                )
        checked.append(rel)
    return checked


def _combined_release_id(
    *,
    destinations: Mapping[str, str],
    destination_connector_versions: Mapping[str, str],
    splits: Mapping[str, str],
    tasks: Mapping[str, str],
    public_runtime_checksums: Mapping[str, str],
    variants: Mapping[str, Sequence[str]],
    rejected_variants: Mapping[str, str],
    el_sources: Mapping[str, Mapping[str, str]],
    scorer_version: str,
    roster_digest: str,
    semantic_scorer_version: str | None = None,
    source_provenance_digests: Mapping[str, str] | None = None,
) -> str:
    """Compute schema-3 identity from manifest-recoverable fields.

    Schema 3.1 did not record the semantic executor version, so callers
    verifying such a manifest pass ``None`` and reproduce its historical
    identity exactly. Schema 3.2 binds the new combined scorer contract.
    """

    identity = {
        "corpus_profile": COMBINED_CORPUS_PROFILE,
        "public_layout": COMBINED_PUBLIC_LAYOUT,
        "destinations": dict(destinations),
        "destination_connector_versions": dict(destination_connector_versions),
        "splits": dict(splits),
        "tasks": dict(tasks),
        "public_runtime_checksums": dict(sorted(public_runtime_checksums.items())),
        "variants": {key: tuple(value) for key, value in variants.items()},
        "rejected_variants": dict(rejected_variants),
        "el_sources": {key: dict(value) for key, value in el_sources.items()},
        "scorer_version": scorer_version,
        "roster_digest": roster_digest,
    }
    if semantic_scorer_version is not None:
        identity["semantic_scorer_version"] = semantic_scorer_version
    # Missing provenance contributes no new identity field, preserving the
    # historical <=3.4 algorithm for development/legacy fixtures. A current
    # certified release necessarily has a non-empty exact-coverage map.
    if source_provenance_digests:
        identity["source_provenance_digests"] = dict(
            sorted(source_provenance_digests.items())
        )
    return "release-" + sha256_hex(canonical_json(identity))[:16]


def _schema_33_private_semantic_checksums(
    checksums: Mapping[str, str], task_ids: Iterable[str]
) -> dict[str, str]:
    """Historical schema-3.3 semantic checksum selection.

    Schema 3.3 selected every file under each parent private directory. That
    accidentally included ``answer_key/runtime/airbyte_connector_contract``;
    retain this helper solely to reproduce and verify already-frozen 3.3
    manifests under their original rules.
    """
    prefixes = tuple(f"private/{tid}/" for tid in task_ids)
    if not prefixes:
        return {}
    return {
        rel: digest
        for rel, digest in sorted(checksums.items())
        if rel.startswith(prefixes)
    }


def _private_semantic_checksums(
    checksums: Mapping[str, str], task_ids: Iterable[str]
) -> dict[str, str]:
    """Return destination-independent private checksums for semantic identity.

    Include populations, destination-neutral answer-key files, TaskIR,
    evaluator SQL, serving manifests, and DuckDB census digests. Exclude runtime
    connector contracts, reports, and destination-bearing variant evidence.
    Difficulty remains release evidence rather than task semantics.
    """
    ids = tuple(task_ids)
    prefixes = tuple(f"private/{tid}/" for tid in ids)
    runtime_prefixes = tuple(
        f"private/{tid}/answer_key/runtime/" for tid in ids
    )
    report_prefixes = tuple(f"private/{tid}/reports/" for tid in ids)
    if not prefixes:
        return {}
    return {
        rel: digest
        for rel, digest in sorted(checksums.items())
        if rel.startswith(prefixes)
        and not rel.startswith(runtime_prefixes)
        and not rel.startswith(report_prefixes)
    }


def _private_runtime_checksums(
    checksums: Mapping[str, str], task_id: str
) -> dict[str, str]:
    """Pinned evaluator-private runtime contract bytes for one task.

    These files are private because they expose harness configuration, not
    because they define benchmark semantics. Schema 3.4 binds them beside the
    public runtime projection in ``runtime_bundle_id``.
    """
    prefix = f"private/{task_id}/answer_key/runtime/"
    return {
        rel: digest
        for rel, digest in sorted(checksums.items())
        if rel.startswith(prefix)
    }


def _public_task_checksums(
    checksums: Mapping[str, str], task_id: str
) -> dict[str, str]:
    """Pinned digests of one task's solver-facing runtime bytes."""
    prefix = f"public/{task_id}/"
    return {
        rel: digest
        for rel, digest in sorted(checksums.items())
        if rel.startswith(prefix)
    }


def _semantic_release_id(
    *,
    tasks: Mapping[str, str],
    el_sources: Mapping[str, Mapping[str, str]],
    private_checksums: Mapping[str, str],
    scorer_version: str,
    roster_digest: str,
    semantic_scorer_version: str,
) -> str:
    """Identify task semantics without destination or split inputs.

    Retargeting or repartitioning a corpus must not change this ID. The legacy
    release ID still binds corpus splits.
    """
    identity = {
        "tasks": dict(tasks),
        "el_sources": {key: dict(value) for key, value in el_sources.items()},
        "private_checksums": dict(sorted(private_checksums.items())),
        "scorer_version": scorer_version,
        "roster_digest": roster_digest,
        "semantic_scorer_version": semantic_scorer_version,
    }
    return SEMANTIC_ID_PREFIX + sha256_hex(canonical_json(identity))[:16]


def _runtime_bundle_id(
    *,
    semantic_release_id: str,
    task_id: str,
    destination: str,
    public_checksums: Mapping[str, str],
    private_runtime_checksums: Mapping[str, str],
) -> str:
    """Compute one task's runtime identity for one destination.

    Bind the semantic release, public destination configuration, and private
    Airbyte connector data.
    """
    identity = {
        "semantic_release_id": semantic_release_id,
        "task_id": task_id,
        "destination": destination,
        "public_checksums": dict(sorted(public_checksums.items())),
        "private_runtime_checksums": dict(
            sorted(private_runtime_checksums.items())
        ),
    }
    return RUNTIME_BUNDLE_ID_PREFIX + sha256_hex(canonical_json(identity))[:16]


def _schema_33_runtime_bundle_id(
    *,
    task_id: str,
    destination: str,
    public_checksums: Mapping[str, str],
) -> str:
    """Reproduce the unchained runtime identity written by schema 3.3."""
    identity = {
        "task_id": task_id,
        "destination": destination,
        "public_checksums": dict(sorted(public_checksums.items())),
    }
    return RUNTIME_BUNDLE_ID_PREFIX + sha256_hex(canonical_json(identity))[:16]


def _certification_id(
    *,
    runtime_bundle_id: str,
    matrix: Mapping[str, str],
    validate_matrix: bool = True,
) -> str:
    """Identify one runtime bundle under one execution matrix.

    Matrix changes produce a new ID while retaining prior evidence.
    ``validate_matrix=False`` supports historical or already-validated frozen
    matrices; new writers must validate the matrix.
    """
    if validate_matrix:
        destination = matrix.get("destination")
        if not isinstance(destination, str) or not destination:
            raise ValueError(
                "schema-3.4 certification matrix records no destination"
            )
        matrix = runtime_matrix.validate_certification_matrix(
            matrix, destination
        )
    identity = {
        "runtime_bundle_id": runtime_bundle_id,
        "matrix": dict(sorted(matrix.items())),
    }
    return CERTIFICATION_ID_PREFIX + sha256_hex(canonical_json(identity))[:16]


def _set_readonly(root: Path) -> None:
    """Best-effort immutability: strip write bits from every released file."""
    for path in sorted(root.rglob("*")):
        if path.is_file():
            os.chmod(path, path.stat().st_mode & 0o555)


# ---------------------------------------------------------------------------
# Freeze
# ---------------------------------------------------------------------------

def freeze_release(
    engine: EngineLike,
    selection: SelectionLike,
    out_dir: Path,
    *,
    allow_unlocked_env: bool = False,
    release_mode: Literal["development", "certified"] = DEVELOPMENT_RELEASE_MODE,
    sandbox_attestation: SandboxAttestation | None = None,
    expected_campaign_fingerprint: str | None = None,
    agents_config: Path | str | None = None,
) -> ReleaseManifest:
    """Freeze a selection while holding every selected task lock.

    Lock at this production boundary so direct callers and the ``release``
    stage share verify-to-copy atomicity. Test doubles without the Engine lock
    API still use the pure freezer.
    """

    ordered_ids = sorted(set(tuple(selection.train)) | set(tuple(selection.val)))
    lock_many = getattr(engine, "task_locks", None)
    if callable(lock_many) and ordered_ids:
        with lock_many(ordered_ids):
            recover = getattr(engine, "recover_pending_repair", None)
            if callable(recover):
                for task_id in ordered_ids:
                    recover(task_id)
            return _freeze_release_locked(
                engine,
                selection,
                out_dir,
                allow_unlocked_env=allow_unlocked_env,
                release_mode=release_mode,
                sandbox_attestation=sandbox_attestation,
                expected_campaign_fingerprint=expected_campaign_fingerprint,
                agents_config=agents_config,
            )
    return _freeze_release_locked(
        engine,
        selection,
        out_dir,
        allow_unlocked_env=allow_unlocked_env,
        release_mode=release_mode,
        sandbox_attestation=sandbox_attestation,
        expected_campaign_fingerprint=expected_campaign_fingerprint,
        agents_config=agents_config,
    )


def _freeze_release_locked(
    engine: EngineLike,
    selection: SelectionLike,
    out_dir: Path,
    *,
    allow_unlocked_env: bool = False,
    release_mode: Literal["development", "certified"] = DEVELOPMENT_RELEASE_MODE,
    sandbox_attestation: SandboxAttestation | None = None,
    expected_campaign_fingerprint: str | None = None,
    agents_config: Path | str | None = None,
) -> ReleaseManifest:
    """Copy accepted tasks into an immutable release without changing acceptance.

    Reject unrecorded dependency drift. Certified releases also require current
    empirical evidence and a tier A or B attestation bound to the release ID.
    """
    if release_mode not in RELEASE_MODES:
        raise ValueError(
            f"release_mode must be one of {sorted(RELEASE_MODES)}, got "
            f"{release_mode!r}"
        )
    certified_release = release_mode == CERTIFIED_RELEASE_MODE
    if not certified_release and sandbox_attestation is not None:
        raise ValueError(
            "sandbox_attestation is only accepted when "
            "release_mode='certified'; development releases must not carry "
            "certification evidence"
        )
    out_dir = out_dir.resolve()
    if out_dir.exists():
        raise ValueError(f"release dir already exists (immutable output): {out_dir}")

    allow_unlocked_env = allow_unlocked_env or bool(
        getattr(engine, "allow_unlocked_env", False)
    )
    drift = environment_drift()
    if drift and not allow_unlocked_env:
        detail = "; ".join(
            f"{pkg}: installed {got}, locked {want or '(none)'}"
            for pkg, (got, want) in sorted(drift.items())
        )
        raise ValueError(
            "runtime environment differs from uv.lock: "
            f"{detail}. Run `uv sync --frozen`, or pass allow_unlocked_env "
            "(CLI: --allow-unlocked-env) to record the drift in the manifest"
        )

    train = tuple(selection.train)
    val = tuple(selection.val)
    overlap = sorted(set(train) & set(val))
    if overlap:
        raise ValueError(f"train/val overlap: {overlap}")
    ordered_ids = sorted(set(train) | set(val))
    if not ordered_ids:
        raise ValueError("selection is empty: nothing to release")
    for task_id in ordered_ids:
        try:
            validate_task_id_segment(task_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"selection contains unsafe task_id {task_id!r}: {exc}"
            ) from exc
    bound_selection = hasattr(selection, "empirical_required")
    empirical_required = certified_release or bool(
        getattr(selection, "empirical_required", False)
    )
    active_campaign = (
        expected_campaign_fingerprint
        if expected_campaign_fingerprint is not None
        else getattr(engine, "expected_campaign_fingerprint", None)
    )
    selected_task_hashes = dict(
        getattr(selection, "task_content_hashes", {}) or {}
    )
    selected_difficulty = dict(
        getattr(selection, "difficulty_measurements", {}) or {}
    )
    if bound_selection:
        expected_ids = set(ordered_ids)
        if set(selected_task_hashes) != expected_ids:
            raise ValueError(
                "selection is not bound to exactly the selected task content "
                "hashes; re-run selection"
            )
        if set(selected_difficulty) != expected_ids:
            raise ValueError(
                "selection is not bound to exactly the selected difficulty "
                "measurements; re-run selection"
            )
    splits = {tid: "train" for tid in train} | {tid: "val" for tid in val}
    selected_variants = getattr(selection, "variants", None)
    expected_variants = tuple(v.value for v in RLVR_TASK_VARIANTS)
    if not isinstance(selected_variants, dict):
        raise ValueError(
            "selection has no per-parent variants map; an EL/T release must "
            "name both selected units explicitly"
        )
    for tid in ordered_ids:
        got = tuple(selected_variants.get(tid, ()))
        if got != expected_variants:
            raise ValueError(
                f"task {tid!r}: selection must contain exactly "
                f"{expected_variants}, got {got}"
            )

    # Check isolation before assembly, then bind it to the computed release ID.
    attestation_batch = {
        "corpus_profile": COMBINED_CORPUS_PROFILE,
        "public_layout": COMBINED_PUBLIC_LAYOUT,
        "variants": {tid: expected_variants for tid in ordered_ids},
    }
    contamination_mode = None
    if certified_release:
        from elt_taskgen.export.attestation_gate import (
            require_attestation_for_labels,
        )
        from elt_taskgen.verification.contamination import enforcement

        contamination_mode = enforcement()
        gate_kwargs: dict[str, Any] = {"agents_config": agents_config}
        require_attestation_for_labels(
            attestation_batch,
            sandbox_attestation,
            contamination_mode=contamination_mode,
            **gate_kwargs,
        )
    if empirical_required and not active_campaign:
        raise ValueError(
            "an active calibration campaign fingerprint is required to freeze "
            "empirical difficulty; load the same agents configuration used by "
            "calibration"
        )

    # Phase 1: load + verify every task before touching the filesystem.
    tasks: dict[str, TaskIR] = {}
    for tid in ordered_ids:
        task = engine.load_task(tid)
        if task.task_id != tid:
            raise ValueError(f"engine returned task {task.task_id!r} for id {tid!r}")
        if task.origin == Origin.ELTBENCH_ANCHOR:
            raise ValueError(
                f"task {tid!r} is an ELT-Bench anchor (measurement-only); refusing to release"
            )
        if bound_selection and selected_task_hashes[tid] != task.content_hash():
            raise ValueError(
                f"task {tid!r}: selection binds content hash "
                f"{selected_task_hashes[tid][:12]}, current task is "
                f"{task.content_hash()[:12]}; re-run selection"
            )
        _verify_required_units(engine, task)
        _verify_release_selection(engine, task, selection)
        tasks[tid] = task

    # Phase 2: family/split isolation.
    families = {tid: tasks[tid].family_id for tid in ordered_ids}
    family_splits: dict[str, set[str]] = {}
    for tid in ordered_ids:
        family_splits.setdefault(families[tid], set()).add(splits[tid])
    straddling = sorted(f for f, s in family_splits.items() if len(s) > 1)
    if straddling:
        raise ValueError(f"families straddle the train/val split: {straddling}")

    workspace = Path(getattr(engine, "workspace"))
    source_provenance: dict[str, IngestProvenance] = {}
    for tid in ordered_ids:
        try:
            record = load_current_provenance(
                workspace / "tasks" / tid,
                task=tasks[tid],
                required=certified_release,
            )
        except ValueError as exc:
            raise ValueError(f"task {tid!r}: invalid ingest provenance: {exc}") from exc
        if record is not None:
            source_provenance[tid] = record

    # Reject links before ``copytree`` can import files outside the task root.
    tasks_root = workspace / "tasks"
    try:
        tasks_root_metadata = tasks_root.lstat()
    except OSError as exc:
        raise ValueError(f"workspace tasks root is unreadable: {exc}") from exc
    if stat.S_ISLNK(tasks_root_metadata.st_mode):
        raise ValueError(
            f"workspace tasks root must not be a symbolic link: {tasks_root}"
        )
    if not stat.S_ISDIR(tasks_root_metadata.st_mode):
        raise ValueError(f"workspace tasks root must be a directory: {tasks_root}")
    for tid in ordered_ids:
        _assert_safe_release_input_tree(
            tasks_root / tid,
            label=f"task {tid!r} release input tree",
        )

    # Phase 3: stage the release tree, then rename into place atomically.
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=out_dir.parent, prefix=".release-stage-"))
    try:
        public_root = staging / "public"
        private_root = staging / "private"
        variants_by_task: dict[str, tuple[str, ...]] = {}
        acceptance_by_task: dict[str, tuple[VariantAcceptance, ...]] = {}
        rejected_variants: dict[str, str] = {}
        el_sources: dict[str, dict[str, str]] = {}
        population_relations: dict[str, dict[str, str]] = {}
        difficulty_measurements: dict[str, str] = {}
        for tid in ordered_ids:
            src_key = workspace / "tasks" / tid / "answer_key"
            _require_nonempty_dir(src_key, f"task {tid!r} answer key")
            # Require the canonical solution that the transform battery checked.
            from elt_taskgen.training import canonical as canonical_mod

            try:
                canonical_mod.assert_canonical_ready(tasks[tid], src_key)
            except ValueError as exc:
                raise ValueError(f"task {tid!r}: {exc}") from None
            shutil.copytree(src_key, private_root / tid / "answer_key")
            difficulty_digest = _stage_difficulty_measurement(
                workspace,
                private_root,
                tasks[tid],
                required=empirical_required,
                expected_campaign_fingerprint=active_campaign,
            )
            if difficulty_digest is not None:
                difficulty_measurements[tid] = difficulty_digest
            if bound_selection and difficulty_digest != selected_difficulty[tid]:
                raise ValueError(
                    f"task {tid!r}: difficulty evidence changed after selection; "
                    "re-run selection"
                )
            # Public schemas omit types and backends, so portable replay needs a
            # checksum-pinned private TaskIR. Reference plans and SQL stay private.
            semantic_dir = private_root / tid / SEMANTIC_DIRNAME
            semantic_dir.mkdir(parents=True, exist_ok=True)
            # The FULL dump, as the local evaluator package ships it: the
            # canonical dump carries no revisions, so the released TaskIR read
            # as its own lineage root and the source-provenance check below
            # compared the intake hash against the current content hash. Every
            # task whose hash moved after intake — every authored task — failed
            # its own freeze (batch50, 2026-09-19). Revisions are not hashed,
            # so the recorded task content hash is unchanged.
            (semantic_dir / SEMANTIC_TASK_IR_FILENAME).write_text(
                readable_json(tasks[tid].model_dump(mode="json")) + "\n",
                encoding="utf-8",
            )
            provenance = source_provenance.get(tid)
            if provenance is not None:
                provenance_dir = private_root / tid / RELEASE_PROVENANCE_DIRNAME
                provenance_dir.mkdir(parents=True, exist_ok=True)
                (provenance_dir / RELEASE_PROVENANCE_FILENAME).write_bytes(
                    provenance.deterministic_bytes()
                )
            # Ship source roots for every population named by the EL reward.
            el_reward = _el_reward_manifest(workspace, tasks[tid])
            shipped_sources = _ship_el_sources(
                workspace, tasks[tid], private_root, el_reward
            )
            if shipped_sources:
                el_sources[tid] = shipped_sources
            # Validate typed values, row counts, and canonical mart gold.
            from elt_taskgen.verification.release_portability import (
                validate_release_portability,
            )

            validate_release_portability(
                tasks[tid],
                private_root / tid / "answer_key",
                {
                    population: staging / rel
                    for population, rel in shipped_sources.items()
                },
            )
            relations = el_reward.get("population_relations")
            if isinstance(relations, dict) and relations:
                population_relations[tid] = {
                    str(k): str(v) for k, v in sorted(relations.items())
                }
            # Both stage batteries certify one public parent task.
            acceptance = variant_acceptance(engine, tasks[tid])
            variants_by_task[tid], acceptance = _ship_combined_task(
                workspace, tasks[tid], public_root, private_root, acceptance
            )
            acceptance_by_task[tid] = tuple(
                acceptance[v.value] for v in RLVR_TASK_VARIANTS
            )
            for record in acceptance_by_task[tid]:
                if not record.shipped:
                    rejected_variants[record.variant_task_id] = record.refusal_reason

        # Identifiers a harness has to provision with (db/bucket), checked on
        # the tree actually being frozen — a bundle emitted by pre-bound code
        # must not slip through.
        _assert_release_source_identifiers(public_root)
        destinations_by_task: dict[str, str] = {}
        destination_versions_by_task: dict[str, str] = {}
        for tid in ordered_ids:
            config = yaml.safe_load(
                (public_root / tid / "config.yaml").read_text(encoding="utf-8")
            )
            if not isinstance(config, dict):
                raise ValueError(f"task {tid!r}: released config is not an object")
            selected_destination = destination_from_config(config)
            destinations_by_task[tid] = selected_destination.value
            destination_versions_by_task[tid] = (
                destination_contract(selected_destination).connector_version
                or "legacy-unpinned"
            )

        pub_sums, pub_kinds, pub_census = _tree_checksums(public_root, staging)
        prv_sums, prv_kinds, prv_census = _tree_checksums(private_root, staging)
        checksums = pub_sums | prv_sums
        checksum_kinds = pub_kinds | prv_kinds
        warehouse_censuses = pub_census | prv_census

        # The pin must be the CERTIFIED census, not a freeze-time
        # self-attestation. UNCONDITIONAL: a T unit shipping zero warehouses is
        # exactly the drift this refuses.
        _reconcile_shipped_warehouses(
            engine, tasks, warehouse_censuses, variants_by_task
        )

        gate_rosters = {
            variant.value: battery_roster(variant) for variant in RLVR_TASK_VARIANTS
        }
        source_provenance_digests = {
            tid: record.evidence_digest()
            for tid, record in sorted(source_provenance.items())
        }
        release_id = _combined_release_id(
            destinations=destinations_by_task,
            destination_connector_versions=destination_versions_by_task,
            splits=splits,
            tasks={t: tasks[t].content_hash() for t in ordered_ids},
            # TaskIR is destination-neutral, so bind the actual public runtime
            # bytes. Retargeting identical semantics changes release identity.
            public_runtime_checksums=pub_sums,
            variants=variants_by_task,
            rejected_variants=rejected_variants,
            el_sources=el_sources,
            scorer_version=_scorer_version(),
            roster_digest=_roster_digest(),
            semantic_scorer_version=SEMANTIC_SCORER_VERSION,
            source_provenance_digests=source_provenance_digests,
        )
        sandbox_attestation_digest = ""
        if certified_release:
            # Bind the already verified attestation to this release.
            from elt_taskgen.export.attestation_gate import (
                require_attestation_for_labels,
            )
            from elt_taskgen.runtime.attestation import seal_sandbox_attestation

            assert sandbox_attestation is not None  # proved by the first gate
            frozen_attestation = sandbox_attestation
            if not frozen_attestation.run_id:
                frozen_attestation = seal_sandbox_attestation(
                    frozen_attestation.model_copy(
                        update={"run_id": release_id, "attestation_digest": ""}
                    )
                )
            decision = require_attestation_for_labels(
                attestation_batch,
                frozen_attestation,
                contamination_mode=contamination_mode,
                run_id=release_id,
                **gate_kwargs,
            )
            attestation_path = staging / SANDBOX_ATTESTATION_FILENAME
            attestation_path.write_text(
                readable_json(frozen_attestation.model_dump(mode="json")) + "\n",
                encoding="utf-8",
            )
            checksums[SANDBOX_ATTESTATION_FILENAME] = _file_sha256(
                attestation_path
            )
            sandbox_attestation_digest = decision.attestation_digest
        # Schema-3.4+ chained identities beside (never replacing) the legacy
        # release_id. The semantic layer excludes destination-specific private
        # runtime contracts; every task runtime layer binds that semantic id.
        semantic_release_id = _semantic_release_id(
            tasks={tid: tasks[tid].content_hash() for tid in ordered_ids},
            el_sources=el_sources,
            private_checksums=_private_semantic_checksums(checksums, ordered_ids),
            scorer_version=_scorer_version(),
            roster_digest=_roster_digest(),
            semantic_scorer_version=SEMANTIC_SCORER_VERSION,
        )
        private_runtime_by_task = {
            tid: _private_runtime_checksums(checksums, tid)
            for tid in ordered_ids
        }
        missing_private_runtime = sorted(
            tid for tid, pins in private_runtime_by_task.items() if not pins
        )
        if missing_private_runtime:
            raise ValueError(
                "schema-3.4+ runtime identity requires private runtime "
                "contract bytes for every task; missing for "
                f"{missing_private_runtime}"
            )
        runtime_bundle_ids = {
            tid: _runtime_bundle_id(
                semantic_release_id=semantic_release_id,
                task_id=tid,
                destination=destinations_by_task[tid],
                public_checksums=_public_task_checksums(pub_sums, tid),
                private_runtime_checksums=private_runtime_by_task[tid],
            )
            for tid in ordered_ids
        }
        # Only destinations actually shipped: a pin change for a destination
        # this release never targets must not invalidate its certifications.
        matrix_by_destination = {
            dest: runtime_matrix.certification_matrix(dest)
            for dest in sorted(set(destinations_by_task.values()))
        }
        certification_ids = {
            tid: _certification_id(
                runtime_bundle_id=runtime_bundle_ids[tid],
                matrix=matrix_by_destination[destinations_by_task[tid]],
            )
            for tid in ordered_ids
        }
        drift_record = {
            pkg: f"installed {got}, locked {want or '(none)'}"
            for pkg, (got, want) in sorted(drift.items())
        }
        manifest = ReleaseManifest(
            schema_version=RELEASE_SCHEMA_VERSION,
            corpus_profile=COMBINED_CORPUS_PROFILE,
            public_layout=COMBINED_PUBLIC_LAYOUT,
            destinations=dict(sorted(destinations_by_task.items())),
            destination_connector_versions=dict(
                sorted(destination_versions_by_task.items())
            ),
            release_mode=release_mode,
            sandbox_attestation_digest=sandbox_attestation_digest,
            release_id=release_id,
            tasks={tid: tasks[tid].content_hash() for tid in ordered_ids},
            difficulty_measurements=dict(sorted(difficulty_measurements.items())),
            splits=dict(sorted(splits.items())),
            families=dict(sorted(families.items())),
            licenses={tid: tasks[tid].license for tid in ordered_ids},
            source_provenance=dict(sorted(source_provenance.items())),
            source_provenance_digests=source_provenance_digests,
            checksums=dict(sorted(checksums.items())),
            checksum_kinds=dict(sorted(checksum_kinds.items())),
            warehouse_census=dict(sorted(warehouse_censuses.items())),
            variants=dict(sorted(variants_by_task.items())),
            variant_acceptance=dict(sorted(acceptance_by_task.items())),
            rejected_variants=dict(sorted(rejected_variants.items())),
            el_sources=dict(sorted(el_sources.items())),
            population_relations=dict(sorted(population_relations.items())),
            gate_rosters=gate_rosters,
            roster_digest=_roster_digest(),
            environment=runtime_versions(),
            environment_drift=drift_record,
            scorer_version=_scorer_version(),
            semantic_scorer_version=SEMANTIC_SCORER_VERSION,
            semantic_release_id=semantic_release_id,
            runtime_bundle_ids=dict(sorted(runtime_bundle_ids.items())),
            certification_ids=dict(sorted(certification_ids.items())),
            certification_matrix=dict(sorted(matrix_by_destination.items())),
            generator_version=GENERATOR_VERSION,
        )
        manifest_path = staging / "release_manifest.json"
        manifest_path.write_text(readable_json(manifest.model_dump(mode="json")) + "\n")

        # Keep census checksums as comments because ``shasum`` expects byte hashes.
        byte_files = {
            rel: digest
            for rel, digest in checksums.items()
            if rel not in checksum_kinds
        }
        byte_files["release_manifest.json"] = _file_sha256(manifest_path)
        lines = list(_CHECKSUM_FILE_HEADER)
        lines += [
            f"# {checksum_kinds[rel]}  {checksums[rel]}  {rel}"
            for rel in sorted(checksum_kinds)
        ]
        lines += [f"{digest}  {rel}" for rel, digest in sorted(byte_files.items())]
        (staging / "checksums.sha256").write_text("\n".join(lines) + "\n")

        _set_readonly(staging)
        os.chmod(staging, 0o755)  # mkdtemp defaults to 0700; release tree is readable
        os.rename(staging, out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

class FileVerdict(BaseModel):
    """One released file, the rule it was pinned under, and the outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rel_path: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    ok: bool
    detail: str = ""


class ReleaseVerification(BaseModel):
    """Result of re-checking a frozen release against its own manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    release_dir: str
    release_id: str
    schema_version: str
    corpus_profile: str
    ok: bool
    #: Every pinned file: manifest.checksums + release_manifest.json itself.
    files_checked: int
    #: Files that verified, by rule. byte_pinned + census_pinned ==
    #: files_checked exactly when ok is True.
    byte_pinned: int
    census_pinned: int
    files: tuple[FileVerdict, ...] = ()
    #: Files present in the tree that NO pin covers — reported separately from
    #: a digest mismatch because nothing frozen changed, something was added
    #: afterwards. Still a failure: an unpinned file in a release is a hole.
    unpinned_files: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()


def _read_flat_checksums(path: Path) -> dict[str, str]:
    """Parse checksums.sha256, skipping the '#' lines (census + header).

    Must agree with `shasum -c` about what is a byte-hash claim.
    """
    entries: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        digest, _, rel = line.partition("  ")
        if not rel:
            raise ValueError(f"unparsable checksum line in {path}: {raw!r}")
        if not _is_sha256(digest):
            raise ValueError(
                f"invalid SHA-256 digest in {path} for {rel!r}: {digest!r}"
            )
        try:
            _validate_release_relative_path(rel)
        except ValueError as exc:
            raise ValueError(
                f"unsafe checksum path in {path}: {rel!r}: {exc}"
            ) from exc
        if rel in entries:
            raise ValueError(f"duplicate checksum entry in {path}: {rel!r}")
        entries[rel] = digest
    return entries


def _schema_tuple(version: str) -> tuple[int, ...]:
    """('2.10') -> (2, 10). Unparsable components sort as -1 (oldest).

    Schema versions are dotted NUMBERS: as strings "2.10" < "2.9", and a
    release frozen under a later schema would be judged by earlier rules.
    """
    parts: list[int] = []
    for chunk in str(version).split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(-1)
    return tuple(parts)


def _verify_combined_public_layout(
    release_dir: Path,
    manifest: ReleaseManifest,
    fail: Any,
) -> None:
    """Verify the schema-3 solver boundary independently of byte pinning."""
    public_root = release_dir / "public"
    actual = {
        path.name for path in public_root.iterdir() if path.is_dir()
    } if public_root.is_dir() else set()
    expected = set(manifest.tasks)
    records_destinations = _schema_tuple(manifest.schema_version) >= (3, 1)
    if records_destinations and set(manifest.destinations) != expected:
        fail(
            "release_manifest.json",
            "layout",
            "schema-3.1 destination map must name every task exactly once",
        )
    if records_destinations and set(manifest.destination_connector_versions) != expected:
        fail(
            "release_manifest.json",
            "layout",
            "schema-3.1 connector-version map must name every task exactly once",
        )
    for name in sorted(expected - actual):
        fail(f"public/{name}", "layout", "combined public task directory is missing")
    for name in sorted(actual - expected):
        fail(
            f"public/{name}",
            "layout",
            "unexpected public directory; schema-3 exposes exactly one parent "
            "directory per task and no __el/__t units",
        )

    forbidden_tokens = ("load_plan", "sql_by_mart", "standalone duckdb")
    for tid in sorted(expected & actual):
        root = public_root / tid
        try:
            assert_public_runtime_shape(root)
        except ValueError as exc:
            fail(f"public/{tid}", "layout", str(exc))
        if records_destinations:
            try:
                config = yaml.safe_load(
                    (root / "config.yaml").read_text(encoding="utf-8")
                )
                if not isinstance(config, dict):
                    raise ValueError("config root is not an object")
                actual_destination = destination_from_config(config).value
            except (OSError, ValueError, yaml.YAMLError) as exc:
                fail(
                    f"public/{tid}/config.yaml",
                    "layout",
                    f"cannot resolve destination: {exc}",
                )
            else:
                if manifest.destinations.get(tid) != actual_destination:
                    fail(
                        f"public/{tid}/config.yaml",
                        "layout",
                        "destination disagrees with release_manifest.json",
                    )
                expected_connector_version = (
                    destination_contract(actual_destination).connector_version
                    or "legacy-unpinned"
                )
                if (
                    manifest.destination_connector_versions.get(tid)
                    != expected_connector_version
                ):
                    fail(
                        "release_manifest.json",
                        "layout",
                        f"connector version for task {tid!r} must be "
                        f"{expected_connector_version!r} for {actual_destination}",
                    )
        if (root / "sources").exists():
            fail(
                f"public/{tid}/sources",
                "layout",
                "source fixtures are deployment-private; solvers must reach "
                "the configured source backends through Airbyte",
            )
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(release_dir).as_posix()
            lowered_rel = rel.lower()
            if path.is_file() and path.suffix.lower() == WAREHOUSE_SUFFIX:
                fail(rel, "public-runtime", "DuckDB is forbidden in a public task")
                continue
            for token in forbidden_tokens[:2]:
                if token in lowered_rel:
                    fail(rel, "public-runtime", f"forbidden legacy token {token!r}")
            if not path.is_file():
                continue
            normalized = " ".join(
                path.read_bytes().decode("utf-8", errors="ignore").lower().split()
            )
            for token in forbidden_tokens:
                if token in normalized:
                    fail(rel, "public-runtime", f"forbidden legacy token {token!r}")


def _verify_semantic_packages(
    release_dir: Path,
    manifest: ReleaseManifest,
    fail: Any,
) -> None:
    """Bind each schema-3.2 private TaskIR to its release task identity.

    Reject re-pinned, misplaced, or hash-mismatched TaskIR files. Older
    manifests retain their recorded rules.
    """
    if _schema_tuple(manifest.schema_version) < _schema_tuple(
        SEMANTIC_PACKAGE_MIN_SCHEMA
    ):
        return
    if not manifest.semantic_scorer_version:
        fail(
            "release_manifest.json",
            "semantic-package",
            "schema-3.2 manifest records no semantic scorer version",
        )
    for task_id, expected_hash in sorted(manifest.tasks.items()):
        rel = f"private/{task_id}/{SEMANTIC_TASK_IR_REL}"
        path = release_dir / rel
        if not path.is_file():
            fail(rel, "semantic-package", "private semantic TaskIR is missing")
            continue
        try:
            task = task_from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            fail(rel, "semantic-package", f"private semantic TaskIR is invalid: {exc}")
            continue
        if task.task_id != task_id:
            fail(
                rel,
                "semantic-package",
                f"TaskIR task_id {task.task_id!r} does not match directory {task_id!r}",
            )
        actual_hash = task.content_hash()
        if actual_hash != expected_hash:
            fail(
                rel,
                "semantic-package",
                "TaskIR content hash does not match release_manifest.json",
            )
        if _schema_tuple(manifest.schema_version) >= _schema_tuple(
            SEMANTIC_PORTABILITY_MIN_SCHEMA
        ):
            from elt_taskgen.verification.release_portability import (
                ReleasePortabilityError,
                validate_release_portability,
            )

            try:
                validate_release_portability(
                    task,
                    release_dir / "private" / task_id / "answer_key",
                    {
                        population: release_dir / source_rel
                        for population, source_rel in (
                            manifest.el_sources.get(task_id) or {}
                        ).items()
                    },
                )
            except ReleasePortabilityError as exc:
                fail(
                    f"private/{task_id}",
                    "semantic-portability",
                    str(exc),
                )


def _verify_serving_surface(
    release_dir: Path,
    manifest: ReleaseManifest,
    on_disk: set[str],
    fail: Any,
) -> None:
    """Check what BYTES alone cannot: is this release actually gradable?

    Four questions a byte-perfect release can still fail: EL source roots
    present and pinned; reward paths resolve; every EL config table covered by
    the serving manifest; identifiers provisionable. Reported, never raised.
    """
    pinned = set(manifest.checksums)

    # (1) the graded EL source roots
    for parent, roots in sorted(manifest.el_sources.items()):
        for pop, rel in sorted(roots.items()):
            path = release_dir / rel
            if not path.is_dir():
                fail(rel, "el-sources", f"population {pop!r} source root is missing")
                continue
            files = {
                p.relative_to(release_dir).as_posix()
                for p in path.rglob("*")
                if p.is_file()
            }
            if not files:
                fail(rel, "el-sources", f"population {pop!r} source root is empty")
            elif not files <= pinned:
                fail(
                    rel,
                    "el-sources",
                    f"population {pop!r} source root holds unpinned file(s): "
                    f"{sorted(files - pinned)[:3]}",
                )
        parent_prefix = f"private/{parent}/"
        if not any(rel.startswith(parent_prefix) for rel in pinned):
            fail(
                parent_prefix,
                "el-sources",
                "manifest names EL sources for a parent with no private tree",
            )

    # (2) reward.json paths resolve under private/<parent>/
    for rel in sorted(r for r in on_disk if r.endswith("/" + REWARD_MANIFEST)):
        payload = _read_json(release_dir / rel)
        if not isinstance(payload, dict):
            fail(rel, "reward", "reward manifest is unreadable")
            continue
        if payload.get("variant") != TaskVariant.EXTRACT_LOAD.value:
            continue
        parent = str(payload.get("parent_task_id") or "")
        if not parent:
            fail(rel, "reward", "EL reward names no parent task")
            continue
        try:
            validate_task_id_segment(parent)
        except (TypeError, ValueError) as exc:
            fail(
                rel,
                "reward",
                f"EL reward names unsafe parent task {parent!r}: {exc}",
            )
            continue
        if parent not in manifest.tasks:
            fail(rel, "reward", f"EL reward names unknown parent task {parent!r}")
            continue
        expected_reward_rel = (
            Path("private")
            / variant_task_id(parent, TaskVariant.EXTRACT_LOAD)
            / REWARD_MANIFEST
        ).as_posix()
        if rel != expected_reward_rel:
            fail(
                rel,
                "reward",
                f"EL reward for parent {parent!r} is filed at {rel!r}, not "
                f"{expected_reward_rel!r}",
            )
            continue
        for key in ("expected", "sources"):
            named = payload.get(key)
            if not isinstance(named, dict):
                continue
            for pop, declared in sorted(named.items()):
                try:
                    safe_declared = _validate_release_relative_path(declared)
                except (TypeError, ValueError) as exc:
                    fail(
                        rel,
                        "reward",
                        f"{key}[{pop!r}] names unsafe path {declared!r}: {exc}",
                    )
                    continue
                target = release_dir / "private" / parent / safe_declared
                if not target.exists():
                    fail(
                        rel,
                        "reward",
                        f"{key}[{pop!r}] names {declared!r}, which does not "
                        f"resolve under private/{parent}/",
                    )

    # (3) every EL bundle's config tables are covered by the serving manifest
    for rel in sorted(r for r in on_disk if r.endswith("/config.yaml")):
        parts = rel.split("/")
        if len(parts) != 3 or parts[0] != "public":
            continue
        unit_id = parts[1]
        if manifest.public_layout == COMBINED_PUBLIC_LAYOUT:
            parent = unit_id if unit_id in manifest.tasks else ""
        else:
            parent = unit_id[: -len("__el")] if unit_id.endswith("__el") else ""
        if not parent:
            continue
        serving_rel = f"private/{parent}/answer_key/{SOURCES_SERVING_MANIFEST}"
        serving = _read_json(release_dir / serving_rel)
        if not isinstance(serving, dict):
            fail(serving_rel, "serving", "EL bundle ships no serving manifest")
            continue
        try:
            config = yaml.safe_load((release_dir / rel).read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            fail(rel, "serving", f"config is unreadable: {exc}")
            continue
        declared: set[str] = set()
        for tables in _config_tables_from_mapping(config).values():
            declared.update(tables)
        covered = set((serving.get("tables") or {}))
        uncovered = sorted(declared - covered)
        if uncovered:
            fail(
                serving_rel,
                "serving",
                f"serving manifest does not cover config table(s) {uncovered}",
            )

    # (4) provisionable identifiers
    try:
        _assert_release_source_identifiers(release_dir / "public")
    except ValueError as exc:
        fail("public", "identifiers", str(exc))


def _config_tables_from_mapping(config: Any) -> dict[str, list[str]]:
    """`eltbench._config_tables_by_section`, tolerant of a non-mapping config."""
    if not isinstance(config, dict):
        return {}
    return eltbench_mod._config_tables_by_section(config)


def _verify_source_provenance(
    release_dir: Path,
    manifest: ReleaseManifest,
    fail: Any,
) -> None:
    """Verify source selector, revision, and digest evidence for each task.

    Schema 3.5 certified releases require exact coverage. Development and older
    releases may omit it, but any supplied record is verified.
    """

    records = manifest.source_provenance
    digests = manifest.source_provenance_digests
    task_ids = set(manifest.tasks)
    record_ids = set(records)
    digest_ids = set(digests)
    current_contract = _schema_tuple(manifest.schema_version) >= _schema_tuple(
        _SOURCE_PROVENANCE_MIN_SCHEMA
    )

    if record_ids != digest_ids:
        fail(
            "release_manifest.json",
            "source-provenance",
            "source provenance record/digest coverage is not exact "
            f"(records_only={sorted(record_ids - digest_ids)}, "
            f"digests_only={sorted(digest_ids - record_ids)})",
        )
    unknown = sorted((record_ids | digest_ids) - task_ids)
    if unknown:
        fail(
            "release_manifest.json",
            "source-provenance",
            f"source provenance names unknown task(s) {unknown}",
        )
    if (
        current_contract
        and manifest.release_mode == CERTIFIED_RELEASE_MODE
        and record_ids != task_ids
    ):
        fail(
            "release_manifest.json",
            "source-provenance",
            "certified schema-3.5+ release must bind source provenance for "
            f"every task (missing={sorted(task_ids - record_ids)})",
        )

    if current_contract:
        expected_paths = {release_provenance_rel(task_id) for task_id in record_ids}
        recorded_paths = {
            rel
            for rel in manifest.checksums
            if rel.endswith(
                f"/{RELEASE_PROVENANCE_DIRNAME}/{RELEASE_PROVENANCE_FILENAME}"
            )
        }
        if expected_paths != recorded_paths:
            fail(
                "release_manifest.json",
                "source-provenance",
                "source provenance map/path coverage is not exact "
                f"(missing_paths={sorted(expected_paths - recorded_paths)}, "
                f"unmapped_paths={sorted(recorded_paths - expected_paths)})",
            )

    for task_id in sorted(record_ids | digest_ids):
        if task_id not in task_ids:
            continue
        record = records.get(task_id)
        expected_digest = digests.get(task_id)
        rel = release_provenance_rel(task_id)
        if record is None or expected_digest is None:
            continue
        if not _is_sha256(expected_digest):
            fail(rel, "source-provenance", "manifest evidence digest is not 64-hex")
        elif record.evidence_digest() != expected_digest:
            fail(
                rel,
                "source-provenance",
                "typed source record disagrees with its manifest digest",
            )
        if record.task_id != task_id:
            fail(
                rel,
                "source-provenance",
                f"record task_id {record.task_id!r} does not match map key {task_id!r}",
            )
        if rel not in manifest.checksums:
            fail(rel, "source-provenance", "source provenance sidecar is not pinned")
        elif manifest.checksum_kinds.get(rel, BYTE_CHECKSUM_KIND) != BYTE_CHECKSUM_KIND:
            fail(rel, "source-provenance", "source provenance must be byte-pinned")

        path = release_dir / rel
        if not path.exists():
            continue  # inventory verification reports the missing file too
        if path.is_symlink():
            fail(rel, "source-provenance", "source provenance sidecar is a symlink")
            continue
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                fail(rel, "source-provenance", "source provenance is not a regular file")
                continue
            raw = path.read_bytes()
            parsed = IngestProvenance.model_validate_json(raw)
        except (OSError, ValueError) as exc:
            fail(rel, "source-provenance", f"source provenance is invalid: {exc}")
            continue
        if raw != parsed.deterministic_bytes():
            fail(rel, "source-provenance", "sidecar is not in deterministic form")
        if parsed != record:
            fail(
                rel,
                "source-provenance",
                "sidecar record disagrees with release_manifest.json",
            )
        semantic_rel = f"private/{task_id}/{SEMANTIC_TASK_IR_REL}"
        try:
            task = task_from_json(
                (release_dir / semantic_rel).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue  # semantic-package verification reports this independently
        try:
            validate_task_binding(task, record)
        except ValueError as exc:
            fail(
                rel,
                "source-provenance",
                f"source record is not bound to released TaskIR: {exc}",
            )


def _verify_release_attestation(
    release_dir: Path,
    manifest: ReleaseManifest,
    fail: Any,
) -> None:
    """Verify the certified-release isolation record and its manifest seal."""

    rel = SANDBOX_ATTESTATION_FILENAME
    pinned = rel in manifest.checksums
    if manifest.release_mode == DEVELOPMENT_RELEASE_MODE:
        if manifest.sandbox_attestation_digest:
            fail(
                "release_manifest.json",
                "attestation",
                "development release records a sandbox attestation digest",
            )
        if pinned:
            fail(
                rel,
                "attestation",
                "development release pins certification evidence; use "
                "release_mode='certified'",
            )
        return

    if not _is_sha256(manifest.sandbox_attestation_digest):
        fail(
            "release_manifest.json",
            "attestation",
            "certified release has no valid sandbox_attestation_digest",
        )
    if not pinned:
        fail(
            rel,
            "attestation",
            "certified release does not pin sandbox_attestation.json",
        )
    elif manifest.checksum_kinds.get(rel, BYTE_CHECKSUM_KIND) != BYTE_CHECKSUM_KIND:
        fail(rel, "attestation", "sandbox attestation must be byte-pinned")

    path = release_dir / rel
    if not path.exists():
        return  # the inventory check reports the missing file too
    if path.is_symlink():
        fail(rel, "attestation", "sandbox attestation is a symlink")
        return
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            fail(rel, "attestation", "sandbox attestation is not a regular file")
            return
        if metadata.st_size > _MAX_SANDBOX_ATTESTATION_BYTES:
            fail(
                rel,
                "attestation",
                f"sandbox attestation exceeds {_MAX_SANDBOX_ATTESTATION_BYTES} bytes",
            )
            return
        raw = path.read_bytes()
        if len(raw) > _MAX_SANDBOX_ATTESTATION_BYTES:
            fail(
                rel,
                "attestation",
                "sandbox attestation grew beyond the size limit while read",
            )
            return
        record = SandboxAttestation.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        fail(rel, "attestation", f"sandbox attestation is unreadable: {exc}")
        return

    from elt_taskgen.export.attestation_gate import (
        AttestationRefusal,
        recompute_attestation_digest,
        require_attestation_for_labels,
    )

    try:
        # Verification is historical, not an expiry timer: use the record's
        # own timestamp as `now` while re-running every static gate. Freshness
        # was enforced against the real clock at freeze time.
        decision = require_attestation_for_labels(
            manifest,
            record,
            contamination_mode=record.contamination_mode,
            run_id=manifest.release_id,
            now=record.attested_at,
        )
        recomputed = recompute_attestation_digest(record)
    except AttestationRefusal as exc:
        fail(rel, "attestation", f"[{exc.code}] {exc}")
        return
    if (
        decision.attestation_digest != manifest.sandbox_attestation_digest
        or recomputed != manifest.sandbox_attestation_digest
    ):
        fail(
            rel,
            "attestation",
            "sandbox attestation content digest disagrees with the manifest",
        )


def _verify_difficulty_measurements(
    release_dir: Path,
    manifest: ReleaseManifest,
    fail: Any,
) -> None:
    """Verify exact task/report coverage and canonical empirical digests."""

    tasks = set(manifest.tasks)
    recorded = set(manifest.difficulty_measurements)
    pinned = {
        task_id
        for task_id in tasks
        if _difficulty_report_rel(task_id) in manifest.checksums
    }
    unknown = sorted(recorded - tasks)
    if unknown:
        fail(
            "release_manifest.json",
            "difficulty",
            f"difficulty_measurements names unknown task(s) {unknown}",
        )
    if recorded != pinned:
        missing_map = sorted(pinned - recorded)
        missing_file_pin = sorted(recorded - pinned)
        detail: list[str] = []
        if missing_map:
            detail.append(f"pinned reports missing from map {missing_map}")
        if missing_file_pin:
            detail.append(f"mapped reports missing exact file pin {missing_file_pin}")
        fail(
            "release_manifest.json",
            "difficulty",
            "difficulty report map/path coverage is not exact: " + "; ".join(detail),
        )
    if manifest.release_mode == CERTIFIED_RELEASE_MODE and recorded != tasks:
        fail(
            "release_manifest.json",
            "difficulty",
            "certified release must bind empirical difficulty for every task "
            f"(missing={sorted(tasks - recorded)})",
        )

    for task_id in sorted(recorded | pinned):
        if task_id not in tasks:
            continue
        rel = _difficulty_report_rel(task_id)
        if manifest.checksum_kinds.get(rel, BYTE_CHECKSUM_KIND) != BYTE_CHECKSUM_KIND:
            fail(rel, "difficulty", "difficulty report must be byte-pinned")
        expected = manifest.difficulty_measurements.get(task_id)
        if expected is not None and not _is_sha256(expected):
            fail(rel, "difficulty", "manifest difficulty digest is not 64-hex")
        path = release_dir / rel
        if not path.exists():
            continue  # the inventory check reports the missing file too
        if path.is_symlink():
            fail(rel, "difficulty", "difficulty report is a symlink")
            continue
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                fail(rel, "difficulty", "difficulty report is not a regular file")
                continue
            if metadata.st_size > _MAX_DIFFICULTY_REPORT_BYTES:
                fail(
                    rel,
                    "difficulty",
                    f"difficulty report exceeds {_MAX_DIFFICULTY_REPORT_BYTES} bytes",
                )
                continue
            raw = path.read_bytes()
            if len(raw) > _MAX_DIFFICULTY_REPORT_BYTES:
                fail(
                    rel,
                    "difficulty",
                    "difficulty report grew beyond the size limit while read",
                )
                continue
            measurement = DifficultyMeasurement.model_validate_json(raw)
            _validate_difficulty_measurement(
                measurement,
                task_id=task_id,
                content_hash=manifest.tasks[task_id],
                require_empirical=(
                    manifest.release_mode == CERTIFIED_RELEASE_MODE
                ),
            )
        except (OSError, ValueError) as exc:
            fail(rel, "difficulty", f"difficulty evidence is invalid: {exc}")
            continue
        actual = _difficulty_measurement_digest(measurement)
        if expected is not None and actual != expected:
            fail(
                rel,
                "difficulty",
                "canonical difficulty digest disagrees with the manifest",
            )


def verify_release(release_dir: Path) -> ReleaseVerification:
    """Verify a release using the pin rules in its own manifest.

    Reject unknown pin kinds, invalid census use, inconsistent summaries,
    unreadable warehouses, file-set changes, and checksum disagreements.
    Equivalent warehouse data may verify when storage bytes differ because
    current releases pin typed census contents rather than the .duckdb bytes.
    That pin is over canonical cell text with NULL distinct from '': two
    warehouses that differ only in storage layout verify, and two that differ
    in whether a cell is absent or empty do not.
    """
    requested_release_dir = Path(release_dir)
    if requested_release_dir.is_symlink():
        raise ValueError(
            f"release directory itself must not be a symbolic link: "
            f"{requested_release_dir}"
        )
    release_dir = requested_release_dir.resolve()
    manifest_path = release_dir / "release_manifest.json"
    try:
        manifest_metadata = manifest_path.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(f"no release manifest at {manifest_path}")
    if stat.S_ISLNK(manifest_metadata.st_mode):
        raise ValueError(
            f"release manifest must not be a symbolic link: {manifest_path}"
        )
    if not stat.S_ISREG(manifest_metadata.st_mode):
        raise ValueError(f"release manifest is not a regular file: {manifest_path}")
    manifest = ReleaseManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    legacy = manifest.schema_version == "1.0"

    failures: list[str] = []
    verdicts: list[FileVerdict] = []

    def fail(rel: str, kind: str, detail: str) -> None:
        failures.append(f"{rel}: {detail}")
        verdicts.append(FileVerdict(rel_path=rel, kind=kind, ok=False, detail=detail))

    # Validate paths and links before semantic helpers open release files.
    for detail in _manifest_path_problems(manifest):
        fail("release_manifest.json", "unsafe-path", detail)
    for rel, detail in _release_tree_problems(release_dir):
        fail(rel, "filesystem", detail)
    if failures:
        return ReleaseVerification(
            release_dir=str(release_dir),
            release_id=manifest.release_id,
            schema_version=manifest.schema_version,
            corpus_profile=manifest.corpus_profile,
            ok=False,
            files_checked=len(manifest.checksums) + 1,
            byte_pinned=0,
            census_pinned=0,
            files=tuple(verdicts),
            unpinned_files=(),
            failures=tuple(failures),
        )

    if _schema_tuple(manifest.schema_version) >= (3, 1):
        expected_release_id = _combined_release_id(
            destinations=manifest.destinations,
            destination_connector_versions=(
                manifest.destination_connector_versions
            ),
            splits=manifest.splits,
            tasks=manifest.tasks,
            public_runtime_checksums={
                key: value
                for key, value in manifest.checksums.items()
                if key.startswith("public/")
            },
            variants=manifest.variants,
            rejected_variants=manifest.rejected_variants,
            el_sources=manifest.el_sources,
            scorer_version=manifest.scorer_version,
            roster_digest=manifest.roster_digest,
            semantic_scorer_version=(
                manifest.semantic_scorer_version
                if _schema_tuple(manifest.schema_version) >= (3, 2)
                else None
            ),
            source_provenance_digests=(
                manifest.source_provenance_digests
                if _schema_tuple(manifest.schema_version)
                >= _schema_tuple(_SOURCE_PROVENANCE_MIN_SCHEMA)
                else None
            ),
        )
        if manifest.release_id != expected_release_id:
            fail(
                "release_manifest.json",
                "identity",
                "release_id does not match the manifest's recorded task/runtime fields",
            )

    if _schema_tuple(manifest.schema_version) >= _schema_tuple(
        _LAYERED_IDENTITY_MIN_SCHEMA
    ):
        # Recompute identities from frozen manifest fields, not current pins.
        chained_identities = _schema_tuple(
            manifest.schema_version
        ) >= _schema_tuple(_CHAINED_IDENTITY_MIN_SCHEMA)
        semantic_checksums = (
            _private_semantic_checksums(manifest.checksums, manifest.tasks)
            if chained_identities
            else _schema_33_private_semantic_checksums(
                manifest.checksums, manifest.tasks
            )
        )
        expected_semantic = _semantic_release_id(
            tasks=manifest.tasks,
            el_sources=manifest.el_sources,
            private_checksums=semantic_checksums,
            scorer_version=manifest.scorer_version,
            roster_digest=manifest.roster_digest,
            semantic_scorer_version=manifest.semantic_scorer_version,
        )
        if not manifest.semantic_release_id:
            fail(
                "release_manifest.json",
                "identity",
                "schema-3.3+ manifest records no semantic_release_id",
            )
        elif manifest.semantic_release_id != expected_semantic:
            fail(
                "release_manifest.json",
                "identity",
                "semantic_release_id does not match the manifest's recorded "
                "semantic fields",
            )
        if set(manifest.runtime_bundle_ids) != set(manifest.tasks):
            fail(
                "release_manifest.json",
                "identity",
                "runtime_bundle_ids must name every task exactly once",
            )
        if set(manifest.certification_ids) != set(manifest.tasks):
            fail(
                "release_manifest.json",
                "identity",
                "certification_ids must name every task exactly once",
            )
        for tid in sorted(set(manifest.runtime_bundle_ids) & set(manifest.tasks)):
            public_checksums = _public_task_checksums(manifest.checksums, tid)
            if chained_identities:
                private_runtime = _private_runtime_checksums(
                    manifest.checksums, tid
                )
                if not private_runtime:
                    fail(
                        "release_manifest.json",
                        "identity",
                        f"schema-3.4+ runtime identity for task {tid!r} has no "
                        "private runtime contract checksums",
                    )
                expected_bundle = _runtime_bundle_id(
                    semantic_release_id=manifest.semantic_release_id,
                    task_id=tid,
                    destination=manifest.destinations.get(tid, ""),
                    public_checksums=public_checksums,
                    private_runtime_checksums=private_runtime,
                )
            else:
                expected_bundle = _schema_33_runtime_bundle_id(
                    task_id=tid,
                    destination=manifest.destinations.get(tid, ""),
                    public_checksums=public_checksums,
                )
            if manifest.runtime_bundle_ids[tid] != expected_bundle:
                fail(
                    "release_manifest.json",
                    "identity",
                    f"runtime_bundle_id for task {tid!r} does not match the "
                    "manifest's recorded semantic, destination, and runtime "
                    "checksums",
                )
        shipped_destinations = set(manifest.destinations.values())
        if (
            chained_identities
            and set(manifest.certification_matrix) != shipped_destinations
        ):
            missing_destinations = sorted(
                shipped_destinations - set(manifest.certification_matrix)
            )
            extra_destinations = sorted(
                set(manifest.certification_matrix) - shipped_destinations
            )
            detail: list[str] = []
            if missing_destinations:
                detail.append(f"missing {missing_destinations}")
            if extra_destinations:
                detail.append(f"unexpected {extra_destinations}")
            fail(
                "release_manifest.json",
                "identity",
                "schema-3.4 certification_matrix destination set is not exact: "
                + "; ".join(detail),
            )
        for dest in sorted(shipped_destinations):
            if dest not in manifest.certification_matrix:
                fail(
                    "release_manifest.json",
                    "identity",
                    "certification_matrix records no matrix for shipped "
                    f"destination {dest!r}",
                )
        for tid in sorted(set(manifest.certification_ids) & set(manifest.tasks)):
            recorded_bundle = manifest.runtime_bundle_ids.get(tid)
            task_destination = manifest.destinations.get(tid, "")
            recorded_matrix = manifest.certification_matrix.get(task_destination)
            if recorded_bundle is None or recorded_matrix is None:
                continue  # the missing piece is already reported above
            try:
                # Validate the recorded recipe under its version and the trusted
                # outer destination before reproducing its frozen identity.
                matrix_for_identity = recorded_matrix
                if chained_identities:
                    matrix_for_identity = (
                        runtime_matrix.validate_recorded_certification_matrix(
                            recorded_matrix,
                            task_destination,
                        )
                    )
                expected_certification = _certification_id(
                    runtime_bundle_id=recorded_bundle,
                    matrix=matrix_for_identity,
                    validate_matrix=False,
                )
            except ValueError as exc:
                fail(
                    "release_manifest.json",
                    "identity",
                    f"certification matrix for task {tid!r} is invalid: {exc}",
                )
                continue
            if manifest.certification_ids[tid] != expected_certification:
                fail(
                    "release_manifest.json",
                    "identity",
                    f"certification_id for task {tid!r} does not match the "
                    "recorded runtime_bundle_id and certification matrix",
                )

    # Certification and difficulty are semantic validations in addition to the
    # byte inventory below. They deliberately run before it so a malformed but
    # checksum-consistent record cannot be mistaken for valid evidence.
    _verify_release_attestation(release_dir, manifest, fail)
    _verify_difficulty_measurements(release_dir, manifest, fail)
    _verify_source_provenance(release_dir, manifest, fail)

    # --- inventory ---------------------------------------------------------
    on_disk = {
        p.relative_to(release_dir).as_posix()
        for p in release_dir.rglob("*")
        if p.is_file()
    } - {"checksums.sha256"}
    recorded = set(manifest.checksums) | {"release_manifest.json"}
    for rel in sorted(recorded - on_disk):
        fail(rel, manifest.checksum_kinds.get(rel, BYTE_CHECKSUM_KIND), "recorded file is missing")
    unpinned = sorted(on_disk - recorded)
    for rel in unpinned:
        fail(rel, "unpinned", "file present in the release but pinned by nothing")

    # --- checksums.sha256 must agree with the manifest ---------------------
    byte_pinned = 0
    census_pinned = 0
    flat_path = release_dir / "checksums.sha256"
    if not flat_path.is_file():
        failures.append("checksums.sha256: missing")
    else:
        try:
            flat = _read_flat_checksums(flat_path)
        except (OSError, ValueError) as exc:
            fail(
                "checksums.sha256",
                "checksum-index",
                f"checksum index is invalid: {exc}",
            )
            flat = {}
        expected_flat = {
            rel: digest
            for rel, digest in manifest.checksums.items()
            if rel not in manifest.checksum_kinds
        }
        expected_flat["release_manifest.json"] = _file_sha256(manifest_path)
        for rel in sorted(set(expected_flat) ^ set(flat)):
            failures.append(
                f"checksums.sha256: byte-hash entries disagree with the manifest at {rel!r}"
            )
        for rel in sorted(set(expected_flat) & set(flat)):
            if expected_flat[rel] != flat[rel]:
                failures.append(
                    f"checksums.sha256: digest for {rel!r} disagrees with the manifest"
                )
        # The manifest is the authority for every other file, so it is itself
        # byte-pinned — in checksums.sha256, the one place outside it.
        recorded_manifest_digest = flat.get("release_manifest.json")
        if recorded_manifest_digest is None:
            fail("release_manifest.json", BYTE_CHECKSUM_KIND, "not pinned in checksums.sha256")
        elif recorded_manifest_digest != expected_flat["release_manifest.json"]:
            fail("release_manifest.json", BYTE_CHECKSUM_KIND, "sha256 mismatch")
        else:
            byte_pinned += 1
            verdicts.append(
                FileVerdict(rel_path="release_manifest.json", kind=BYTE_CHECKSUM_KIND, ok=True)
            )

    # --- per-file verification --------------------------------------------
    for rel in sorted(manifest.checksums):
        path = release_dir / rel
        if not path.is_file():
            continue  # already reported as missing
        expected = manifest.checksums[rel]
        kind = manifest.checksum_kinds.get(rel, BYTE_CHECKSUM_KIND)
        if kind == BYTE_CHECKSUM_KIND:
            if not legacy and path.suffix == WAREHOUSE_SUFFIX:
                fail(
                    rel,
                    kind,
                    "DuckDB warehouse is byte-pinned in an EL/T manifest; "
                    "warehouse bytes are not reproducible and must carry "
                    f"{WAREHOUSE_CHECKSUM_KIND}",
                )
                continue
            actual = _file_sha256(path)
            if actual != expected:
                fail(rel, kind, f"sha256 mismatch (expected {expected[:12]}, got {actual[:12]})")
            else:
                byte_pinned += 1
                verdicts.append(FileVerdict(rel_path=rel, kind=kind, ok=True))
            continue
        if kind != WAREHOUSE_CHECKSUM_KIND:
            # Name the version for a census pin from ANOTHER algorithm: this
            # check fires before the 'recorded under version' branch, so
            # otherwise a v1 tree just reads "unknown kind".
            if kind.startswith(_WAREHOUSE_CHECKSUM_KIND_PREFIX):
                detail = (
                    f"warehouse census pinned under {kind!r} but this code "
                    f"computes census version {CENSUS_VERSION!r}; digests are "
                    "per-version and not comparable (fail closed) — re-freeze "
                    "the release under the current census"
                )
            else:
                detail = f"unknown checksum kind {kind!r} (fail closed)"
            fail(rel, kind, detail)
            continue
        if path.suffix != WAREHOUSE_SUFFIX:
            fail(
                rel,
                kind,
                f"census pin claimed for a non-{WAREHOUSE_SUFFIX} file; the "
                "byte exemption covers DuckDB warehouses only",
            )
            continue
        record = manifest.warehouse_census.get(rel)
        if record is None:
            fail(rel, kind, "census-pinned but the manifest records no census evidence")
            continue
        if record.census_version != CENSUS_VERSION:
            fail(
                rel,
                kind,
                f"census recorded under version {record.census_version!r} but this "
                f"code computes version {CENSUS_VERSION!r}; digests are not comparable",
            )
            continue
        try:
            recomputed_record = _record_census_digest(record)
        except ValueError as exc:
            fail(rel, kind, f"census record is internally inconsistent: {exc}")
            continue
        if recomputed_record != record.census_digest or record.census_digest != expected:
            fail(rel, kind, "census record disagrees with its own roll-up digest")
            continue
        try:
            actual_census = warehouse_census(path)
        except Exception as exc:  # noqa: BLE001 - an unreadable warehouse is a
            # verification failure, not a crash: a corrupt file must be
            # REPORTED, never escape as a traceback read as a tooling problem.
            fail(rel, kind, f"warehouse could not be censused: {type(exc).__name__}: {exc}")
            continue
        if actual_census["census_digest"] != expected:
            diverged = sorted(
                name
                for name in set(actual_census["tables"]) | set(record.row_counts)
                if actual_census["tables"].get(name, {}).get("row_digest")
                != record.row_digests.get(name)
            )
            detail = (
                "warehouse census mismatch: content differs from the released "
                f"warehouse (relations: {diverged})"
            )
            if str(actual_census.get("catalog_digest")) != record.catalog_digest:
                detail += (
                    "; catalog objects (macros/views/comments/types/...) differ"
                )
            fail(rel, kind, detail)
            continue
        census_pinned += 1
        verdicts.append(FileVerdict(rel_path=rel, kind=kind, ok=True))

    # --- public runtime boundary + serving surface -------------------------
    if manifest.public_layout == COMBINED_PUBLIC_LAYOUT:
        if manifest.corpus_profile != COMBINED_CORPUS_PROFILE:
            fail(
                "release_manifest.json",
                "layout",
                "combined public layout requires corpus profile "
                f"{COMBINED_CORPUS_PROFILE!r}",
            )
        _verify_combined_public_layout(release_dir, manifest, fail)
        _verify_semantic_packages(release_dir, manifest, fail)
    elif manifest.corpus_profile == COMBINED_CORPUS_PROFILE:
        fail(
            "release_manifest.json",
            "layout",
            "end-to-end corpus profile requires the combined public layout",
        )

    if not legacy and _schema_tuple(manifest.schema_version) >= _schema_tuple(
        _SERVING_SURFACE_MIN_SCHEMA
    ):
        _verify_serving_surface(release_dir, manifest, on_disk, fail)

    files_checked = len(manifest.checksums) + 1  # + release_manifest.json
    return ReleaseVerification(
        release_dir=str(release_dir),
        release_id=manifest.release_id,
        schema_version=manifest.schema_version,
        corpus_profile=manifest.corpus_profile,
        ok=not failures,
        files_checked=files_checked,
        byte_pinned=byte_pinned,
        census_pinned=census_pinned,
        files=tuple(verdicts),
        unpinned_files=tuple(unpinned),
        failures=tuple(failures),
    )

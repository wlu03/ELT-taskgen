"""Ingest pinned manifests from the five source pools with fail-closed checks.

Inputs, adapters, generator code, and dependency locks are verified before the
workspace opens. Runtime dependencies remain outside this local receipt.
Registration is lock-protected, resumable, and idempotent; the receipt publishes
only after every declared record completes. This module performs no network or
provider work.
"""

from __future__ import annotations

import os
import json
import shutil
import stat
import tempfile
import hashlib
from contextlib import nullcontext as _nullcontext
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from elt_taskgen import __version__
from elt_taskgen.catalog import SourceCatalog, load_source_catalog
from elt_taskgen.engine import Engine
from elt_taskgen.models import (
    Origin,
    TaskIR,
    TaskStatus,
    canonical_json,
    readable_json,
    sha256_hex,
    task_from_json,
    validate_task_id_segment,
)
from elt_taskgen.package_resources import resource_path
from elt_taskgen.provenance import (
    ImplementationAdapterTransition,
    IngestProvenance,
    ProvenanceArtifact,
    SourceIdentity,
    generator_equivalence_problem,
    implementation_equivalence_problem,
    load_for_lineage,
    lineage_root_hash,
    publish_generator_equivalence,
    publish_implementation_equivalence,
    publish_or_confirm,
    sha256_file,
    sha256_tree,
)


FIVE_SOURCE_MANIFEST_SCHEMA_VERSION = "five-source-ingest-v1"
FIVE_SOURCE_BATCH_MANIFEST_SCHEMA_VERSION = "five-source-ingest-v2"
SELECTED_SOURCE_MANIFEST_SCHEMA_VERSION = "selected-source-ingest-v3"
FIVE_SOURCE_RECEIPT_SCHEMA_VERSION = "1.0"
FIVE_SOURCE_BATCH_RECEIPT_SCHEMA_VERSION = "2.0"
SELECTED_SOURCE_RECEIPT_SCHEMA_VERSION = "3.0"
INGEST_RECEIPT_DIRNAME = "ingest_manifests"

FIVE_ORIGINS: tuple[Origin, ...] = (
    Origin.DBT,
    Origin.DLT,
    Origin.SYNSQL,
    Origin.SCHEMAPILE,
    Origin.WIKIDBS,
)


class FiveSourceIngestError(ValueError):
    """The manifest or one of its candidates failed before ingestion."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict:
    loader.flatten_mapping(node)
    result: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class ArtifactPin(BaseModel):
    """One local adapter input and the digest expected for its exact bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    digest_kind: Literal["sha256-file", "sha256-tree-v1"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _clean_path(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("artifact path must not have surrounding whitespace")
        if any(ord(character) < 32 for character in value):
            raise ValueError("artifact path must not contain control characters")
        return value


class GeneratorPin(BaseModel):
    """Exact local generator and dependency-lock identity required by v2."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal["elt-taskgen-five-source"]
    version: str = Field(min_length=1)
    digest_kind: Literal["sha256-generator-tree-v1"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("version")
    @classmethod
    def _clean_version(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("generator version must be clean printable text")
        return value


def _package_python_inventory(
    package_root: Path,
) -> tuple[dict[str, str | int], ...]:
    """Inventory Python sources without ever following a package symlink dir."""

    try:
        root_metadata = package_root.lstat()
    except OSError as exc:
        raise FiveSourceIngestError(
            f"cannot inspect installed generator package {package_root}: {exc}"
        ) from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise FiveSourceIngestError(
            "installed generator package must be a non-symlink directory: "
            f"{package_root}"
        )

    inventory: list[dict[str, str | int]] = []

    def refuse_walk_error(exc: OSError) -> None:
        raise exc

    try:
        for directory_raw, directory_names, file_names in os.walk(
            package_root,
            topdown=True,
            onerror=refuse_walk_error,
            followlinks=False,
        ):
            directory = Path(directory_raw)
            retained_directories: list[str] = []
            for name in sorted(directory_names):
                path = directory / name
                relative = path.relative_to(package_root)
                if relative.parts[0] == "_resources":
                    # Wheel resources are outside the package-code half of the
                    # boundary; the one executable helper is added explicitly.
                    continue
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                    metadata.st_mode
                ):
                    raise FiveSourceIngestError(
                        "generator package directory must be a regular "
                        f"non-symlink directory: {path}"
                    )
                retained_directories.append(name)
            # Mutating this list is the documented top-down os.walk mechanism
            # for pruning; no excluded or symlinked directory is traversed.
            directory_names[:] = retained_directories

            for name in sorted(file_names):
                if not name.endswith(".py"):
                    continue
                path = directory / name
                relative = path.relative_to(package_root)
                if relative.parts[0] == "_resources":
                    continue
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(
                    metadata.st_mode
                ):
                    raise FiveSourceIngestError(
                        f"generator source must be a regular non-symlink file: {path}"
                    )
                digest = sha256_file(path)
                inventory.append(
                    {
                        "path": f"elt_taskgen/{relative.as_posix()}",
                        "size": metadata.st_size,
                        "sha256": digest,
                    }
                )
    except FiveSourceIngestError:
        raise
    except (OSError, ValueError) as exc:
        raise FiveSourceIngestError(
            f"cannot inventory installed generator package {package_root}: {exc}"
        ) from exc
    return tuple(sorted(inventory, key=lambda item: str(item["path"])))


def _generator_tree_inventory() -> tuple[dict[str, str | int], ...]:
    """Inventory regular Python sources and the WikiDBs helper for generator hashing.

    Exclude bundled resources, bytecode, metadata, interpreter, native, and OS
    inputs. Reject symlinks and special files.
    """

    # Do not resolve this path: an installation reached through a symlinked
    # package directory must be refused by the walker, not silently normalized.
    package_root = Path(__file__).absolute().parent
    inventory = list(_package_python_inventory(package_root))

    helper = resource_path("tools/wikidbs_family_map.py")
    try:
        helper_metadata = helper.lstat()
    except OSError as exc:
        raise FiveSourceIngestError(
            f"cannot inspect generator helper {helper}: {exc}"
        ) from exc
    if stat.S_ISLNK(helper_metadata.st_mode) or not stat.S_ISREG(
        helper_metadata.st_mode
    ):
        raise FiveSourceIngestError(
            f"generator helper must be a regular non-symlink file: {helper}"
        )
    try:
        helper_digest = sha256_file(helper)
    except (OSError, ValueError) as exc:
        raise FiveSourceIngestError(
            f"cannot hash generator helper {helper}: {exc}"
        ) from exc
    inventory.append(
        {
            "path": "tools/wikidbs_family_map.py",
            "size": helper_metadata.st_size,
            "sha256": helper_digest,
        }
    )
    return tuple(sorted(inventory, key=lambda item: str(item["path"])))


def current_generator_pin() -> GeneratorPin:
    """Calculate current generator-source and dependency-lock pins."""

    lock_path = resource_path("uv.lock")
    try:
        lock_digest = sha256_file(lock_path)
    except (OSError, ValueError) as exc:
        raise FiveSourceIngestError(
            f"cannot hash dependency lock {lock_path}: {exc}"
        ) from exc
    return GeneratorPin(
        name="elt-taskgen-five-source",
        version=__version__,
        digest_kind="sha256-generator-tree-v1",
        sha256=sha256_hex(canonical_json(_generator_tree_inventory())),
        lock_sha256=lock_digest,
    )


def _verify_generator_pin(pin: GeneratorPin, *, label: str) -> None:
    """Fail unless a declared v2 generator pin matches this installation."""

    if pin.version != __version__:
        raise FiveSourceIngestError(
            f"{label} generator version mismatch: manifest pins {pin.version!r}, "
            f"installed package is {__version__!r}"
        )
    observed = current_generator_pin()
    if pin.sha256 != observed.sha256:
        raise FiveSourceIngestError(
            f"{label} generator digest mismatch: expected {pin.sha256}, "
            f"observed {observed.sha256}"
        )
    if pin.lock_sha256 != observed.lock_sha256:
        raise FiveSourceIngestError(
            f"{label} dependency lock digest mismatch: expected {pin.lock_sha256}, "
            f"observed {observed.lock_sha256} at {resource_path('uv.lock')}"
        )


class _SourceEntry(BaseModel):
    """Pins shared by every source-specific entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selector: str = Field(min_length=1)
    expected_task_id: str = Field(min_length=1)
    upstream_url: str = Field(min_length=1)
    upstream_revision: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    adapter_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    license: str = Field(min_length=1)
    license_evidence: str = Field(min_length=1)

    @field_validator(
        "selector",
        "upstream_revision",
        "adapter_version",
        "license",
        "license_evidence",
    )
    @classmethod
    def _clean_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("source identity text must not have surrounding whitespace")
        if any(ord(character) < 32 for character in value):
            raise ValueError("source identity text must not contain control characters")
        return value

    @field_validator("upstream_url")
    @classmethod
    def _public_upstream_url(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("upstream_url must be clean printable text")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("upstream_url must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("upstream_url must not contain credentials")
        return value

    @field_validator("expected_task_id")
    @classmethod
    def _safe_expected_task_id(cls, value: str) -> str:
        return validate_task_id_segment(value)


class DbtIngest(_SourceEntry):
    pool: Literal["dbt"]
    manifest: ArtifactPin
    family: str = Field(min_length=1)


class DltIngest(_SourceEntry):
    pool: Literal["dlt"]
    manifest: ArtifactPin
    connector: str = Field(min_length=1)
    #: V2 requires embedded upstream URL and commit data matching the outer entry.
    require_embedded_provenance: Literal[True] | None = None


class SynSQLIngest(_SourceEntry):
    pool: Literal["synsql"]
    tables: ArtifactPin


class SchemaPileIngest(_SourceEntry):
    pool: Literal["schemapile"]
    source: ArtifactPin
    index: ArtifactPin


class WikiDBsIngest(_SourceEntry):
    pool: Literal["wikidbs"]
    database: ArtifactPin
    family_map: ArtifactPin
    #: Production intake verifies all part listings; only exploratory intake may opt out.
    verify_nodes: Literal[True] = True
    #: Digest of the five sorted ``part-N`` directory-name listings. This binds
    #: the graph-component input without hashing 213 GB of table contents.
    node_inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FiveSourceEntries(BaseModel):
    """The closed five-origin roster.  Missing and extra origins are invalid."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dbt: DbtIngest
    dlt: DltIngest
    synsql: SynSQLIngest
    schemapile: SchemaPileIngest
    wikidbs: WikiDBsIngest


class FiveSourceIngestManifest(BaseModel):
    """Versioned local-input contract for one five-source candidate roster."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["five-source-ingest-v1"] = (
        FIVE_SOURCE_MANIFEST_SCHEMA_VERSION
    )
    #: Pin the policy-bearing source catalog in checkout and wheel installations.
    catalog: ArtifactPin
    sources: FiveSourceEntries

    def ordered_entries(self) -> tuple[_SourceEntry, ...]:
        return (
            self.sources.dbt,
            self.sources.dlt,
            self.sources.synsql,
            self.sources.schemapile,
            self.sources.wikidbs,
        )

    def manifest_sha256(self) -> str:
        payload = self.model_dump(mode="json")
        # Preserve the original v1 run identity when the new v2-only dlt
        # strictness flag is absent/defaulted.
        if payload["sources"]["dlt"].get("require_embedded_provenance") is None:
            del payload["sources"]["dlt"]["require_embedded_provenance"]
        return sha256_hex(canonical_json(payload))

    def source_entry_sha256(self, entry: _SourceEntry) -> str:
        """Digest one source entry without host-local artifact paths."""

        try:
            declared = getattr(self.sources, entry.pool)
        except AttributeError:
            raise FiveSourceIngestError(
                f"source entry has unsupported pool {entry.pool!r}"
            ) from None
        if declared != entry:
            raise FiveSourceIngestError(
                f"source entry for {entry.pool!r} is not from this manifest"
            )
        return _portable_source_entry_sha256(
            schema_version=self.schema_version,
            entry=entry,
        )


class FiveSourceBatchEntries(BaseModel):
    """A non-empty, canonically ordered candidate list for every real pool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dbt: tuple[DbtIngest, ...] = Field(min_length=1)
    dlt: tuple[DltIngest, ...] = Field(min_length=1)
    synsql: tuple[SynSQLIngest, ...] = Field(min_length=1)
    schemapile: tuple[SchemaPileIngest, ...] = Field(min_length=1)
    wikidbs: tuple[WikiDBsIngest, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _canonical_entry_order(self) -> "FiveSourceBatchEntries":
        for origin in FIVE_ORIGINS:
            entries = getattr(self, origin.value)
            task_ids = tuple(entry.expected_task_id for entry in entries)
            if task_ids != tuple(sorted(task_ids)):
                raise ValueError(
                    f"sources.{origin.value} must be sorted by expected_task_id"
                )
        return self


class FiveSourceBatchIngestManifest(BaseModel):
    """Exact-count five-source roster with generator and lockfile pins."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["five-source-ingest-v2"] = (
        FIVE_SOURCE_BATCH_MANIFEST_SCHEMA_VERSION
    )
    expected_task_count: int = Field(ge=len(FIVE_ORIGINS))
    #: Require family and cluster independence before release-bound ingestion.
    require_unique_independence_units: Literal[True] = True
    generator: GeneratorPin
    catalog: ArtifactPin
    sources: FiveSourceBatchEntries

    @model_validator(mode="after")
    def _exact_unique_roster(self) -> "FiveSourceBatchIngestManifest":
        entries = self.ordered_entries()
        if len(entries) != self.expected_task_count:
            raise ValueError(
                f"expected_task_count={self.expected_task_count} but the five "
                f"source lists contain {len(entries)} entries"
            )
        task_ids = tuple(entry.expected_task_id for entry in entries)
        duplicates = sorted(
            task_id for task_id in set(task_ids) if task_ids.count(task_id) > 1
        )
        if duplicates:
            raise ValueError(f"duplicate expected_task_id values: {duplicates}")
        if any(
            entry.require_embedded_provenance is not True
            for entry in self.sources.dlt
        ):
            raise ValueError(
                "every v2 dlt entry must set require_embedded_provenance: true"
            )
        return self

    def ordered_entries(self) -> tuple[_SourceEntry, ...]:
        return tuple(
            entry
            for origin in FIVE_ORIGINS
            for entry in getattr(self.sources, origin.value)
        )

    def manifest_sha256(self) -> str:
        return sha256_hex(canonical_json(self.model_dump(mode="json")))

    def source_entry_sha256(self, entry: _SourceEntry) -> str:
        try:
            declared = getattr(self.sources, entry.pool)
        except AttributeError:
            raise FiveSourceIngestError(
                f"source entry has unsupported pool {entry.pool!r}"
            ) from None
        if entry not in declared:
            raise FiveSourceIngestError(
                f"source entry for {entry.pool!r} is not from this manifest"
            )
        return _portable_source_entry_sha256(
            schema_version=self.schema_version,
            entry=entry,
        )


class SelectedSourceEntries(BaseModel):
    """Closed, possibly empty source lists for a configured candidate run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dbt: tuple[DbtIngest, ...] = ()
    dlt: tuple[DltIngest, ...] = ()
    synsql: tuple[SynSQLIngest, ...] = ()
    schemapile: tuple[SchemaPileIngest, ...] = ()
    wikidbs: tuple[WikiDBsIngest, ...] = ()

    @model_validator(mode="after")
    def _canonical_entry_order(self) -> "SelectedSourceEntries":
        for origin in FIVE_ORIGINS:
            entries = getattr(self, origin.value)
            task_ids = tuple(entry.expected_task_id for entry in entries)
            if task_ids != tuple(sorted(task_ids)):
                raise ValueError(
                    f"sources.{origin.value} must be sorted by expected_task_id"
                )
        return self


class SelectedSourceIngestManifest(BaseModel):
    """Reproducible candidate roster selected from a pinned v2 pool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["selected-source-ingest-v3"] = (
        SELECTED_SOURCE_MANIFEST_SCHEMA_VERSION
    )
    expected_task_count: int = Field(ge=1)
    parent_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_seed: int
    source_allocation: dict[Origin, int]
    require_unique_independence_units: Literal[True] = True
    generator: GeneratorPin
    catalog: ArtifactPin
    sources: SelectedSourceEntries

    @model_validator(mode="after")
    def _exact_selected_roster(self) -> "SelectedSourceIngestManifest":
        entries = self.ordered_entries()
        if len(entries) != self.expected_task_count:
            raise ValueError(
                f"expected_task_count={self.expected_task_count} but the selected "
                f"source lists contain {len(entries)} entries"
            )
        task_ids = tuple(entry.expected_task_id for entry in entries)
        duplicates = sorted(
            task_id for task_id in set(task_ids) if task_ids.count(task_id) > 1
        )
        if duplicates:
            raise ValueError(f"duplicate expected_task_id values: {duplicates}")
        expected_allocation = {
            origin: len(getattr(self.sources, origin.value)) for origin in FIVE_ORIGINS
        }
        normalized = {origin: int(self.source_allocation.get(origin, 0)) for origin in FIVE_ORIGINS}
        if set(self.source_allocation) - set(FIVE_ORIGINS):
            raise ValueError("source_allocation contains an unknown origin")
        if normalized != expected_allocation:
            raise ValueError(
                "source_allocation does not equal the selected source-list counts"
            )
        if any(
            entry.require_embedded_provenance is not True
            for entry in self.sources.dlt
        ):
            raise ValueError(
                "every selected dlt entry must set require_embedded_provenance: true"
            )
        return self

    def ordered_entries(self) -> tuple[_SourceEntry, ...]:
        return tuple(
            entry
            for origin in FIVE_ORIGINS
            for entry in getattr(self.sources, origin.value)
        )

    def manifest_sha256(self) -> str:
        return sha256_hex(canonical_json(self.model_dump(mode="json")))

    def source_entry_sha256(self, entry: _SourceEntry) -> str:
        try:
            declared = getattr(self.sources, entry.pool)
        except AttributeError:
            raise FiveSourceIngestError(
                f"source entry has unsupported pool {entry.pool!r}"
            ) from None
        if entry not in declared:
            raise FiveSourceIngestError(
                f"source entry for {entry.pool!r} is not from this manifest"
            )
        return _portable_source_entry_sha256(
            schema_version=self.schema_version,
            entry=entry,
        )


GeneratorPinnedManifest = FiveSourceBatchIngestManifest | SelectedSourceIngestManifest
FiveSourceManifest = (
    FiveSourceIngestManifest
    | FiveSourceBatchIngestManifest
    | SelectedSourceIngestManifest
)


def _generator_pinned(manifest: FiveSourceManifest) -> bool:
    return isinstance(
        manifest, (FiveSourceBatchIngestManifest, SelectedSourceIngestManifest)
    )


def _portable_source_entry_sha256(
    *, schema_version: str, entry: _SourceEntry
) -> str:
    """Hash a task selector without binding it to host-local artifact paths."""

    artifact_roles = dict(_entry_artifacts(entry))
    semantic = entry.model_dump(
        mode="json", exclude=set(artifact_roles), exclude_none=False
    )
    # This optional field was added for the v2 contract. Keeping an absent v1
    # flag out of the projection preserves existing v1 provenance identities.
    if semantic.get("require_embedded_provenance") is None:
        semantic.pop("require_embedded_provenance", None)
    semantic["artifacts"] = {
        role: {
            "digest_kind": pin.digest_kind,
            "sha256": pin.sha256,
        }
        for role, pin in artifact_roles.items()
    }
    return sha256_hex(
        canonical_json(
            {
                "schema_version": schema_version,
                "source": semantic,
            }
        )
    )


class IngestReceiptTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: Origin
    task_id: str
    task_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FiveSourceIngestReceipt(BaseModel):
    """Deterministic completion marker for a resumable five-task ingestion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = FIVE_SOURCE_RECEIPT_SCHEMA_VERSION
    manifest_schema_version: Literal["five-source-ingest-v1"] = (
        FIVE_SOURCE_MANIFEST_SCHEMA_VERSION
    )
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tasks: tuple[IngestReceiptTask, ...]

    @model_validator(mode="after")
    def _exact_origin_roster(self) -> "FiveSourceIngestReceipt":
        observed = tuple(task.origin for task in self.tasks)
        if observed != FIVE_ORIGINS:
            raise ValueError(
                "ingest receipt must contain exactly the five origins in canonical order"
            )
        if len({task.task_id for task in self.tasks}) != len(self.tasks):
            raise ValueError("ingest receipt task ids must be unique")
        return self

    def deterministic_bytes(self) -> bytes:
        return (readable_json(self.model_dump(mode="json")) + "\n").encode("utf-8")


class FiveSourceBatchIngestReceipt(BaseModel):
    """Deterministic completion marker for an exact v2 batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["2.0"] = FIVE_SOURCE_BATCH_RECEIPT_SCHEMA_VERSION
    manifest_schema_version: Literal["five-source-ingest-v2"] = (
        FIVE_SOURCE_BATCH_MANIFEST_SCHEMA_VERSION
    )
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generator: GeneratorPin
    expected_task_count: int = Field(ge=len(FIVE_ORIGINS))
    origin_counts: dict[Origin, int]
    tasks: tuple[IngestReceiptTask, ...]

    @model_validator(mode="after")
    def _exact_canonical_roster(self) -> "FiveSourceBatchIngestReceipt":
        if len(self.tasks) != self.expected_task_count:
            raise ValueError(
                "batch receipt task count does not equal expected_task_count"
            )
        task_ids = tuple(task.task_id for task in self.tasks)
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("ingest receipt task ids must be unique")
        observed_counts = {
            origin: sum(task.origin is origin for task in self.tasks)
            for origin in FIVE_ORIGINS
        }
        if self.origin_counts != observed_counts or any(
            count < 1 for count in observed_counts.values()
        ):
            raise ValueError(
                "batch receipt must record the exact non-empty five-origin counts"
            )
        expected_order = tuple(
            sorted(
                self.tasks,
                key=lambda task: (FIVE_ORIGINS.index(task.origin), task.task_id),
            )
        )
        if self.tasks != expected_order:
            raise ValueError(
                "batch receipt tasks must be in canonical origin/task-id order"
            )
        return self

    def deterministic_bytes(self) -> bytes:
        return (readable_json(self.model_dump(mode="json")) + "\n").encode("utf-8")


class SelectedSourceIngestReceipt(BaseModel):
    """Deterministic completion marker for an arbitrary-count v3 roster."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["3.0"] = SELECTED_SOURCE_RECEIPT_SCHEMA_VERSION
    manifest_schema_version: Literal["selected-source-ingest-v3"] = (
        SELECTED_SOURCE_MANIFEST_SCHEMA_VERSION
    )
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generator: GeneratorPin
    expected_task_count: int = Field(ge=1)
    selection_seed: int
    origin_counts: dict[Origin, int]
    tasks: tuple[IngestReceiptTask, ...]

    @model_validator(mode="after")
    def _exact_canonical_roster(self) -> "SelectedSourceIngestReceipt":
        if len(self.tasks) != self.expected_task_count:
            raise ValueError(
                "selected receipt task count does not equal expected_task_count"
            )
        task_ids = tuple(task.task_id for task in self.tasks)
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("ingest receipt task ids must be unique")
        observed_counts = {
            origin: sum(task.origin is origin for task in self.tasks)
            for origin in FIVE_ORIGINS
        }
        normalized = {origin: int(self.origin_counts.get(origin, 0)) for origin in FIVE_ORIGINS}
        if set(self.origin_counts) - set(FIVE_ORIGINS) or normalized != observed_counts:
            raise ValueError(
                "selected receipt must record the exact five-origin counts"
            )
        expected_order = tuple(
            sorted(
                self.tasks,
                key=lambda task: (FIVE_ORIGINS.index(task.origin), task.task_id),
            )
        )
        if self.tasks != expected_order:
            raise ValueError(
                "selected receipt tasks must be in canonical origin/task-id order"
            )
        return self

    def deterministic_bytes(self) -> bytes:
        return (readable_json(self.model_dump(mode="json")) + "\n").encode("utf-8")


IngestReceipt = (
    FiveSourceIngestReceipt
    | FiveSourceBatchIngestReceipt
    | SelectedSourceIngestReceipt
)


@dataclass(frozen=True)
class PreparedSource:
    origin: Origin
    task: TaskIR
    provenance: IngestProvenance
    artifacts: tuple[tuple[str, Path, ArtifactPin], ...]
    collisions: tuple[object, ...] = ()
    source_entry_sha256: str = ""
    #: Optional proof that a new generator reproduced the root from identical inputs.
    generator_equivalence: IngestProvenance | None = None
    #: V2 permits one adapter movement without changing authoritative provenance.
    implementation_equivalence: IngestProvenance | None = None
    implementation_adapter_transition: ImplementationAdapterTransition | None = None


class GeneratorEquivalenceTaskBinding(BaseModel):
    """Exact old/new evidence expected for one member of a run migration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    source_entry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    intake_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("task_id")
    @classmethod
    def _safe_task_id(cls, value: str) -> str:
        return validate_task_id_segment(value)


class GeneratorReportRevalidationRequest(BaseModel):
    """Explicit run-local capability to revalidate one exact obsolete report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["generator-report-revalidation-request-v2"] = (
        "generator-report-revalidation-request-v2"
    )
    run_id: str = Field(min_length=1)
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: Literal["declarative-prose-function-call-false-positive-v1"]
    task_id: str = Field(min_length=1)
    task_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: Literal["author"] = "author"
    cause_report_id: int = Field(gt=0)
    cause_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_report_id: int = Field(gt=0)
    target_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prose_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    author_session_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_paths: tuple[str, ...] = Field(min_length=1)
    transcript_sha256s: tuple[str, ...] = Field(min_length=1)
    session_index_path: str = Field(min_length=1)
    session_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trajectory_path: str = Field(min_length=1)
    trajectory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_summary_path: str = Field(min_length=1)
    session_summary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_old_error: str = Field(min_length=1)
    expected_old_error_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_new_findings: tuple[str, ...] = ()

    @field_validator("transcript_sha256s")
    @classmethod
    def _valid_transcript_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            len(item) != 64 or any(character not in "0123456789abcdef" for character in item)
            for item in value
        ):
            raise ValueError("transcript digests must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _exact_request(self) -> "GeneratorReportRevalidationRequest":
        validate_task_id_segment(self.run_id)
        validate_task_id_segment(self.task_id)
        if self.cause_report_id >= self.target_report_id:
            raise ValueError("revalidation cause must precede fatal target")
        if len(self.transcript_paths) != len(self.transcript_sha256s):
            raise ValueError("transcript paths and digests differ in length")
        if len(set(self.transcript_paths)) != len(self.transcript_paths):
            raise ValueError("revalidation transcript paths contain duplicates")
        if (
            hashlib.sha256(self.expected_old_error.encode("utf-8")).hexdigest()
            != self.expected_old_error_sha256
        ):
            raise ValueError("expected old validator error digest differs")
        if self.expected_new_findings:
            raise ValueError("revalidation request must expect zero new findings")
        return self

    def deterministic_bytes(self) -> bytes:
        return (readable_json(self.model_dump(mode="json")) + "\n").encode("utf-8")

    def evidence_digest(self) -> str:
        return hashlib.sha256(self.deterministic_bytes()).hexdigest()


class GeneratorEquivalenceAuthorization(BaseModel):
    """Persistable run-scoped authority for a generator-only same-root repin."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["generator-equivalence-authorization-v1"] = (
        "generator-equivalence-authorization-v1"
    )
    run_id: str = Field(min_length=1)
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_readiness_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_pool_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_pool_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordered_task_ids: tuple[str, ...] = Field(min_length=1)
    tasks: tuple[GeneratorEquivalenceTaskBinding, ...] = Field(min_length=1)
    budget_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_snapshot: dict[str, Any]
    report_revalidation_request_path: str = ""
    report_revalidation_request_sha256: str = ""
    report_revalidation_request: GeneratorReportRevalidationRequest | None = None

    @model_validator(mode="after")
    def _exact_roster(self) -> "GeneratorEquivalenceAuthorization":
        validate_task_id_segment(self.run_id)
        if self.authoritative_selected_sha256 == self.reproduced_selected_sha256:
            raise ValueError("generator equivalence must move selected-manifest identity")
        if self.authoritative_pool_sha256 == self.reproduced_pool_sha256:
            raise ValueError("generator equivalence must move pool-manifest identity")
        if self.authoritative_generator_sha256 == self.reproduced_generator_sha256:
            raise ValueError("generator equivalence must move generator identity")
        if tuple(task.task_id for task in self.tasks) != self.ordered_task_ids:
            raise ValueError("generator equivalence task bindings differ from roster")
        if len(set(self.ordered_task_ids)) != len(self.ordered_task_ids):
            raise ValueError("generator equivalence roster contains duplicate tasks")
        if sha256_hex(canonical_json(self.budget_snapshot)) != self.budget_snapshot_sha256:
            raise ValueError("generator equivalence budget snapshot digest differs")
        request = self.report_revalidation_request
        if request is None:
            if self.report_revalidation_request_path or self.report_revalidation_request_sha256:
                raise ValueError("revalidation request binding is incomplete")
        else:
            if (
                not self.report_revalidation_request_path
                or self.report_revalidation_request_sha256 != request.evidence_digest()
                or request.run_id != self.run_id
                or request.config_sha256 != self.config_sha256
                or request.authoritative_selected_sha256
                != self.authoritative_selected_sha256
                or request.reproduced_selected_sha256 != self.reproduced_selected_sha256
                or request.authoritative_generator_sha256
                != self.authoritative_generator_sha256
                or request.reproduced_generator_sha256
                != self.reproduced_generator_sha256
                or request.task_id not in self.ordered_task_ids
            ):
                raise ValueError("revalidation request differs from authorization")
        return self

    def deterministic_bytes(self) -> bytes:
        return (readable_json(self.model_dump(mode="json")) + "\n").encode("utf-8")

    def evidence_digest(self) -> str:
        return hashlib.sha256(self.deterministic_bytes()).hexdigest()

    def binding_for(self, task_id: str) -> GeneratorEquivalenceTaskBinding:
        for binding in self.tasks:
            if binding.task_id == task_id:
                return binding
        raise FiveSourceIngestError(
            f"generator equivalence has no binding for {task_id!r}"
        )


class ImplementationEquivalenceTaskBinding(BaseModel):
    """Exact old and new evidence for one implementation-rederived task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    origin: Origin
    authoritative_source_entry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_source_entry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    intake_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_transition: ImplementationAdapterTransition | None = None

    @field_validator("task_id")
    @classmethod
    def _safe_task_id(cls, value: str) -> str:
        return validate_task_id_segment(value)

    @model_validator(mode="after")
    def _exact_adapter_transition(self) -> "ImplementationEquivalenceTaskBinding":
        transition = self.adapter_transition
        source_entry_moved = (
            self.authoritative_source_entry_sha256
            != self.reproduced_source_entry_sha256
        )
        if transition is None:
            if source_entry_moved:
                raise ValueError(
                    "source-entry identity moved without an adapter transition"
                )
            return self
        if (
            transition.pool != self.origin
            or transition.authoritative_source_entry_sha256
            != self.authoritative_source_entry_sha256
            or transition.reproduced_source_entry_sha256
            != self.reproduced_source_entry_sha256
        ):
            raise ValueError(
                "adapter transition differs from its task origin/source entries"
            )
        if not source_entry_moved:
            raise ValueError("adapter transition did not move source-entry identity")
        return self


class ImplementationEquivalenceAuthorization(BaseModel):
    """Run-scoped authority for an off-workspace, same-root implementation replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["implementation-equivalence-authorization-v2"] = (
        "implementation-equivalence-authorization-v2"
    )
    run_id: str = Field(min_length=1)
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_readiness_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_selected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_pool_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_pool_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_generator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordered_task_ids: tuple[str, ...] = Field(min_length=1)
    tasks: tuple[ImplementationEquivalenceTaskBinding, ...] = Field(min_length=1)
    budget_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_snapshot: dict[str, Any]
    report_revalidation_request_path: str = ""
    report_revalidation_request_sha256: str = ""
    report_revalidation_request: GeneratorReportRevalidationRequest | None = None

    @model_validator(mode="after")
    def _exact_roster(self) -> "ImplementationEquivalenceAuthorization":
        validate_task_id_segment(self.run_id)
        if self.authoritative_selected_sha256 == self.reproduced_selected_sha256:
            raise ValueError(
                "implementation equivalence must move selected-manifest identity"
            )
        if self.authoritative_pool_sha256 == self.reproduced_pool_sha256:
            raise ValueError(
                "implementation equivalence must move pool-manifest identity"
            )
        if self.authoritative_generator_sha256 == self.reproduced_generator_sha256:
            raise ValueError("implementation equivalence must move generator identity")
        if tuple(task.task_id for task in self.tasks) != self.ordered_task_ids:
            raise ValueError(
                "implementation equivalence task bindings differ from roster"
            )
        if len(set(self.ordered_task_ids)) != len(self.ordered_task_ids):
            raise ValueError("implementation equivalence roster contains duplicate tasks")
        if not any(task.adapter_transition is not None for task in self.tasks):
            raise ValueError(
                "implementation v2 authorization must bind an adapter transition"
            )
        if sha256_hex(canonical_json(self.budget_snapshot)) != self.budget_snapshot_sha256:
            raise ValueError("implementation equivalence budget snapshot digest differs")
        if (
            self.report_revalidation_request is not None
            or self.report_revalidation_request_path
            or self.report_revalidation_request_sha256
        ):
            raise ValueError(
                "implementation v2 does not support report revalidation requests"
            )
        return self

    def deterministic_bytes(self) -> bytes:
        return (readable_json(self.model_dump(mode="json")) + "\n").encode("utf-8")

    def evidence_digest(self) -> str:
        return hashlib.sha256(self.deterministic_bytes()).hexdigest()

    def binding_for(self, task_id: str) -> ImplementationEquivalenceTaskBinding:
        for binding in self.tasks:
            if binding.task_id == task_id:
                return binding
        raise FiveSourceIngestError(
            f"implementation equivalence has no binding for {task_id!r}"
        )


EquivalenceAuthorization = (
    GeneratorEquivalenceAuthorization | ImplementationEquivalenceAuthorization
)


def load_generator_equivalence_authorization(
    path: Path, *, workspace: Path
) -> EquivalenceAuthorization:
    """Load a strictly discriminated v1 generator or v2 implementation authorization."""

    target = Path(workspace).absolute()
    source = Path(path).absolute()
    root = target / "state" / "pipeline_runs"
    try:
        relative = source.relative_to(root)
    except ValueError:
        raise FiveSourceIngestError(
            "generator equivalence authorization is outside the run-local store"
        ) from None
    if len(relative.parts) != 4 or relative.parts[1:3] != (
        "identity_migrations",
        "authorizations",
    ):
        raise FiveSourceIngestError(
            "generator equivalence authorization path is not canonical"
        )
    current = target
    try:
        for part in source.relative_to(target).parts[:-1]:
            current = current / part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise FiveSourceIngestError(
                    "generator equivalence authorization has an unsafe ancestor"
                )
        before = source.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size > 16 * 1024 * 1024
        ):
            raise FiveSourceIngestError(
                "generator equivalence authorization is not a bounded regular file"
            )
        descriptor = os.open(
            source,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except FiveSourceIngestError:
        raise
    except OSError as exc:
        raise FiveSourceIngestError(
            f"cannot read generator equivalence authorization: {exc}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise FiveSourceIngestError(
                "generator equivalence authorization changed while opening"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(16 * 1024 * 1024 + 1)
        after = source.lstat()
        if (
            len(payload) != opened.st_size
            or len(payload) > 16 * 1024 * 1024
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise FiveSourceIngestError(
                "generator equivalence authorization changed while reading"
            )
    finally:
        os.close(descriptor)
    try:
        document = json.loads(payload)
        if not isinstance(document, dict):
            raise ValueError("authorization must be a JSON object")
        schema_version = document.get("schema_version")
        authorization_type: (
            type[GeneratorEquivalenceAuthorization]
            | type[ImplementationEquivalenceAuthorization]
        )
        if schema_version == "generator-equivalence-authorization-v1":
            authorization_type = GeneratorEquivalenceAuthorization
        elif schema_version == "implementation-equivalence-authorization-v2":
            authorization_type = ImplementationEquivalenceAuthorization
        else:
            raise ValueError(
                "unsupported equivalence authorization schema_version "
                f"{schema_version!r}"
            )
        authorization = authorization_type.model_validate(document)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise FiveSourceIngestError(
            f"generator equivalence authorization is invalid: {exc}"
        ) from exc
    if relative.parts[0] != authorization.run_id:
        raise FiveSourceIngestError(
            "generator equivalence authorization is stored under another run id"
        )
    expected_name = f"{authorization.evidence_digest()}.json"
    if source.name != expected_name or payload != authorization.deterministic_bytes():
        raise FiveSourceIngestError(
            "generator equivalence authorization is not content-addressed"
        )
    return authorization


def load_implementation_equivalence_authorization(
    path: Path, *, workspace: Path
) -> EquivalenceAuthorization:
    """Generic spelling for the v1/v2 compatibility loader."""

    return load_generator_equivalence_authorization(path, workspace=workspace)


@dataclass(frozen=True)
class CandidateIngestOutcome:
    """One selected descriptor's intake result, including non-created tasks."""

    origin: Origin
    task_id: str
    state: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "origin": self.origin.value,
            "task_id": self.task_id,
            "state": self.state,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class FiveSourceIngestResult:
    manifest_sha256: str
    tasks: tuple[TaskIR, ...]
    receipt: Path | None
    dry_run: bool
    candidate_outcomes: tuple[CandidateIngestOutcome, ...] = ()

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(task.task_id for task in self.tasks)


def load_five_source_manifest(path: Path) -> FiveSourceManifest:
    """Parse a v1, v2, or selected-roster v3 manifest; reject other versions."""

    source = Path(path)
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise FiveSourceIngestError(f"ingest manifest is unavailable: {source}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise FiveSourceIngestError(
            f"ingest manifest must be a regular non-symlink file: {source}"
        )
    try:
        raw = yaml.load(source.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise FiveSourceIngestError(f"invalid ingest manifest {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise FiveSourceIngestError(f"{source}: ingest manifest must be a mapping")
    schema_version = raw.get("schema_version")
    manifest_type: (
        type[FiveSourceIngestManifest]
        | type[FiveSourceBatchIngestManifest]
        | type[SelectedSourceIngestManifest]
    )
    if schema_version == FIVE_SOURCE_MANIFEST_SCHEMA_VERSION:
        manifest_type = FiveSourceIngestManifest
    elif schema_version == FIVE_SOURCE_BATCH_MANIFEST_SCHEMA_VERSION:
        manifest_type = FiveSourceBatchIngestManifest
    elif schema_version == SELECTED_SOURCE_MANIFEST_SCHEMA_VERSION:
        manifest_type = SelectedSourceIngestManifest
    else:
        raise FiveSourceIngestError(
            f"invalid ingest manifest {source}: unsupported schema_version "
            f"{schema_version!r}; expected {FIVE_SOURCE_MANIFEST_SCHEMA_VERSION!r} "
            f", {FIVE_SOURCE_BATCH_MANIFEST_SCHEMA_VERSION!r}, or "
            f"{SELECTED_SOURCE_MANIFEST_SCHEMA_VERSION!r}"
        )
    try:
        return manifest_type.model_validate(raw)
    except ValueError as exc:
        raise FiveSourceIngestError(f"invalid ingest manifest {source}: {exc}") from exc


def load_five_source_batch_manifest(path: Path) -> FiveSourceBatchIngestManifest:
    """Load a v2 batch while refusing a valid-but-singleton v1 manifest."""

    manifest = load_five_source_manifest(path)
    if not isinstance(manifest, FiveSourceBatchIngestManifest):
        raise FiveSourceIngestError(
            f"batch ingestion requires {FIVE_SOURCE_BATCH_MANIFEST_SCHEMA_VERSION!r}, "
            f"got {manifest.schema_version!r}"
        )
    return manifest


def _selection_rank(seed: int, origin: Origin, task_id: str) -> str:
    """Stable pseudo-random rank without depending on Python's hash seed."""

    return hashlib.sha256(
        f"selected-source-v1\0{seed}\0{origin.value}\0{task_id}".encode("utf-8")
    ).hexdigest()


def select_candidate_manifest(
    pool: FiveSourceBatchIngestManifest,
    *,
    candidate_count: int,
    source_families: tuple[Origin | str, ...] | None = None,
    source_allocation: dict[Origin | str, int] | None = None,
    seed: int = 0,
) -> SelectedSourceIngestManifest:
    """Select an exact seed-stable roster from a pinned v2 pool.

    Automatic allocation balances available origins. Explicit allocations must
    sum to the requested count and fit every origin's capacity.
    """

    if (
        isinstance(candidate_count, bool)
        or not isinstance(candidate_count, int)
        or candidate_count < 1
    ):
        raise FiveSourceIngestError("candidate_count must be a positive integer")
    try:
        normalized_seed = int(seed)
    except (TypeError, ValueError) as exc:
        raise FiveSourceIngestError("selection seed must be an integer") from exc

    if source_families is None:
        families = FIVE_ORIGINS
    else:
        converted: list[Origin] = []
        for raw in source_families:
            try:
                origin = raw if isinstance(raw, Origin) else Origin(str(raw))
            except ValueError as exc:
                raise FiveSourceIngestError(
                    f"unknown source family {raw!r}; expected one of "
                    f"{[origin.value for origin in FIVE_ORIGINS]}"
                ) from exc
            if origin not in FIVE_ORIGINS:
                raise FiveSourceIngestError(
                    f"source family {origin.value!r} is not a candidate source"
                )
            if origin in converted:
                raise FiveSourceIngestError(
                    f"source family {origin.value!r} is repeated"
                )
            converted.append(origin)
        if not converted:
            raise FiveSourceIngestError("source_families must name at least one source")
        families = tuple(converted)

    capacities = {
        origin: len(getattr(pool.sources, origin.value)) for origin in FIVE_ORIGINS
    }
    available = sum(capacities[origin] for origin in families)
    if candidate_count > available:
        raise FiveSourceIngestError(
            f"requested {candidate_count} candidate(s), but selected sources have "
            f"capacity {available}: "
            + ", ".join(
                f"{origin.value}={capacities[origin]}" for origin in families
            )
        )

    allocation = {origin: 0 for origin in FIVE_ORIGINS}
    if source_allocation is not None:
        seen: set[Origin] = set()
        for raw_origin, raw_count in source_allocation.items():
            try:
                origin = (
                    raw_origin
                    if isinstance(raw_origin, Origin)
                    else Origin(str(raw_origin))
                )
            except ValueError as exc:
                raise FiveSourceIngestError(
                    f"unknown source-allocation family {raw_origin!r}"
                ) from exc
            if origin not in families:
                raise FiveSourceIngestError(
                    f"source allocation names {origin.value!r}, which is not in "
                    "source_families"
                )
            if origin in seen:
                raise FiveSourceIngestError(
                    f"source allocation repeats {origin.value!r}"
                )
            seen.add(origin)
            if isinstance(raw_count, bool) or not isinstance(raw_count, int):
                raise FiveSourceIngestError(
                    f"source allocation for {origin.value} must be an integer"
                )
            count = raw_count
            if count < 0 or count > capacities[origin]:
                raise FiveSourceIngestError(
                    f"source allocation {origin.value}={count} is outside "
                    f"0..{capacities[origin]}"
                )
            allocation[origin] = count
        if sum(allocation.values()) != candidate_count:
            raise FiveSourceIngestError(
                "source allocation must sum exactly to candidate_count "
                f"({sum(allocation.values())} != {candidate_count})"
            )
    else:
        # Balanced water-filling with a seed-stable tie order.  Exhausted
        # families leave the rotation and remaining capacity is redistributed.
        tie_order = tuple(
            sorted(
                families,
                key=lambda origin: _selection_rank(
                    normalized_seed, origin, "__family__"
                ),
            )
        )
        for _ in range(candidate_count):
            eligible = [
                origin
                for origin in tie_order
                if allocation[origin] < capacities[origin]
            ]
            if not eligible:  # guarded by the capacity check above
                raise FiveSourceIngestError("candidate allocation exhausted unexpectedly")
            minimum = min(allocation[origin] for origin in eligible)
            origin = next(
                origin for origin in eligible if allocation[origin] == minimum
            )
            allocation[origin] += 1

    selected: dict[Origin, tuple[_SourceEntry, ...]] = {}
    for origin in FIVE_ORIGINS:
        entries = tuple(getattr(pool.sources, origin.value))
        ranked = sorted(
            entries,
            key=lambda entry: (
                _selection_rank(
                    normalized_seed, origin, entry.expected_task_id
                ),
                entry.expected_task_id,
            ),
        )[: allocation[origin]]
        selected[origin] = tuple(
            sorted(ranked, key=lambda entry: entry.expected_task_id)
        )

    return SelectedSourceIngestManifest(
        expected_task_count=candidate_count,
        parent_manifest_sha256=pool.manifest_sha256(),
        selection_seed=normalized_seed,
        source_allocation=allocation,
        generator=pool.generator,
        catalog=pool.catalog,
        sources=SelectedSourceEntries(
            dbt=selected[Origin.DBT],
            dlt=selected[Origin.DLT],
            synsql=selected[Origin.SYNSQL],
            schemapile=selected[Origin.SCHEMAPILE],
            wikidbs=selected[Origin.WIKIDBS],
        ),
    )


def _safe_uri_relative(raw: str, *, label: str) -> PurePosixPath:
    relative = PurePosixPath(raw)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise FiveSourceIngestError(f"{label} contains an unsafe relative path")
    return relative


def _resolve_path(
    raw: str,
    *,
    manifest_path: Path,
    catalog: SourceCatalog | None,
    expected_pool: str | None,
) -> Path:
    """Resolve absolute, manifest-relative, package://, or pool:// paths."""

    if raw.startswith("package://"):
        relative = _safe_uri_relative(raw.removeprefix("package://"), label=raw)
        return resource_path(Path(*relative.parts))

    if raw.startswith("pool://"):
        if catalog is None:
            raise FiveSourceIngestError(
                "the catalog artifact cannot itself use a pool:// path"
            )
        remainder = raw.removeprefix("pool://")
        pool, separator, child = remainder.partition("/")
        if not separator:
            raise FiveSourceIngestError(f"pool URI has no relative path: {raw!r}")
        if expected_pool is not None and pool != expected_pool:
            raise FiveSourceIngestError(
                f"source {expected_pool!r} cannot resolve an artifact through "
                f"pool {pool!r}"
            )
        relative = _safe_uri_relative(child, label=raw)
        try:
            root = catalog.pool(pool).root_path()
        except (KeyError, FileNotFoundError, ValueError) as exc:
            raise FiveSourceIngestError(f"cannot resolve {raw!r}: {exc}") from exc
        candidate = root.joinpath(*relative.parts)
        try:
            candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        except ValueError:
            raise FiveSourceIngestError(f"pool URI escapes its source root: {raw!r}") from None
        return candidate

    if "://" in raw:
        raise FiveSourceIngestError(f"unsupported artifact path scheme: {raw!r}")
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    relative = _safe_uri_relative(raw, label=raw)
    return manifest_path.parent.joinpath(*relative.parts)


def _artifact_digest(path: Path, pin: ArtifactPin) -> str:
    if pin.digest_kind == "sha256-file":
        try:
            return sha256_file(path)
        except (OSError, ValueError) as exc:
            raise FiveSourceIngestError(f"cannot hash file artifact {path}: {exc}") from exc
    if pin.digest_kind == "sha256-tree-v1":
        try:
            return sha256_tree(path)
        except (OSError, ValueError) as exc:
            raise FiveSourceIngestError(f"cannot hash tree artifact {path}: {exc}") from exc
    # Kept defensive even though ArtifactPin is a closed Literal.
    raise FiveSourceIngestError(f"unsupported artifact digest kind {pin.digest_kind!r}")


def _verify_artifact(path: Path, pin: ArtifactPin, *, label: str) -> None:
    observed = _artifact_digest(path, pin)
    if observed != pin.sha256:
        raise FiveSourceIngestError(
            f"{label} digest mismatch: expected {pin.sha256}, observed {observed} "
            f"at {path}"
        )


def _verify_artifact_once(
    path: Path,
    pin: ArtifactPin,
    *,
    label: str,
    verified: dict[Path, ArtifactPin],
) -> None:
    """Hash an artifact once per verification phase using a phase-local cache."""

    key = path.absolute()
    previous = verified.get(key)
    if previous is not None:
        if previous != pin:
            raise FiveSourceIngestError(
                f"artifact {path} is pinned inconsistently within the manifest"
            )
        return
    _verify_artifact(path, pin, label=label)
    verified[key] = pin


def _copy_regular_snapshot(source: str | Path, destination: str | Path) -> str:
    """Copy through an inode-bound descriptor and refuse links/special files."""

    source_path = Path(source)
    destination_path = Path(destination)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        source_descriptor = os.open(source_path, flags)
    except OSError as exc:
        raise FiveSourceIngestError(
            f"cannot open regular snapshot source {source_path}: {exc}"
        ) from exc
    try:
        metadata = os.fstat(source_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FiveSourceIngestError(
                f"snapshot source must be a regular non-symlink file: {source_path}"
            )
        destination_descriptor = os.open(
            destination_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            while True:
                chunk = os.read(source_descriptor, 1 << 20)
                if not chunk:
                    break
                remaining = memoryview(chunk)
                while remaining:
                    written = os.write(destination_descriptor, remaining)
                    remaining = remaining[written:]
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)
    return str(destination_path)


def _snapshot_artifact_once(
    path: Path,
    pin: ArtifactPin,
    *,
    label: str,
    snapshot_root: Path,
    snapshots: dict[Path, tuple[ArtifactPin, Path]],
) -> Path:
    """Copy a verified input into a private snapshot and recheck its digest.

    Preserve links during copying so validation can reject links and special
    files before adapters read the snapshot.
    """

    key = path.absolute()
    previous = snapshots.get(key)
    if previous is not None:
        previous_pin, snapshot = previous
        if previous_pin != pin:
            raise FiveSourceIngestError(
                f"artifact {path} is pinned inconsistently within the manifest"
            )
        return snapshot

    ordinal = len(snapshots)
    parent = snapshot_root / f"{ordinal:06d}"
    snapshot = parent / path.name
    try:
        parent.mkdir(mode=0o700)
        if pin.digest_kind == "sha256-file":
            _copy_regular_snapshot(path, snapshot)
        elif pin.digest_kind == "sha256-tree-v1":
            shutil.copytree(
                path,
                snapshot,
                symlinks=True,
                copy_function=_copy_regular_snapshot,
            )
        else:  # defensive: ArtifactPin is a closed Literal
            raise FiveSourceIngestError(
                f"unsupported artifact digest kind {pin.digest_kind!r}"
            )
    except FiveSourceIngestError:
        raise
    except OSError as exc:
        raise FiveSourceIngestError(
            f"cannot snapshot {label} artifact {path}: {exc}"
        ) from exc

    _verify_artifact(snapshot, pin, label=f"{label} snapshot")
    snapshots[key] = (pin, snapshot)
    return snapshot


def _entry_artifacts(entry: _SourceEntry) -> tuple[tuple[str, ArtifactPin], ...]:
    if isinstance(entry, DbtIngest):
        return (("manifest", entry.manifest),)
    if isinstance(entry, DltIngest):
        return (("manifest", entry.manifest),)
    if isinstance(entry, SynSQLIngest):
        return (("tables", entry.tables),)
    if isinstance(entry, SchemaPileIngest):
        return (("source", entry.source), ("index", entry.index))
    if isinstance(entry, WikiDBsIngest):
        return (("database", entry.database), ("family_map", entry.family_map))
    raise FiveSourceIngestError(f"unsupported source entry {type(entry).__name__}")


def _primary_artifact(entry: _SourceEntry) -> ArtifactPin:
    if isinstance(entry, (DbtIngest, DltIngest)):
        return entry.manifest
    if isinstance(entry, SynSQLIngest):
        return entry.tables
    if isinstance(entry, SchemaPileIngest):
        return entry.source
    if isinstance(entry, WikiDBsIngest):
        return entry.database
    raise FiveSourceIngestError(f"unsupported source entry {type(entry).__name__}")


def _origin_for_entry(entry: _SourceEntry) -> Origin:
    return Origin(entry.pool)


def _adapter_module(entry: _SourceEntry):
    if isinstance(entry, DbtIngest):
        from elt_taskgen.adapters import dbt

        return dbt
    if isinstance(entry, DltIngest):
        from elt_taskgen.adapters import dlt

        return dlt
    if isinstance(entry, SynSQLIngest):
        from elt_taskgen.adapters import synsql

        return synsql
    if isinstance(entry, SchemaPileIngest):
        from elt_taskgen.adapters import schemapile

        return schemapile
    if isinstance(entry, WikiDBsIngest):
        from elt_taskgen.adapters import wikidbs

        return wikidbs
    raise FiveSourceIngestError(f"unsupported source entry {type(entry).__name__}")


def _verify_adapter(entry: _SourceEntry) -> str:
    module = _adapter_module(entry)
    adapter_name = str(module.__name__)
    module_path_raw = getattr(module, "__file__", None)
    if not module_path_raw:
        raise FiveSourceIngestError(f"adapter {adapter_name} has no inspectable source file")
    try:
        observed = sha256_file(Path(module_path_raw))
    except (OSError, ValueError) as exc:
        raise FiveSourceIngestError(
            f"cannot hash adapter {adapter_name} at {module_path_raw}: {exc}"
        ) from exc
    if entry.adapter_version != __version__:
        raise FiveSourceIngestError(
            f"{entry.pool} adapter version mismatch: manifest pins "
            f"{entry.adapter_version!r}, installed package is {__version__!r}"
        )
    if observed != entry.adapter_digest:
        raise FiveSourceIngestError(
            f"{entry.pool} adapter digest mismatch: expected {entry.adapter_digest}, "
            f"observed {observed} at {module_path_raw}"
        )
    return adapter_name


def _verify_adapter_once(
    entry: _SourceEntry,
    verified: dict[str, tuple[str, str, str]],
) -> str:
    """Hash one installed adapter once within a verification phase."""

    claimed = (entry.adapter_version, entry.adapter_digest)
    previous = verified.get(entry.pool)
    if previous is not None:
        previous_version, previous_digest, adapter_name = previous
        if claimed != (previous_version, previous_digest):
            raise FiveSourceIngestError(
                f"{entry.pool} entries pin inconsistent adapter identities"
            )
        return adapter_name
    adapter_name = _verify_adapter(entry)
    verified[entry.pool] = (
        entry.adapter_version,
        entry.adapter_digest,
        adapter_name,
    )
    return adapter_name


def _current_adapter_identity(entry: _SourceEntry) -> tuple[str, str, str]:
    """Return the current module, version, and digest for an installed adapter."""

    module = _adapter_module(entry)
    name = str(module.__name__)
    module_path_raw = getattr(module, "__file__", None)
    if not module_path_raw:
        raise FiveSourceIngestError(f"adapter {name} has no inspectable source file")
    try:
        digest = sha256_file(Path(module_path_raw))
    except (OSError, ValueError) as exc:
        raise FiveSourceIngestError(
            f"cannot hash adapter {name} at {module_path_raw}: {exc}"
        ) from exc
    return name, __version__, digest


def repin_current_implementation(
    manifest: GeneratorPinnedManifest,
    *,
    parent_manifest_sha256: str | None = None,
) -> GeneratorPinnedManifest:
    """Repin only generator, adapter, and supplied parent-pool identities.

    Direct v3 repinning requires the new parent digest because a selected subset
    cannot reconstruct its full pool.
    """

    if not isinstance(
        manifest, (FiveSourceBatchIngestManifest, SelectedSourceIngestManifest)
    ):
        raise FiveSourceIngestError(
            "implementation repinning requires a generator-pinned v2/v3 manifest"
        )
    if isinstance(manifest, FiveSourceBatchIngestManifest):
        if parent_manifest_sha256 is not None:
            raise FiveSourceIngestError(
                "a v2 pool has no parent_manifest_sha256 to override"
            )
    elif parent_manifest_sha256 is None:
        raise FiveSourceIngestError(
            "repinning a selected v3 manifest requires the new parent pool digest"
        )

    by_pool: dict[str, tuple[str, str, str]] = {}
    updated_sources: dict[str, tuple[_SourceEntry, ...]] = {}
    for origin in FIVE_ORIGINS:
        updated_entries: list[_SourceEntry] = []
        for entry in getattr(manifest.sources, origin.value):
            observed = by_pool.get(origin.value)
            if observed is None:
                observed = _current_adapter_identity(entry)
                by_pool[origin.value] = observed
            _adapter_name, adapter_version, adapter_digest = observed
            updated_entries.append(
                entry.model_copy(
                    update={
                        "adapter_version": adapter_version,
                        "adapter_digest": adapter_digest,
                    }
                )
            )
        updated_sources[origin.value] = tuple(updated_entries)

    updates: dict[str, Any] = {
        "generator": current_generator_pin(),
        "sources": manifest.sources.model_copy(update=updated_sources),
    }
    if isinstance(manifest, SelectedSourceIngestManifest):
        updates["parent_manifest_sha256"] = parent_manifest_sha256
    payload = manifest.model_dump(mode="json")
    payload.update(
        {
            "generator": updates["generator"].model_dump(mode="json"),
            "sources": updates["sources"].model_dump(mode="json"),
        }
    )
    if isinstance(manifest, SelectedSourceIngestManifest):
        payload["parent_manifest_sha256"] = parent_manifest_sha256
    return type(manifest).model_validate(payload)


def implementation_adapter_transition(
    authoritative: GeneratorPinnedManifest,
    reproduced: GeneratorPinnedManifest,
    *,
    task_id: str,
) -> ImplementationAdapterTransition | None:
    """Describe an adapter-only transition between otherwise identical entries."""

    validate_task_id_segment(task_id)
    old_by_id = {
        entry.expected_task_id: entry for entry in authoritative.ordered_entries()
    }
    new_by_id = {
        entry.expected_task_id: entry for entry in reproduced.ordered_entries()
    }
    if tuple(old_by_id) != tuple(new_by_id):
        raise FiveSourceIngestError(
            "implementation transition changed the ordered task roster"
        )
    try:
        old_entry = old_by_id[task_id]
        new_entry = new_by_id[task_id]
    except KeyError:
        raise FiveSourceIngestError(
            f"implementation transition has no task {task_id!r}"
        ) from None
    old_payload = old_entry.model_dump(mode="json")
    new_payload = new_entry.model_dump(mode="json")
    old_digest = str(old_payload.pop("adapter_digest"))
    new_digest = str(new_payload.pop("adapter_digest"))
    old_version = str(old_payload.pop("adapter_version"))
    new_version = str(new_payload.pop("adapter_version"))
    if old_payload != new_payload:
        raise FiveSourceIngestError(
            f"implementation transition changed non-adapter source fields for {task_id!r}"
        )
    if (old_version, old_digest) == (new_version, new_digest):
        return None
    adapter_name, installed_version, installed_digest = _current_adapter_identity(
        new_entry
    )
    if (
        new_version != installed_version
        or new_digest != installed_digest
    ):
        raise FiveSourceIngestError(
            f"reproduced adapter pin for {task_id!r} is not the installed adapter"
        )
    return ImplementationAdapterTransition(
        pool=new_entry.pool,
        adapter_name=adapter_name,
        authoritative_version=old_version,
        reproduced_version=new_version,
        authoritative_digest=old_digest,
        reproduced_digest=new_digest,
        authoritative_source_entry_sha256=(
            authoritative.source_entry_sha256(old_entry)
        ),
        reproduced_source_entry_sha256=reproduced.source_entry_sha256(new_entry),
    )


def _catalog_row(catalog: SourceCatalog, entry: _SourceEntry):
    try:
        row = catalog.pool(entry.pool)
    except KeyError as exc:
        raise FiveSourceIngestError(str(exc)) from exc
    expected = _origin_for_entry(entry)
    if row.origin is not expected:
        raise FiveSourceIngestError(
            f"catalog pool {entry.pool!r} maps to {row.origin.value!r}, "
            f"expected {expected.value!r}"
        )
    if not row.license_per_record and row.license != entry.license:
        raise FiveSourceIngestError(
            f"{entry.pool} license pin {entry.license!r} disagrees with catalog "
            f"license {row.license!r}"
        )
    return row


def _pinned_attribution(row, entry: _SourceEntry) -> str:
    """Attribution bound to the same exact upstream identity as provenance."""

    base = row.attribution or row.pool
    return (
        f"{base}: {entry.selector} "
        f"({entry.upstream_url} @ {entry.upstream_revision})"
    )


def _build_dbt_task(
    entry: DbtIngest, paths: dict[str, Path], catalog: SourceCatalog
) -> TaskIR:
    from elt_taskgen.adapters import dbt

    row = _catalog_row(catalog, entry)
    spec = dbt.load_manifest(paths["manifest"])

    # Validate both the manifest's build-project selector and resource package
    # family before relabeling CandidateSpec; either check alone permits spoofing.
    valid_selectors = {spec.package_name, f"{entry.pool}_{spec.package_name}"}
    if entry.selector not in valid_selectors:
        raise FiveSourceIngestError(
            f"dbt manifest project {spec.package_name!r} does not match pinned "
            f"selector {entry.selector!r}; expected one of "
            f"{sorted(valid_selectors)!r}"
        )
    resource_packages = sorted(
        {
            node.package_name
            for node in (*spec.sources, *spec.models)
            if node.package_name
        }
    )
    if resource_packages != [entry.family]:
        raise FiveSourceIngestError(
            f"dbt manifest resource package identities {resource_packages!r} "
            f"do not exactly match pinned family {entry.family!r}"
        )

    selection = row.selection(
        entry.selector,
        attribution=_pinned_attribution(row, entry),
        family=entry.family,
    )
    if selection.license != entry.license:
        raise FiveSourceIngestError(
            f"dbt catalog selection license {selection.license!r} does not match "
            f"manifest pin {entry.license!r}"
        )
    spec = spec.model_copy(
        update={"package_name": entry.family, "license": entry.license}
    )
    result = dbt.extract_candidates(spec, pool=entry.pool)
    tasks: list[TaskIR] = []
    for candidate in result.tasks:
        if candidate.family_id != selection.family_id:
            raise FiveSourceIngestError(
                f"dbt adapter family {candidate.family_id!r} disagrees with "
                f"manifest/catalog family {selection.family_id!r}"
            )
        tasks.append(
            candidate.model_copy(
                update={**selection.ir_identity(), "cluster_id": selection.family_id}
            )
        )
    selected = [task for task in tasks if task.task_id == entry.expected_task_id]
    if len(selected) != 1:
        available = sorted(task.task_id for task in tasks)
        skipped = sorted(item.reason for item in result.skipped)
        raise FiveSourceIngestError(
            f"dbt selector {entry.selector!r} produced {len(selected)} matches for "
            f"expected task {entry.expected_task_id!r}; available={available}, "
            f"skipped={skipped}"
        )
    return selected[0]


def _build_dlt_task(
    entry: DltIngest, paths: dict[str, Path], catalog: SourceCatalog
) -> TaskIR:
    from elt_taskgen.adapters import dlt

    row = _catalog_row(catalog, entry)
    manifest = dlt.load_connector(paths["manifest"])
    if manifest.connector != entry.connector:
        raise FiveSourceIngestError(
            f"dlt manifest connector {manifest.connector!r} != pinned "
            f"connector {entry.connector!r}"
        )
    if manifest.selector != entry.selector:
        raise FiveSourceIngestError(
            f"dlt manifest selector {manifest.selector!r} != pinned "
            f"selector {entry.selector!r}"
        )
    if bool(manifest.upstream) != bool(manifest.commit):
        raise FiveSourceIngestError(
            "dlt manifest provenance must declare upstream and commit together"
        )
    if entry.require_embedded_provenance is True and not (
        manifest.upstream and manifest.commit
    ):
        raise FiveSourceIngestError(
            "v2 dlt manifest must embed both upstream and commit provenance"
        )
    # If present, embedded source identity must match the digest-bound outer entry.
    if manifest.upstream and manifest.upstream != entry.upstream_url:
        raise FiveSourceIngestError(
            f"dlt manifest upstream {manifest.upstream!r} != pinned upstream_url "
            f"{entry.upstream_url!r}"
        )
    if manifest.commit and manifest.commit != entry.upstream_revision:
        raise FiveSourceIngestError(
            f"dlt manifest commit {manifest.commit!r} != pinned "
            f"upstream_revision {entry.upstream_revision!r}"
        )
    selection = row.selection(
        manifest.selector,
        license=manifest.license or None,
        attribution=(
            _pinned_attribution(row, entry)
            if entry.require_embedded_provenance is True
            else manifest.attribution or None
        ),
        family=manifest.connector,
    )
    if selection.license != entry.license:
        raise FiveSourceIngestError(
            f"dlt source license {selection.license!r} != pinned {entry.license!r}"
        )
    return dlt.to_task_ir(manifest, pool=entry.pool, selection=selection)


def _build_synsql_task(
    entry: SynSQLIngest, paths: dict[str, Path], catalog: SourceCatalog
) -> TaskIR:
    from elt_taskgen.adapters import synsql

    _catalog_row(catalog, entry)
    task, _trusted_schema_atoms = synsql.to_task_ir_with_schema_atoms(
        entry.selector, paths["tables"], pool=entry.pool
    )
    # Deliberately do not accept data.json in this production manifest.  It is
    # answer-bearing; the schema-only adapter is the stronger trust boundary.
    return task


def _build_schemapile_task(
    entry: SchemaPileIngest, paths: dict[str, Path], catalog: SourceCatalog
) -> TaskIR:
    from elt_taskgen.adapters import schemapile

    _catalog_row(catalog, entry)
    index = schemapile.load_index(paths["index"])
    indexed = index.record(entry.selector)
    record = schemapile.find_record(paths["source"], entry.selector)

    # Recompute policy and schema fields from the pinned record; the index is not authoritative.
    url, license_name = schemapile.assert_usable_license(record, entry.selector)
    tables = record.get("TABLES") or {}
    if not isinstance(tables, dict):
        tables = {}
    actual = {
        "url": url,
        "license": license_name,
        # ``assert_usable_license`` returns only after the source record's
        # permissive flag is truthy.
        "permissive": True,
        "repo": schemapile.origin_repo(url, key=entry.selector),
        "shape": schemapile.shape_fingerprint(tables),
        "metrics": schemapile.record_metrics(record),
    }
    expected = {
        "url": indexed.url,
        "license": indexed.license,
        "permissive": indexed.permissive,
        "repo": indexed.repo,
        "shape": indexed.shape,
        "metrics": indexed.metrics,
    }
    stale = [
        name for name in expected if expected[name] != actual[name]
    ]
    if stale:
        raise FiveSourceIngestError(
            f"SchemaPile index is stale for {entry.selector!r}; source/index "
            f"mismatch in {stale}"
        )
    if not indexed.cluster:
        raise FiveSourceIngestError(
            f"SchemaPile index record {entry.selector!r} has no cluster"
        )
    cluster = index.cluster(indexed.cluster)
    if entry.selector not in cluster.members or indexed.repo not in cluster.repos:
        raise FiveSourceIngestError(
            f"SchemaPile index cluster {indexed.cluster!r} does not contain "
            f"record/repository {entry.selector!r}/{indexed.repo!r}"
        )

    # Do not call task_from_index: it would reopen the source snapshot and
    # repeat selection after the source/index binding above.
    return schemapile.to_task_ir(
        record,
        key=entry.selector,
        cluster=indexed.cluster,
        pool=entry.pool,
        filt=index.filter,
        catalog=catalog,
    )


def _build_wikidbs_task(
    entry: WikiDBsIngest, paths: dict[str, Path], catalog: SourceCatalog
) -> TaskIR:
    from elt_taskgen.adapters import wikidbs

    _catalog_row(catalog, entry)
    database = paths["database"]
    if database.name != entry.selector:
        raise FiveSourceIngestError(
            f"WikiDBs database directory {database.name!r} != pinned selector "
            f"{entry.selector!r}"
        )
    # The coordinator already pins the inventory. Pass its private snapshots,
    # not the mutable pool root, to avoid re-listing verified data.
    return wikidbs.to_task_ir(
        database,
        pool=entry.pool,
        family_map_path=paths["family_map"],
        wikidbs_root=None,
        catalog=catalog,
    )


def _verify_wikidbs_inventory(
    entry: WikiDBsIngest,
    *,
    database: Path,
    root: Path,
    verified: dict[Path, str] | None = None,
) -> None:
    """Bind the selected database to the complete five-part node topology."""

    try:
        database.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        raise FiveSourceIngestError(
            f"WikiDBs database {database} is outside catalog root {root}"
        ) from None
    key = root.absolute()
    observed_inventory = (
        verified.get(key) if verified is not None else None
    )
    if observed_inventory is None:
        observed_inventory = wikidbs_node_inventory_sha256(root)
        if verified is not None:
            verified[key] = observed_inventory
    if observed_inventory != entry.node_inventory_sha256:
        raise FiveSourceIngestError(
            "WikiDBs node inventory digest mismatch: expected "
            f"{entry.node_inventory_sha256}, observed {observed_inventory}"
        )


def wikidbs_node_inventory_sha256(root: Path) -> str:
    """Digest the exact directory-name inventory used by node verification."""

    from elt_taskgen.adapters import wikidbs

    source = Path(root)
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise FiveSourceIngestError(
            f"WikiDBs root is unavailable for node inventory: {source}: {exc}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise FiveSourceIngestError(
            f"WikiDBs root must be a non-symlink directory: {source}"
        )
    parts: list[dict[str, object]] = []
    for part_index in range(wikidbs.PART_COUNT):
        part_name = f"part-{part_index}"
        part = source / part_name
        try:
            part_metadata = part.lstat()
        except OSError as exc:
            raise FiveSourceIngestError(
                f"WikiDBs node inventory cannot read {part}: {exc}"
            ) from exc
        if stat.S_ISLNK(part_metadata.st_mode) or not stat.S_ISDIR(part_metadata.st_mode):
            raise FiveSourceIngestError(
                f"WikiDBs part must be a non-symlink directory: {part}"
            )
        names: list[str] = []
        for child in sorted(part.iterdir(), key=lambda item: item.name):
            child_metadata = child.lstat()
            if stat.S_ISLNK(child_metadata.st_mode):
                raise FiveSourceIngestError(
                    f"WikiDBs part contains a symbolic link: {child}"
                )
            if stat.S_ISDIR(child_metadata.st_mode):
                names.append(child.name)
        parts.append({"part": part_name, "directories": names})
    return sha256_hex(
        canonical_json(
            {"schema_version": "wikidbs-node-inventory-v1", "parts": parts}
        )
    )


_TaskBuilder = Callable[[_SourceEntry, dict[str, Path], SourceCatalog], TaskIR]


def _builder(entry: _SourceEntry) -> _TaskBuilder:
    if isinstance(entry, DbtIngest):
        return _build_dbt_task
    if isinstance(entry, DltIngest):
        return _build_dlt_task
    if isinstance(entry, SynSQLIngest):
        return _build_synsql_task
    if isinstance(entry, SchemaPileIngest):
        return _build_schemapile_task
    if isinstance(entry, WikiDBsIngest):
        return _build_wikidbs_task
    raise FiveSourceIngestError(f"unsupported source entry {type(entry).__name__}")


def _validate_task(entry: _SourceEntry, task: TaskIR) -> None:
    # Enforce population readiness at intake before provider work can begin.
    from elt_taskgen.generation.populations import validate_population_coverage

    origin = _origin_for_entry(entry)
    problems: list[str] = []
    if task.task_id != entry.expected_task_id:
        problems.append(
            f"task id {task.task_id!r} != expected {entry.expected_task_id!r}"
        )
    if task.origin is not origin:
        problems.append(f"origin {task.origin.value!r} != expected {origin.value!r}")
    if task.license != entry.license:
        problems.append(f"license {task.license!r} != pinned {entry.license!r}")
    if not task.family_id.startswith(f"{entry.pool}__"):
        problems.append(
            f"family {task.family_id!r} is outside pool {entry.pool!r}"
        )
    if task.status is not TaskStatus.DRAFT:
        problems.append(f"new adapter task status is {task.status.value!r}, not draft")
    if task.revisions:
        problems.append("new adapter task already carries revision history")
    problems.extend(
        f"population coverage: {problem}"
        for problem in validate_population_coverage(task)
    )
    if problems:
        raise FiveSourceIngestError(
            f"{entry.pool} adapter output failed manifest binding: " + "; ".join(problems)
        )


def _identity(
    entry: _SourceEntry,
    task: TaskIR,
    *,
    adapter_name: str,
    manifest: FiveSourceManifest,
) -> SourceIdentity:
    source = _primary_artifact(entry)
    selection_inputs: dict[str, ProvenanceArtifact] = {
        "ingest_manifest": ProvenanceArtifact(
            locator=(
                f"manifest-entry:{manifest.schema_version}:{entry.pool}"
                + (
                    f":{entry.expected_task_id}"
                    if _generator_pinned(manifest)
                    else ""
                )
            ),
            digest=manifest.source_entry_sha256(entry),
            digest_kind="sha256-canonical-json-v1",
        ),
        "source_catalog": ProvenanceArtifact(
            locator="manifest:catalog",
            digest=manifest.catalog.sha256,
            digest_kind=manifest.catalog.digest_kind,
        ),
    }
    if _generator_pinned(manifest):
        selection_inputs["generator_code"] = ProvenanceArtifact(
            locator=(
                f"generator:{manifest.generator.name}@{manifest.generator.version}"
            ),
            digest=manifest.generator.sha256,
        # This artifact kind stores the canonical generator-tree inventory.
            digest_kind="sha256-canonical-json-v1",
        )
        selection_inputs["dependency_lock"] = ProvenanceArtifact(
            locator="package-resource:uv.lock",
            digest=manifest.generator.lock_sha256,
            digest_kind="sha256-file",
        )
    if isinstance(entry, SchemaPileIngest):
        selection_inputs["schemapile_index"] = ProvenanceArtifact(
            locator="manifest:sources.schemapile.index",
            digest=entry.index.sha256,
            digest_kind=entry.index.digest_kind,
        )
    if isinstance(entry, WikiDBsIngest):
        selection_inputs["wikidbs_family_map"] = ProvenanceArtifact(
            locator="manifest:sources.wikidbs.family_map",
            digest=entry.family_map.sha256,
            digest_kind=entry.family_map.digest_kind,
        )
        selection_inputs["wikidbs_node_inventory"] = ProvenanceArtifact(
            locator="catalog:wikidbs-node-inventory-v1",
            digest=entry.node_inventory_sha256,
            digest_kind="sha256-canonical-json-v1",
        )
    return SourceIdentity(
        pool=entry.pool,
        origin=_origin_for_entry(entry),
        selector=entry.selector,
        upstream_url=entry.upstream_url,
        upstream_revision=entry.upstream_revision,
        source_digest=source.sha256,
        source_digest_kind=source.digest_kind,
        adapter_name=adapter_name,
        adapter_version=entry.adapter_version,
        adapter_digest=entry.adapter_digest,
        license=task.license,
        license_evidence=entry.license_evidence,
        selection_inputs=selection_inputs,
    )


def _seed_embedded_deny_lists(index) -> None:
    """Match the existing single-source CLI's deterministic bootstrap."""

    from elt_taskgen.verification import contamination as cont

    for name, families in (
        ("eltbench", cont.ELTBENCH_FAMILIES),
        ("spider2_dbt", cont.SPIDER2_DBT_FAMILIES),
        ("ade_bench", cont.ADE_BENCH_FAMILIES),
    ):
        index.add_benchmark(
            name, sorted({f"family:{cont.normalize_name(family)}" for family in families})
        )


def _copy_contamination_snapshot(workspace: Path, destination: Path) -> bool:
    """Copy only regular stores into a private preflight index."""

    source = workspace / "state" / "contamination"
    try:
        metadata = source.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise FiveSourceIngestError(
            f"cannot inspect workspace contamination index {source}: {exc}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise FiveSourceIngestError(
            f"workspace contamination index must be a non-symlink directory: {source}"
        )
    copied = False
    for path in sorted(source.glob("*.json"), key=lambda item: item.name):
        file_metadata = path.lstat()
        if stat.S_ISLNK(file_metadata.st_mode) or not stat.S_ISREG(file_metadata.st_mode):
            raise FiveSourceIngestError(
                f"contamination store must be a regular non-symlink file: {path}"
            )
        shutil.copyfile(path, destination / path.name)
        copied = True
    return copied


def _scan_preflight(
    workspace: Path,
    prepared: tuple[PreparedSource, ...],
    *,
    retain_task_local_fatals: bool = False,
) -> tuple[PreparedSource, ...]:
    """Run all five checks against a private, stable contamination snapshot."""

    from elt_taskgen.verification import contamination as cont

    with tempfile.TemporaryDirectory(prefix="elt-taskgen-ingest-preflight-") as temp:
        index_dir = Path(temp) / "contamination"
        index_dir.mkdir()
        copied = _copy_contamination_snapshot(workspace, index_dir)
        index = cont.ContaminationIndex(index_dir)
        if not copied:
            _seed_embedded_deny_lists(index)
        required = cont.required_coverage_from_env()
        checked: list[PreparedSource] = []
        fatal_lines: list[str] = []
        for item in prepared:
            result = index.scan_pre(item.task, require=required)
            for collision in result.collisions:
                if collision.fatal:
                    fatal_lines.append(
                        f"{item.origin.value}/{item.task.task_id} "
                        f"[{collision.kind}/{collision.against}] {collision.detail}"
                    )
            checked.append(replace(item, collisions=tuple(result.collisions)))
        if fatal_lines and not retain_task_local_fatals:
            raise FiveSourceIngestError(
                "five-source contamination preflight refused the roster: "
                + "; ".join(fatal_lines)
            )
        return tuple(checked)


def _stored_tasks(workspace: Path) -> dict[str, TaskIR]:
    tasks_root = workspace / "tasks"
    try:
        metadata = tasks_root.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise FiveSourceIngestError(
            f"cannot inspect workspace tasks root {tasks_root}: {exc}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise FiveSourceIngestError(
            f"workspace tasks root must be a non-symlink directory: {tasks_root}"
        )
    result: dict[str, TaskIR] = {}
    for directory in sorted(tasks_root.iterdir(), key=lambda item: item.name):
        item_metadata = directory.lstat()
        if stat.S_ISLNK(item_metadata.st_mode) or not stat.S_ISDIR(item_metadata.st_mode):
            raise FiveSourceIngestError(f"invalid entry in workspace tasks root: {directory}")
        task_path = directory / "task_ir.json"
        if not task_path.exists():
            raise FiveSourceIngestError(
                f"task directory has no task_ir.json (incomplete workspace): {directory}"
            )
        task_metadata = task_path.lstat()
        if stat.S_ISLNK(task_metadata.st_mode) or not stat.S_ISREG(task_metadata.st_mode):
            raise FiveSourceIngestError(
                f"task_ir.json must be a regular non-symlink file: {task_path}"
            )
        try:
            task = task_from_json(task_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise FiveSourceIngestError(f"cannot read stored task {task_path}: {exc}") from exc
        if task.task_id != directory.name:
            raise FiveSourceIngestError(
                f"task directory {directory.name!r} contains task {task.task_id!r}"
            )
        result[task.task_id] = task
    return result


def _assert_safe_workspace_shell(workspace: Path) -> None:
    """Refuse existing workspace roots that could redirect coordinator writes."""

    try:
        metadata = workspace.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise FiveSourceIngestError(f"cannot inspect workspace {workspace}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise FiveSourceIngestError(
            f"workspace must be a non-symlink directory when it exists: {workspace}"
        )
    for name in ("state", "tasks"):
        child = workspace / name
        try:
            child_metadata = child.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise FiveSourceIngestError(
                f"cannot inspect workspace {name} root {child}: {exc}"
            ) from exc
        if stat.S_ISLNK(child_metadata.st_mode) or not stat.S_ISDIR(
            child_metadata.st_mode
        ):
            raise FiveSourceIngestError(
                f"workspace {name} root must be a non-symlink directory: {child}"
            )


def _assert_workspace_compatible(
    workspace: Path,
    prepared: tuple[PreparedSource, ...],
    *,
    reingest: bool,
    allow_extras: bool = False,
) -> None:
    expected = {item.task.task_id: item for item in prepared}
    stored = _stored_tasks(workspace)
    extras = sorted(set(stored) - set(expected))
    if extras and not allow_extras:
        raise FiveSourceIngestError(
            "five-source workspace contains tasks outside this manifest's exact "
            f"roster: {extras}; use a fresh workspace"
        )
    for task_id, old in stored.items():
        if task_id not in expected:
        # Best-effort ingestion compares only the current singleton descriptor.
            continue
        new = expected[task_id].task
        if (
            old.content_hash() == new.content_hash()
            or lineage_root_hash(old) == new.content_hash()
        ):
        # Use the immutable lineage root so replay does not roll back later edits.
            continue
        if not reingest:
            raise FiveSourceIngestError(
                f"task {task_id!r} exists at {old.content_hash()[:12]} with status "
                f"{old.status.value!r} but the manifest builds "
                f"{new.content_hash()[:12]}; pass --reingest to replace that "
                "identity deliberately, or use a fresh workspace"
            )


def _assert_provenance_compatible(
    workspace: Path,
    prepared: tuple[PreparedSource, ...],
    *,
    reingest: bool,
    equivalence_authorization: EquivalenceAuthorization | None = None,
) -> tuple[PreparedSource, ...]:
    resolved: list[PreparedSource] = []
    for item in prepared:
        task_dir = workspace / "tasks" / item.task.task_id
        try:
            metadata = task_dir.lstat()
        except FileNotFoundError:
            resolved.append(item)
            continue
        except OSError as exc:
            raise FiveSourceIngestError(
                f"cannot inspect task directory {task_dir}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise FiveSourceIngestError(
                f"task directory must be a non-symlink directory: {task_dir}"
            )
        # Validate all history and inspect the requested lineage before re-ingest.
        current = load_for_lineage(
            task_dir,
            task_id=item.task.task_id,
            lineage_hash=item.provenance.task_content_hash,
            required=False,
            recoverable_evidence_digest=item.provenance.evidence_digest(),
        )
        if current is None or current == item.provenance:
            resolved.append(item)
            continue
        old_generator = current.source.selection_inputs.get("generator_code")
        new_generator = item.provenance.source.selection_inputs.get("generator_code")
        binding = (
            equivalence_authorization.binding_for(item.task.task_id)
            if equivalence_authorization is not None
            and item.task.task_id in equivalence_authorization.ordered_task_ids
            else None
        )
        if isinstance(
            equivalence_authorization, ImplementationEquivalenceAuthorization
        ):
            implementation_binding = (
                binding
                if isinstance(binding, ImplementationEquivalenceTaskBinding)
                else None
            )
            transition = (
                implementation_binding.adapter_transition
                if implementation_binding is not None
                else None
            )
            problem = implementation_equivalence_problem(
                current,
                item.provenance,
                adapter_transition=transition,
            )
            old_entry = current.source.selection_inputs.get("ingest_manifest")
            new_entry = item.provenance.source.selection_inputs.get(
                "ingest_manifest"
            )
            authorized = (
                reingest
                and implementation_binding is not None
                and old_generator is not None
                and new_generator is not None
                and old_entry is not None
                and new_entry is not None
                and old_generator.digest
                == equivalence_authorization.authoritative_generator_sha256
                and new_generator.digest
                == equivalence_authorization.reproduced_generator_sha256
                and implementation_binding.origin is item.origin
                and implementation_binding.authoritative_source_entry_sha256
                == old_entry.digest
                and implementation_binding.reproduced_source_entry_sha256
                == item.source_entry_sha256
                and new_entry.digest == item.source_entry_sha256
                and implementation_binding.intake_content_hash
                == item.provenance.task_content_hash
                and implementation_binding.authoritative_provenance_sha256
                == current.evidence_digest()
                and implementation_binding.reproduced_provenance_sha256
                == item.provenance.evidence_digest()
            )
        else:
            generator_binding = (
                binding if isinstance(binding, GeneratorEquivalenceTaskBinding) else None
            )
            transition = None
            problem = generator_equivalence_problem(current, item.provenance)
            authorized = (
                reingest
                and equivalence_authorization is not None
                and generator_binding is not None
                and old_generator is not None
                and new_generator is not None
                and old_generator.digest
                == equivalence_authorization.authoritative_generator_sha256
                and new_generator.digest
                == equivalence_authorization.reproduced_generator_sha256
                and generator_binding.source_entry_sha256 == item.source_entry_sha256
                and generator_binding.intake_content_hash
                == item.provenance.task_content_hash
                and generator_binding.authoritative_provenance_sha256
                == current.evidence_digest()
                and generator_binding.reproduced_provenance_sha256
                == item.provenance.evidence_digest()
            )
        if not authorized or problem:
            detail = (
                f" ({problem})" if authorized and problem else ""
            )
            raise FiveSourceIngestError(
                f"task {item.task.task_id!r} lineage "
                f"{item.provenance.task_content_hash[:12]} is already bound to "
                "different source provenance; --reingest cannot relabel an "
                "unchanged lineage, so use a fresh workspace or restore the "
                f"original source-entry pins{detail}"
            )
        if isinstance(
            equivalence_authorization, ImplementationEquivalenceAuthorization
        ):
            resolved.append(
                replace(
                    item,
                    provenance=current,
                    implementation_equivalence=item.provenance,
                    implementation_adapter_transition=transition,
                )
            )
        else:
            resolved.append(
                replace(
                    item,
                    provenance=current,
                    generator_equivalence=item.provenance,
                )
            )
    return tuple(resolved)


def _assert_unique_independence_units(
    prepared: tuple[PreparedSource, ...],
) -> None:
    """Require a v2 roster that cannot shrink on family/cluster deduplication."""

    for field in ("family_id", "cluster_id"):
        owners: dict[str, list[str]] = {}
        for item in prepared:
            owners.setdefault(str(getattr(item.task, field)), []).append(
                item.task.task_id
            )
        duplicates = {
            value: sorted(task_ids)
            for value, task_ids in sorted(owners.items())
            if len(task_ids) > 1
        }
        if duplicates:
            raise FiveSourceIngestError(
                f"batch roster repeats {field} independence units: {duplicates}"
            )


def _receipt(
    manifest: FiveSourceManifest, prepared: tuple[PreparedSource, ...]
) -> IngestReceipt:
    tasks = tuple(
        IngestReceiptTask(
            origin=item.origin,
            task_id=item.task.task_id,
        # The receipt records immutable intake identity, not later task state.
            task_content_hash=item.provenance.task_content_hash,
            provenance_digest=item.provenance.evidence_digest(),
        )
        for item in prepared
    )
    if isinstance(manifest, SelectedSourceIngestManifest):
        return SelectedSourceIngestReceipt(
            manifest_sha256=manifest.manifest_sha256(),
            parent_manifest_sha256=manifest.parent_manifest_sha256,
            generator=manifest.generator,
            expected_task_count=manifest.expected_task_count,
            selection_seed=manifest.selection_seed,
            origin_counts={
                origin: sum(task.origin is origin for task in tasks)
                for origin in FIVE_ORIGINS
            },
            tasks=tasks,
        )
    if isinstance(manifest, FiveSourceBatchIngestManifest):
        return FiveSourceBatchIngestReceipt(
            manifest_sha256=manifest.manifest_sha256(),
            generator=manifest.generator,
            expected_task_count=manifest.expected_task_count,
            origin_counts={
                origin: sum(task.origin is origin for task in tasks)
                for origin in FIVE_ORIGINS
            },
            tasks=tasks,
        )
    return FiveSourceIngestReceipt(
        manifest_sha256=manifest.manifest_sha256(),
        tasks=tasks,
    )


def _receipt_path(workspace: Path, receipt: IngestReceipt) -> Path:
    return (
        workspace
        / "state"
        / INGEST_RECEIPT_DIRNAME
        / f"{receipt.manifest_sha256}.json"
    )


def _assert_receipt_compatible(workspace: Path, receipt: IngestReceipt) -> None:
    destination = _receipt_path(workspace, receipt)
    store = destination.parent
    try:
        store_metadata = store.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise FiveSourceIngestError(
            f"cannot inspect ingest receipt store {store}: {exc}"
        ) from exc
    if stat.S_ISLNK(store_metadata.st_mode) or not stat.S_ISDIR(
        store_metadata.st_mode
    ):
        raise FiveSourceIngestError(
            f"ingest receipt store must be a non-symlink directory: {store}"
        )
    try:
        metadata = destination.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise FiveSourceIngestError(
            f"cannot inspect ingest receipt destination {destination}: {exc}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise FiveSourceIngestError(
            f"ingest receipt destination is not a regular file: {destination}"
        )
    if destination.read_bytes() != receipt.deterministic_bytes():
        raise FiveSourceIngestError(
            "this manifest fingerprint already has a different completion "
            f"receipt: {destination}"
        )


def _publish_receipt(workspace: Path, receipt: IngestReceipt) -> Path:
    destination = _receipt_path(workspace, receipt)
    store = destination.parent
    store.mkdir(parents=True, mode=0o755, exist_ok=True)
    store_metadata = store.lstat()
    if stat.S_ISLNK(store_metadata.st_mode) or not stat.S_ISDIR(store_metadata.st_mode):
        raise FiveSourceIngestError(f"ingest receipt store is unsafe: {store}")
    payload = receipt.deterministic_bytes()
    try:
        destination_metadata = destination.lstat()
    except FileNotFoundError:
        destination_metadata = None
    except OSError as exc:
        raise FiveSourceIngestError(
            f"cannot inspect ingest receipt destination {destination}: {exc}"
        ) from exc
    if destination_metadata is not None:
        if stat.S_ISLNK(destination_metadata.st_mode) or not stat.S_ISREG(
            destination_metadata.st_mode
        ):
            raise FiveSourceIngestError(
                f"ingest receipt destination is not a regular file: {destination}"
            )
        if destination.read_bytes() != payload:
            raise FiveSourceIngestError(
                f"ingest receipt changed at immutable destination {destination}"
            )
        return destination
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".five-source-ingest-", suffix=".tmp", dir=store
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
            concurrent_metadata = destination.lstat()
            if stat.S_ISLNK(concurrent_metadata.st_mode) or not stat.S_ISREG(
                concurrent_metadata.st_mode
            ):
                raise FiveSourceIngestError(
                    "concurrent publisher created an unsafe receipt destination: "
                    f"{destination}"
                )
            if destination.read_bytes() != payload:
                raise FiveSourceIngestError(
                    f"concurrent publisher wrote different receipt bytes: {destination}"
                )
        os.chmod(destination, destination.stat().st_mode & 0o555)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def prepare_five_sources(
    manifest: FiveSourceManifest,
    *,
    manifest_path: Path,
    workspace: Path,
    reingest: bool = False,
    allow_workspace_extras: bool = False,
    equivalence_authorization: EquivalenceAuthorization | None = None,
    retain_task_local_fatals: bool = False,
) -> tuple[PreparedSource, ...]:
    """Build and contamination-check the roster without modifying the workspace."""

    manifest_path = Path(manifest_path).absolute()
    workspace = Path(workspace).absolute()
    if _generator_pinned(manifest):
        # This is deliberately the first content verification in preflight.
        _verify_generator_pin(manifest.generator, label="preflight")
    _assert_safe_workspace_shell(workspace)
    # Keep snapshots outside the workspace until all preflight checks finish.
    with tempfile.TemporaryDirectory(
        prefix="elt-taskgen-ingest-inputs-"
    ) as temporary:
        return _prepare_five_sources_from_snapshots(
            manifest,
            manifest_path=manifest_path,
            workspace=workspace,
            reingest=reingest,
            allow_workspace_extras=allow_workspace_extras,
            equivalence_authorization=equivalence_authorization,
            retain_task_local_fatals=retain_task_local_fatals,
            snapshot_root=Path(temporary),
        )


def _prepare_five_sources_from_snapshots(
    manifest: FiveSourceManifest,
    *,
    manifest_path: Path,
    workspace: Path,
    reingest: bool,
    allow_workspace_extras: bool,
    equivalence_authorization: EquivalenceAuthorization | None,
    retain_task_local_fatals: bool,
    snapshot_root: Path,
) -> tuple[PreparedSource, ...]:
    """Implementation whose adapter-visible inputs live under snapshot_root."""

    catalog_path = _resolve_path(
        manifest.catalog.path,
        manifest_path=manifest_path,
        catalog=None,
        expected_pool=None,
    )
    prebuild_artifacts: dict[Path, ArtifactPin] = {}
    _verify_artifact_once(
        catalog_path,
        manifest.catalog,
        label="source catalog",
        verified=prebuild_artifacts,
    )
    snapshots: dict[Path, tuple[ArtifactPin, Path]] = {}
    catalog_snapshot = _snapshot_artifact_once(
        catalog_path,
        manifest.catalog,
        label="source catalog",
        snapshot_root=snapshot_root,
        snapshots=snapshots,
    )
    try:
        catalog = load_source_catalog(catalog_snapshot)
    except (OSError, ValueError) as exc:
        raise FiveSourceIngestError(f"cannot load pinned source catalog: {exc}") from exc

    built: list[PreparedSource] = []
    task_ids: set[str] = set()
    prebuild_adapters: dict[str, tuple[str, str, str]] = {}
    prebuild_wikidbs_inventories: dict[Path, str] = {}
    for entry in manifest.ordered_entries():
        adapter_name = _verify_adapter_once(entry, prebuild_adapters)
        row = _catalog_row(catalog, entry)
        resolved: list[tuple[str, Path, ArtifactPin]] = []
        path_map: dict[str, Path] = {}
        original_path_map: dict[str, Path] = {}
        for role, artifact in _entry_artifacts(entry):
            path = _resolve_path(
                artifact.path,
                manifest_path=manifest_path,
                catalog=catalog,
                expected_pool=entry.pool,
            )
            _verify_artifact_once(
                path,
                artifact,
                label=f"{entry.pool}.{role}",
                verified=prebuild_artifacts,
            )
            resolved.append((role, path, artifact))
            original_path_map[role] = path
            path_map[role] = _snapshot_artifact_once(
                path,
                artifact,
                label=f"{entry.pool}.{role}",
                snapshot_root=snapshot_root,
                snapshots=snapshots,
            )
        if isinstance(entry, WikiDBsIngest):
            _verify_wikidbs_inventory(
                entry,
                database=original_path_map["database"],
                root=row.root_path(),
                verified=prebuild_wikidbs_inventories,
            )
        try:
            task = _builder(entry)(entry, path_map, catalog)
        except FiveSourceIngestError:
            raise
        except (OSError, ValueError, KeyError) as exc:
            raise FiveSourceIngestError(
                f"{entry.pool} adapter refused selector {entry.selector!r}: {exc}"
            ) from exc
        _validate_task(entry, task)
        if task.task_id in task_ids:
            raise FiveSourceIngestError(f"duplicate task id across sources: {task.task_id!r}")
        task_ids.add(task.task_id)
        identity = _identity(
            entry,
            task,
            adapter_name=adapter_name,
            manifest=manifest,
        )
        built.append(
            PreparedSource(
                origin=_origin_for_entry(entry),
                task=task,
                provenance=IngestProvenance(
                    task_id=task.task_id,
                    task_content_hash=task.content_hash(),
                    source=identity,
                ),
                artifacts=tuple(resolved),
                source_entry_sha256=manifest.source_entry_sha256(entry),
            )
        )

    prepared = tuple(built)
    observed_origins = tuple(item.origin for item in prepared)
    if isinstance(manifest, FiveSourceIngestManifest):
        if observed_origins != FIVE_ORIGINS:
            raise FiveSourceIngestError(
                "v1 manifest did not resolve to the exact five-origin roster"
            )
    elif isinstance(manifest, FiveSourceBatchIngestManifest) and set(
        observed_origins
    ) != set(FIVE_ORIGINS):
        raise FiveSourceIngestError(
            "v2 manifest did not resolve to a non-empty five-origin roster"
        )
    elif isinstance(manifest, SelectedSourceIngestManifest) and len(
        prepared
    ) != manifest.expected_task_count:
        raise FiveSourceIngestError(
            "v3 selected manifest did not resolve to its exact candidate count"
        )
    if _generator_pinned(manifest):
        _assert_unique_independence_units(prepared)

    # Re-read all inputs after adapters run to detect source mutation before writes.
    postbuild_artifacts: dict[Path, ArtifactPin] = {}
    postbuild_adapters: dict[str, tuple[str, str, str]] = {}
    postbuild_wikidbs_inventories: dict[Path, str] = {}
    _verify_artifact_once(
        catalog_path,
        manifest.catalog,
        label="source catalog (post-build)",
        verified=postbuild_artifacts,
    )
    for entry, item in zip(manifest.ordered_entries(), prepared, strict=True):
        _verify_adapter_once(entry, postbuild_adapters)
        path_map = {role: path for role, path, _artifact in item.artifacts}
        for role, path, artifact in item.artifacts:
            _verify_artifact_once(
                path,
                artifact,
                label=f"{item.origin.value}.{role} (post-build)",
                verified=postbuild_artifacts,
            )
        if isinstance(entry, WikiDBsIngest):
            row = _catalog_row(catalog, entry)
            _verify_wikidbs_inventory(
                entry,
                database=path_map["database"],
                root=row.root_path(),
                verified=postbuild_wikidbs_inventories,
            )

    prepared = _scan_preflight(
        workspace,
        prepared,
        retain_task_local_fatals=retain_task_local_fatals,
    )
    _assert_workspace_compatible(
        workspace,
        prepared,
        reingest=reingest,
        allow_extras=allow_workspace_extras,
    )
    prepared = _assert_provenance_compatible(
        workspace,
        prepared,
        reingest=reingest,
        equivalence_authorization=equivalence_authorization,
    )
    if not any(
        item.generator_equivalence is not None
        or item.implementation_equivalence is not None
        for item in prepared
    ):
        _assert_receipt_compatible(workspace, _receipt(manifest, prepared))
    return prepared


def _check_live_contamination(
    engine: Engine,
    prepared: tuple[PreparedSource, ...],
    *,
    preserve_existing_rejections: bool = False,
) -> None:
    """Recheck the live index once, before the first registration."""

    from elt_taskgen.verification import contamination as cont

    index = cont.ContaminationIndex(engine.workspace / "state" / "contamination")
    if not index.is_armed():
        _seed_embedded_deny_lists(index)
    required = cont.required_coverage_from_env()
    fatal: list[str] = []
    for item in prepared:
        result = index.scan_pre(item.task, require=required)
        for collision in result.collisions:
            if not collision.fatal:
                continue
            preserved = False
            if preserve_existing_rejections:
                try:
                    existing = engine.load_task(item.task.task_id)
                    row = engine.latest_report(
                        item.task.task_id, "contamination_pre"
                    )
                    payload = (
                        json.loads(row.payload_json)
                        if row is not None and row.verdict == "fatal"
                        else {}
                    )
                    text = canonical_json(payload)
                    preserved = bool(
                        lineage_root_hash(existing) == item.task.content_hash()
                        and engine.final_verdict(item.task.task_id) == "rejected"
                        and collision.kind in text
                        and collision.detail in text
                    )
                except (OSError, TypeError, ValueError):
                    preserved = False
            if not preserved:
                fatal.append(
                    f"{item.origin.value}/{item.task.task_id} "
                    f"[{collision.kind}/{collision.against}] {collision.detail}"
                )
    if fatal:
        raise FiveSourceIngestError(
            "live contamination index changed/refused before registration: "
            + "; ".join(fatal)
        )


def _recheck_live_inputs(
    manifest: FiveSourceManifest,
    *,
    manifest_path: Path,
    prepared: tuple[PreparedSource, ...],
) -> None:
    """Close path, catalog, adapter, and inventory TOCTOU windows."""

    if _generator_pinned(manifest):
        # The caller holds the coordinator lock; refuse code/lock drift before
        # checking or publishing any task in the roster.
        _verify_generator_pin(manifest.generator, label="live")

    catalog_path = _resolve_path(
        manifest.catalog.path,
        manifest_path=manifest_path,
        catalog=None,
        expected_pool=None,
    )
    live_artifacts: dict[Path, ArtifactPin] = {}
    live_adapters: dict[str, tuple[str, str, str]] = {}
    live_wikidbs_inventories: dict[Path, str] = {}
    _verify_artifact_once(
        catalog_path,
        manifest.catalog,
        label="source catalog (live)",
        verified=live_artifacts,
    )
    try:
        catalog = load_source_catalog(catalog_path)
    except (OSError, ValueError) as exc:
        raise FiveSourceIngestError(
            f"cannot reload pinned source catalog before registration: {exc}"
        ) from exc

    for entry, item in zip(manifest.ordered_entries(), prepared, strict=True):
        _verify_adapter_once(entry, live_adapters)
        path_map: dict[str, Path] = {}
        expected_artifacts = {
            role: (path, artifact) for role, path, artifact in item.artifacts
        }
        for role, pin in _entry_artifacts(entry):
            path = _resolve_path(
                pin.path,
                manifest_path=manifest_path,
                catalog=catalog,
                expected_pool=entry.pool,
            )
            prepared_path, prepared_pin = expected_artifacts[role]
            if path.absolute() != prepared_path.absolute() or pin != prepared_pin:
                raise FiveSourceIngestError(
                    f"{entry.pool}.{role} resolved differently after preflight: "
                    f"{prepared_path} -> {path}"
                )
            _verify_artifact_once(
                path,
                pin,
                label=f"{entry.pool}.{role} (live)",
                verified=live_artifacts,
            )
            path_map[role] = path
        if isinstance(entry, WikiDBsIngest):
            row = _catalog_row(catalog, entry)
            _verify_wikidbs_inventory(
                entry,
                database=path_map["database"],
                root=row.root_path(),
                verified=live_wikidbs_inventories,
            )

    _verify_artifact(
        catalog_path,
        manifest.catalog,
        label="source catalog (live post-check)",
    )


def ingest_five_sources(
    manifest_path: Path,
    *,
    workspace: Path,
    reingest: bool = False,
    dry_run: bool = False,
) -> FiveSourceIngestResult:
    """Preflight and resumably register a v1 singleton or v2 batch manifest."""

    source = Path(manifest_path).absolute()
    target = Path(workspace).absolute()
    manifest = load_five_source_manifest(source)
    return _ingest_loaded_manifest(
        manifest,
        manifest_path=source,
        workspace=target,
        reingest=reingest,
        dry_run=dry_run,
    )


def ingest_five_source_batch(
    manifest_path: Path,
    *,
    workspace: Path,
    reingest: bool = False,
    dry_run: bool = False,
) -> FiveSourceIngestResult:
    """Explicit v2-only wrapper for programmatic and future CLI callers."""

    source = Path(manifest_path).absolute()
    manifest = load_five_source_batch_manifest(source)
    return _ingest_loaded_manifest(
        manifest,
        manifest_path=source,
        workspace=Path(workspace).absolute(),
        reingest=reingest,
        dry_run=dry_run,
    )


def persist_selected_manifest(
    manifest: SelectedSourceIngestManifest, *, workspace: Path
) -> Path:
    """Publish an immutable, machine-readable copy of a resolved v3 plan."""

    target = Path(workspace).absolute()
    store = target / "state" / "candidate_manifests"
    store.mkdir(parents=True, mode=0o755, exist_ok=True)
    metadata = store.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise FiveSourceIngestError(
            f"candidate manifest store must be a non-symlink directory: {store}"
        )
    destination = store / f"{manifest.manifest_sha256()}.json"
    payload = (
        readable_json(manifest.model_dump(mode="json")) + "\n"
    ).encode("utf-8")
    try:
        existing = destination.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
            raise FiveSourceIngestError(
                f"candidate manifest destination is unsafe: {destination}"
            )
        if destination.read_bytes() != payload:
            raise FiveSourceIngestError(
                f"candidate manifest changed at immutable destination {destination}"
            )
        return destination
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".selected-source-", suffix=".tmp", dir=store
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
            if destination.read_bytes() != payload:
                raise FiveSourceIngestError(
                    f"concurrent publisher wrote a different plan: {destination}"
                )
        os.chmod(destination, destination.stat().st_mode & 0o555)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def ingest_selected_sources(
    pool_manifest_path: Path,
    *,
    workspace: Path,
    candidate_count: int,
    source_families: tuple[Origin | str, ...] | None = None,
    source_allocation: dict[Origin | str, int] | None = None,
    seed: int = 0,
    resume: bool = True,
    reingest: bool = False,
    dry_run: bool = False,
    continue_on_candidate_error: bool = False,
    equivalence_authorization: EquivalenceAuthorization | None = None,
    equivalence_authorization_path: Path | None = None,
) -> tuple[SelectedSourceIngestManifest, FiveSourceIngestResult]:
    """Select and ingest exactly N candidates from a pinned v2 pool.

    Optional candidate-local continuation does not relax global generator or
    catalog failures.
    """

    source = Path(pool_manifest_path).absolute()
    pool = load_five_source_batch_manifest(source)
    selected = select_candidate_manifest(
        pool,
        candidate_count=candidate_count,
        source_families=source_families,
        source_allocation=source_allocation,
        seed=seed,
    )
    if equivalence_authorization_path is not None:
        loaded_authorization = load_generator_equivalence_authorization(
            equivalence_authorization_path, workspace=workspace
        )
        if (
            equivalence_authorization is not None
            and loaded_authorization != equivalence_authorization
        ):
            raise FiveSourceIngestError(
                "in-memory generator authorization differs from persisted evidence"
            )
        equivalence_authorization = loaded_authorization
    elif equivalence_authorization is not None:
        raise FiveSourceIngestError(
            "generator equivalence requires a persisted run-local authorization"
        )
    if equivalence_authorization is not None:
        expected_ids = tuple(
            entry.expected_task_id for entry in selected.ordered_entries()
        )
        problems: list[str] = []
        if not reingest:
            problems.append("explicit reingest is not enabled")
        if selected.manifest_sha256() != equivalence_authorization.reproduced_selected_sha256:
            problems.append("selected roster digest differs")
        if selected.generator.sha256 != equivalence_authorization.reproduced_generator_sha256:
            problems.append("generator digest differs")
        if pool.manifest_sha256() != equivalence_authorization.reproduced_pool_sha256:
            problems.append("pool digest differs")
        if expected_ids != equivalence_authorization.ordered_task_ids:
            problems.append("ordered task roster differs")
        if problems:
            raise FiveSourceIngestError(
                "generator equivalence authorization mismatch: "
                + "; ".join(problems)
            )
    if not resume:
        existing = [
            entry.expected_task_id
            for entry in selected.ordered_entries()
            if (
                Path(workspace).absolute()
                / "tasks"
                / entry.expected_task_id
                / "task_ir.json"
            ).exists()
        ]
        if existing:
            raise FiveSourceIngestError(
                "resume is disabled but selected task ids already exist: "
                f"{existing}; enable resume or choose a different seed/workspace"
            )
    target = Path(workspace).absolute()
    if equivalence_authorization is not None:
    # Verify the full identity migration before appending any equivalence.
        result = _ingest_loaded_manifest(
            selected,
            manifest_path=source,
            workspace=target,
            reingest=reingest,
            dry_run=dry_run,
            equivalence_authorization=equivalence_authorization,
            equivalence_authorization_path=equivalence_authorization_path,
        )
        if not dry_run:
            persist_selected_manifest(selected, workspace=target)
        outcomes = tuple(
            CandidateIngestOutcome(
                origin=task.origin,
                task_id=task.task_id,
                state="rederived",
            )
            for task in result.tasks
        )
        return selected, FiveSourceIngestResult(
            manifest_sha256=result.manifest_sha256,
            tasks=result.tasks,
            receipt=result.receipt,
            dry_run=result.dry_run,
            candidate_outcomes=outcomes,
        )
    if not continue_on_candidate_error:
        result = _ingest_loaded_manifest(
            selected,
            # Selected entries retain the pool manifest's relative-path base.
            manifest_path=source,
            workspace=target,
            reingest=reingest,
            dry_run=dry_run,
            equivalence_authorization=equivalence_authorization,
            equivalence_authorization_path=equivalence_authorization_path,
        )
        if not dry_run:
            persist_selected_manifest(selected, workspace=target)
        return selected, result

    # Establish shared trust inputs once. A stale generator or catalog is not a
    # candidate defect and must not be reported as N coincidental adapter errors.
    _assert_safe_workspace_shell(target)
    if not dry_run:
        # The immutable plan is useful accounting evidence even when a shared
        # generator/catalog check subsequently blocks every candidate.
        persist_selected_manifest(selected, workspace=target)
    _verify_generator_pin(selected.generator, label="configured ingest")
    catalog_path = _resolve_path(
        selected.catalog.path,
        manifest_path=source,
        catalog=None,
        expected_pool=None,
    )
    _verify_artifact(catalog_path, selected.catalog, label="source catalog")
    created: list[TaskIR] = []
    outcomes: list[CandidateIngestOutcome] = []
    for entry in selected.ordered_entries():
        origin = _origin_for_entry(entry)
        source_lists = {
            candidate_origin: ((entry,) if candidate_origin is origin else ())
            for candidate_origin in FIVE_ORIGINS
        }
        singleton = SelectedSourceIngestManifest(
            expected_task_count=1,
            parent_manifest_sha256=selected.parent_manifest_sha256,
            selection_seed=selected.selection_seed,
            source_allocation={
                candidate_origin: int(candidate_origin is origin)
                for candidate_origin in FIVE_ORIGINS
            },
            generator=selected.generator,
            catalog=selected.catalog,
            sources=SelectedSourceEntries(
                dbt=source_lists[Origin.DBT],
                dlt=source_lists[Origin.DLT],
                synsql=source_lists[Origin.SYNSQL],
                schemapile=source_lists[Origin.SCHEMAPILE],
                wikidbs=source_lists[Origin.WIKIDBS],
            ),
        )
        task_path = target / "tasks" / entry.expected_task_id / "task_ir.json"
        existed = task_path.is_file()
        try:
            one = _ingest_loaded_manifest(
                singleton,
                manifest_path=source,
                workspace=target,
                reingest=reingest,
                dry_run=dry_run,
                allow_workspace_extras=True,
                equivalence_authorization=equivalence_authorization,
            )
        except (FiveSourceIngestError, OSError, ValueError) as exc:
            outcomes.append(
                CandidateIngestOutcome(
                    origin=origin,
                    task_id=entry.expected_task_id,
                    state="failed",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        created.extend(one.tasks)
        outcomes.append(
            CandidateIngestOutcome(
                origin=origin,
                task_id=entry.expected_task_id,
                state=("prepared" if dry_run else "resumed" if existed else "created"),
            )
        )

    receipt_path: Path | None = None
    if not dry_run and len(created) == selected.expected_task_count:
        # The per-candidate commits are already durable. Publish the master
        # exact-roster receipt after reconstructing its provenance bindings.
        from elt_taskgen.provenance import load_current

        engine = Engine(target)
        try:
            prepared = tuple(
                PreparedSource(
                    origin=task.origin,
                    task=task,
                    provenance=load_current(
                        engine.task_dir(task.task_id), task=task, required=True
                    ),
                    artifacts=(),
                )
                for task in created
            )
        # Preserve the original intake receipt; equivalence records carry revalidation.
            if all(
                item.provenance.source.selection_inputs.get("generator_code")
                == ProvenanceArtifact(
                    locator=(
                        f"generator:{selected.generator.name}@"
                        f"{selected.generator.version}"
                    ),
                    digest=selected.generator.sha256,
                    digest_kind="sha256-canonical-json-v1",
                )
                for item in prepared
            ):
                receipt_path = _publish_receipt(
                    target, _receipt(selected, prepared)
                )
        finally:
            engine.close()

    return selected, FiveSourceIngestResult(
        manifest_sha256=selected.manifest_sha256(),
        tasks=tuple(created),
        receipt=receipt_path,
        dry_run=dry_run,
        candidate_outcomes=tuple(outcomes),
    )


def _ingest_loaded_manifest(
    manifest: FiveSourceManifest,
    *,
    manifest_path: Path,
    workspace: Path,
    reingest: bool,
    dry_run: bool,
    allow_workspace_extras: bool = False,
    equivalence_authorization: EquivalenceAuthorization | None = None,
    equivalence_authorization_path: Path | None = None,
) -> FiveSourceIngestResult:
    """Shared execution after one strict manifest parse."""

    source = Path(manifest_path).absolute()
    target = Path(workspace).absolute()
    if equivalence_authorization is not None:
        if equivalence_authorization_path is None:
            raise FiveSourceIngestError(
                "generator equivalence requires persisted authorization evidence"
            )
        confirmed = load_generator_equivalence_authorization(
            equivalence_authorization_path, workspace=target
        )
        if confirmed != equivalence_authorization:
            raise FiveSourceIngestError(
                "persisted generator equivalence authorization changed"
            )
    prepared = prepare_five_sources(
        manifest,
        manifest_path=source,
        workspace=target,
        reingest=reingest,
        allow_workspace_extras=allow_workspace_extras,
        equivalence_authorization=equivalence_authorization,
        retain_task_local_fatals=equivalence_authorization is not None,
    )
    if equivalence_authorization is not None:
        observed_ids = tuple(item.task.task_id for item in prepared)
        if observed_ids != equivalence_authorization.ordered_task_ids:
            protocol = (
                "implementation-equivalence"
                if isinstance(
                    equivalence_authorization,
                    ImplementationEquivalenceAuthorization,
                )
                else "generator-equivalence"
            )
            raise FiveSourceIngestError(
                f"rebuilt {protocol} roster is not exact"
            )
        for item in prepared:
            binding = equivalence_authorization.binding_for(item.task.task_id)
            if isinstance(
                equivalence_authorization, ImplementationEquivalenceAuthorization
            ):
                reproduced = item.implementation_equivalence
                binding_ok = bool(
                    isinstance(binding, ImplementationEquivalenceTaskBinding)
                    and reproduced is not None
                    and binding.origin is item.origin
                    and binding.reproduced_source_entry_sha256
                    == item.source_entry_sha256
                    and binding.authoritative_source_entry_sha256
                    == item.provenance.source.selection_inputs[
                        "ingest_manifest"
                    ].digest
                    and binding.adapter_transition
                    == item.implementation_adapter_transition
                    and binding.intake_content_hash == item.task.content_hash()
                    and binding.authoritative_provenance_sha256
                    == item.provenance.evidence_digest()
                    and binding.reproduced_provenance_sha256
                    == reproduced.evidence_digest()
                )
            else:
                reproduced = item.generator_equivalence
                binding_ok = bool(
                    isinstance(binding, GeneratorEquivalenceTaskBinding)
                    and reproduced is not None
                    and binding.source_entry_sha256 == item.source_entry_sha256
                    and binding.intake_content_hash == item.task.content_hash()
                    and binding.authoritative_provenance_sha256
                    == item.provenance.evidence_digest()
                    and binding.reproduced_provenance_sha256
                    == reproduced.evidence_digest()
                )
            if not binding_ok:
                protocol = (
                    "implementation equivalence"
                    if isinstance(
                        equivalence_authorization,
                        ImplementationEquivalenceAuthorization,
                    )
                    else "generator equivalence"
                )
                raise FiveSourceIngestError(
                    f"{protocol} binding changed for {item.task.task_id!r}"
                )
    receipt = _receipt(manifest, prepared)
    if dry_run:
        return FiveSourceIngestResult(
            manifest_sha256=manifest.manifest_sha256(),
            tasks=tuple(item.task for item in prepared),
            receipt=None,
            dry_run=True,
        )

    engine = Engine(target)
    try:
        lock_ids = tuple(item.task.task_id for item in prepared)
        with engine.coordinator_lock(), (
            engine.task_locks(lock_ids)
            if equivalence_authorization is not None
            else _nullcontext()
        ):
            # Recheck every expected refusal before registering the first task.
            _recheck_live_inputs(
                manifest,
                manifest_path=source,
                prepared=prepared,
            )
            _assert_workspace_compatible(
                target,
                prepared,
                reingest=reingest,
                allow_extras=allow_workspace_extras,
            )
            live_prepared = _assert_provenance_compatible(
                target,
                prepared,
                reingest=reingest,
                equivalence_authorization=equivalence_authorization,
            )
            if _receipt(manifest, live_prepared) != receipt:
                raise FiveSourceIngestError(
                    "authoritative provenance changed between preflight and "
                    "registration"
                )
            prepared = live_prepared
            has_implementation_equivalence = any(
                item.generator_equivalence is not None
                or item.implementation_equivalence is not None
                for item in prepared
            )
            if not has_implementation_equivalence:
                _assert_receipt_compatible(target, receipt)
            _check_live_contamination(
                engine,
                prepared,
                preserve_existing_rejections=equivalence_authorization is not None,
            )

            for item in prepared:
                task_path = engine.task_dir(item.task.task_id) / "task_ir.json"
                already_same_intake = False
                if task_path.is_file():
                    existing = engine.load_task(item.task.task_id)
                    already_same_intake = (
                        lineage_root_hash(existing) == item.task.content_hash()
                    )
                if not already_same_intake:
                    engine.register(item.task, allow_overwrite=reingest)
                publish_or_confirm(engine.task_dir(item.task.task_id), item.provenance)
                if item.generator_equivalence is not None:
                    publish_generator_equivalence(
                        engine.task_dir(item.task.task_id),
                        authoritative=item.provenance,
                        reproduced=item.generator_equivalence,
                    )
                if item.implementation_equivalence is not None:
                    publish_implementation_equivalence(
                        engine.task_dir(item.task.task_id),
                        authoritative=item.provenance,
                        reproduced=item.implementation_equivalence,
                        adapter_transition=item.implementation_adapter_transition,
                    )
                    # Preserve the original intake receipt for same-root revalidation.
            receipt_path = (
                None
                if has_implementation_equivalence
                else _publish_receipt(target, receipt)
            )
            stored = tuple(engine.load_task(item.task.task_id) for item in prepared)
    finally:
        engine.close()

    return FiveSourceIngestResult(
        manifest_sha256=manifest.manifest_sha256(),
        tasks=stored,
        receipt=receipt_path,
        dry_run=False,
    )


__all__ = [
    "ArtifactPin",
    "CandidateIngestOutcome",
    "DbtIngest",
    "DltIngest",
    "FIVE_ORIGINS",
    "FIVE_SOURCE_BATCH_MANIFEST_SCHEMA_VERSION",
    "FIVE_SOURCE_BATCH_RECEIPT_SCHEMA_VERSION",
    "FIVE_SOURCE_MANIFEST_SCHEMA_VERSION",
    "SELECTED_SOURCE_MANIFEST_SCHEMA_VERSION",
    "SELECTED_SOURCE_RECEIPT_SCHEMA_VERSION",
    "INGEST_RECEIPT_DIRNAME",
    "EquivalenceAuthorization",
    "GeneratorEquivalenceAuthorization",
    "GeneratorEquivalenceTaskBinding",
    "GeneratorReportRevalidationRequest",
    "GeneratorPin",
    "ImplementationAdapterTransition",
    "ImplementationEquivalenceAuthorization",
    "ImplementationEquivalenceTaskBinding",
    "FiveSourceBatchEntries",
    "FiveSourceBatchIngestManifest",
    "FiveSourceBatchIngestReceipt",
    "FiveSourceEntries",
    "FiveSourceIngestError",
    "FiveSourceIngestManifest",
    "FiveSourceIngestReceipt",
    "FiveSourceIngestResult",
    "SelectedSourceEntries",
    "SelectedSourceIngestManifest",
    "SelectedSourceIngestReceipt",
    "PreparedSource",
    "SchemaPileIngest",
    "SynSQLIngest",
    "WikiDBsIngest",
    "current_generator_pin",
    "ingest_five_source_batch",
    "ingest_five_sources",
    "ingest_selected_sources",
    "implementation_adapter_transition",
    "load_five_source_batch_manifest",
    "load_five_source_manifest",
    "load_generator_equivalence_authorization",
    "load_implementation_equivalence_authorization",
    "prepare_five_sources",
    "persist_selected_manifest",
    "repin_current_implementation",
    "select_candidate_manifest",
    "wikidbs_node_inventory_sha256",
]

"""Store immutable, task-bound source-ingestion evidence.

Records bind selectors and upstream pins to the intake lineage root without
affecting TaskIR semantic identity. Publication creates or confirms exact bytes
and never replaces a record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from elt_taskgen.models import (
    Origin,
    TaskIR,
    canonical_json,
    readable_json,
    task_from_json,
    validate_task_id_segment,
)

PROVENANCE_SCHEMA_VERSION = "1.0"
INGEST_PROVENANCE_DIRNAME = "ingest_provenance"
RELEASE_PROVENANCE_DIRNAME = "provenance"
RELEASE_PROVENANCE_FILENAME = "ingest_provenance.json"

_POOL_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ADAPTER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.-]*$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECORD_FILENAME_RE = re.compile(r"^([0-9a-f]{64})\.([0-9a-f]{64})\.json$")
_CLAIM_FILENAME_RE = re.compile(r"^([0-9a-f]{64})\.claim$")
_GENERATOR_EQUIVALENCE_FILENAME_RE = re.compile(
    r"^([0-9a-f]{64})\.([0-9a-f]{64})\.generator-equivalence\.json$"
)
GENERATOR_EQUIVALENCE_SCHEMA_VERSION = "generator-provenance-equivalence-v1"
_IMPLEMENTATION_EQUIVALENCE_FILENAME_RE = re.compile(
    r"^([0-9a-f]{64})\.([0-9a-f]{64})\.implementation-equivalence\.json$"
)
IMPLEMENTATION_EQUIVALENCE_SCHEMA_VERSION = (
    "implementation-provenance-equivalence-v2"
)

SourceDigestKind = Literal[
    "sha256-file",
    "sha256-tree-v1",
    "sha256-canonical-json-v1",
]

_COMMON_SELECTION_INPUTS = frozenset({"ingest_manifest", "source_catalog"})
_POOL_SELECTION_INPUTS: dict[Origin, frozenset[str]] = {
    Origin.SCHEMAPILE: frozenset({"schemapile_index"}),
    Origin.WIKIDBS: frozenset(
        {"wikidbs_family_map", "wikidbs_node_inventory"}
    ),
}
_IMPLEMENTATION_EQUIVALENCE_ORIGINS = frozenset(
    {
        Origin.DBT,
        Origin.DLT,
        Origin.SYNSQL,
        Origin.SCHEMAPILE,
        Origin.WIKIDBS,
    }
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    """SHA-256 of one regular, non-symlink file's exact bytes."""

    source = Path(path)
    metadata = source.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"source artifact must be a regular non-symlink file: {source}")
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_canonical_json(value: Any) -> str:
    """SHA-256 of a parsed JSON value under the package canonical encoding."""

    return _sha256_bytes(canonical_json(value).encode("utf-8"))


def sha256_tree(root: Path) -> str:
    """Digest regular files by portable path, size, and bytes; reject links."""

    tree = Path(root)
    root_metadata = tree.lstat()
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError(f"source artifact must be a non-symlink directory: {tree}")
    inventory: list[dict[str, str | int]] = []
    for path in sorted(tree.rglob("*"), key=lambda item: item.relative_to(tree).as_posix()):
        metadata = path.lstat()
        rel = path.relative_to(tree).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"source tree contains a symbolic link: {rel}")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"source tree contains a special file: {rel}")
        inventory.append(
            {"path": rel, "size": metadata.st_size, "sha256": sha256_file(path)}
        )
    return sha256_canonical_json(inventory)


class ProvenanceArtifact(BaseModel):
    """One portable, digest-pinned input that affected source selection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    locator: str = Field(min_length=1)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    digest_kind: SourceDigestKind

    @field_validator("locator")
    @classmethod
    def _portable_locator(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("artifact locator must be clean printable text")
        if value.startswith(("/", "~")) or "\\" in value:
            raise ValueError("artifact locator must not expose a host-local path")
        return value


class SourceIdentity(BaseModel):
    """Exact upstream identity and ingestion implementation for one selector."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool: str = Field(min_length=1)
    origin: Origin
    selector: str = Field(min_length=1)
    upstream_url: str = Field(min_length=1)
    upstream_revision: str = Field(min_length=1)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_digest_kind: SourceDigestKind
    adapter_name: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    adapter_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    license: str = Field(min_length=1)
    #: Portable pointer to the immutable bytes/metadata establishing the term.
    #: Examples: ``selected-record:INFO.LICENSE`` or ``repository:LICENSE``.
    license_evidence: str = Field(min_length=1)
    #: Other selection inputs. Pool-specific inputs are required below, and
    #: additional named inputs remain allowed.
    selection_inputs: dict[str, ProvenanceArtifact] = Field(min_length=2)

    @field_validator(
        "upstream_revision",
        "adapter_version",
        "license",
    )
    @classmethod
    def _portable_nonblank_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("value must not have leading or trailing whitespace")
        if any(ord(character) < 32 for character in value):
            raise ValueError("control characters are forbidden")
        return value

    @field_validator("selector")
    @classmethod
    def _portable_selector(cls, value: str) -> str:
        value = cls._portable_nonblank_text(value)
        if value.startswith(("/", "~")) or "\\" in value:
            raise ValueError("selector must be source-relative, not a host path")
        return value

    @field_validator("upstream_url")
    @classmethod
    def _portable_upstream_url(cls, value: str) -> str:
        value = cls._portable_nonblank_text(value)
        if re.fullmatch(r"https?://[^\s]+", value) is None:
            raise ValueError("upstream_url must be an absolute HTTP(S) URL")
        authority = value.split("//", 1)[1].split("/", 1)[0]
        if "@" in authority:
            raise ValueError("upstream_url must not contain credentials")
        return value

    @field_validator("upstream_revision")
    @classmethod
    def _pinned_revision(cls, value: str) -> str:
        value = cls._portable_nonblank_text(value)
        if value.casefold() in {
            "current",
            "head",
            "latest",
            "main",
            "master",
            "unknown",
            "unversioned",
        }:
            raise ValueError("upstream_revision must be immutable, not floating")
        return value

    @field_validator("license_evidence")
    @classmethod
    def _portable_license_evidence(cls, value: str) -> str:
        value = cls._portable_nonblank_text(value)
        if value.startswith(("/", "~")) or "\\" in value:
            raise ValueError("license_evidence must be a portable source locator")
        return value

    @field_validator("pool")
    @classmethod
    def _valid_pool(cls, value: str) -> str:
        if _POOL_RE.fullmatch(value) is None:
            raise ValueError("pool must be a lowercase identifier")
        return value

    @field_validator("adapter_name")
    @classmethod
    def _valid_adapter_name(cls, value: str) -> str:
        if _ADAPTER_RE.fullmatch(value) is None:
            raise ValueError("adapter_name must be a portable dotted identifier")
        return value

    @field_validator("selection_inputs")
    @classmethod
    def _valid_selection_roles(
        cls, value: dict[str, ProvenanceArtifact]
    ) -> dict[str, ProvenanceArtifact]:
        invalid = sorted(role for role in value if _ROLE_RE.fullmatch(role) is None)
        if invalid:
            raise ValueError(f"selection input roles are not portable: {invalid}")
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def _complete_selection_inputs(self) -> "SourceIdentity":
        required = _COMMON_SELECTION_INPUTS | _POOL_SELECTION_INPUTS.get(
            self.origin, frozenset()
        )
        missing = sorted(required - set(self.selection_inputs))
        if missing:
            raise ValueError(
                f"source {self.origin.value!r} is missing selection input(s) {missing}"
            )
        return self


class IngestProvenance(BaseModel):
    """One append-only source claim bound to a TaskIR lineage root."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = PROVENANCE_SCHEMA_VERSION
    task_id: str = Field(min_length=1)
    #: Semantic hash at intake.  This is the first TaskRevision content hash,
    #: not necessarily the current hash after authoring/repair.
    task_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: SourceIdentity

    @field_validator("task_id")
    @classmethod
    def _safe_task_id(cls, value: str) -> str:
        return validate_task_id_segment(value)

    @model_validator(mode="after")
    def _pool_matches_origin(self) -> "IngestProvenance":
        if self.source.pool != self.source.origin.value:
            raise ValueError(
                "source pool must equal its Origin value "
                f"({self.source.pool!r} != {self.source.origin.value!r})"
            )
        return self

    def evidence_digest(self) -> str:
        """Canonical parsed-record digest exposed in the release manifest."""

        return sha256_canonical_json(self.model_dump(mode="json"))

    def deterministic_bytes(self) -> bytes:
        return (
            readable_json(self.model_dump(mode="json")) + "\n"
        ).encode("utf-8")


class GeneratorEquivalenceAttestation(BaseModel):
    """Append-only proof that a new generator reproduced an intake root."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["generator-provenance-equivalence-v1"] = (
        GENERATOR_EQUIVALENCE_SCHEMA_VERSION
    )
    task_id: str = Field(min_length=1)
    task_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_provenance: IngestProvenance

    @model_validator(mode="after")
    def _bound_transition(self) -> "GeneratorEquivalenceAttestation":
        derived = self.reproduced_provenance
        if derived.task_id != self.task_id:
            raise ValueError("generator equivalence task_id differs from its record")
        if derived.task_content_hash != self.task_content_hash:
            raise ValueError("generator equivalence lineage differs from its record")
        if derived.evidence_digest() == self.authoritative_evidence_digest:
            raise ValueError("generator equivalence must name a new observation")
        return self

    def deterministic_bytes(self) -> bytes:
        return (
            readable_json(self.model_dump(mode="json")) + "\n"
        ).encode("utf-8")


class ImplementationAdapterTransition(BaseModel):
    """Exact adapter pins and source-entry hashes admitted by a v2 replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool: Origin
    adapter_name: str = Field(min_length=1)
    authoritative_version: str = Field(min_length=1)
    reproduced_version: str = Field(min_length=1)
    authoritative_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_source_entry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduced_source_entry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("adapter_name")
    @classmethod
    def _valid_adapter_name(cls, value: str) -> str:
        if _ADAPTER_RE.fullmatch(value) is None:
            raise ValueError("adapter_name must be a portable dotted identifier")
        return value

    @field_validator("authoritative_version", "reproduced_version")
    @classmethod
    def _clean_adapter_version(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("adapter versions must be clean printable text")
        return value

    @model_validator(mode="after")
    def _real_supported_transition(self) -> "ImplementationAdapterTransition":
        if self.pool not in _IMPLEMENTATION_EQUIVALENCE_ORIGINS:
            raise ValueError("adapter transition pool must be one of the five sources")
        if (
            self.authoritative_version == self.reproduced_version
            and self.authoritative_digest == self.reproduced_digest
        ):
            raise ValueError(
                "adapter transition must change the adapter version and/or digest"
            )
        if (
            self.authoritative_source_entry_sha256
            == self.reproduced_source_entry_sha256
        ):
            raise ValueError("adapter transition must change the source-entry digest")
        return self


# A concise alias retained for callers that describe this exact object by what
# moves rather than by the containing implementation-equivalence protocol.
AdapterDigestTransition = ImplementationAdapterTransition


class ImplementationEquivalenceAttestation(BaseModel):
    """Append-only proof of an exact generator and adapter replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["implementation-provenance-equivalence-v2"] = (
        IMPLEMENTATION_EQUIVALENCE_SCHEMA_VERSION
    )
    task_id: str = Field(min_length=1)
    task_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_transition: ImplementationAdapterTransition | None = None
    reproduced_provenance: IngestProvenance

    @field_validator("task_id")
    @classmethod
    def _safe_task_id(cls, value: str) -> str:
        return validate_task_id_segment(value)

    @model_validator(mode="after")
    def _bound_transition(self) -> "ImplementationEquivalenceAttestation":
        derived = self.reproduced_provenance
        if derived.task_id != self.task_id:
            raise ValueError(
                "implementation equivalence task_id differs from its record"
            )
        if derived.task_content_hash != self.task_content_hash:
            raise ValueError(
                "implementation equivalence lineage differs from its record"
            )
        if derived.evidence_digest() == self.authoritative_evidence_digest:
            raise ValueError(
                "implementation equivalence must name a new observation"
            )
        return self

    def evidence_digest(self) -> str:
        """Canonical digest of the complete v2 attestation."""

        return sha256_canonical_json(self.model_dump(mode="json"))

    def deterministic_bytes(self) -> bytes:
        return (
            readable_json(self.model_dump(mode="json")) + "\n"
        ).encode("utf-8")


def generator_equivalence_problem(
    previous: IngestProvenance, derived: IngestProvenance
) -> str:
    """Empty iff only generator/dependency-lock provenance changed."""

    if previous.task_id != derived.task_id:
        return "task_id changed"
    if previous.task_content_hash != derived.task_content_hash:
        return "intake lineage changed"
    old_source = previous.source.model_dump(mode="json")
    new_source = derived.source.model_dump(mode="json")
    old_inputs = dict(old_source.pop("selection_inputs"))
    new_inputs = dict(new_source.pop("selection_inputs"))
    if old_source != new_source:
        return "source selector, revision, digest, adapter, or license changed"
    if set(old_inputs) != set(new_inputs):
        return "selection-input roles changed"
    generator_roles = {"generator_code", "dependency_lock"}
    if not generator_roles.issubset(old_inputs):
        return "prior provenance has no complete generator identity"
    if any(
        old_inputs[role] != new_inputs[role]
        for role in old_inputs
        if role not in generator_roles
    ):
        return "a non-generator selection input changed"
    if old_inputs["dependency_lock"] != new_inputs["dependency_lock"]:
        return "dependency-lock identity changed"
    old_generator = dict(old_inputs["generator_code"])
    new_generator = dict(new_inputs["generator_code"])
    old_digest = old_generator.pop("digest", None)
    new_digest = new_generator.pop("digest", None)
    if old_generator != new_generator:
        return "generator locator or digest kind changed"
    if old_digest == new_digest:
        return "generator code identity did not change"
    return ""


def implementation_equivalence_problem(
    previous: IngestProvenance,
    derived: IngestProvenance,
    *,
    adapter_transition: ImplementationAdapterTransition | None = None,
) -> str:
    """Return empty only for an allowed code-identity replay.

    Selectors, source bytes, licenses, semantics, adapter name, dependency lock,
    and all non-code selection inputs must remain fixed.
    """

    if previous.task_id != derived.task_id:
        return "task_id changed"
    if previous.task_content_hash != derived.task_content_hash:
        return "intake lineage changed"

    old_source = previous.source.model_dump(mode="json")
    new_source = derived.source.model_dump(mode="json")
    old_inputs = dict(old_source.pop("selection_inputs"))
    new_inputs = dict(new_source.pop("selection_inputs"))
    old_adapter_version = old_source.pop("adapter_version")
    new_adapter_version = new_source.pop("adapter_version")
    old_adapter_digest = old_source.pop("adapter_digest")
    new_adapter_digest = new_source.pop("adapter_digest")

    if old_source != new_source:
        return (
            "source selector, revision, digest, adapter name, or license changed"
        )

    if adapter_transition is None:
        if (
            old_adapter_version != new_adapter_version
            or old_adapter_digest != new_adapter_digest
        ):
            return (
                "adapter version or digest changed without an exact adapter "
                "transition"
            )
    else:
        source = previous.source
        if (
            adapter_transition.pool is not source.origin
            or source.pool != adapter_transition.pool.value
            or adapter_transition.adapter_name != source.adapter_name
            or adapter_transition.authoritative_version != old_adapter_version
            or adapter_transition.reproduced_version != new_adapter_version
            or adapter_transition.authoritative_digest != old_adapter_digest
            or adapter_transition.reproduced_digest != new_adapter_digest
        ):
            return "adapter transition does not match the source identity"
        if (
            old_adapter_version == new_adapter_version
            and old_adapter_digest == new_adapter_digest
        ):
            return (
                "adapter transition was supplied but the adapter implementation "
                "did not change"
            )

    if set(old_inputs) != set(new_inputs):
        return "selection-input roles changed"
    generator_roles = {"generator_code", "dependency_lock"}
    if not generator_roles.issubset(old_inputs):
        return "prior provenance has no complete generator identity"

    ignored_roles = set(generator_roles)
    if adapter_transition is not None:
        ignored_roles.add("ingest_manifest")
    if any(
        old_inputs[role] != new_inputs[role]
        for role in old_inputs
        if role not in ignored_roles
    ):
        return "a non-implementation selection input changed"

    if adapter_transition is not None:
        old_manifest = dict(old_inputs["ingest_manifest"])
        new_manifest = dict(new_inputs["ingest_manifest"])
        old_manifest_digest = old_manifest.pop("digest", None)
        new_manifest_digest = new_manifest.pop("digest", None)
        if old_manifest != new_manifest:
            return "ingest-manifest locator or digest kind changed"
        if (
            old_manifest_digest
            != adapter_transition.authoritative_source_entry_sha256
            or new_manifest_digest
            != adapter_transition.reproduced_source_entry_sha256
        ):
            return "adapter transition source-entry digest mismatch"

    if old_inputs["dependency_lock"] != new_inputs["dependency_lock"]:
        return "dependency-lock identity changed"
    old_generator = dict(old_inputs["generator_code"])
    new_generator = dict(new_inputs["generator_code"])
    old_generator_digest = old_generator.pop("digest", None)
    new_generator_digest = new_generator.pop("digest", None)
    old_generator_locator = old_generator.pop("locator", None)
    new_generator_locator = new_generator.pop("locator", None)
    if old_generator != new_generator:
        return "generator metadata beyond its locator/digest changed"
    if adapter_transition is None or (
        old_adapter_version == new_adapter_version
    ):
        if old_generator_locator != new_generator_locator:
            return "generator locator changed without an adapter version transition"
    else:
        old_suffix = f"@{old_adapter_version}"
        new_suffix = f"@{new_adapter_version}"
        if (
            not isinstance(old_generator_locator, str)
            or not isinstance(new_generator_locator, str)
            or not old_generator_locator.startswith("generator:")
            or not new_generator_locator.startswith("generator:")
            or not old_generator_locator.endswith(old_suffix)
            or not new_generator_locator.endswith(new_suffix)
            or old_generator_locator[: -len(old_suffix)]
            != new_generator_locator[: -len(new_suffix)]
        ):
            return "generator locator does not bind the adapter version transition"
    if old_generator_digest == new_generator_digest:
        return "generator code identity did not change"
    return ""


def lineage_root_hash(task: TaskIR) -> str:
    """The stable TaskIR identity to which intake evidence must bind."""

    return task.revisions[0].content_hash if task.revisions else task.content_hash()


def provenance_for_task(task: TaskIR, source: SourceIdentity) -> IngestProvenance:
    """Build and validate the canonical evidence record for a stored task."""

    record = IngestProvenance(
        task_id=task.task_id,
        task_content_hash=lineage_root_hash(task),
        source=source,
    )
    validate_task_binding(task, record)
    return record


def validate_task_binding(task: TaskIR, provenance: IngestProvenance) -> None:
    """Fail unless a provenance record describes this task lineage exactly."""

    problems: list[str] = []
    if provenance.task_id != task.task_id:
        problems.append(
            f"task_id {provenance.task_id!r} != stored task {task.task_id!r}"
        )
    root_hash = lineage_root_hash(task)
    if provenance.task_content_hash != root_hash:
        problems.append(
            "intake task content hash "
            f"{provenance.task_content_hash[:12]} != lineage root {root_hash[:12]}"
        )
    if provenance.source.origin is not task.origin:
        problems.append(
            f"origin {provenance.source.origin.value!r} != task origin {task.origin.value!r}"
        )
    if provenance.source.license != task.license:
        problems.append(
            f"license {provenance.source.license!r} != task license {task.license!r}"
        )
    if not task.family_id.startswith(f"{provenance.source.pool}__"):
        problems.append(
            f"family {task.family_id!r} is outside pool {provenance.source.pool!r}"
        )
    if problems:
        raise ValueError("ingest provenance is not bound to task: " + "; ".join(problems))


def _load_task(task_dir: Path) -> TaskIR:
    task_path = task_dir / "task_ir.json"
    metadata = task_path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"task_ir.json must be a regular non-symlink file: {task_path}")
    return task_from_json(task_path.read_text(encoding="utf-8"))


def _store_dir(task_dir: Path, *, create: bool) -> Path:
    root = Path(task_dir)
    metadata = root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"task directory must be a non-symlink directory: {root}")
    store = root / INGEST_PROVENANCE_DIRNAME
    if create:
        store.mkdir(mode=0o755, exist_ok=True)
    store_metadata = store.lstat()
    if stat.S_ISLNK(store_metadata.st_mode) or not stat.S_ISDIR(store_metadata.st_mode):
        raise ValueError(f"provenance store must be a non-symlink directory: {store}")
    return store


def _read_regular_bytes(path: Path, *, label: str) -> bytes:
    """Read one path without following a symlink planted during a race."""

    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} must be a regular non-symlink file: {path}")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(
            f"{label} must be a readable regular non-symlink file: {path}: {exc}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValueError(f"{label} changed while it was being opened: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _set_regular_readonly(path: Path, *, label: str) -> None:
    """Strip write bits through a no-follow descriptor, never a raced path."""

    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ValueError(
            f"{label} must be a regular non-symlink file: {path}: {exc}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} must be a regular file: {path}")
        os.fchmod(descriptor, metadata.st_mode & 0o555)
    finally:
        os.close(descriptor)


def publish_or_confirm(task_dir: Path, provenance: IngestProvenance) -> Path:
    """Atomically append evidence or confirm identical published bytes."""

    root = Path(task_dir)
    task = _load_task(root)
    validate_task_binding(task, provenance)
    store = _store_dir(root, create=True)
    payload = provenance.deterministic_bytes()
    evidence_digest = provenance.evidence_digest()
    # Validate all history first. Only an identical target claim without its
    # record is recoverable after a crash between hard links.
    records = _load_store_records(
        root,
        task_id=task.task_id,
        required=False,
        recoverable_incomplete_claim=(
            provenance.task_content_hash,
            evidence_digest,
        ),
    )
    published = _select_lineage_record(
        records,
        task_id=task.task_id,
        lineage_hash=provenance.task_content_hash,
        required=False,
    )
    if published is not None:
        validate_task_binding(task, published)
        if published != provenance:
            raise ValueError(
                "lineage already has different ingest provenance bytes: "
                f"{provenance.task_content_hash[:12]}"
            )
        return store / (
            f"{published.task_content_hash}.{published.evidence_digest()}.json"
        )
    claim_destination = store / f"{provenance.task_content_hash}.claim"
    destination = store / (
        f"{provenance.task_content_hash}.{evidence_digest}.json"
    )

    _publish_immutable_file(
        store,
        claim_destination,
        (evidence_digest + "\n").encode("ascii"),
        conflict=(
            "lineage already has different ingest provenance bytes: "
            f"{provenance.task_content_hash[:12]}"
        ),
    )
    _publish_immutable_file(
        store,
        destination,
        payload,
        conflict=f"provenance destination has different bytes: {destination}",
    )
    confirmed = load_current(root, task=task)
    if confirmed != provenance:
        raise ValueError(
            "published ingest provenance does not match the requested record"
        )
    return destination


def publish_generator_equivalence(
    task_dir: Path,
    *,
    authoritative: IngestProvenance,
    reproduced: IngestProvenance,
) -> Path:
    """Append or confirm a generator-only replay without changing provenance."""

    root = Path(task_dir)
    task = _load_task(root)
    validate_task_binding(task, authoritative)
    validate_task_binding(task, reproduced)
    current = load_for_lineage(
        root,
        task_id=task.task_id,
        lineage_hash=authoritative.task_content_hash,
        required=True,
    )
    if current != authoritative:
        raise ValueError("generator equivalence does not name authoritative provenance")
    problem = generator_equivalence_problem(authoritative, reproduced)
    if problem:
        raise ValueError(f"invalid generator equivalence: {problem}")
    attestation = GeneratorEquivalenceAttestation(
        task_id=task.task_id,
        task_content_hash=authoritative.task_content_hash,
        authoritative_evidence_digest=authoritative.evidence_digest(),
        reproduced_provenance=reproduced,
    )
    store = _store_dir(root, create=True)
    destination = store / (
        f"{attestation.task_content_hash}."
        f"{reproduced.evidence_digest()}.generator-equivalence.json"
    )
    _publish_immutable_file(
        store,
        destination,
        attestation.deterministic_bytes(),
        conflict=f"generator equivalence destination has different bytes: {destination}",
    )
    # Re-read the whole store so a concurrent or malformed attestation cannot
    # be hidden by successful publication of this one.
    confirmed = load_current(root, task=task, required=True)
    if confirmed != authoritative:
        raise ValueError("generator equivalence changed authoritative provenance")
    return destination


def publish_implementation_equivalence(
    task_dir: Path,
    *,
    authoritative: IngestProvenance,
    reproduced: IngestProvenance,
    adapter_transition: ImplementationAdapterTransition | None = None,
) -> Path:
    """Append or confirm a v2 replay without changing provenance or lineage."""

    root = Path(task_dir)
    task = _load_task(root)
    validate_task_binding(task, authoritative)
    validate_task_binding(task, reproduced)
    current = load_for_lineage(
        root,
        task_id=task.task_id,
        lineage_hash=authoritative.task_content_hash,
        required=True,
    )
    if current != authoritative:
        raise ValueError(
            "implementation equivalence does not name authoritative provenance"
        )
    problem = implementation_equivalence_problem(
        authoritative,
        reproduced,
        adapter_transition=adapter_transition,
    )
    if problem:
        raise ValueError(f"invalid implementation equivalence: {problem}")
    attestation = ImplementationEquivalenceAttestation(
        task_id=task.task_id,
        task_content_hash=authoritative.task_content_hash,
        authoritative_evidence_digest=authoritative.evidence_digest(),
        adapter_transition=adapter_transition,
        reproduced_provenance=reproduced,
    )
    store = _store_dir(root, create=True)
    destination = store / (
        f"{attestation.task_content_hash}."
        f"{reproduced.evidence_digest()}.implementation-equivalence.json"
    )
    _publish_immutable_file(
        store,
        destination,
        attestation.deterministic_bytes(),
        conflict=(
            "implementation equivalence destination has different bytes: "
            f"{destination}"
        ),
    )
    # Re-read the whole store. This verifies both v1 and v2 history and keeps a
    # malformed concurrent sidecar from being hidden by this successful write.
    confirmed = load_current(root, task=task, required=True)
    if confirmed != authoritative:
        raise ValueError(
            "implementation equivalence changed authoritative provenance"
        )
    return destination


def _publish_immutable_file(
    store: Path,
    destination: Path,
    payload: bytes,
    *,
    conflict: str,
) -> None:
    """Hard-link exact bytes into one no-replace append-only destination."""

    try:
        existing_metadata = destination.lstat()
    except FileNotFoundError:
        existing_metadata = None
    if existing_metadata is not None:
        if stat.S_ISLNK(existing_metadata.st_mode) or not stat.S_ISREG(
            existing_metadata.st_mode
        ):
            raise ValueError(f"provenance destination is not a regular file: {destination}")
        if _read_regular_bytes(destination, label="provenance destination") != payload:
            raise ValueError(conflict)
        _set_regular_readonly(destination, label="provenance destination")
        return

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".ingest-provenance-", suffix=".tmp", dir=store.parent
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
            if (
                _read_regular_bytes(destination, label="provenance destination")
                != payload
            ):
                raise ValueError(conflict)
        _set_regular_readonly(destination, label="provenance destination")
    finally:
        temporary.unlink(missing_ok=True)


def _load_store_records(
    task_dir: Path,
    *,
    task_id: str,
    required: bool,
    verify_task_file: bool = True,
    recoverable_incomplete_claim: tuple[str, str] | None = None,
) -> list[IngestProvenance]:
    """Read and byte-validate every append-only record without choosing one."""

    root = Path(task_dir)
    safe_task_id = validate_task_id_segment(task_id)
    if verify_task_file:
        stored_task = _load_task(root)
        if stored_task.task_id != safe_task_id:
            raise ValueError(
                f"task directory contains {stored_task.task_id!r}, not {safe_task_id!r}"
            )
    store = root / INGEST_PROVENANCE_DIRNAME
    try:
        store.lstat()
    except FileNotFoundError:
        if required:
            raise ValueError(
                f"task {safe_task_id!r} has no ingest provenance; re-ingest it "
                "through the typed five-source manifest coordinator"
            )
        return []
    store = _store_dir(root, create=False)
    records: list[IngestProvenance] = []
    equivalences: list[GeneratorEquivalenceAttestation] = []
    implementation_equivalences: list[ImplementationEquivalenceAttestation] = []
    claims: dict[str, str] = {}
    for path in sorted(store.iterdir(), key=lambda item: item.name):
        metadata = path.lstat()
        match = _RECORD_FILENAME_RE.fullmatch(path.name)
        claim_match = _CLAIM_FILENAME_RE.fullmatch(path.name)
        equivalence_match = _GENERATOR_EQUIVALENCE_FILENAME_RE.fullmatch(path.name)
        implementation_equivalence_match = (
            _IMPLEMENTATION_EQUIVALENCE_FILENAME_RE.fullmatch(path.name)
        )
        if (
            match is None
            and claim_match is None
            and equivalence_match is None
            and implementation_equivalence_match is None
        ) or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"invalid entry in provenance store: {path}")
        if claim_match is not None:
            try:
                raw_claim = _read_regular_bytes(path, label="ingest provenance claim")
                claim_digest = raw_claim.decode("ascii").removesuffix("\n")
            except (OSError, UnicodeError) as exc:
                raise ValueError(f"invalid ingest provenance claim {path.name}: {exc}") from exc
            if (
                raw_claim != (claim_digest + "\n").encode("ascii")
                or _SHA256_RE.fullmatch(claim_digest) is None
            ):
                raise ValueError(
                    f"invalid ingest provenance claim bytes: {path.name}"
                )
            claims[claim_match.group(1)] = claim_digest
            continue
        if equivalence_match is not None:
            try:
                raw = _read_regular_bytes(
                    path, label="generator equivalence attestation"
                )
                attestation = GeneratorEquivalenceAttestation.model_validate_json(raw)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError(
                    f"invalid generator equivalence attestation {path.name}: {exc}"
                ) from exc
            if raw != attestation.deterministic_bytes():
                raise ValueError(
                    "generator equivalence attestation is not in deterministic "
                    f"form: {path.name}"
                )
            if attestation.task_content_hash != equivalence_match.group(1):
                raise ValueError(
                    f"generator equivalence filename/lineage mismatch: {path.name}"
                )
            if (
                attestation.reproduced_provenance.evidence_digest()
                != equivalence_match.group(2)
            ):
                raise ValueError(
                    f"generator equivalence filename/evidence mismatch: {path.name}"
                )
            if attestation.task_id != safe_task_id:
                raise ValueError(
                    f"provenance store for {safe_task_id!r} contains generator "
                    f"equivalence for {attestation.task_id!r}"
                )
            equivalences.append(attestation)
            continue
        if implementation_equivalence_match is not None:
            try:
                raw = _read_regular_bytes(
                    path, label="implementation equivalence attestation"
                )
                implementation_attestation = (
                    ImplementationEquivalenceAttestation.model_validate_json(raw)
                )
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError(
                    "invalid implementation equivalence attestation "
                    f"{path.name}: {exc}"
                ) from exc
            if raw != implementation_attestation.deterministic_bytes():
                raise ValueError(
                    "implementation equivalence attestation is not in deterministic "
                    f"form: {path.name}"
                )
            if (
                implementation_attestation.task_content_hash
                != implementation_equivalence_match.group(1)
            ):
                raise ValueError(
                    "implementation equivalence filename/lineage mismatch: "
                    f"{path.name}"
                )
            if (
                implementation_attestation.reproduced_provenance.evidence_digest()
                != implementation_equivalence_match.group(2)
            ):
                raise ValueError(
                    "implementation equivalence filename/evidence mismatch: "
                    f"{path.name}"
                )
            if implementation_attestation.task_id != safe_task_id:
                raise ValueError(
                    f"provenance store for {safe_task_id!r} contains implementation "
                    f"equivalence for {implementation_attestation.task_id!r}"
                )
            implementation_equivalences.append(implementation_attestation)
            continue
        assert match is not None
        try:
            raw = _read_regular_bytes(path, label="ingest provenance record")
            record = IngestProvenance.model_validate_json(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid ingest provenance record {path.name}: {exc}") from exc
        if raw != record.deterministic_bytes():
            raise ValueError(
                f"ingest provenance record is not in deterministic form: {path.name}"
            )
        if record.task_content_hash != match.group(1):
            raise ValueError(
                f"ingest provenance filename/lineage mismatch: {path.name}"
            )
        if record.evidence_digest() != match.group(2):
            raise ValueError(
                f"ingest provenance filename/evidence digest mismatch: {path.name}"
            )
        if record.task_id != safe_task_id:
            raise ValueError(
                f"provenance store for {safe_task_id!r} contains record for "
                f"{record.task_id!r}"
            )
        records.append(record)
    by_lineage: dict[str, list[IngestProvenance]] = {}
    for record in records:
        by_lineage.setdefault(record.task_content_hash, []).append(record)
    for lineage_hash, lineage_records in sorted(by_lineage.items()):
        if len(lineage_records) != 1:
            raise ValueError(
                f"task {safe_task_id!r} has {len(lineage_records)} competing "
                f"ingest provenance records for lineage root {lineage_hash[:12]}"
            )
        record_digest = lineage_records[0].evidence_digest()
        claim_digest = claims.get(lineage_hash)
        if claim_digest != record_digest:
            raise ValueError(
                f"ingest provenance record has no matching immutable claim: "
                f"{lineage_hash[:12]}"
            )
    incomplete_claims = sorted(set(claims) - set(by_lineage))
    if recoverable_incomplete_claim is not None:
        recoverable_lineage, recoverable_digest = recoverable_incomplete_claim
        if (
            incomplete_claims == [recoverable_lineage]
            and claims[recoverable_lineage] == recoverable_digest
        ):
            incomplete_claims = []
    if incomplete_claims:
        raise ValueError(
            "ingest provenance store contains claim(s) without records: "
            f"{[value[:12] for value in incomplete_claims]}"
        )
    for attestation in equivalences:
        lineage_records = by_lineage.get(attestation.task_content_hash, [])
        if len(lineage_records) != 1:
            raise ValueError(
                "generator equivalence has no sole authoritative provenance "
                f"record for lineage {attestation.task_content_hash[:12]}"
            )
        authoritative = lineage_records[0]
        if authoritative.evidence_digest() != attestation.authoritative_evidence_digest:
            raise ValueError(
                "generator equivalence authoritative evidence digest changed"
            )
        problem = generator_equivalence_problem(
            authoritative, attestation.reproduced_provenance
        )
        if problem:
            raise ValueError(f"invalid generator equivalence: {problem}")
    seen_implementation_equivalences: set[tuple[str, str]] = set()
    for attestation in implementation_equivalences:
        reproduced_digest = attestation.reproduced_provenance.evidence_digest()
        key = (attestation.task_content_hash, reproduced_digest)
        if key in seen_implementation_equivalences:
            raise ValueError(
                "duplicate implementation equivalence for lineage/reproduction "
                f"{attestation.task_content_hash[:12]}/{reproduced_digest[:12]}"
            )
        seen_implementation_equivalences.add(key)
        lineage_records = by_lineage.get(attestation.task_content_hash, [])
        if len(lineage_records) != 1:
            raise ValueError(
                "implementation equivalence has no sole authoritative provenance "
                f"record for lineage {attestation.task_content_hash[:12]}"
            )
        authoritative = lineage_records[0]
        if (
            authoritative.evidence_digest()
            != attestation.authoritative_evidence_digest
        ):
            raise ValueError(
                "implementation equivalence authoritative evidence digest changed"
            )
        problem = implementation_equivalence_problem(
            authoritative,
            attestation.reproduced_provenance,
            adapter_transition=attestation.adapter_transition,
        )
        if problem:
            raise ValueError(f"invalid implementation equivalence: {problem}")
    return records


def _select_lineage_record(
    records: list[IngestProvenance],
    *,
    task_id: str,
    lineage_hash: str,
    required: bool,
) -> IngestProvenance | None:
    matches = [
        record for record in records if record.task_content_hash == lineage_hash
    ]
    if not matches:
        if required:
            raise ValueError(
                f"task {task_id!r} has no ingest provenance for lineage root "
                f"{lineage_hash[:12]}"
            )
        return None
    if len(matches) != 1:
        raise ValueError(
            f"task {task_id!r} has {len(matches)} competing ingest provenance "
            f"records for lineage root {lineage_hash[:12]}"
        )
    return matches[0]


def load_for_lineage(
    task_dir: Path,
    *,
    task_id: str,
    lineage_hash: str,
    required: bool = True,
    recoverable_evidence_digest: str | None = None,
) -> IngestProvenance | None:
    """Validate and load one lineage without assuming it is current.

    ``recoverable_evidence_digest`` admits only its exact claim-only crash state.
    Malformed, ambiguous, or conflicting history fails closed.
    """

    safe_task_id = validate_task_id_segment(task_id)
    if _SHA256_RE.fullmatch(lineage_hash) is None:
        raise ValueError("lineage_hash must be a lowercase SHA-256 digest")
    if (
        recoverable_evidence_digest is not None
        and _SHA256_RE.fullmatch(recoverable_evidence_digest) is None
    ):
        raise ValueError(
            "recoverable_evidence_digest must be a lowercase SHA-256 digest"
        )
    records = _load_store_records(
        Path(task_dir),
        task_id=safe_task_id,
        required=required,
        recoverable_incomplete_claim=(
            (lineage_hash, recoverable_evidence_digest)
            if recoverable_evidence_digest is not None
            else None
        ),
    )
    return _select_lineage_record(
        records,
        task_id=safe_task_id,
        lineage_hash=lineage_hash,
        required=required,
    )


def load_current(
    task_dir: Path,
    *,
    task: TaskIR | None = None,
    required: bool = True,
) -> IngestProvenance | None:
    """Load the sole valid record for the current lineage root."""

    root = Path(task_dir)
    task = task if task is not None else _load_task(root)
    root_hash = lineage_root_hash(task)
    records = _load_store_records(
        root,
        task_id=task.task_id,
        required=required,
        verify_task_file=False,
    )
    match = _select_lineage_record(
        records,
        task_id=task.task_id,
        lineage_hash=root_hash,
        required=required,
    )
    if match is not None:
        validate_task_binding(task, match)
    return match


def release_provenance_rel(task_id: str) -> str:
    """Canonical release-relative path of one frozen provenance sidecar."""

    safe_task_id = validate_task_id_segment(task_id)
    return (
        f"private/{safe_task_id}/{RELEASE_PROVENANCE_DIRNAME}/"
        f"{RELEASE_PROVENANCE_FILENAME}"
    )

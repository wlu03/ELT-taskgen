"""Load and identity-bind a private semantic package from a frozen release."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from elt_taskgen.export import release as release_mod
from elt_taskgen.models import PopulationName, TaskIR, task_from_json
from elt_taskgen.reference.gold import GoldBundle, load_gold
from elt_taskgen.semantic_contract import SEMANTIC_SCORER_VERSION
from elt_taskgen.verification.gates import GRADED_POPULATIONS


class SemanticPackageError(RuntimeError):
    """A release cannot safely serve as a semantic scoring package."""


class SemanticPackage(BaseModel):
    """Verified evaluator-private inputs for one combined public task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    release_dir: Path
    manifest: release_mod.ReleaseManifest
    task: TaskIR
    gold: GoldBundle
    source_roots: dict[str, Path]

    def source_root(self, population: PopulationName | str) -> Path:
        name = population.value if isinstance(population, PopulationName) else str(population)
        try:
            return self.source_roots[name]
        except KeyError as exc:
            raise SemanticPackageError(
                f"semantic package has no source root for population {name!r}"
            ) from exc


def _schema_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(version).split("."))
    except ValueError:
        return (-1,)


def _confined_directory(release_dir: Path, task_id: str, rel: str) -> Path:
    """Resolve a manifest path inside this task's private tree."""
    if Path(rel).is_absolute():
        raise SemanticPackageError(f"source root for {task_id!r} is absolute")
    release_root = release_dir.resolve()
    private_root = (release_root / "private" / task_id).resolve()
    candidate = release_root / rel
    cursor = release_root
    for part in Path(rel).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise SemanticPackageError(
                f"source root for {task_id!r} traverses a symlink"
            )
    path = candidate.resolve()
    if path != private_root and private_root not in path.parents:
        raise SemanticPackageError(
            f"source root for {task_id!r} escapes its private task tree"
        )
    if not path.is_dir():
        raise SemanticPackageError(
            f"source root for {task_id!r} is missing or is not a directory"
        )
    return path


def _reject_symlinks(root: Path, what: str) -> None:
    if root.is_symlink():
        raise SemanticPackageError(f"{what} is a symlink")
    if root.is_dir() and any(path.is_symlink() for path in root.rglob("*")):
        raise SemanticPackageError(f"{what} contains a symlink")


def load_semantic_package(
    release_dir: Path,
    task_id: str,
    *,
    verify: bool = True,
) -> SemanticPackage:
    """Load a schema-3.2 task after verifying private replay inputs.

    Do not reconstruct TaskIR, gold, or sources from public descriptions.
    """
    release_dir = Path(release_dir).resolve()
    manifest_path = release_dir / "release_manifest.json"
    if not manifest_path.is_file():
        raise SemanticPackageError(f"no release manifest at {manifest_path}")
    try:
        manifest = release_mod.ReleaseManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise SemanticPackageError("release manifest is invalid") from exc
    if (
        manifest.corpus_profile != release_mod.COMBINED_CORPUS_PROFILE
        or manifest.public_layout != release_mod.COMBINED_PUBLIC_LAYOUT
    ):
        raise SemanticPackageError(
            "semantic scoring requires a combined ELT-Bench release"
        )
    if _schema_tuple(manifest.schema_version) < _schema_tuple(
        release_mod.SEMANTIC_PACKAGE_MIN_SCHEMA
    ):
        raise SemanticPackageError(
            "semantic scoring requires release schema "
            f"{release_mod.SEMANTIC_PACKAGE_MIN_SCHEMA} or newer"
        )
    if manifest.semantic_scorer_version != SEMANTIC_SCORER_VERSION:
        raise SemanticPackageError(
            "release semantic scorer version is not supported by this build"
        )
    if task_id not in manifest.tasks:
        raise SemanticPackageError(
            f"release holds no task {task_id!r} (has: {sorted(manifest.tasks)})"
        )
    if verify:
        verification = release_mod.verify_release(release_dir)
        if not verification.ok:
            raise SemanticPackageError(
                "release verification failed; refusing semantic scoring"
            )

    task_path = (
        release_dir / "private" / task_id / release_mod.SEMANTIC_TASK_IR_REL
    )
    _reject_symlinks(task_path, "private semantic TaskIR")
    if not task_path.is_file():
        raise SemanticPackageError("private semantic TaskIR is missing")
    try:
        task = task_from_json(task_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SemanticPackageError("private semantic TaskIR is invalid") from exc
    if task.task_id != task_id:
        raise SemanticPackageError("private semantic TaskIR has the wrong task_id")
    expected_hash = manifest.tasks[task_id]
    if task.content_hash() != expected_hash:
        raise SemanticPackageError(
            "private semantic TaskIR does not match the release task hash"
        )

    answer_key_dir = release_dir / "private" / task_id / "answer_key"
    _reject_symlinks(answer_key_dir, "private semantic gold")
    try:
        gold = load_gold(answer_key_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise SemanticPackageError("private semantic gold is missing or invalid") from exc
    if gold.task_id != task_id:
        raise SemanticPackageError("private gold has the wrong task_id")
    if gold.task_content_hash != expected_hash:
        raise SemanticPackageError(
            "private gold and semantic TaskIR do not share one content hash"
        )

    expected_tables = {table.name for table in task.tables}
    expected_marts = {mart.name for mart in task.marts}
    required_populations = {population.value for population in GRADED_POPULATIONS}
    declared_populations = {population.name.value for population in task.populations}
    if declared_populations:
        required_populations |= declared_populations
    roots: dict[str, Path] = {}
    source_map = manifest.el_sources.get(task_id) or {}
    for population in sorted(required_populations):
        if set(gold.stage1.get(population) or {}) != expected_tables:
            raise SemanticPackageError(
                f"population {population!r} has incomplete stage-1 gold"
            )
        if set(gold.stage2_csv.get(population) or {}) != expected_marts:
            raise SemanticPackageError(
                f"population {population!r} has incomplete stage-2 gold"
            )
        rel = source_map.get(population)
        if not rel:
            raise SemanticPackageError(
                f"population {population!r} has no released source root"
            )
        root = _confined_directory(release_dir, task_id, rel)
        _reject_symlinks(root, f"population {population!r} source root")
        roots[population] = root

    return SemanticPackage(
        release_dir=release_dir,
        manifest=manifest,
        task=task,
        gold=gold,
        source_roots=dict(sorted(roots.items())),
    )

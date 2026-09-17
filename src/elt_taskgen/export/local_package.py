"""Immutable, self-verifying evaluator packages for locally accepted tasks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from elt_taskgen.engine import Engine
from elt_taskgen.models import (
    RLVR_TASK_VARIANTS,
    TaskIR,
    canonical_json,
    task_from_json,
    validate_task_id_segment,
    variant_task_id,
)


LOCAL_PACKAGE_SCHEMA_VERSION = "local-evaluator-package-v1"
LOCAL_PACKAGE_MANIFEST = "package_manifest.json"


class LocalPackageManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["local-evaluator-package-v1"] = (
        LOCAL_PACKAGE_SCHEMA_VERSION
    )
    task_id: str
    task_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    variants: tuple[str, ...]
    files: dict[str, str]

    @field_validator("task_id")
    @classmethod
    def _safe_task_id(cls, value: str) -> str:
        return validate_task_id_segment(value)

    @field_validator("files")
    @classmethod
    def _safe_file_inventory(cls, value: dict[str, str]) -> dict[str, str]:
        for rel, digest in value.items():
            parsed = PurePosixPath(rel)
            if (
                parsed.is_absolute()
                or not parsed.parts
                or any(part in {"", ".", ".."} for part in parsed.parts)
                or "\\" in rel
            ):
                raise ValueError(f"unsafe package manifest path: {rel!r}")
            if (
                len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(
                    f"invalid sha256 digest for package file {rel!r}"
                )
        return value


class PackageVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    files_checked: int = 0
    failures: tuple[str, ...] = ()
    task_id: str = ""
    task_content_hash: str = ""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_inventory(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"package contains a symlink: {rel}")
        if path.is_dir():
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"package contains a non-regular file: {rel}")
        if rel != LOCAL_PACKAGE_MANIFEST:
            result[rel] = _sha(path)
    return result


def _copy_regular_tree(source: Path, destination: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise FileNotFoundError(f"required package input is not a directory: {source}")
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"package input contains a symlink: {source / rel}")
        target = destination / rel
        if stat.S_ISDIR(metadata.st_mode):
            target.mkdir(parents=True, exist_ok=True)
        elif stat.S_ISREG(metadata.st_mode):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        else:
            raise ValueError(f"package input contains a special file: {source / rel}")


def _copy_variant_private(source: Path, destination: Path) -> None:
    """Copy a measured variant's private files, excluding its public task tree."""

    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if rel.parts and rel.parts[0] == "task":
            continue
        metadata = path.lstat()
        target = destination / rel
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"variant bundle contains a symlink: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            target.mkdir(parents=True, exist_ok=True)
        elif stat.S_ISREG(metadata.st_mode):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        else:
            raise ValueError(f"variant bundle contains a special file: {path}")


def _assert_no_original_workspace_paths(root: Path, workspace: Path) -> None:
    needle = str(workspace.resolve())
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 8_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if needle in text:
            raise ValueError(
                f"package file {path.relative_to(root)} contains the original "
                "workspace absolute path"
            )


def _assert_exact_layout(
    package: Path, manifest: LocalPackageManifest, task: TaskIR
) -> None:
    """Refuse undeclared units and keep every evaluator artifact private."""

    expected_top = {LOCAL_PACKAGE_MANIFEST, "public", "private"}
    observed_top = {path.name for path in package.iterdir()}
    if observed_top != expected_top:
        raise ValueError(
            "package top-level layout differs from the exact public/private "
            f"contract (missing={sorted(expected_top - observed_top)}, "
            f"unexpected={sorted(observed_top - expected_top)})"
        )

    public_root = package / "public"
    private_root = package / "private"
    if (
        public_root.is_symlink()
        or not public_root.is_dir()
        or private_root.is_symlink()
        or not private_root.is_dir()
    ):
        raise ValueError("package public/private roots must be real directories")

    expected_public = {
        task.task_id,
        *(variant_task_id(task.task_id, variant) for variant in RLVR_TASK_VARIANTS),
    }
    observed_public = {path.name for path in public_root.iterdir()}
    if observed_public != expected_public or any(
        not path.is_dir() or path.is_symlink() for path in public_root.iterdir()
    ):
        raise ValueError(
            "package public unit roster differs from the parent plus exact EL/T "
            f"roster (missing={sorted(expected_public - observed_public)}, "
            f"unexpected={sorted(observed_public - expected_public)})"
        )

    observed_private = {path.name for path in private_root.iterdir()}
    if observed_private != {manifest.task_id} or any(
        not path.is_dir() or path.is_symlink() for path in private_root.iterdir()
    ):
        raise ValueError(
            "package private roster must contain exactly the parent task "
            f"(found={sorted(observed_private)})"
        )


def _assert_canonical_reachability(
    task: TaskIR, public_parent: Path, private_parent: Path
) -> None:
    """Require a current canonical solution linked to its packaged bytes."""
    from elt_taskgen.training import canonical

    del public_parent  # the bundle-root destination comes from the private contract
    canonical.assert_canonical_ready(task, private_parent / "answer_key")


def _validate_gold_and_populations(
    task: TaskIR, private_parent: Path
) -> object:
    from elt_taskgen.generation import source_data
    from elt_taskgen.reference import gold as gold_mod

    gold = gold_mod.load_gold(private_parent / "answer_key")
    if gold.task_id != task.task_id:
        raise ValueError("private gold task_id differs from the package TaskIR")
    if gold.task_content_hash != task.content_hash():
        raise ValueError("private gold is bound to a different task identity")

    populations = {spec.name.value for spec in task.populations}
    if set(gold.stage1) != populations or set(gold.stage2_csv) != populations:
        raise ValueError("private gold does not cover the exact TaskIR populations")
    tables = {table.name for table in task.tables}
    marts = {mart.name for mart in task.marts}
    for population in sorted(populations):
        if set(gold.stage1[population]) != tables:
            raise ValueError(
                f"private gold table roster differs on population {population!r}"
            )
        if set(gold.stage2_csv[population]) != marts:
            raise ValueError(
                f"private gold mart roster differs on population {population!r}"
            )

    population_root = private_parent / "populations"
    if not population_root.is_dir() or population_root.is_symlink():
        raise ValueError("private population root is missing or unsafe")
    observed = {path.name for path in population_root.iterdir()}
    if observed != populations or any(
        not path.is_dir() or path.is_symlink() for path in population_root.iterdir()
    ):
        raise ValueError(
            "private population roster differs from the TaskIR "
            f"(missing={sorted(populations - observed)}, "
            f"unexpected={sorted(observed - populations)})"
        )
    for spec in task.populations:
        source_data.verify_population_manifest(
            task, spec.name, population_root / spec.name.value
        )
    return gold


def _validate_reward_manifests(
    task: TaskIR, gold: object, private_parent: Path
) -> None:
    from elt_taskgen.export import eltbench

    for variant in RLVR_TASK_VARIANTS:
        path = private_parent / "variants" / variant.value / "reward.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"private {variant.value} reward manifest is invalid: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(
                f"private {variant.value} reward manifest must be an object"
            )
        relations = payload.get("population_relations")
        memorization = payload.get("memorization_evidence")
        if relations is not None and not isinstance(relations, dict):
            raise ValueError("reward population_relations must be an object")
        if memorization is not None and not isinstance(memorization, dict):
            raise ValueError("reward memorization_evidence must be an object")
        expected = eltbench.reward_manifest(
            task,
            gold,
            variant,
            population_relations=relations,
            memorization_evidence=memorization,
        )
        if payload != expected:
            raise ValueError(
                f"private {variant.value} reward manifest differs from the "
                "TaskIR and frozen gold"
            )


def verify_local_package(root: Path) -> PackageVerification:
    """Verify one package without consulting the workspace it came from."""

    # ``resolve`` would hide a symlink at the package root before the lstat.
    package = Path(root).absolute()
    failures: list[str] = []
    manifest: LocalPackageManifest | None = None
    try:
        metadata = package.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("package root is not a non-symlink directory")
        manifest_path = package / LOCAL_PACKAGE_MANIFEST
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("package manifest is missing or unsafe")
        manifest = LocalPackageManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        observed = _file_inventory(package)
        missing = sorted(set(manifest.files) - set(observed))
        extra = sorted(set(observed) - set(manifest.files))
        changed = sorted(
            rel
            for rel in set(observed) & set(manifest.files)
            if observed[rel] != manifest.files[rel]
        )
        if missing:
            failures.append(f"missing files: {missing}")
        if extra:
            failures.append(f"undeclared files: {extra}")
        if changed:
            failures.append(f"digest mismatches: {changed}")

        public_parent = package / "public" / manifest.task_id
        private_parent = package / "private" / manifest.task_id
        task_ir_path = private_parent / "task_ir.json"
        task = task_from_json(task_ir_path.read_text(encoding="utf-8"))
        if task.task_id != manifest.task_id:
            failures.append("private TaskIR task_id differs from package manifest")
        if task.content_hash() != manifest.task_content_hash:
            failures.append("private TaskIR content hash differs from package manifest")
        _assert_exact_layout(package, manifest, task)
        from elt_taskgen.export import eltbench

        for public in (
            public_parent,
            *(
                package / "public" / variant_task_id(task.task_id, variant)
                for variant in RLVR_TASK_VARIANTS
            ),
        ):
            eltbench.assert_public_tree_clean(task, public)
        gold = _validate_gold_and_populations(task, private_parent)
        _validate_reward_manifests(task, gold, private_parent)
        _assert_canonical_reachability(task, public_parent, private_parent)
        expected_variants = tuple(variant.value for variant in RLVR_TASK_VARIANTS)
        if manifest.variants != expected_variants:
            failures.append("package does not contain the exact accepted EL/T roster")
    except (OSError, TypeError, ValueError) as exc:
        failures.append(str(exc))
    return PackageVerification(
        ok=not failures,
        files_checked=(len(manifest.files) if manifest is not None else 0),
        failures=tuple(failures),
        task_id=(manifest.task_id if manifest is not None else ""),
        task_content_hash=(
            manifest.task_content_hash if manifest is not None else ""
        ),
    )


def freeze_local_package(
    engine: Engine, task: TaskIR, out_dir: Path
) -> LocalPackageManifest:
    """Create or adopt an immutable local evaluator package for one task.

    Only a current accepted EL/T pair may be packaged.  The source populations,
    gold and variant reward assets are private; solver-visible trees contain
    only the already measured public bundles.
    """

    from elt_taskgen.export import release as release_mod

    acceptances = release_mod.variant_acceptance(engine, task)
    refused = [
        variant.value
        for variant in RLVR_TASK_VARIANTS
        if not acceptances[variant.value].accepted
    ]
    if refused:
        raise ValueError(
            f"task {task.task_id!r} is not locally evaluator-ready; refused "
            f"variants: {refused}"
        )

    # Preserve the final path itself for symlink refusal; resolving here would
    # silently adopt a package reached through a link.
    destination = Path(out_dir).absolute()
    if destination.is_symlink():
        raise ValueError(
            f"local package destination must not be a symlink: {destination}"
        )
    if destination.exists():
        verification = verify_local_package(destination)
        if (
            verification.ok
            and verification.task_id == task.task_id
            and verification.task_content_hash == task.content_hash()
        ):
            return LocalPackageManifest.model_validate_json(
                (destination / LOCAL_PACKAGE_MANIFEST).read_text(encoding="utf-8")
            )
        raise ValueError(
            f"local package destination already exists and is not the same "
            f"verified immutable package: {destination}: "
            + "; ".join(verification.failures[:3])
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    tdir = engine.task_dir(task.task_id)
    with tempfile.TemporaryDirectory(
        dir=destination.parent, prefix=f".{task.task_id}.package-"
    ) as temporary:
        stage = Path(temporary) / "package"
        public_root = stage / "public"
        private_parent = stage / "private" / task.task_id
        public_root.mkdir(parents=True)
        private_parent.mkdir(parents=True)

        _copy_regular_tree(tdir / "task", public_root / task.task_id)
        _copy_regular_tree(tdir / "answer_key", private_parent / "answer_key")
        _copy_regular_tree(tdir / "populations", private_parent / "populations")
        (private_parent / "task_ir.json").write_text(
            canonical_json(task.model_dump(mode="json")) + "\n", encoding="utf-8"
        )
        for variant in RLVR_TASK_VARIANTS:
            source = tdir / "variants" / variant.value
            _copy_regular_tree(
                source / "task", public_root / variant_task_id(task.task_id, variant)
            )
            _copy_variant_private(
                source, private_parent / "variants" / variant.value
            )

        _assert_no_original_workspace_paths(stage, engine.workspace)
        manifest = LocalPackageManifest(
            task_id=task.task_id,
            task_content_hash=task.content_hash(),
            variants=tuple(variant.value for variant in RLVR_TASK_VARIANTS),
            files=_file_inventory(stage),
        )
        (stage / LOCAL_PACKAGE_MANIFEST).write_text(
            json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        check = verify_local_package(stage)
        if not check.ok:
            raise ValueError(
                "new local evaluator package failed verification: "
                + "; ".join(check.failures[:3])
            )
        os.replace(stage, destination)
        stage = None  # type: ignore[assignment]

    final = verify_local_package(destination)
    if not final.ok:
        raise ValueError(
            "published local evaluator package failed verification: "
            + "; ".join(final.failures[:3])
        )
    return manifest


__all__ = [
    "LOCAL_PACKAGE_MANIFEST",
    "LOCAL_PACKAGE_SCHEMA_VERSION",
    "LocalPackageManifest",
    "PackageVerification",
    "freeze_local_package",
    "verify_local_package",
]

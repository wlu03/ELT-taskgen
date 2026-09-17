"""Build hook for immutable non-Python runtime resources.

The files stay in their operator-friendly repository locations.  A wheel gets
an exact private copy under ``elt_taskgen/_resources`` so installed commands
do not depend on a source checkout.  The runtime image manifest is the single
reviewed allowlist for image inputs; local virtualenvs and build debris can
therefore never enter a distribution accidentally.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath

# Reproducible by default while still honoring an explicit release epoch.
os.environ.setdefault("SOURCE_DATE_EPOCH", "946684800")  # 2000-01-01 UTC

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist


ROOT = Path(__file__).resolve().parent
RUNNER_MANIFEST = ROOT / "runtime-images" / "manifest.json"
CONFIG_FILES = (
    "agents.yaml",
    "dlt_connectors/airtable.yaml",
    "dlt_connectors/chess.yaml",
    "dlt_connectors/freshdesk.yaml",
    "dlt_connectors/google_analytics.yaml",
    "dlt_connectors/matomo.yaml",
    "dlt_connectors/mux.yaml",
    "dlt_connectors/notion.yaml",
    "dlt_connectors/personio.yaml",
    "dlt_connectors/pipedrive.yaml",
    "dlt_connectors/slack.yaml",
    "dlt_connectors/strapi.yaml",
    "dlt_connectors/workable.yaml",
    "filters.yaml",
    "five_source_ingest.example.yaml",
    "generation_run.example.yaml",
    "sources.yaml",
    "wikidbs_family_map.csv.gz",
)
TOOLS = (
    "build_dbt_manifest.py",
    "parity_sample.py",
    "schemapile_index.py",
    "wikidbs_family_map.py",
)
RUNNER_IMAGE_KEYS = (
    "dbt:databricks",
    "dbt:redshift",
    "dbt:snowflake",
    "terraform",
)
PINNED_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one JSON object while rejecting ambiguous duplicate names."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(f"runtime image manifest has duplicate key {key!r}")
        result[key] = value
    return result


def _require_regular_resource(path: Path, *, root: Path = ROOT) -> None:
    """Reject missing, non-regular, escaped, or symlinked package inputs."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        raise RuntimeError(
            f"package resource is outside the source tree: {path}"
        ) from None
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError:
            raise RuntimeError(
                f"required package resource is missing: {path}"
            ) from None
        if stat.S_ISLNK(mode):
            raise RuntimeError(f"package resource must not be a symlink: {path}")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(mode):
            raise RuntimeError(
                f"package resource parent is not a directory: {current}"
            )
    if not relative.parts or not stat.S_ISREG(mode):
        raise RuntimeError(f"package resource must be a regular file: {path}")


def _safe_manifest_files() -> tuple[Path, ...]:
    _require_regular_resource(RUNNER_MANIFEST)
    try:
        payload = json.loads(
            RUNNER_MANIFEST.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("runtime image manifest is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "files",
        "images",
    }:
        raise RuntimeError("runtime image manifest has unexpected keys")
    if payload["schema_version"] != "runner-images-manifest-v2":
        raise RuntimeError("runtime image manifest has an unsupported schema")
    images = payload["images"]
    if (
        not isinstance(images, dict)
        or tuple(images) != RUNNER_IMAGE_KEYS
        or any(
            not isinstance(value, str) or PINNED_IMAGE.fullmatch(value) is None
            for value in images.values()
        )
    ):
        raise RuntimeError(
            "runtime image manifest must pin the complete sorted runner image set"
        )
    raw_files = payload["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise RuntimeError("runtime image manifest files must be a non-empty list")
    if any(not isinstance(raw, str) for raw in raw_files):
        raise RuntimeError("runtime image manifest paths must be strings")
    if raw_files != sorted(raw_files) or len(raw_files) != len(set(raw_files)):
        raise RuntimeError("runtime image manifest files must be sorted and unique")
    result: list[Path] = []
    resolved_root = RUNNER_MANIFEST.parent.resolve()
    for raw in raw_files:
        posix = PurePosixPath(raw)
        if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
            raise RuntimeError("runtime image manifest contains an unsafe path")
        path = RUNNER_MANIFEST.parent.joinpath(*posix.parts)
        _require_regular_resource(path)
        try:
            path.resolve(strict=True).relative_to(resolved_root)
        except (OSError, ValueError):
            raise RuntimeError(
                f"runtime image manifest file is missing or unsafe: {raw}"
            ) from None
        result.append(path)
    return tuple(result)


def _resource_files() -> tuple[tuple[Path, Path], ...]:
    files: list[tuple[Path, Path]] = [
        (ROOT / ".dockerignore", Path(".dockerignore")),
        (ROOT / "uv.lock", Path("uv.lock")),
        (RUNNER_MANIFEST, Path("runtime-images/manifest.json")),
    ]
    files.extend(
        (path, Path("runtime-images") / path.relative_to(RUNNER_MANIFEST.parent))
        for path in _safe_manifest_files()
    )
    files.extend(
        (ROOT / "config" / name, Path("config") / name)
        for name in CONFIG_FILES
    )
    files.extend(
        (ROOT / "tools" / name, Path("tools") / name) for name in TOOLS
    )
    for source, _ in files:
        _require_regular_resource(source)
    destinations = [destination.as_posix() for _, destination in files]
    if len(destinations) != len(set(destinations)):
        raise RuntimeError("package resource destinations are not unique")
    return tuple(files)


class build_py(_build_py):
    """Copy the reviewed resource closure into the wheel build tree."""

    def run(self) -> None:
        super().run()
        destination_root = Path(self.build_lib) / "elt_taskgen" / "_resources"
        if destination_root.exists():
            shutil.rmtree(destination_root)
        for source, relative in _resource_files():
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)


class sdist(_sdist):
    """Refuse unsafe package resources before assembling a source archive."""

    def run(self) -> None:
        _resource_files()
        super().run()


setup(cmdclass={"build_py": build_py, "sdist": sdist})

"""Turnkey `manifest.json` production for a VENDORED Fivetran dbt package.

WHY THIS EXISTS
`adapters/dbt.py` ingests a compiled dbt `manifest.json`, but nothing in this
repo produced one: the maintainer had to hand-build a dbt project, invent a
profile, run `dbt deps` + `dbt parse` and remember not to leave build droppings
inside the vendored tree. That manual step is why 16 of the 18 vendored
Fivetran packages were unreachable. This module is that step, mechanized and
fail-closed, so `elt-taskgen ingest-dbt --package dbt_servicenow` is one
command.

WHICH PROJECT GETS PARSED (the choice is explicit, never guessed silently)
A vendored `dbt_<family>/` is a dbt PACKAGE, not a runnable project:

  * `integration_tests/` (DEFAULT, `ProjectChoice.INTEGRATION_TESTS`) is the
    runnable project Fivetran ships for CI. It declares `profile:
    integration_tests`, depends on the package itself via `packages.yml`
    (`- local: ../`), and pins the source identifiers its seeds provide. Its
    manifest therefore contains the package's models under the package's own
    `package_name`, every `source.<pkg>.…` entry the models reference, and the
    schema tests — exactly the graph `adapters/dbt.py` reads. It is also the
    project that can later be RUN (seeds exist), which is why it is the
    default.
  * `package` (`ProjectChoice.PACKAGE`) parses `dbt_<family>/` itself. dbt has
    no `profile:` key there, so the profile name is the project name. This mode
    exists for packages that ship no `integration_tests/`; it produces the same
    node graph when it works, but it has no seeds and some packages reference
    integration-test-only vars.
  * `auto` (DEFAULT for the CLI) = integration_tests when that directory holds
    a `dbt_project.yml`, else the package project. The choice actually used is
    recorded on `ManifestBuild.project_choice` — the caller always learns which
    project the manifest came from.

The VENDORED TREE IS NEVER WRITTEN TO. The package is mirrored into a build
root (default `<workspace>/dbt_builds/<package>/package/`) and dbt runs there,
so `package-lock.yml`, `dbt_packages/`, `logs/` and `target/` all land in the
build copy. `assert_vendored_untouched` makes that a checkable property, and a
rebuild preserves the mirrored `dbt_packages/` so only the FIRST build of a
package needs the network (`dbt deps` fetches from hub.getdbt.com; every later
build is offline).

PARSE IS NOT ENOUGH — WHY `dbt compile` RUNS TOO
`dbt parse` populates `raw_code` and leaves `compiled_code` NULL. A Fivetran
model's `raw_code` is Jinja (`{{ ref('stg_x') }}`, `{{ fivetran_utils.… }}`,
`{% if %}`), so `adapters/dbt.py::recover_projections` — the one place the
pool's REAL transformation logic can be read — got nothing back from it:
measured on `dbt_servicenow`, 0 of 7 terminal marts parsed and 0 aggregate
expressions were recovered, while the same 7 models carry 24 aggregates, 17 of
them `count(case when … end)` filtered aggregates. `dbt compile` renders the
Jinja and fills `compiled_code`; recovery then reads real SQL. Compile needs a
resolvable connection, which the generated duckdb profile already supplies, and
it never materialises anything (no `dbt run`), so the build stays read-only
apart from the build copy's own `target/`.

`dbt seed` is deliberately NOT run. It rewrites `target/manifest.json` with the
models UNCOMPILED (that is how this was first got wrong), and the only models it
would change are the `*_base` union shims, whose raw code is pure Jinja and
whose compiled form carries no aliases either way. Seeding buys nothing and
costs the compiled manifest.

FAIL CLOSED
  * a package the source catalog EXCLUDES is refused before anything is copied
    — the catalog is the one authority (the dbt pool excludes nothing;
    the dlt pool still lists 26);
  * a missing package dir, a missing dbt interpreter, a nonzero `dbt deps` with
    no cached `dbt_packages/`, a nonzero `dbt parse`, or a parse that produced
    no `target/manifest.json` all raise `ManifestBuildError` with the captured
    dbt output tail. A half-built manifest is never returned.
  * a nonzero `dbt compile`, or a compile that left every model uncompiled,
    raises too. `allow_parse_fallback=True` downgrades that to a RECORDED
    degradation (`ManifestBuild.compiled=False` + `compile_error`) and restores
    the parse-only manifest — never a silently half-compiled one.

DETERMINISM
The manifest dbt emits carries wall-clock metadata (`generated_at`,
`invocation_id`) — it is a BUILD INTERMEDIATE, never a task artifact, and no
byte of it is copied into a TaskIR. What must be stable is its SEMANTIC
content, so `manifest_fingerprint()` hashes the `CandidateSpec` the adapter
parses out of it (canonical JSON, no clock), and that is what the idempotence
test compares across rebuilds. `profiles.yml` is written with
`yaml.safe_dump(sort_keys=True)`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from elt_taskgen.catalog import PINNED_ROOTS, PoolSource, SourceCatalog, load_source_catalog
from elt_taskgen.models import canonical_json, sha256_hex

#: The dbt that can actually import `dbt.cli.main`. The console script in this
#: foreign venv has a broken shebang, so dbt is always invoked as `-m`.
_DISCOVERED_DATA_ROOT = PINNED_ROOTS.get("ELT_TASKGEN_DATA_ROOT", "")
DEFAULT_DBT_PYTHON = (
    str(Path(_DISCOVERED_DATA_ROOT) / ".venv" / "bin" / "python3")
    if _DISCOVERED_DATA_ROOT
    else ""
)
#: Override for the interpreter above (a machine with its own dbt).
DBT_PYTHON_ENV = "ELT_TASKGEN_DBT_PYTHON"

#: Build-copy paths that a re-mirror must PRESERVE: dbt's own outputs and the
#: fetched package cache (deleting the cache would force a network round trip).
_MIRROR_PRESERVE = frozenset({"dbt_packages", "target", "logs", "package-lock.yml"})

MANIFEST_RELPATH = Path("target") / "manifest.json"
#: Where the parse-only manifest is kept while `dbt compile` runs, so a compile
#: failure can be downgraded to a recorded degradation instead of losing the
#: manifest entirely. A build INTERMEDIATE like the manifest itself.
PARSE_BACKUP_RELPATH = Path("target") / "manifest.parse.json"
PROFILES_FILENAME = "profiles.yml"
#: Target name inside the generated profile. `dbt parse` never connects, but a
#: profile must still resolve, and duckdb is the one adapter installed.
PROFILE_TARGET = "duckdb"


class ManifestBuildError(RuntimeError):
    """A manifest could not be produced. Never raised with a partial result."""


class ProjectChoice(str, Enum):
    AUTO = "auto"
    INTEGRATION_TESTS = "integration_tests"
    PACKAGE = "package"


class ManifestBuild(BaseModel):
    """Everything the caller needs about one produced manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Vendored record name, e.g. 'dbt_servicenow'.
    package: str = Field(min_length=1)
    #: `name:` of the PACKAGE's dbt_project.yml, e.g. 'servicenow'. This is the
    #: family stem — never the integration-tests project name.
    package_project_name: str = Field(min_length=1)
    #: Which project was parsed (never AUTO: the resolved choice is recorded).
    project_choice: ProjectChoice
    vendored_dir: str
    build_root: str
    project_dir: str
    manifest_path: str
    #: sha256 of the CandidateSpec parsed out of the manifest (clock-free).
    fingerprint: str = Field(min_length=1)
    #: False when an existing manifest was reused (no dbt invocation).
    rebuilt: bool
    #: True when `dbt deps` ran in this call (False on a reuse or --no-deps).
    deps_ran: bool
    #: True when the manifest carries rendered `compiled_code` for its models —
    #: the precondition for recovering the package's real transformation logic.
    compiled: bool = False
    #: models total / models carrying compiled_code, measured on the manifest.
    model_count: int = 0
    compiled_model_count: int = 0
    #: Non-empty ONLY on a recorded degradation (`allow_parse_fallback`): the
    #: captured reason `dbt compile` did not produce a compiled manifest.
    compile_error: str = ""


# ---------------------------------------------------------------------------
# Catalog-gated package resolution
# ---------------------------------------------------------------------------

def pool_source(pool: str = "dbt", *, catalog: SourceCatalog | None = None) -> PoolSource:
    cat = catalog if catalog is not None else load_source_catalog()
    return cat.pool(pool)


def resolve_package_dir(
    package: str,
    *,
    pool: str = "dbt",
    root: Path | None = None,
    catalog: SourceCatalog | None = None,
) -> Path:
    """Vendored directory of `package`, EXCLUSION-CHECKED against the catalog.

    Fail closed twice: an excluded record raises before any filesystem work
    (the catalog's own message), and a package that is not on disk (or holds no
    `dbt_project.yml` — the empty placeholder shape) raises with the path.
    """
    cat = catalog if catalog is not None else load_source_catalog()
    src = cat.pool(pool)
    if package in src.excluded:
        raise ManifestBuildError(
            f"package {package!r} is EXCLUDED by the source catalog "
            f"(pool {src.pool!r}, {cat.config_source}); it is a vendored "
            "placeholder with no usable model graph and must not be ingested. "
            "Remove it from `excluded:` only with a licensing decision on record."
        )
    base = Path(root) if root is not None else src.root_path()
    pkg_dir = base / package
    if not pkg_dir.is_dir():
        raise ManifestBuildError(f"vendored package not found: {pkg_dir}")
    if not (pkg_dir / "dbt_project.yml").is_file():
        raise ManifestBuildError(
            f"{pkg_dir} has no dbt_project.yml — not a dbt package (fail closed)"
        )
    return pkg_dir


def _project_name(project_dir: Path) -> tuple[str, str]:
    """(project name, profile name) from a dbt_project.yml."""
    doc = yaml.safe_load((project_dir / "dbt_project.yml").read_text(encoding="utf-8")) or {}
    name = str(doc.get("name") or "").strip()
    if not name:
        raise ManifestBuildError(f"{project_dir}/dbt_project.yml declares no name")
    profile = str(doc.get("profile") or name).strip()
    return name, profile


def resolve_project_choice(pkg_dir: Path, choice: ProjectChoice) -> ProjectChoice:
    """Turn AUTO into the concrete project, and validate an explicit one."""
    has_it = (pkg_dir / "integration_tests" / "dbt_project.yml").is_file()
    if choice is ProjectChoice.AUTO:
        return ProjectChoice.INTEGRATION_TESTS if has_it else ProjectChoice.PACKAGE
    if choice is ProjectChoice.INTEGRATION_TESTS and not has_it:
        raise ManifestBuildError(
            f"{pkg_dir}/integration_tests has no dbt_project.yml; re-run with "
            "--project package"
        )
    return choice


# ---------------------------------------------------------------------------
# Build tree
# ---------------------------------------------------------------------------

def _mirror(src: Path, dst: Path) -> None:
    """Copy `src` onto `dst`, preserving dbt's own build outputs in `dst`.

    Not `copytree(dirs_exist_ok=True)` alone: files deleted upstream must also
    disappear from the copy, but `dbt_packages/`, `target/`, `logs/` and
    `package-lock.yml` are BUILD state that only exists in the copy and must
    survive (dropping `dbt_packages/` would force a network fetch).
    """
    dst.mkdir(parents=True, exist_ok=True)
    src_entries = {p.name for p in src.iterdir()}
    for existing in dst.iterdir():
        if existing.name in _MIRROR_PRESERVE or existing.name in src_entries:
            continue
        if existing.is_dir() and not existing.is_symlink():
            shutil.rmtree(existing)
        else:
            existing.unlink()
    for entry in sorted(src.iterdir(), key=lambda p: p.name):
        target = dst / entry.name
        if entry.is_dir() and not entry.is_symlink():
            _mirror(entry, target)
        else:
            shutil.copy2(entry, target)


def write_profiles(build_root: Path, profile_names: list[str]) -> Path:
    """Minimal duckdb profile covering every profile name the build may need.

    `dbt parse` does not connect, but it DOES resolve the profile (and renders
    `target.database` / `target.type` inside Fivetran source yml). One duckdb
    output under every candidate profile name is the smallest thing that
    resolves for both project choices.
    """
    output = {
        "type": "duckdb",
        "path": str(build_root / "warehouse.duckdb"),
        "schema": "main",
        "threads": 1,
    }
    doc = {
        name: {"target": PROFILE_TARGET, "outputs": {PROFILE_TARGET: dict(output)}}
        for name in sorted(set(profile_names))
    }
    path = build_root / PROFILES_FILENAME
    path.write_text(yaml.safe_dump(doc, sort_keys=True), encoding="utf-8")
    return path


def _dbt_python(dbt_python: str | None) -> str:
    exe = dbt_python or os.environ.get(DBT_PYTHON_ENV) or DEFAULT_DBT_PYTHON
    if not exe or not Path(exe).is_file():
        raise ManifestBuildError(
            f"dbt interpreter not found: {exe or '(not configured)'} — set "
            f"{DBT_PYTHON_ENV} to a "
            "python that can `import dbt.cli.main` (the vendored venv's console "
            "script has a broken shebang, so dbt is always run as `-m`)"
        )
    return exe


def _run_dbt(
    exe: str,
    args: list[str],
    *,
    project_dir: Path,
    profiles_dir: Path,
    profile: str | None = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        {
            "DBT_PROFILES_DIR": str(profiles_dir),
            "DO_NOT_TRACK": "1",
            "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
            # dbt writes ./logs relative to cwd; keep it inside the build copy.
            "PYTHONWARNINGS": "ignore",
        }
    )
    # --profile is passed EXPLICITLY: a Fivetran PACKAGE project declares no
    # `profile:` key, and dbt >= 1.10 refuses to run without one rather than
    # falling back to the project name.
    profile_args = ["--profile", profile] if profile else []
    return subprocess.run(
        [exe, "-m", "dbt.cli.main", *args, *profile_args,
         "--project-dir", str(project_dir), "--profiles-dir", str(profiles_dir)],
        cwd=str(project_dir),
        env=env,
        capture_output=True,
        text=True,
    )


def _tail(proc: subprocess.CompletedProcess, lines: int = 25) -> str:
    text = (proc.stdout or "") + (proc.stderr or "")
    return "\n".join(text.strip().splitlines()[-lines:])


# ---------------------------------------------------------------------------
# Fingerprint + non-mutation assertion
# ---------------------------------------------------------------------------

def manifest_fingerprint(manifest_path: Path) -> str:
    """sha256 of the SEMANTIC content of a manifest (no clock, no ids).

    Hashes the `CandidateSpec` the adapter parses — the only part of the
    manifest that can move a TaskIR — so two rebuilds of the same commit
    fingerprint identically even though dbt stamps a fresh `generated_at` and
    `invocation_id` into every manifest it writes.
    """
    from elt_taskgen.adapters import dbt as dbt_adapter

    spec = dbt_adapter.load_manifest(Path(manifest_path))
    return sha256_hex(canonical_json(spec.model_dump(mode="json")))


def compile_coverage(manifest_path: Path) -> tuple[int, int]:
    """(models, models carrying `compiled_code`) for a manifest on disk.

    The one measurement that says whether `dbt compile` actually ran: `dbt
    parse` leaves `compiled_code` NULL on every model, and so does `dbt seed`
    (which re-parses). Read straight off the JSON rather than through
    `load_manifest`, because `load_manifest` deliberately collapses raw and
    compiled into one `sql_text` and would hide the difference.
    """
    try:
        raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (0, 0)
    nodes = (raw.get("nodes") or {}) if isinstance(raw, dict) else {}
    models = [n for n in nodes.values() if isinstance(n, dict) and n.get("resource_type") == "model"]
    compiled = [n for n in models if (n.get("compiled_code") or "").strip()]
    return (len(models), len(compiled))


def manifest_is_compiled(manifest_path: Path) -> bool:
    """True when a manifest carries rendered SQL for at least one model."""
    total, compiled = compile_coverage(manifest_path)
    return total > 0 and compiled > 0


def vendored_snapshot(pkg_dir: Path) -> dict[str, str]:
    """rel_path -> sha256 for every file under a vendored package."""
    out: dict[str, str] = {}
    base = Path(pkg_dir)
    for path in sorted(base.rglob("*")):
        if path.is_file() and not path.is_symlink():
            digest = hashlib.sha256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            out[str(path.relative_to(base))] = digest.hexdigest()
    return out


def reusable_copy_snapshot(copy_dir: Path) -> dict[str, str]:
    """Source-owned bytes in a build mirror, excluding dbt build state.

    A pre-existing manifest is reusable only when the mirror still represents
    the vendored tree supplied to *this* invocation. Project-name equality is
    not enough: two releases of the same package have the same dbt project
    name. Paths in ``_MIRROR_PRESERVE`` are generated build state that need not
    exist in the read-only source tree.
    """
    out: dict[str, str] = {}
    base = Path(copy_dir)
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(base)
        if any(part in _MIRROR_PRESERVE for part in rel.parts):
            continue
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        out[str(rel)] = digest.hexdigest()
    return out


def assert_reusable_copy_matches_vendored(
    pkg_dir: Path,
    copy_dir: Path,
    source_snapshot: dict[str, str] | None = None,
) -> None:
    """Refuse a cached manifest whose build mirror is from other source bytes."""
    source_files = (
        source_snapshot if source_snapshot is not None else vendored_snapshot(pkg_dir)
    )
    expected = {
        rel: digest
        for rel, digest in source_files.items()
        if not any(part in _MIRROR_PRESERVE for part in Path(rel).parts)
    }
    observed = reusable_copy_snapshot(copy_dir)
    if observed == expected:
        return
    added = sorted(set(observed) - set(expected))
    removed = sorted(set(expected) - set(observed))
    changed = sorted(
        key for key in set(expected) & set(observed) if expected[key] != observed[key]
    )
    raise ManifestBuildError(
        f"build root {copy_dir.parent} contains a manifest for different source "
        f"bytes than {pkg_dir} (extra={added[:5]} missing={removed[:5]} "
        f"changed={changed[:5]}). Refusing stale reuse; pass "
        "--rebuild-manifest to rebuild this per-package root."
    )


def assert_vendored_untouched(pkg_dir: Path, before: dict[str, str]) -> None:
    """Fail closed if a build wrote into the vendored tree."""
    after = vendored_snapshot(pkg_dir)
    if after != before:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(k for k in set(after) & set(before) if after[k] != before[k])
        raise ManifestBuildError(
            f"vendored tree {pkg_dir} was MUTATED by the build "
            f"(added={added[:5]} removed={removed[:5]} changed={changed[:5]})"
        )


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------

def default_build_root(workspace: Path, package: str) -> Path:
    return Path(workspace) / "dbt_builds" / package


def build_manifest(
    package: str,
    *,
    build_root: Path,
    pool: str = "dbt",
    root: Path | None = None,
    project: ProjectChoice = ProjectChoice.AUTO,
    force: bool = False,
    run_deps: bool = True,
    run_compile: bool = True,
    allow_parse_fallback: bool = False,
    dbt_python: str | None = None,
    catalog: SourceCatalog | None = None,
    echo=None,
) -> ManifestBuild:
    """Produce `target/manifest.json` for one vendored package.

    Idempotent: with an existing manifest and `force=False` no dbt process is
    started and the same path/fingerprint comes back (`rebuilt=False`). With
    `force=True` the tree is re-mirrored, `target/` is dropped and dbt re-runs;
    the SEMANTIC fingerprint is unchanged (only dbt's clock metadata moves).

    `run_compile` (default True) adds `dbt compile` after `dbt parse` so the
    manifest carries rendered `compiled_code`. A manifest already on disk that
    is only PARSED does not satisfy `run_compile`, so the reuse shortcut is
    skipped and the package is rebuilt — otherwise every workspace built before
    this change would keep silently serving Jinja to the recovery pass.
    """
    say = echo or (lambda _msg: None)
    pkg_dir = resolve_package_dir(package, pool=pool, root=root, catalog=catalog)
    choice = resolve_project_choice(pkg_dir, project)
    before = vendored_snapshot(pkg_dir)

    build_root = Path(build_root).resolve()
    copy_dir = build_root / "package"
    project_dir = (
        copy_dir / "integration_tests"
        if choice is ProjectChoice.INTEGRATION_TESTS
        else copy_dir
    )
    manifest_path = project_dir / MANIFEST_RELPATH

    package_project_name, package_profile = _project_name(pkg_dir)

    # A per-package build root cannot mirror a different project; otherwise a
    # cached manifest could be reused under the wrong family and provenance.
    if (copy_dir / "dbt_project.yml").is_file():
        mirrored_name, _ = _project_name(copy_dir)
        if mirrored_name != package_project_name:
            raise ManifestBuildError(
                f"build root {build_root} already holds package "
                f"{mirrored_name!r}, but {package!r} (project "
                f"{package_project_name!r}) was requested. `--build-root` is "
                "PER PACKAGE: pass a directory of its own (or omit it and let "
                "default_build_root append the package name)."
            )

    if manifest_path.is_file() and not force and (
        not run_compile or manifest_is_compiled(manifest_path)
    ):
        assert_reusable_copy_matches_vendored(pkg_dir, copy_dir, before)
        say(f"manifest present, reusing: {manifest_path}")
        total, compiled = compile_coverage(manifest_path)
        build = ManifestBuild(
            package=package,
            package_project_name=package_project_name,
            project_choice=choice,
            vendored_dir=str(pkg_dir),
            build_root=str(build_root),
            project_dir=str(project_dir),
            manifest_path=str(manifest_path),
            fingerprint=manifest_fingerprint(manifest_path),
            rebuilt=False,
            deps_ran=False,
            compiled=compiled > 0,
            model_count=total,
            compiled_model_count=compiled,
        )
        assert_vendored_untouched(pkg_dir, before)
        return build

    build_root.mkdir(parents=True, exist_ok=True)
    say(f"mirroring {pkg_dir} -> {copy_dir}")
    _mirror(pkg_dir, copy_dir)
    if not (project_dir / "dbt_project.yml").is_file():
        raise ManifestBuildError(f"{project_dir} has no dbt_project.yml (fail closed)")

    profile_names = [package_project_name, package_profile]
    active_profile = package_profile
    if choice is ProjectChoice.INTEGRATION_TESTS:
        it_name, it_profile = _project_name(project_dir)
        profile_names += [it_name, it_profile]
        active_profile = it_profile
    profiles_dir = build_root
    write_profiles(build_root, profile_names)

    exe = _dbt_python(dbt_python)
    deps_ran = False
    if run_deps:
        say(f"dbt deps ({project_dir})")
        proc = _run_dbt(
            exe,
            ["deps"],
            project_dir=project_dir,
            profiles_dir=profiles_dir,
            profile=active_profile,
        )
        deps_ran = True
        if proc.returncode != 0:
            cached = (project_dir / "dbt_packages").is_dir()
            if not cached:
                raise ManifestBuildError(
                    f"`dbt deps` failed for {package} (exit {proc.returncode}) and no "
                    f"cached dbt_packages/ exists in {project_dir}. The FIRST build of a "
                    "package needs network access to hub.getdbt.com; later builds do "
                    f"not.\n--- dbt output ---\n{_tail(proc)}"
                )
            say(
                f"WARNING: dbt deps failed (exit {proc.returncode}); continuing on the "
                f"cached dbt_packages/ in {project_dir}"
            )

    if manifest_path.exists():
        manifest_path.unlink()
    say(f"dbt parse ({project_dir})")
    proc = _run_dbt(
        exe,
        ["parse", "--no-partial-parse"],
        project_dir=project_dir,
        profiles_dir=profiles_dir,
        profile=active_profile,
    )
    if proc.returncode != 0:
        raise ManifestBuildError(
            f"`dbt parse` failed for {package} (exit {proc.returncode}), project "
            f"{project_dir}\n--- dbt output ---\n{_tail(proc)}"
        )
    if not manifest_path.is_file():
        raise ManifestBuildError(
            f"`dbt parse` succeeded but wrote no {manifest_path} (fail closed)\n"
            f"--- dbt output ---\n{_tail(proc)}"
        )

    compile_error = ""
    if run_compile:
        # Keep the parse manifest: `dbt compile` REWRITES manifest.json, and a
        # failed compile must not leave the build with nothing.
        backup = project_dir / PARSE_BACKUP_RELPATH
        shutil.copy2(manifest_path, backup)
        say(f"dbt compile ({project_dir})")
        cproc = _run_dbt(
            exe,
            ["compile", "--no-partial-parse"],
            project_dir=project_dir,
            profiles_dir=profiles_dir,
            profile=active_profile,
        )
        total, compiled_n = compile_coverage(manifest_path)
        if cproc.returncode != 0:
            compile_error = (
                f"`dbt compile` failed for {package} (exit {cproc.returncode}), "
                f"project {project_dir}\n--- dbt output ---\n{_tail(cproc)}"
            )
        elif not manifest_path.is_file():
            compile_error = (
                f"`dbt compile` succeeded but wrote no {manifest_path} (fail closed)"
            )
        elif compiled_n == 0:
            compile_error = (
                f"`dbt compile` produced a manifest with 0 of {total} models "
                "carrying compiled_code — the rendered SQL the recovery pass "
                "reads would be empty, which is indistinguishable from a parse"
            )
        if compile_error:
            if not allow_parse_fallback:
                raise ManifestBuildError(compile_error)
            say(f"WARNING: falling back to the PARSE-only manifest — {compile_error}")
            shutil.copy2(backup, manifest_path)

    total, compiled_n = compile_coverage(manifest_path)
    build = ManifestBuild(
        package=package,
        package_project_name=package_project_name,
        project_choice=choice,
        vendored_dir=str(pkg_dir),
        build_root=str(build_root),
        project_dir=str(project_dir),
        manifest_path=str(manifest_path),
        fingerprint=manifest_fingerprint(manifest_path),
        rebuilt=True,
        deps_ran=deps_ran,
        compiled=compiled_n > 0,
        model_count=total,
        compiled_model_count=compiled_n,
        compile_error=compile_error,
    )
    assert_vendored_untouched(pkg_dir, before)
    return build


# ---------------------------------------------------------------------------
# Standalone CLI (`python tools/build_dbt_manifest.py dbt_servicenow`)
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_dbt_manifest",
        description=(
            "Compile target/manifest.json for a vendored Fivetran dbt package "
            "without writing into the vendored tree."
        ),
    )
    parser.add_argument("package", help="vendored package dir name, e.g. dbt_servicenow")
    parser.add_argument("--pool", default="dbt", help="source-catalog pool (default dbt)")
    parser.add_argument("--root", default=None, help="override the vendored pool root")
    parser.add_argument(
        "--build-root",
        default=None,
        help="build directory (default <workspace>/dbt_builds/<package>)",
    )
    parser.add_argument(
        "--workspace", default="runs", help="workspace root for the default build dir"
    )
    parser.add_argument(
        "--project",
        choices=[c.value for c in ProjectChoice],
        default=ProjectChoice.AUTO.value,
        help="which project to parse (default auto: integration_tests when present)",
    )
    parser.add_argument("--force", action="store_true", help="rebuild even if a manifest exists")
    parser.add_argument("--no-deps", action="store_true", help="skip `dbt deps`")
    parser.add_argument(
        "--no-compile",
        action="store_true",
        help="skip `dbt compile` (models keep Jinja raw_code; recovery reads nothing)",
    )
    parser.add_argument(
        "--allow-parse-fallback",
        action="store_true",
        help="on a compile failure, RECORD it and keep the parse-only manifest",
    )
    parser.add_argument("--dbt-python", default=None, help=f"override ${DBT_PYTHON_ENV}")
    args = parser.parse_args(argv)

    build_root = (
        Path(args.build_root).resolve()
        if args.build_root
        else default_build_root(Path(args.workspace).resolve(), args.package)
    )
    try:
        build = build_manifest(
            args.package,
            build_root=build_root,
            pool=args.pool,
            root=Path(args.root).resolve() if args.root else None,
            project=ProjectChoice(args.project),
            force=args.force,
            run_deps=not args.no_deps,
            run_compile=not args.no_compile,
            allow_parse_fallback=args.allow_parse_fallback,
            dbt_python=args.dbt_python,
            echo=lambda m: print(m, file=sys.stderr),
        )
    except ManifestBuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"project_choice: {build.project_choice.value}")
    print(f"project_dir:    {build.project_dir}")
    print(f"fingerprint:    {build.fingerprint}")
    print(
        f"compiled:       {build.compiled_model_count}/{build.model_count} models"
        + ("" if not build.compile_error else "  (PARSE FALLBACK)")
    )
    print(build.manifest_path)
    return 0


if __name__ == "__main__":  # pragma: no cover - shell entry point
    raise SystemExit(main())

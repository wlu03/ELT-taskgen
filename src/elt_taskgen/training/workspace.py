"""Fresh install, deterministic seal, and clean replay for workspace-v1."""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import sys
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Iterable

from elt_taskgen.models import canonical_json
from elt_taskgen.training.contract import (
    FORBIDDEN_STATE_NAMES,
    FORBIDDEN_STATE_PARTS,
    MAX_WORKSPACE_ACTIONS,
    MAX_WORKSPACE_FILE_BYTES,
    MAX_WORKSPACE_FILES,
    MAX_WORKSPACE_MANIFEST_BYTES,
    MAX_WORKSPACE_TOTAL_BYTES,
    REQUIRED_WORKSPACE_FILES,
    WORKSPACE_SUBMISSION_SCHEMA_VERSION,
    WorkspaceErrorCode,
)
from elt_taskgen.training.models import (
    WorkspaceActionTraceEntry,
    WorkspaceArtifactFile,
    WorkspaceSubmission,
    workspace_artifact_digest,
)
from elt_taskgen.training.package import (
    WorkspacePackage,
    public_tree_digest,
)
from elt_taskgen.workspace import repo_root


#: The admitted candidate surface under ``elt/``.  ``macros`` is deliberately
#: absent: ``dbt_runner._prepare_execution_project`` rejects any ``macros``
#: path component as UNSAFE_ARTIFACT, so the seal refuses it up front and the
#: two agree (roadmap Phase 0.A; SoT T2).
_ALLOWED_TOP_LEVEL = frozenset({"main.tf", "dbt_project.yml", "models"})
_MODEL_SUFFIXES = frozenset({".sql", ".yml", ".yaml"})
_SENSITIVE_HCL_LITERAL = re.compile(
    r"(?im)^\s*(?:password|client_secret|secret|secret_access_key|"
    r"aws_secret_access_key|access_key_id|aws_access_key_id|access_token|"
    r"api_key|api_secret_key|personal_access_token|private_key|"
    r"private_key_password)"
    r"\s*=\s*\"([^\"]+)\""
)
_SENSITIVE_YAML_LITERAL = re.compile(
    r"(?im)^\s*(?:password|client_secret|secret|secret_access_key|"
    r"aws_secret_access_key|access_key_id|aws_access_key_id|access_token|"
    r"api_key|api_secret_key|personal_access_token|private_key|"
    r"private_key_password)"
    r"\s*:\s*['\"]?([^\s'\"#][^#\r\n]*)"
)
_HIGH_CONFIDENCE_SECRET = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{16,})"
)

# A process-local capability binds a trusted harness seal to the manifest bytes
# observed at the sealing boundary.  It is deliberately not serialized into
# the solver-controlled directory.  Durable coordinators must persist and pass
# ``seal_sha256`` explicitly when loading in another process.
_SEAL_AUTH_KEY = secrets.token_bytes(32)
_DARWIN_SYSTEM_ALIASES = {
    Path("/tmp"): Path("/private/tmp"),
    Path("/var"): Path("/private/var"),
}


class WorkspaceLifecycleError(RuntimeError):
    """Stable-code workspace refusal with no evaluator-private detail."""

    def __init__(self, code: WorkspaceErrorCode, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AttemptWorkspace:
    """One fresh mutable candidate attempt."""

    root: Path
    task_dir: Path
    elt_dir: Path
    marker_path: Path
    task_id: str
    public_static_sha256: str
    public_static_manifest: tuple[_StaticTreeEntry, ...]


@dataclass(frozen=True)
class SealedWorkspace:
    """Candidate snapshot plus a harness-held authenticity capability.

    Read-only modes prevent accidental edits only.  They are not a security
    boundary against the file owner, so callers must seal after the solver has
    exited or write beneath storage the solver cannot access.
    """

    root: Path
    elt_dir: Path
    manifest_path: Path
    submission: WorkspaceSubmission
    seal_sha256: str
    _trusted_token: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class _TargetClaim:
    """Final target directory exclusively claimed before any payload write."""

    target: Path


@dataclass(frozen=True)
class _CandidateTreeEntry:
    """One bounded, no-follow candidate tree entry."""

    path: Path
    relative: Path
    is_directory: bool


@dataclass(frozen=True)
class _StaticTreeEntry:
    """One public-static path identity captured before solver access."""

    path: str
    kind: str
    size_bytes: int = 0
    sha256: str = ""

    def digest_record(self) -> dict[str, object]:
        if self.kind == "directory":
            return {"kind": self.kind, "path": self.path}
        return {
            "kind": self.kind,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


def _fail(code: WorkspaceErrorCode, message: str) -> None:
    raise WorkspaceLifecycleError(code, message)


def _reject_dangerous_target(path: Path) -> None:
    protected = {
        Path.home().resolve(),
        Path.cwd().resolve(),
        repo_root().resolve(),
    }
    anchor = Path(path.anchor).resolve()
    if path == anchor or any(
        item == path or item.is_relative_to(path) for item in protected
    ):
        _fail(WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE, "workspace target is too broad")


def _reject_symlink_components(path: Path) -> Path:
    """Reject path symlinks except the two fixed macOS filesystem aliases.

    macOS exposes ``/var`` and ``/tmp`` through root-owned links to their fixed
    ``/private`` locations.  Ownership alone is not sufficient: every other
    symlink is refused regardless of which uid owns it.
    """

    lexical = Path(path).expanduser()
    if ".." in lexical.parts:
        _fail(WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE, "workspace path contains '..'")
    absolute = lexical if lexical.is_absolute() else Path.cwd() / lexical
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor = cursor / part
        if not cursor.is_symlink():
            continue
        allowed_target = (
            _DARWIN_SYSTEM_ALIASES.get(cursor)
            if sys.platform == "darwin"
            else None
        )
        try:
            allowed = (
                allowed_target is not None
                and cursor.lstat().st_uid == 0
                and cursor.resolve(strict=True) == allowed_target
            )
        except (OSError, RuntimeError):
            allowed = False
        if not allowed:
            _fail(
                WorkspaceErrorCode.WORKSPACE_SYMLINK,
                "workspace path traverses a symlink",
            )
    return absolute


def _paths_overlap(left: Path, right: Path) -> bool:
    return (
        left == right
        or left.is_relative_to(right)
        or right.is_relative_to(left)
    )


def _claim_fresh_target(
    path: Path,
    *,
    forbidden_roots: Iterable[Path] = (),
) -> _TargetClaim:
    """Resolve and exclusively claim a target without publishing partial data."""

    lexical = _reject_symlink_components(path)
    resolved = lexical.resolve()
    _reject_dangerous_target(resolved)
    for root in forbidden_roots:
        protected = Path(root).resolve(strict=True)
        if _paths_overlap(resolved, protected):
            _fail(
                WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE,
                "workspace target overlaps trusted package or attempt storage",
            )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    try:
        resolved.mkdir(mode=0o700)
    except FileExistsError:
        _fail(
            WorkspaceErrorCode.WORKSPACE_NOT_FRESH,
            "workspace target is already present or claimed",
        )
    except OSError as exc:
        raise WorkspaceLifecycleError(
            WorkspaceErrorCode.HARNESS_INTERNAL,
            "workspace target could not be claimed",
        ) from exc
    return _TargetClaim(target=resolved)


def _remove_staging(path: Path) -> None:
    """Remove an owned tree even after its descendants were made read-only."""

    if not os.path.lexists(path):
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return

    # ``rmtree`` needs write permission on every containing directory, not on
    # the files themselves. Restore owner access top-down before unlinking.
    for directory, dirnames, _filenames in os.walk(
        path,
        topdown=True,
        followlinks=False,
    ):
        current = Path(directory)
        current.chmod(0o700)
        for name in dirnames:
            child = current / name
            if not child.is_symlink():
                child.chmod(0o700)

    def _retry_readonly(function, name, _exc_info) -> None:
        failed = Path(name)
        for candidate in (failed.parent, failed):
            try:
                if not candidate.is_symlink():
                    candidate.chmod(0o700)
            except FileNotFoundError:
                pass
        function(name)

    shutil.rmtree(path, onerror=_retry_readonly)


def _reject_tree_symlinks(root: Path) -> None:
    if root.is_symlink():
        _fail(WorkspaceErrorCode.WORKSPACE_SYMLINK, "workspace tree is a symlink")
    try:
        if any(path.is_symlink() for path in root.rglob("*")):
            _fail(WorkspaceErrorCode.WORKSPACE_SYMLINK, "workspace tree contains a symlink")
    except OSError as exc:
        raise WorkspaceLifecycleError(
            WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
            "workspace tree could not be inspected",
        ) from exc


def _seal_token(seal_sha256: str) -> str:
    return hmac.new(
        _SEAL_AUTH_KEY,
        seal_sha256.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _has_trusted_seal(sealed: SealedWorkspace) -> bool:
    token = sealed._trusted_token
    return token is not None and hmac.compare_digest(
        token,
        _seal_token(sealed.seal_sha256),
    )


def _bounded_action_trace(
    entries: Iterable[WorkspaceActionTraceEntry],
) -> tuple[WorkspaceActionTraceEntry, ...]:
    bounded = tuple(islice(iter(entries), MAX_WORKSPACE_ACTIONS + 1))
    if len(bounded) > MAX_WORKSPACE_ACTIONS:
        _fail(
            WorkspaceErrorCode.WORKSPACE_SIZE_LIMIT,
            "workspace action trace has too many entries",
        )
    return bounded


def _bounded_candidate_entries(elt_dir: Path) -> tuple[_CandidateTreeEntry, ...]:
    """Walk at most the admitted number of entries without following links."""

    try:
        root_status = os.lstat(elt_dir)
    except OSError as exc:
        raise WorkspaceLifecycleError(
            WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
            "candidate workspace could not be inspected",
        ) from exc
    if stat.S_ISLNK(root_status.st_mode):
        _fail(WorkspaceErrorCode.WORKSPACE_SYMLINK, "workspace tree is a symlink")
    if not stat.S_ISDIR(root_status.st_mode):
        _fail(
            WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
            "candidate workspace root is not a directory",
        )

    pending = [elt_dir]
    found: list[_CandidateTreeEntry] = []
    while pending:
        directory = pending.pop()
        try:
            directory_status = os.lstat(directory)
            if stat.S_ISLNK(directory_status.st_mode):
                _fail(
                    WorkspaceErrorCode.WORKSPACE_SYMLINK,
                    "workspace tree contains a symlink",
                )
            if not stat.S_ISDIR(directory_status.st_mode):
                _fail(
                    WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
                    "candidate workspace changed while it was inspected",
                )
            child_directories: list[Path] = []
            with os.scandir(directory) as children:
                for child in children:
                    path = Path(child.path)
                    relative = path.relative_to(elt_dir)
                    if child.is_symlink():
                        _fail(
                            WorkspaceErrorCode.WORKSPACE_SYMLINK,
                            "workspace tree contains a symlink",
                        )
                    candidate_relative = Path("elt") / relative
                    _check_candidate_path(candidate_relative)
                    is_directory = child.is_dir(follow_symlinks=False)
                    if not is_directory and not child.is_file(follow_symlinks=False):
                        _fail(
                            WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
                            "candidate workspace contains a non-regular file",
                        )
                    found.append(
                        _CandidateTreeEntry(
                            path=path,
                            relative=relative,
                            is_directory=is_directory,
                        )
                    )
                    if len(found) > MAX_WORKSPACE_FILES:
                        _fail(
                            WorkspaceErrorCode.WORKSPACE_SIZE_LIMIT,
                            "candidate workspace has too many entries",
                        )
                    if is_directory:
                        child_directories.append(path)
            pending.extend(child_directories)
        except WorkspaceLifecycleError:
            raise
        except OSError as exc:
            raise WorkspaceLifecycleError(
                WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
                "candidate workspace could not be inspected",
            ) from exc
    return tuple(sorted(found, key=lambda item: item.relative.as_posix()))


def _read_bounded_regular_file(
    path: Path,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    """Read no more than ``max_bytes + 1`` through a no-follow descriptor."""

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                _fail(
                    WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
                    f"{label} is not a regular file",
                )
            if before.st_size > max_bytes:
                _fail(WorkspaceErrorCode.WORKSPACE_SIZE_LIMIT, f"{label} is too large")
            content = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
    except WorkspaceLifecycleError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _fail(WorkspaceErrorCode.WORKSPACE_SYMLINK, f"{label} is a symlink")
        raise WorkspaceLifecycleError(
            WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
            f"{label} could not be read",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(content) > max_bytes or after.st_size > max_bytes:
        _fail(WorkspaceErrorCode.WORKSPACE_SIZE_LIMIT, f"{label} is too large")
    if before.st_size != after.st_size or len(content) != after.st_size:
        _fail(
            WorkspaceErrorCode.WORKSPACE_DIGEST_MISMATCH,
            f"{label} changed while it was read",
        )
    return content


def _set_installed_modes(task_dir: Path) -> None:
    """Make the public specification read-only and candidate ``elt/`` writable."""

    elt_dir = task_dir / "elt"
    for path in sorted(task_dir.rglob("*"), reverse=True):
        relative = path.relative_to(task_dir)
        candidate = bool(relative.parts and relative.parts[0] == "elt")
        if path.is_dir():
            path.chmod(0o755 if candidate else 0o555)
        elif path.is_file():
            path.chmod(0o644 if candidate else 0o444)
    task_dir.chmod(0o555)


def install_workspace(package: WorkspacePackage, attempt_dir: Path) -> AttemptWorkspace:
    """Install the exact public combined task into a fresh attempt directory."""

    claim = _claim_fresh_target(
        Path(attempt_dir),
        forbidden_roots=(package.release_dir,),
    )
    try:
        if public_tree_digest(package.public_dir) != package.public_tree_sha256:
            _fail(
                WorkspaceErrorCode.TASK_PACKAGE_INVALID,
                "verified public task changed before workspace installation",
            )
        task_dir = claim.target / "task"
        shutil.copytree(package.public_dir, task_dir)
        _reject_tree_symlinks(task_dir)
        if public_tree_digest(task_dir) != package.public_tree_sha256:
            _fail(
                WorkspaceErrorCode.TASK_PACKAGE_INVALID,
                "installed public task does not match the verified package",
            )
        public_static_manifest = _capture_public_static_manifest(task_dir)
        if (
            _static_manifest_digest(public_static_manifest)
            != package.public_static_sha256
        ):
            _fail(
                WorkspaceErrorCode.TASK_PACKAGE_INVALID,
                "installed public-static files do not match the verified package",
            )
        marker = {
            "schema_version": WORKSPACE_SUBMISSION_SCHEMA_VERSION,
            "task_id": package.task_id,
            "public_tree_sha256": package.public_tree_sha256,
            "public_static_sha256": package.public_static_sha256,
            "candidate_root": "task/elt",
            "profile_owner": "harness",
        }
        marker_path = claim.target / "attempt.json"
        _set_installed_modes(task_dir)
        # The ownership marker is the completion record and is written last.
        marker_path.write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        marker_path.chmod(0o444)
    except BaseException:
        _remove_staging(claim.target)
        raise
    return AttemptWorkspace(
        root=claim.target,
        task_dir=claim.target / "task",
        elt_dir=claim.target / "task" / "elt",
        marker_path=claim.target / "attempt.json",
        task_id=package.task_id,
        public_static_sha256=package.public_static_sha256,
        public_static_manifest=public_static_manifest,
    )


def load_attempt_workspace(
    package: WorkspacePackage, attempt_dir: Path
) -> AttemptWorkspace:
    """Re-open an installed authoring workspace for a later CLI seal step."""

    root = _reject_symlink_components(Path(attempt_dir)).resolve(strict=True)
    marker_path = root / "attempt.json"
    task_dir = root / "task"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except Exception as error:
        raise WorkspaceLifecycleError(
            WorkspaceErrorCode.SUBMISSION_INVALID, "attempt marker is invalid"
        ) from error
    if (
        marker.get("schema_version") != WORKSPACE_SUBMISSION_SCHEMA_VERSION
        or marker.get("task_id") != package.task_id
        or marker.get("public_tree_sha256") != package.public_tree_sha256
        or marker.get("public_static_sha256") != package.public_static_sha256
        or marker.get("candidate_root") != "task/elt"
        or marker.get("profile_owner") != "harness"
    ):
        raise WorkspaceLifecycleError(
            WorkspaceErrorCode.SUBMISSION_INVALID, "attempt marker does not match package"
        )
    manifest = _capture_public_static_manifest(task_dir)
    attempt = AttemptWorkspace(
        root=root,
        task_dir=task_dir,
        elt_dir=task_dir / "elt",
        marker_path=marker_path,
        task_id=package.task_id,
        public_static_sha256=package.public_static_sha256,
        public_static_manifest=manifest,
    )
    _verify_attempt(attempt, package)
    return attempt


def _bounded_static_entries(
    task_dir: Path,
    *,
    max_entries: int,
) -> tuple[_CandidateTreeEntry, ...]:
    """Walk public-static paths only, stopping after the captured entry count."""

    pending = [task_dir]
    found: list[_CandidateTreeEntry] = []
    try:
        while pending:
            directory = pending.pop()
            status = os.lstat(directory)
            if stat.S_ISLNK(status.st_mode):
                _fail(WorkspaceErrorCode.WORKSPACE_SYMLINK, "public task contains a symlink")
            if not stat.S_ISDIR(status.st_mode):
                _fail(
                    WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
                    "public task contains a non-directory path",
                )
            descendants: list[Path] = []
            with os.scandir(directory) as children:
                for child in children:
                    path = Path(child.path)
                    relative = path.relative_to(task_dir)
                    if directory == task_dir and child.name == "elt":
                        continue
                    if child.is_symlink():
                        _fail(
                            WorkspaceErrorCode.WORKSPACE_SYMLINK,
                            "public task contains a symlink",
                        )
                    is_directory = child.is_dir(follow_symlinks=False)
                    if not is_directory and not child.is_file(follow_symlinks=False):
                        _fail(
                            WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
                            "public task contains a non-regular file",
                        )
                    found.append(
                        _CandidateTreeEntry(
                            path=path,
                            relative=relative,
                            is_directory=is_directory,
                        )
                    )
                    if len(found) > max_entries:
                        _fail(
                            WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
                            "public task contains unexpected extra entries",
                        )
                    if is_directory:
                        descendants.append(path)
            pending.extend(descendants)
    except WorkspaceLifecycleError:
        raise
    except OSError as exc:
        raise WorkspaceLifecycleError(
            WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
            "public task could not be verified",
        ) from exc
    return tuple(sorted(found, key=lambda item: item.relative.as_posix()))


def _bounded_file_digest(
    path: Path,
    *,
    max_bytes: int,
) -> tuple[int, str]:
    """Hash through a no-follow descriptor while reading bounded chunks."""

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor: int | None = None
    digest = hashlib.sha256()
    size = 0
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                _fail(
                    WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
                    "public task contains a non-regular file",
                )
            if before.st_size > max_bytes:
                _fail(
                    WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
                    "public task file grew beyond its installed size",
                )
            while True:
                chunk = handle.read(min(64 * 1024, max_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    _fail(
                        WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
                        "public task file grew beyond its installed size",
                    )
                digest.update(chunk)
            after = os.fstat(handle.fileno())
    except WorkspaceLifecycleError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _fail(WorkspaceErrorCode.WORKSPACE_SYMLINK, "public task contains a symlink")
        raise WorkspaceLifecycleError(
            WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
            "public task file could not be read",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if before.st_size != after.st_size or size != after.st_size:
        _fail(
            WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
            "public task file changed while it was read",
        )
    return size, digest.hexdigest()


def _static_manifest_digest(entries: tuple[_StaticTreeEntry, ...]) -> str:
    records = [entry.digest_record() for entry in entries]
    return hashlib.sha256(canonical_json(records).encode("utf-8")).hexdigest()


def _capture_public_static_manifest(task_dir: Path) -> tuple[_StaticTreeEntry, ...]:
    """Capture trusted installed bytes before the workspace reaches the solver."""

    # The package tree was already release-verified. Its exact entry count and
    # file sizes become the bounds used for every later mutable-attempt check.
    captured: list[_StaticTreeEntry] = []

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            relative_path = path.relative_to(task_dir)
            if directory == task_dir and path.name == "elt":
                continue
            status = os.lstat(path)
            if stat.S_ISLNK(status.st_mode):
                _fail(
                    WorkspaceErrorCode.WORKSPACE_SYMLINK,
                    "public task contains a symlink",
                )
            relative = relative_path.as_posix()
            if stat.S_ISDIR(status.st_mode):
                captured.append(_StaticTreeEntry(path=relative, kind="directory"))
                visit(path)
                continue
            if not stat.S_ISREG(status.st_mode):
                _fail(
                    WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
                    "public task contains a non-regular file",
                )
            size, digest = _bounded_file_digest(path, max_bytes=status.st_size)
            captured.append(
                _StaticTreeEntry(
                    path=relative,
                    kind="file",
                    size_bytes=size,
                    sha256=digest,
                )
            )

    try:
        visit(task_dir)
    except WorkspaceLifecycleError:
        raise
    except OSError as exc:
        raise WorkspaceLifecycleError(
            WorkspaceErrorCode.TASK_PACKAGE_INVALID,
            "installed public task could not be inspected",
        ) from exc
    return tuple(captured)


def _verify_public_static_manifest(
    task_dir: Path,
    expected: tuple[_StaticTreeEntry, ...],
) -> str:
    expected_by_path = {entry.path: entry for entry in expected}
    actual_by_path: dict[str, _StaticTreeEntry] = {}
    total_expected = sum(entry.size_bytes for entry in expected)
    total_actual = 0
    for entry in _bounded_static_entries(task_dir, max_entries=len(expected)):
        relative = entry.relative.as_posix()
        pinned = expected_by_path.get(relative)
        expected_kind = "directory" if entry.is_directory else "file"
        if pinned is None or pinned.kind != expected_kind:
            _fail(
                WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
                "public task path set or kind changed",
            )
        if entry.is_directory:
            actual_by_path[relative] = _StaticTreeEntry(
                path=relative,
                kind="directory",
            )
            continue
        size, digest = _bounded_file_digest(entry.path, max_bytes=pinned.size_bytes)
        total_actual += size
        if total_actual > total_expected or size != pinned.size_bytes or digest != pinned.sha256:
            _fail(
                WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
                "public task file bytes changed",
            )
        actual_by_path[relative] = _StaticTreeEntry(
            path=relative,
            kind="file",
            size_bytes=size,
            sha256=digest,
        )
    if len(actual_by_path) != len(expected):
        _fail(
            WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
            "public task paths changed",
        )
    ordered_actual = tuple(actual_by_path[entry.path] for entry in expected)
    return _static_manifest_digest(ordered_actual)


def _verify_attempt(attempt: AttemptWorkspace, package: WorkspacePackage) -> None:
    structurally_scoped = (
        attempt.task_dir == attempt.root / "task"
        and attempt.elt_dir == attempt.task_dir / "elt"
        and attempt.marker_path == attempt.root / "attempt.json"
    )
    try:
        paths_stable = (
            structurally_scoped
            and attempt.root.resolve(strict=True) == attempt.root
            and attempt.task_dir.resolve(strict=True) == attempt.task_dir
            and attempt.elt_dir.resolve(strict=True) == attempt.elt_dir
            and attempt.marker_path.resolve(strict=True) == attempt.marker_path
        )
    except (OSError, RuntimeError):
        paths_stable = False
    if not paths_stable:
        _fail(WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE, "attempt paths changed")
    if attempt.task_id != package.task_id:
        _fail(WorkspaceErrorCode.TASK_ID_MISMATCH, "attempt task identity changed")
    marker_bytes = _read_bounded_regular_file(
        attempt.marker_path,
        max_bytes=MAX_WORKSPACE_MANIFEST_BYTES,
        label="attempt ownership marker",
    )
    try:
        marker = json.loads(marker_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceLifecycleError(
            WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
            "attempt ownership marker is invalid",
        ) from exc
    if not isinstance(marker, dict) or marker.get("task_id") != package.task_id:
        _fail(WorkspaceErrorCode.TASK_ID_MISMATCH, "attempt task identity changed")
    if marker.get("schema_version") != WORKSPACE_SUBMISSION_SCHEMA_VERSION:
        _fail(WorkspaceErrorCode.SUBMISSION_INVALID, "attempt schema version changed")
    expected_marker = {
        "schema_version": WORKSPACE_SUBMISSION_SCHEMA_VERSION,
        "task_id": package.task_id,
        "public_tree_sha256": package.public_tree_sha256,
        "public_static_sha256": package.public_static_sha256,
        "candidate_root": "task/elt",
        "profile_owner": "harness",
    }
    if marker != expected_marker:
        _fail(
            WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
            "attempt ownership marker changed",
        )
    if (
        marker.get("public_static_sha256") != package.public_static_sha256
        or attempt.public_static_sha256 != package.public_static_sha256
        or _static_manifest_digest(attempt.public_static_manifest)
        != package.public_static_sha256
        or _verify_public_static_manifest(
            attempt.task_dir,
            attempt.public_static_manifest,
        )
        != package.public_static_sha256
    ):
        _fail(
            WorkspaceErrorCode.WORKSPACE_PUBLIC_MUTATED,
            "solver-visible files outside elt/ changed",
        )


def _check_candidate_path(relative: Path) -> None:
    posix = relative.as_posix()
    if not relative.parts or relative.parts[0] != "elt":
        _fail(WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE, "candidate path escapes elt/")
    inner = relative.parts[1:]
    if not inner:
        return
    if inner[0] not in _ALLOWED_TOP_LEVEL:
        _fail(
            WorkspaceErrorCode.WORKSPACE_TRANSIENT_STATE,
            f"candidate artifact {posix!r} is outside the admitted workspace surface",
        )
    if any(part in FORBIDDEN_STATE_PARTS or part == "dbt_packages" for part in inner):
        _fail(
            WorkspaceErrorCode.WORKSPACE_TRANSIENT_STATE,
            f"candidate artifact {posix!r} contains attempt-local state",
        )
    name = inner[-1]
    if (
        name in FORBIDDEN_STATE_NAMES
        or name.startswith("terraform.tfstate")
        or name.endswith(".tfplan")
        or name.endswith(".duckdb")
    ):
        _fail(
            WorkspaceErrorCode.WORKSPACE_TRANSIENT_STATE,
            f"candidate artifact {posix!r} contains attempt-local state",
        )
    if len(inner) == 1 and name not in _ALLOWED_TOP_LEVEL:
        _fail(
            WorkspaceErrorCode.WORKSPACE_TRANSIENT_STATE,
            f"candidate artifact {posix!r} is not admitted",
        )
    if inner[0] == "models" and len(inner) > 1 and Path(name).suffix not in _MODEL_SUFFIXES:
        _fail(
            WorkspaceErrorCode.SUBMISSION_INVALID,
            f"model artifact {posix!r} has an unsupported suffix",
        )


def _contains_secret_literal(text: str) -> bool:
    if _HIGH_CONFIDENCE_SECRET.search(text):
        return True
    for expression in (_SENSITIVE_HCL_LITERAL, _SENSITIVE_YAML_LITERAL):
        for match in expression.finditer(text):
            value = match.group(1).strip().strip("'\"")
            if not value or (value.startswith("<") and value.endswith(">")):
                continue
            if value.startswith(("${", "{{", "env_var(")):
                continue
            return True
    return False


def _candidate_manifest(elt_dir: Path) -> tuple[WorkspaceArtifactFile, ...]:
    all_entries = _bounded_candidate_entries(elt_dir)
    folded: dict[str, str] = {}
    records: list[WorkspaceArtifactFile] = []
    total_bytes = 0
    sql_models = 0
    for entry in all_entries:
        path = entry.path
        relative = Path("elt") / entry.relative
        posix = relative.as_posix()
        folded_path = posix.casefold()
        if folded_path in folded and folded[folded_path] != posix:
            _fail(
                WorkspaceErrorCode.WORKSPACE_CASE_COLLISION,
                "candidate paths collide after case folding",
            )
        folded[folded_path] = posix
        if entry.is_directory:
            continue
        content = _read_bounded_regular_file(
            path,
            max_bytes=MAX_WORKSPACE_FILE_BYTES,
            label="candidate file",
        )
        total_bytes += len(content)
        if total_bytes > MAX_WORKSPACE_TOTAL_BYTES:
            _fail(WorkspaceErrorCode.WORKSPACE_SIZE_LIMIT, "candidate workspace is too large")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceLifecycleError(
                WorkspaceErrorCode.SUBMISSION_INVALID,
                "candidate artifacts must be UTF-8 text",
            ) from exc
        if _contains_secret_literal(text):
            _fail(
                WorkspaceErrorCode.WORKSPACE_SECRET_LITERAL,
                "candidate workspace contains a literal in a secret-bearing field",
            )
        if relative.parts[1] == "models" and path.suffix == ".sql":
            sql_models += 1
        records.append(
            WorkspaceArtifactFile(
                path=posix,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    present = {record.path for record in records}
    missing = sorted(REQUIRED_WORKSPACE_FILES - present)
    if missing or sql_models == 0:
        _fail(
            WorkspaceErrorCode.WORKSPACE_MISSING_ARTIFACT,
            "candidate workspace is missing required Terraform or dbt artifacts",
        )
    return tuple(records)


def _copy_candidate_files(
    source: Path,
    destination: Path,
    *,
    expected: tuple[WorkspaceArtifactFile, ...],
) -> None:
    """Copy one bounded tree while matching every file to the trusted manifest."""

    try:
        destination.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
            _fail(
                WorkspaceErrorCode.HARNESS_INTERNAL,
                "candidate copy destination is not a fresh empty directory",
            )
    expected_by_path = {item.path: item for item in expected}
    copied: set[str] = set()
    total_bytes = 0
    for entry in _bounded_candidate_entries(source):
        target = destination / entry.relative
        if entry.is_directory:
            target.mkdir(parents=True, exist_ok=True)
            continue
        posix = (Path("elt") / entry.relative).as_posix()
        record = expected_by_path.get(posix)
        if record is None:
            _fail(
                WorkspaceErrorCode.WORKSPACE_DIGEST_MISMATCH,
                "candidate file appeared while the workspace was copied",
            )
        content = _read_bounded_regular_file(
            entry.path,
            max_bytes=MAX_WORKSPACE_FILE_BYTES,
            label="candidate file",
        )
        total_bytes += len(content)
        if total_bytes > MAX_WORKSPACE_TOTAL_BYTES:
            _fail(WorkspaceErrorCode.WORKSPACE_SIZE_LIMIT, "candidate workspace is too large")
        if (
            len(content) != record.size_bytes
            or hashlib.sha256(content).hexdigest() != record.sha256
        ):
            _fail(
                WorkspaceErrorCode.WORKSPACE_DIGEST_MISMATCH,
                "candidate bytes changed while the workspace was copied",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(content)
        copied.add(posix)
    if copied != set(expected_by_path):
        _fail(
            WorkspaceErrorCode.WORKSPACE_DIGEST_MISMATCH,
            "candidate file disappeared while the workspace was copied",
        )


def _make_tree_read_only(root: Path) -> None:
    for entry in reversed(_bounded_candidate_entries(root)):
        entry.path.chmod(0o555 if entry.is_directory else 0o444)
    root.chmod(0o555)


def seal_workspace(
    attempt: AttemptWorkspace,
    package: WorkspacePackage,
    sealed_dir: Path,
    *,
    action_trace: Iterable[WorkspaceActionTraceEntry] = (),
) -> SealedWorkspace:
    """Seal candidate bytes after solver access to the attempt has ended.

    Public inputs and attempt state are excluded.  The returned process-local
    capability, or its persisted ``seal_sha256``, must be presented for replay;
    chmod is only defense against accidental mutation by the owning uid.
    """

    trace = _bounded_action_trace(action_trace)
    records = _candidate_manifest(attempt.elt_dir)
    _verify_attempt(attempt, package)
    submission = WorkspaceSubmission(
        task_id=package.task_id,
        artifact_sha256=workspace_artifact_digest(records),
        files=records,
        action_trace=trace,
    )
    claim = _claim_fresh_target(
        Path(sealed_dir),
        forbidden_roots=(package.release_dir, attempt.root),
    )
    seal_sha256 = ""
    try:
        snapshot_elt = claim.target / "elt"
        _copy_candidate_files(attempt.elt_dir, snapshot_elt, expected=records)
        copied = _candidate_manifest(snapshot_elt)
        if copied != records:
            _fail(
                WorkspaceErrorCode.WORKSPACE_DIGEST_MISMATCH,
                "sealed candidate bytes changed while copying",
            )
        _make_tree_read_only(snapshot_elt)
        manifest_path = claim.target / "workspace.json"
        manifest_bytes = (submission.model_dump_json(indent=2) + "\n").encode("utf-8")
        if len(manifest_bytes) > MAX_WORKSPACE_MANIFEST_BYTES:
            _fail(
                WorkspaceErrorCode.WORKSPACE_SIZE_LIMIT,
                "sealed workspace manifest is too large",
            )
        manifest_path.write_bytes(manifest_bytes)
        seal_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        manifest_path.chmod(0o444)
    except BaseException:
        _remove_staging(claim.target)
        raise
    try:
        return load_sealed_workspace(
            claim.target,
            expected_seal_sha256=seal_sha256,
        )
    except BaseException:
        _remove_staging(claim.target)
        raise


def load_sealed_workspace(
    path: Path,
    *,
    expected_seal_sha256: str | None = None,
) -> SealedWorkspace:
    """Load and byte-verify a sealed workspace.

    Supplying the harness-retained digest authenticates the snapshot and gives
    the returned object a process-local replay capability.  Loading without it
    is useful for inspection, but the result cannot authorize hidden replay.
    """

    lexical = _reject_symlink_components(Path(path))
    try:
        root = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceLifecycleError(
            WorkspaceErrorCode.SUBMISSION_INVALID,
            "sealed workspace is missing",
        ) from exc
    manifest_path = root / "workspace.json"
    elt_dir = root / "elt"
    try:
        root_status = os.lstat(root)
        if not stat.S_ISDIR(root_status.st_mode):
            _fail(
                WorkspaceErrorCode.SUBMISSION_INVALID,
                "sealed workspace root is not a directory",
            )
        with os.scandir(root) as children:
            top_level = list(islice(children, 3))
            if len(top_level) > 2:
                _fail(
                    WorkspaceErrorCode.SUBMISSION_INVALID,
                    "sealed workspace contains undeclared top-level artifacts",
                )
        names = {child.name for child in top_level}
        if names != {"elt", "workspace.json"}:
            _fail(
                WorkspaceErrorCode.SUBMISSION_INVALID,
                "sealed workspace contains undeclared top-level artifacts",
            )
        for child in top_level:
            if child.is_symlink():
                _fail(
                    WorkspaceErrorCode.WORKSPACE_SYMLINK,
                    "sealed workspace contains a symlink",
                )
            valid_type = (
                child.is_dir(follow_symlinks=False)
                if child.name == "elt"
                else child.is_file(follow_symlinks=False)
            )
            if not valid_type:
                _fail(
                    WorkspaceErrorCode.WORKSPACE_NON_REGULAR_FILE,
                    "sealed workspace contains a non-regular top-level artifact",
                )
    except WorkspaceLifecycleError:
        raise
    except OSError as exc:
        raise WorkspaceLifecycleError(
            WorkspaceErrorCode.SUBMISSION_INVALID,
            "sealed workspace could not be inspected",
        ) from exc

    manifest_bytes = _read_bounded_regular_file(
        manifest_path,
        max_bytes=MAX_WORKSPACE_MANIFEST_BYTES,
        label="sealed workspace manifest",
    )
    seal_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if (
        expected_seal_sha256 is not None
        and not hmac.compare_digest(seal_sha256, expected_seal_sha256)
    ):
        _fail(
            WorkspaceErrorCode.WORKSPACE_DIGEST_MISMATCH,
            "sealed workspace does not match the harness-retained seal",
        )
    try:
        submission = WorkspaceSubmission.model_validate_json(manifest_bytes)
    except ValueError as exc:
        raise WorkspaceLifecycleError(
            WorkspaceErrorCode.SUBMISSION_INVALID,
            "sealed workspace manifest is invalid",
        ) from exc
    actual = _candidate_manifest(elt_dir)
    if (
        actual != submission.files
        or workspace_artifact_digest(actual) != submission.artifact_sha256
    ):
        _fail(
            WorkspaceErrorCode.WORKSPACE_DIGEST_MISMATCH,
            "sealed workspace bytes do not match its manifest",
        )
    return SealedWorkspace(
        root=root,
        elt_dir=elt_dir,
        manifest_path=manifest_path,
        submission=submission,
        seal_sha256=seal_sha256,
        _trusted_token=(
            _seal_token(seal_sha256)
            if expected_seal_sha256 is not None
            else None
        ),
    )


def replay_workspace(
    package: WorkspacePackage,
    sealed: SealedWorkspace | Path,
    attempt_dir: Path,
    *,
    expected_seal_sha256: str | None = None,
) -> AttemptWorkspace:
    """Install one clean attempt from an authenticated candidate seal."""

    if isinstance(sealed, SealedWorkspace):
        if expected_seal_sha256 is None:
            if not _has_trusted_seal(sealed):
                _fail(
                    WorkspaceErrorCode.SUBMISSION_INVALID,
                    "replay requires a harness-authenticated sealed workspace",
                )
            expected_seal_sha256 = sealed.seal_sha256
        elif not hmac.compare_digest(expected_seal_sha256, sealed.seal_sha256):
            _fail(
                WorkspaceErrorCode.WORKSPACE_DIGEST_MISMATCH,
                "sealed workspace object and retained seal disagree",
            )
        sealed_path = sealed.root
    else:
        if expected_seal_sha256 is None:
            _fail(
                WorkspaceErrorCode.SUBMISSION_INVALID,
                "path replay requires the harness-retained seal digest",
            )
        sealed_path = Path(sealed)
    verified = load_sealed_workspace(
        sealed_path,
        expected_seal_sha256=expected_seal_sha256,
    )
    if verified.submission.task_id != package.task_id:
        _fail(WorkspaceErrorCode.TASK_ID_MISMATCH, "sealed workspace belongs to another task")
    lexical_attempt = _reject_symlink_components(Path(attempt_dir)).resolve()
    if _paths_overlap(lexical_attempt, verified.root):
        _fail(
            WorkspaceErrorCode.WORKSPACE_PATH_ESCAPE,
            "replay target overlaps the sealed workspace",
        )
    attempt = install_workspace(package, attempt_dir)
    try:
        for child in tuple(attempt.elt_dir.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        _copy_candidate_files(
            verified.elt_dir,
            attempt.elt_dir,
            expected=verified.submission.files,
        )
        for entry in _bounded_candidate_entries(attempt.elt_dir):
            entry.path.chmod(0o755 if entry.is_directory else 0o644)
        attempt.elt_dir.chmod(0o755)
        replayed = _candidate_manifest(attempt.elt_dir)
        if (
            replayed != verified.submission.files
            or workspace_artifact_digest(replayed)
            != verified.submission.artifact_sha256
        ):
            _fail(
                WorkspaceErrorCode.WORKSPACE_DIGEST_MISMATCH,
                "replayed candidate bytes do not match the sealed workspace",
            )
        _verify_attempt(attempt, package)
        return attempt
    except BaseException:
        # This path is owned by this call and was absent on entry.
        _remove_staging(attempt.root)
        raise


__all__ = [
    "AttemptWorkspace",
    "SealedWorkspace",
    "WorkspaceLifecycleError",
    "install_workspace",
    "load_attempt_workspace",
    "load_sealed_workspace",
    "replay_workspace",
    "seal_workspace",
]

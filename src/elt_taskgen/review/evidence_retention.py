"""Apply owner-only permissions and bounded retention to council evidence.

Retention removes whole stores only, preserving internal trajectory links. Deletion
requires an applied, store-specific plan digest and records a durable recovery receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


TRANSCRIPT_RETENTION_DAYS = 365
# Raw validator bytes are digest-linked from transcript trajectory records.
# They therefore share the transcript horizon and may be purged only after the
# referencing transcript store is gone.
TOOL_RAW_RETENTION_DAYS = TRANSCRIPT_RETENTION_DAYS
EVIDENCE_DIR_MODE = 0o700
EVIDENCE_FILE_MODE = 0o600
RETENTION_SCHEMA = "elt-taskgen-evidence-retention-v1"
QUARANTINE_DIRNAME = ".evidence-retention-quarantine"
ADMISSION_RECORD_RELATIVE = Path("state") / "council.live_admitted"

# Reports and state are hardened but deliberately never aged out here.
# In particular, council admission/tombstone and metrology records are durable
# evidence rather than disposable caches.
PERMISSION_STORES = ("transcripts", "tool_raw", "reports", "state")
RETENTION_POLICIES = {
    "transcripts": TRANSCRIPT_RETENTION_DAYS,
    "tool_raw": TOOL_RAW_RETENTION_DAYS,
}


class EvidenceMaintenanceError(RuntimeError):
    """An evidence tree is unsafe to traverse or mutate."""


@dataclass(frozen=True)
class PermissionReport:
    root: Path
    directories: int = 0
    files: int = 0
    changed: int = 0


@dataclass(frozen=True)
class RetentionCandidate:
    """One complete evidence store eligible under its age policy."""

    kind: str
    root: Path
    retention_days: int
    directories: int
    files: int
    bytes: int
    newest_mtime_ns: int
    tree_sha256: str
    plan_sha256: str


@dataclass(frozen=True)
class RetentionApplication:
    """The durable result of one explicitly confirmed whole-store purge."""

    kind: str
    root: Path
    receipt: Path
    plan_sha256: str
    state: str
    replacement_store_present: bool


@dataclass(frozen=True)
class _FileIdentity:
    relative: str
    size: int
    mtime_ns: int
    sha256: str


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_document(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _clock(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("retention clock must be timezone-aware")
    return moment.astimezone(timezone.utc)


def _timestamp(moment: datetime | None = None) -> str:
    return _clock(moment).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _datetime_ns(moment: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = moment - epoch
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise EvidenceMaintenanceError(f"cannot inspect evidence path {path}: {exc}") from exc


def _entries(root: Path) -> tuple[list[Path], list[Path]]:
    """Return directories/files without following or accepting symlinks.

    ``Path.exists`` is intentionally not used for the root because it silently
    treats a dangling symlink as absent. ``os.walk`` errors are also promoted;
    a partial walk is never a complete retention plan.
    """

    root = Path(root)
    try:
        root_mode = root.lstat().st_mode
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        raise EvidenceMaintenanceError(
            f"cannot inspect evidence root {root}: {exc}"
        ) from exc
    if stat.S_ISLNK(root_mode):
        raise EvidenceMaintenanceError(f"evidence root must not be a symlink: {root}")
    if not stat.S_ISDIR(root_mode):
        raise EvidenceMaintenanceError(f"evidence root must be a directory: {root}")

    directories = [root]
    files: list[Path] = []

    def fail(exc: OSError) -> None:
        raise EvidenceMaintenanceError(
            f"cannot completely traverse evidence root {root}: {exc}"
        ) from exc

    for current, dirnames, filenames in os.walk(
        root, topdown=True, followlinks=False, onerror=fail
    ):
        base = Path(current)
        dirnames.sort()
        filenames.sort()
        for name in dirnames:
            path = base / name
            mode = _lstat(path).st_mode
            if stat.S_ISLNK(mode):
                raise EvidenceMaintenanceError(
                    f"evidence tree must not contain symlinks: {path}"
                )
            if not stat.S_ISDIR(mode):
                raise EvidenceMaintenanceError(
                    f"evidence directory entry is not a directory: {path}"
                )
            directories.append(path)
        for name in filenames:
            path = base / name
            mode = _lstat(path).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise EvidenceMaintenanceError(
                    f"evidence file entry is not a regular file: {path}"
                )
            files.append(path)
    return sorted(directories), sorted(files)


def _open_verified(path: Path, *, directory: bool) -> tuple[int, os.stat_result]:
    """Open ``path`` without following its final component and verify its type."""

    before = _lstat(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvidenceMaintenanceError(f"cannot safely open evidence path {path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected_type(opened.st_mode):
            raise EvidenceMaintenanceError(
                f"evidence path changed type while opening it: {path}"
            )
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise EvidenceMaintenanceError(
                f"evidence path changed while opening it: {path}"
            )
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _chmod_verified(path: Path, *, directory: bool, mode: int) -> bool:
    descriptor, opened = _open_verified(path, directory=directory)
    try:
        changed = stat.S_IMODE(opened.st_mode) != mode
        if changed:
            os.fchmod(descriptor, mode)
        return changed
    except OSError as exc:
        raise EvidenceMaintenanceError(
            f"cannot restrict evidence permissions for {path}: {exc}"
        ) from exc
    finally:
        os.close(descriptor)


def harden_permissions(root: Path) -> PermissionReport:
    """Idempotently restrict one existing evidence tree to its owner."""

    directories, files = _entries(Path(root))
    changed = 0
    for path in directories:
        changed += int(
            _chmod_verified(path, directory=True, mode=EVIDENCE_DIR_MODE)
        )
    for path in files:
        changed += int(
            _chmod_verified(path, directory=False, mode=EVIDENCE_FILE_MODE)
        )
    return PermissionReport(
        root=Path(root), directories=len(directories), files=len(files), changed=changed
    )


def harden_workspace(workspace: Path) -> tuple[PermissionReport, ...]:
    """Harden mutable council evidence roots belonging to one workspace.

    Transcript and raw-tool stores are included, as are the top-level
    metrology ``reports`` and admission ``state`` roots. Only the first two
    have an age-based deletion policy.
    """

    workspace = Path(workspace)
    return tuple(harden_permissions(workspace / name) for name in PERMISSION_STORES)


def _file_identity(root: Path, path: Path) -> _FileIdentity:
    descriptor, before = _open_verified(path, directory=False)
    digest = hashlib.sha256()
    try:
        while True:
            payload = os.read(descriptor, 1024 * 1024)
            if not payload:
                break
            digest.update(payload)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise EvidenceMaintenanceError(f"cannot hash evidence file {path}: {exc}") from exc
    finally:
        os.close(descriptor)
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise EvidenceMaintenanceError(f"evidence file changed while hashing it: {path}")
    return _FileIdentity(
        relative=path.relative_to(root).as_posix(),
        size=int(after.st_size),
        mtime_ns=int(after.st_mtime_ns),
        sha256=digest.hexdigest(),
    )


def _tree_identity(
    root: Path, directories: Sequence[Path], files: Sequence[Path]
) -> tuple[int, int, str]:
    """Return newest-file mtime, byte count and a stable full-tree digest."""

    digest = hashlib.sha256()
    relative_directories = sorted(
        path.relative_to(root).as_posix() for path in directories if path != root
    )
    for relative in relative_directories:
        digest.update(b"d\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\n")
    identities = [_file_identity(root, path) for path in files]
    for identity in identities:
        digest.update(b"f\0")
        digest.update(identity.relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(identity.sha256.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(identity.size).encode("ascii"))
        digest.update(b"\n")

    # Detect additions/removals/renames that occurred during the hashing pass.
    again_directories, again_files = _entries(root)
    if [p.relative_to(root).as_posix() for p in directories] != [
        p.relative_to(root).as_posix() for p in again_directories
    ] or [p.relative_to(root).as_posix() for p in files] != [
        p.relative_to(root).as_posix() for p in again_files
    ]:
        raise EvidenceMaintenanceError(f"evidence tree changed while hashing it: {root}")

    newest = max((identity.mtime_ns for identity in identities), default=0)
    total = sum(identity.size for identity in identities)
    return newest, total, digest.hexdigest()


def _candidate_document(
    *,
    workspace: Path,
    kind: str,
    root: Path,
    retention_days: int,
    directories: int,
    files: int,
    total_bytes: int,
    newest_mtime_ns: int,
    tree_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": RETENTION_SCHEMA,
        "action": "purge_whole_store",
        "workspace": str(workspace),
        "kind": kind,
        "root": str(root),
        "retention_days": retention_days,
        "directories": directories,
        "files": files,
        "bytes": total_bytes,
        "newest_mtime_ns": newest_mtime_ns,
        "tree_sha256": tree_sha256,
    }


def _candidate_for_root(
    workspace: Path,
    kind: str,
    root: Path,
    *,
    now: datetime,
    require_expired: bool,
) -> RetentionCandidate | None:
    days = RETENTION_POLICIES[kind]
    directories, files = _entries(root)
    if not files:
        return None
    newest, total, tree_digest = _tree_identity(root, directories, files)
    cutoff_ns = _datetime_ns(now) - days * 86_400 * 1_000_000_000
    if require_expired and newest > cutoff_ns:
        return None
    document = _candidate_document(
        workspace=workspace,
        kind=kind,
        root=root,
        retention_days=days,
        directories=len(directories),
        files=len(files),
        total_bytes=total,
        newest_mtime_ns=newest,
        tree_sha256=tree_digest,
    )
    return RetentionCandidate(
        kind=kind,
        root=root,
        retention_days=days,
        directories=len(directories),
        files=len(files),
        bytes=total,
        newest_mtime_ns=newest,
        tree_sha256=tree_digest,
        plan_sha256=_sha256_document(document),
    )


def retention_candidates(
    workspace: Path,
    *,
    now: datetime | None = None,
) -> tuple[RetentionCandidate, ...]:
    """Plan complete stores whose newest file is older than policy.

    The plan is read-only. Each candidate carries its own exact digest so an
    operator applies at most one complete store in a transaction.
    """

    moment = _clock(now)
    resolved = Path(workspace).resolve()
    candidates: list[RetentionCandidate] = []
    for kind in RETENTION_POLICIES:
        candidate = _candidate_for_root(
            resolved,
            kind,
            resolved / kind,
            now=moment,
            require_expired=True,
        )
        if candidate is not None:
            candidates.append(candidate)
    return tuple(candidates)


def _candidate_payload(candidate: RetentionCandidate) -> dict[str, Any]:
    payload = asdict(candidate)
    payload["root"] = str(candidate.root)
    return payload


def retention_plan(workspace: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Return the stable, content-free operator plan document."""

    moment = _clock(now)
    resolved = Path(workspace).resolve()
    candidates: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    for candidate in retention_candidates(resolved, now=moment):
        blocker = _retention_blocker(resolved, candidate.kind)
        if blocker is not None:
            refusals.append(
                {
                    "kind": candidate.kind,
                    **blocker,
                    "candidate": _candidate_payload(candidate),
                }
            )
        else:
            candidates.append(_candidate_payload(candidate))
    return {
        "schema_version": RETENTION_SCHEMA,
        "mode": "read_only_plan",
        "workspace": str(resolved),
        "evaluated_at": _timestamp(moment),
        "policies_days": dict(RETENTION_POLICIES),
        "candidates": candidates,
        "refusals": refusals,
    }


def _admission_record(workspace: Path) -> Path | None:
    """Return the local council admission record when present, without reading it."""

    path = Path(workspace) / ADMISSION_RECORD_RELATIVE
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EvidenceMaintenanceError(
            f"cannot inspect council admission record {path}: {exc}"
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise EvidenceMaintenanceError(
            f"council admission record is not a regular file: {path}"
        )
    return path


def _retention_blocker(workspace: Path, kind: str) -> dict[str, Any] | None:
    """Why a currently expired store still cannot be safely purged."""

    admission = _admission_record(workspace)
    if admission is not None:
        return {
            "code": "admission_record_present",
            "admission_record": str(admission),
            "reason": (
                f"{kind} evidence cannot be purged while the council admission "
                "record exists; revoke or replace admission first"
            ),
        }
    if kind == "tool_raw":
        transcript_root = Path(workspace) / "transcripts"
        _directories, transcript_files = _entries(transcript_root)
        if transcript_files:
            return {
                "code": "transcript_store_present",
                "transcript_store": str(transcript_root),
                "reason": (
                    "raw validator outputs cannot be purged while transcript "
                    "trajectories that bind their digests remain"
                ),
            }
    return None


def _validated_workspace(workspace: Path) -> Path:
    path = Path(workspace)
    mode = _lstat(path).st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise EvidenceMaintenanceError(
            f"maintenance workspace must be a real directory, not a symlink: {path}"
        )
    resolved = path.resolve(strict=True)
    protected = {Path(resolved.anchor), Path.home().resolve()}
    try:
        from elt_taskgen.workspace import repo_root

        checkout = repo_root().resolve()
        protected.update({checkout, checkout / "runs"})
    except (ImportError, OSError):  # pragma: no cover - installed fallback
        pass
    if resolved in protected:
        raise EvidenceMaintenanceError(
            f"refusing broad maintenance workspace {resolved}; name one workspace, "
            "not a filesystem/home/repository/container root"
        )
    return resolved


def _ensure_private_directory(directory: Path) -> None:
    """Create missing receipt/quarantine parents owner-only, symlink-free."""

    target = Path(directory)
    missing: list[Path] = []
    cursor = target
    while True:
        try:
            mode = cursor.lstat().st_mode
        except FileNotFoundError:
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise EvidenceMaintenanceError(
                    f"cannot find an existing parent for {target}"
                )
            cursor = parent
            continue
        except OSError as exc:
            raise EvidenceMaintenanceError(
                f"cannot inspect maintenance directory {cursor}: {exc}"
            ) from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise EvidenceMaintenanceError(
                f"maintenance directory path is not a real directory: {cursor}"
            )
        break
    for path in reversed(missing):
        try:
            path.mkdir(mode=EVIDENCE_DIR_MODE)
        except FileExistsError:
            mode = _lstat(path).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise EvidenceMaintenanceError(
                    f"maintenance directory was replaced during creation: {path}"
                )
        _chmod_verified(path, directory=True, mode=EVIDENCE_DIR_MODE)


def _fsync_directory(directory: Path) -> None:
    descriptor, _opened = _open_verified(directory, directory=True)
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise EvidenceMaintenanceError(
            f"cannot fsync maintenance directory {directory}: {exc}"
        ) from exc
    finally:
        os.close(descriptor)


def _staged_private_bytes(path: Path, payload: bytes) -> Path:
    _ensure_private_directory(path.parent)
    descriptor, raw = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.stage-")
    temporary = Path(raw)
    try:
        os.fchmod(descriptor, EVIDENCE_FILE_MODE)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short evidence-receipt write")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    return temporary


def _staged_receipt(path: Path, payload: Mapping[str, Any]) -> Path:
    return _staged_private_bytes(path, _canonical_bytes(payload))


def write_private_text(path: Path, payload: str) -> Path:
    """Atomically publish mutable evidence with owner-only permissions."""

    target = Path(path)
    try:
        mode = target.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise EvidenceMaintenanceError(
                f"private evidence target is not a regular file: {target}"
            )
    temporary = _staged_private_bytes(target, str(payload).encode("utf-8"))
    try:
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _publish_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = _staged_receipt(path, payload)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise EvidenceMaintenanceError(
                f"retention receipt already exists; refusing to overwrite audit evidence: {path}"
            ) from exc
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    mode = _lstat(path).st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise EvidenceMaintenanceError(f"retention receipt is no longer a regular file: {path}")
    temporary = _staged_receipt(path, payload)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _receipt_path(receipt: Path, *, workspace: Path, store_root: Path) -> Path:
    raw = Path(receipt)
    if raw.exists() or raw.is_symlink():
        raise EvidenceMaintenanceError(
            f"retention receipt path must be new: {raw}"
        )
    parent = raw.parent.resolve(strict=False)
    resolved = parent / raw.name
    quarantine = workspace / QUARANTINE_DIRNAME
    if _is_within(resolved, store_root) or _is_within(resolved, quarantine):
        raise EvidenceMaintenanceError(
            "retention receipt must be outside the store and its quarantine tree"
        )
    return resolved


def _same_candidate(left: RetentionCandidate, right: RetentionCandidate) -> bool:
    return (
        left.kind,
        left.retention_days,
        left.directories,
        left.files,
        left.bytes,
        left.newest_mtime_ns,
        left.tree_sha256,
    ) == (
        right.kind,
        right.retention_days,
        right.directories,
        right.files,
        right.bytes,
        right.newest_mtime_ns,
        right.tree_sha256,
    )


def apply_retention(
    workspace: Path,
    *,
    kind: str,
    plan_sha256: str,
    receipt: Path,
    now: datetime | None = None,
) -> RetentionApplication:
    """Purge one expired evidence store after exact-plan confirmation.

    Require the applied store and digest to match the current plan. Publish an
    owner-only receipt before quarantine, then atomically mark completion so crashes
    leave a recoverable location.
    """

    if kind not in RETENTION_POLICIES:
        raise EvidenceMaintenanceError(
            f"unsupported retention store {kind!r}; choose one of {sorted(RETENTION_POLICIES)}"
        )
    supplied_digest = str(plan_sha256 or "")
    if len(supplied_digest) != 64 or any(c not in "0123456789abcdef" for c in supplied_digest):
        raise EvidenceMaintenanceError("--plan-digest must be exactly 64 lowercase hex characters")
    moment = _clock(now)
    resolved = _validated_workspace(Path(workspace))
    store_root = resolved / kind
    candidate = _candidate_for_root(
        resolved,
        kind,
        store_root,
        now=moment,
        require_expired=True,
    )
    if candidate is None:
        days = RETENTION_POLICIES[kind]
        raise EvidenceMaintenanceError(
            f"{kind} is absent, empty, or still inside its "
            f"{days}-day retention window"
        )
    if not hmac.compare_digest(candidate.plan_sha256, supplied_digest):
        raise EvidenceMaintenanceError(
            f"retention plan changed for {kind}: current digest is "
            f"{candidate.plan_sha256}; run the read-only plan again"
        )

    receipt_path = _receipt_path(
        Path(receipt), workspace=resolved, store_root=store_root
    )
    blocker = _retention_blocker(resolved, kind)
    if blocker is not None:
        refused = {
            "schema_version": RETENTION_SCHEMA,
            "action": "purge_whole_store",
            "state": "refused",
            "refused_at": _timestamp(),
            "workspace": str(resolved),
            "kind": kind,
            "root": str(store_root),
            "plan_sha256": candidate.plan_sha256,
            "candidate": _candidate_payload(candidate),
            "purged": False,
            "failure": blocker["code"],
            **blocker,
        }
        _publish_receipt(receipt_path, refused)
        raise EvidenceMaintenanceError(
            f"refusing to purge {kind}: {blocker['reason']} "
            f"(refusal receipt: {receipt_path})"
        )
    quarantine_base = resolved / QUARANTINE_DIRNAME
    _ensure_private_directory(quarantine_base)
    transaction = Path(
        tempfile.mkdtemp(
            prefix=f"{kind}-{candidate.plan_sha256[:12]}-",
            dir=quarantine_base,
        )
    )
    _chmod_verified(transaction, directory=True, mode=EVIDENCE_DIR_MODE)
    quarantine_target = transaction / kind
    prepared: dict[str, Any] = {
        "schema_version": RETENTION_SCHEMA,
        "action": "purge_whole_store",
        "state": "prepared",
        "prepared_at": _timestamp(),
        "workspace": str(resolved),
        "kind": kind,
        "root": str(store_root),
        "quarantine": str(quarantine_target),
        "plan_sha256": candidate.plan_sha256,
        "candidate": _candidate_payload(candidate),
        "purged": False,
    }
    try:
        _publish_receipt(receipt_path, prepared)
    except BaseException:
        try:
            transaction.rmdir()
        except OSError:
            pass
        raise

    try:
        os.rename(store_root, quarantine_target)
        _fsync_directory(resolved)
        _fsync_directory(transaction)
    except OSError as exc:
        failed = {
            **prepared,
            "state": "aborted_before_quarantine",
            "failure": type(exc).__name__,
        }
        _replace_receipt(receipt_path, failed)
        try:
            transaction.rmdir()
        except OSError:
            pass
        raise EvidenceMaintenanceError(
            f"could not move {kind} into quarantine; nothing was purged: {exc}"
        ) from exc

    quarantined = {
        **prepared,
        "state": "quarantined",
        "quarantined_at": _timestamp(),
    }
    _replace_receipt(receipt_path, quarantined)

    try:
        moved = _candidate_for_root(
            resolved,
            kind,
            quarantine_target,
            now=moment,
            require_expired=False,
        )
    except BaseException as exc:
        recovery = {
            **quarantined,
            "state": "recovery_required",
            "failure": type(exc).__name__,
        }
        _replace_receipt(receipt_path, recovery)
        raise EvidenceMaintenanceError(
            f"quarantined {kind} could not be reverified; it was preserved at "
            f"{quarantine_target} and requires operator recovery"
        ) from exc
    if moved is None or not _same_candidate(candidate, moved):
        # Restore only when no concurrent writer recreated the live name. If
        # it did, overwriting it would lose new evidence, so preserve both and
        # point the receipt at the quarantined original.
        if not store_root.exists() and not store_root.is_symlink():
            try:
                os.rename(quarantine_target, store_root)
                _fsync_directory(resolved)
                state = "aborted_and_restored"
            except OSError:
                state = "recovery_required"
        else:
            state = "recovery_required"
        mismatch = {
            **quarantined,
            "state": state,
            "failure": "post_quarantine_identity_mismatch",
        }
        _replace_receipt(receipt_path, mismatch)
        raise EvidenceMaintenanceError(
            f"{kind} changed during quarantine; no unverified tree was purged "
            f"(receipt: {receipt_path})"
        )

    try:
        shutil.rmtree(quarantine_target)
        _fsync_directory(transaction)
    except OSError as exc:
        # The last durable state is intentionally still "quarantined". It
        # names the recovery directory and does not falsely claim deletion.
        raise EvidenceMaintenanceError(
            f"purge did not complete; quarantined evidence remains at "
            f"{quarantine_target} (receipt: {receipt_path}): {exc}"
        ) from exc
    try:
        transaction.rmdir()
        _fsync_directory(quarantine_base)
    except OSError:
        # An empty transaction directory is harmless; the evidence itself is
        # already gone. Completion remains an honest statement about the store.
        pass

    complete = {
        **quarantined,
        "state": "complete",
        "completed_at": _timestamp(),
        "purged": True,
        "replacement_store_present": store_root.exists() or store_root.is_symlink(),
    }
    _replace_receipt(receipt_path, complete)
    return RetentionApplication(
        kind=kind,
        root=store_root,
        receipt=receipt_path,
        plan_sha256=candidate.plan_sha256,
        state="complete",
        replacement_store_present=bool(complete["replacement_store_present"]),
    )


def _permission_payload(report: PermissionReport) -> dict[str, Any]:
    return {
        "root": str(report.root.resolve()),
        "directories": report.directories,
        "files": report.files,
        "changed": report.changed,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="elt-taskgen-evidence-maintenance",
        description=(
            "Plan bounded council-evidence retention (default), explicitly "
            "harden legacy permissions, or purge one exact expired whole store."
        ),
    )
    parser.add_argument("--workspace", required=True, type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--harden",
        action="store_true",
        help="chmod existing evidence roots to owner-only modes",
    )
    action.add_argument(
        "--apply",
        action="store_true",
        help="purge one expired whole store after exact plan confirmation",
    )
    parser.add_argument("--kind", choices=tuple(RETENTION_POLICIES))
    parser.add_argument("--plan-digest")
    parser.add_argument("--receipt", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workspace = _validated_workspace(args.workspace)
        if args.apply:
            missing = [
                flag
                for flag, value in (
                    ("--kind", args.kind),
                    ("--plan-digest", args.plan_digest),
                    ("--receipt", args.receipt),
                )
                if value is None
            ]
            if missing:
                raise EvidenceMaintenanceError(
                    "--apply also requires " + ", ".join(missing)
                )
            result = apply_retention(
                workspace,
                kind=args.kind,
                plan_sha256=args.plan_digest,
                receipt=args.receipt,
            )
            payload: Mapping[str, Any] = {
                "schema_version": RETENTION_SCHEMA,
                "mode": "apply",
                "result": {
                    **asdict(result),
                    "root": str(result.root),
                    "receipt": str(result.receipt),
                },
            }
        elif args.harden:
            if args.kind or args.plan_digest or args.receipt:
                raise EvidenceMaintenanceError(
                    "--kind, --plan-digest, and --receipt are valid only with --apply"
                )
            payload = {
                "schema_version": RETENTION_SCHEMA,
                "mode": "harden",
                "workspace": str(workspace),
                "reports": [
                    _permission_payload(report)
                    for report in harden_workspace(workspace)
                ],
            }
        else:
            if args.kind or args.plan_digest or args.receipt:
                raise EvidenceMaintenanceError(
                    "--kind, --plan-digest, and --receipt are valid only with --apply"
                )
            payload = retention_plan(workspace)
        print(_canonical_bytes(payload).decode("utf-8"))
        return 0
    except (EvidenceMaintenanceError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "ADMISSION_RECORD_RELATIVE",
    "EVIDENCE_DIR_MODE",
    "EVIDENCE_FILE_MODE",
    "EvidenceMaintenanceError",
    "PERMISSION_STORES",
    "PermissionReport",
    "QUARANTINE_DIRNAME",
    "RETENTION_POLICIES",
    "RETENTION_SCHEMA",
    "RetentionApplication",
    "RetentionCandidate",
    "TOOL_RAW_RETENTION_DAYS",
    "TRANSCRIPT_RETENTION_DAYS",
    "apply_retention",
    "build_parser",
    "harden_permissions",
    "harden_workspace",
    "main",
    "retention_candidates",
    "retention_plan",
    "write_private_text",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

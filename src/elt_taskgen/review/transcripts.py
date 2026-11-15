"""Durable private transcript storage and immutable raw evidence publication."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from elt_taskgen.models import readable_json
from elt_taskgen.review import trajectory as _trajectory


def _entry_path(root: Path, role_name: str, prompt_sha: str) -> Path:
    return root / role_name / f"{prompt_sha}.json"


def _displaced_entry_path(
    root: Path, role_name: str, prompt_sha: str, response_sha: str
) -> Path:
    """Where an entry displaced by a DIFFERENT response to the same prompt is
    kept. Named by both digests, so every distinct observation of one prompt
    survives and any evidence row naming it stays verifiable."""
    return root / role_name / f"{prompt_sha}.{response_sha[:16]}.json"


def _read_entry_if_any(path: Path) -> dict | None:
    """The recorded entry at `path`, or None when it is absent or unreadable.
    Never raises: a corrupt neighbour must not stop a live response being
    recorded (the reader's own `lookup` fails closed on corruption)."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


#: The per-role subdirectory of a transcript store holding SESSION RECORDS
#: (`TranscriptStore.record_session`): one JSON per completed bounded session
#: under its session key (the turn-0 memo key), beside — never among — the
#: role's turn entries, so every `<role>/*.json` walker still sees entries only.
SESSION_RECORDS_SUBDIR = "sessions"


def _session_record_path(root: Path, role_name: str, session_key: str) -> Path:
    return root / role_name / SESSION_RECORDS_SUBDIR / f"{session_key}.json"


def _fsync_directory(directory: Path) -> None:
    """Make a completed publication durable before its caller returns."""

    path = Path(directory)
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise RuntimeError(f"evidence directory is not a real directory: {path}")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise RuntimeError(f"evidence directory changed while opening it: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _absolute_lexical(path: Path) -> Path:
    """Absolute path without resolving (and thereby hiding) symlinks."""

    return Path(os.path.abspath(os.fspath(path)))


def _directory_chain(directory: Path, boundary: Path) -> tuple[Path, ...]:
    """``boundary`` through ``directory``, rejecting lexical escapes."""

    target = _absolute_lexical(Path(directory))
    root = _absolute_lexical(Path(boundary))
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise RuntimeError(
            f"evidence directory {target} escapes its store root {root}"
        ) from None
    chain = [root]
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        chain.append(cursor)
    return tuple(chain)


def _ensure_private_directory(directory: Path, *, boundary: Path | None = None) -> None:
    """Create an owner-only evidence directory without following planted symlinks.

    Existing ancestors are not chmodded implicitly, and every existing component must be
    a real directory.
    """

    target = _absolute_lexical(Path(directory))
    root = target if boundary is None else _absolute_lexical(Path(boundary))
    # A caller may name a fresh workspace/output whose parents do not exist
    # yet. Find one real existing anchor, but treat ``root`` itself as the
    # security boundary and validate every component from there downward.
    missing_parents: list[Path] = []
    cursor = root.parent
    while True:
        try:
            parent_mode = cursor.lstat().st_mode
        except FileNotFoundError:
            missing_parents.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise RuntimeError(
                    f"cannot find an existing parent for evidence store {root}"
                )
            cursor = parent
            continue
        if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
            raise RuntimeError(
                f"evidence store parent is not a real directory: {cursor}"
            )
        break
    missing: list[Path] = list(reversed(missing_parents))
    for cursor in _directory_chain(target, root):
        try:
            mode = cursor.lstat().st_mode
        except FileNotFoundError:
            missing.append(cursor)
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise RuntimeError(
                f"evidence directory path is not a real directory: {cursor}"
            )
    for created in missing:
        try:
            created.mkdir(mode=0o700)
        except FileExistsError:
            pass
        before = created.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise RuntimeError(
                f"evidence directory was replaced during creation: {created}"
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_DIRECTORY", 0)
        )
        descriptor = os.open(created, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise RuntimeError(
                    f"evidence directory changed during creation: {created}"
                )
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)


def _staged_bytes(
    path: Path, payload: bytes, *, boundary: Path | None = None
) -> Path:
    """Write and fsync ``payload`` under a private same-directory name."""

    target = Path(path)
    _ensure_private_directory(target.parent, boundary=boundary or target.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.stage-",
    )
    temporary = Path(temporary_name)
    try:
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short transcript-store write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _atomic_replace_bytes(
    path: Path, payload: bytes, *, boundary: Path | None = None
) -> None:
    """Atomically replace a mutable memo/session record with complete bytes."""

    target = Path(path)
    temporary = _staged_bytes(target, payload, boundary=boundary)
    try:
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_bytes_once(
    path: Path, payload: bytes, *, boundary: Path | None = None
) -> bool:
    """Atomically publish immutable bytes; return False when a winner exists.

    A hard link is the exclusive commit point.  The staged inode is complete
    and fsynced before the final name can become visible, so a process death can
    leave an unused staging name but never a truncated final artifact.
    """

    target = Path(path)
    temporary = _staged_bytes(target, payload, boundary=boundary)
    published = False
    try:
        try:
            os.link(temporary, target)
        except FileExistsError:
            return False
        published = True
        return True
    finally:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            if published:
                _fsync_directory(target.parent)


def _read_regular_bytes(path: Path, *, boundary: Path | None = None) -> bytes | None:
    """Read one stable evidence file without following a symlink.

    ``None`` means absent. Any present non-regular path, replacement race, or
    in-place change is corrupt evidence and fails closed.
    """

    target = _absolute_lexical(Path(path))
    if boundary is not None:
        for directory in _directory_chain(target.parent, Path(boundary)):
            try:
                mode = directory.lstat().st_mode
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise RuntimeError(
                    f"evidence directory path is not a real directory: {directory}"
                )
    try:
        before = target.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"evidence path is not a regular file: {target}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise RuntimeError(f"cannot safely open evidence file: {target}") from exc
    chunks: list[bytes] = []
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise RuntimeError(f"evidence file changed while opening it: {target}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(opened, name) != getattr(after, name) for name in fields):
            raise RuntimeError(f"evidence file changed while reading it: {target}")
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _publish_raw_output(
    path: Path,
    payload: bytes,
    expected_sha256: str,
    *,
    boundary: Path | None = None,
) -> None:
    """Publish-or-confirm one immutable raw validator output, fail closed."""

    expected = str(expected_sha256 or "")
    candidate_digest = hashlib.sha256(payload).hexdigest()
    published = False
    if not expected or candidate_digest == expected:
        published = _publish_bytes_once(path, payload, boundary=boundary)
    if published:
        visible = payload
    else:
        visible = _read_regular_bytes(Path(path), boundary=boundary)
        if visible is None:
            raise RuntimeError(f"immutable raw validator output is unavailable: {path}")
    visible_digest = hashlib.sha256(visible).hexdigest()
    if (expected and visible_digest != expected) or (
        not expected and visible != payload
    ):
        raise RuntimeError(
            f"immutable raw validator output conflicts with its trajectory: {path}"
        )


def _confirm_raw_output(
    path: Path, expected_sha256: str, *, boundary: Path | None = None
) -> None:
    """Require an already-published raw output to reproduce its sealed digest."""

    visible = _read_regular_bytes(Path(path), boundary=boundary)
    if visible is None:
        raise RuntimeError(f"immutable raw validator output is unavailable: {path}")
    if hashlib.sha256(visible).hexdigest() != str(expected_sha256):
        raise RuntimeError(
            f"immutable raw validator output conflicts with its trajectory: {path}"
        )


class TranscriptStore:
    """Recorded (role, prompt_sha256) -> raw response entries.

    Lookup order: the workspace record dir FIRST, then committed fixtures as a
    fallback seed — what `--record` just paid for must never be shadowed by a
    same-key fixture. Entries are canonical JSON at `<dir>/<role>/<sha256>.json`;
    recording always targets `record_dir`.
    """

    def __init__(self, record_dir: Path | None, *, fixtures_dir: Path | None = None):
        self.record_dir = record_dir
        self.fixtures_dir = fixtures_dir

    def _search_dirs(self) -> list[Path]:
        return [d for d in (self.record_dir, self.fixtures_dir) if d is not None]

    def lookup(self, role_name: str, prompt_sha: str) -> dict | None:
        for root in self._search_dirs():
            path = _entry_path(root, role_name, prompt_sha)
            payload = _read_regular_bytes(path, boundary=root)
            if payload is not None:
                entry = json.loads(payload.decode("utf-8"))
                # The filename IS the key, but the entry must SAY so: an entry
                # with a missing or different prompt_sha256 is corrupt or
                # hand-planted, never a replay (fail closed).
                if entry.get("prompt_sha256") != prompt_sha:
                    raise RuntimeError(
                        f"transcript at {path} is bound to prompt "
                        f"{str(entry.get('prompt_sha256'))[:12]}, expected "
                        f"{prompt_sha[:12]} (corrupt store — fail closed)"
                    )
                return entry
        return None

    def record(self, role_name: str, prompt_sha: str, entry: dict) -> Path:
        if self.record_dir is None:
            raise RuntimeError(
                "TranscriptStore has no record_dir; cannot record a live "
                "response (fail closed)"
            )
        path = _entry_path(self.record_dir, role_name, prompt_sha)
        # Preserve a displaced prompt-keyed transcript under its response digest
        # so old evidence remains verifiable; replay still reads the newest entry.
        previous = _read_entry_if_any(path)
        if previous is not None:
            was = str(previous.get("response_sha256") or "")
            now = str(entry.get("response_sha256") or "")
            if was and was != now:
                kept = _displaced_entry_path(self.record_dir, role_name, prompt_sha, was)
                if not kept.exists():
                    _atomic_replace_bytes(
                        kept,
                        readable_json(previous).encode("utf-8"),
                        boundary=self.record_dir,
                    )
        _atomic_replace_bytes(
            path,
            readable_json(entry).encode("utf-8"),
            boundary=self.record_dir,
        )
        return path

    def lookup_session(self, role_name: str, session_key: str) -> dict | None:
        """The SESSION RECORD filed under (role, session key) — the digest
        chain, terminal, turn keys and the full observation digests of one
        completed bounded session (`RoutedProvider.run_session`) — or None.
        Same search order as `lookup`; a record whose stated key is not its
        filename is a corrupt store and raises (fail closed)."""
        for root in self._search_dirs():
            path = _session_record_path(root, role_name, session_key)
            payload = _read_regular_bytes(path, boundary=root)
            if payload is not None:
                record = json.loads(payload.decode("utf-8"))
                if record.get("session_key") != session_key:
                    raise RuntimeError(
                        f"session record at {path} is bound to session "
                        f"{str(record.get('session_key'))[:12]}, expected "
                        f"{session_key[:12]} (corrupt store — fail closed)"
                    )
                return record
        return None

    def record_session(self, role_name: str, session_key: str, record: dict) -> Path:
        if self.record_dir is None:
            raise RuntimeError(
                "TranscriptStore has no record_dir; cannot record a session "
                "record (fail closed)"
            )
        path = _session_record_path(self.record_dir, role_name, session_key)
        _atomic_replace_bytes(
            path,
            readable_json(record).encode("utf-8"),
            boundary=self.record_dir,
        )
        return path

    def lookup_trajectory(self, role_name: str, trajectory_sha256: str) -> dict | None:
        """The verified full trajectory named by a session record, or None.

        New bounded-session recordings link their identity-only session record
        to this content-addressed artifact. Older records omit the link and
        remain replayable; when a link is present, a missing, misfiled or
        internally inconsistent trajectory is a corrupt replay store.
        """
        digest = str(trajectory_sha256)
        for root in self._search_dirs():
            path = trajectory_record_path(root, role_name, digest)
            payload = _read_regular_bytes(path, boundary=root)
            if payload is None:
                continue
            record = json.loads(payload.decode("utf-8"))
            try:
                verified = _trajectory.verify_trajectory_record(record)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"trajectory record at {path} does not verify (corrupt store — fail closed)"
                ) from exc
            if verified != digest or path.stem != digest:
                raise RuntimeError(
                    f"trajectory record at {path} is bound to {verified[:12]}, expected "
                    f"{digest[:12]} (corrupt store — fail closed)"
                )
            return record
        return None


# Routed provider. Trials use isolated executors, and trajectory records live
# under `transcripts/trajectories/<role>/<digest>.json` away from turn entries.
TRAJECTORIES_SUBDIR = "trajectories"

#: The raw-validator-output tier, OUTSIDE the transcript store: `<ws>/tool_raw/
#: <trajectory_sha256>/<turn_index>.bin` (unsanitized output for audit and
#: sanitizer regression; never in a fixture, never read by a stage, never
#: served to a model).
TOOL_RAW_SUBDIR = "tool_raw"


def trajectories_root(record_dir: Path) -> Path:
    """`<record_dir>/trajectories` (the store's record dir is `<ws>/transcripts`)."""
    return Path(record_dir) / TRAJECTORIES_SUBDIR


def trajectory_record_path(record_dir: Path, role_name: str, trajectory_sha256: str) -> Path:
    return trajectories_root(record_dir) / role_name / f"{trajectory_sha256}.json"


def tool_raw_root(record_dir: Path) -> Path:
    """`<ws>/tool_raw` for a store recording at `<ws>/transcripts`: the tier
    sits BESIDE the transcript store, never under it, so nothing that walks
    or ships the store (fixtures, `record-transcripts`, replay) can carry a
    raw validator output. A record dir not named `transcripts` gets a
    sibling `tool_raw` all the same."""
    record_dir = Path(record_dir)
    return record_dir.parent / TOOL_RAW_SUBDIR

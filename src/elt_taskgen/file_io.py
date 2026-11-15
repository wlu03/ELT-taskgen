"""Durable replacement of mutable workspace records.

Immutable publication and symlink-confined private transcript storage have
different contracts and remain with their owners.
"""

from __future__ import annotations
import os
from pathlib import Path
import tempfile


def atomic_write(
    path: Path,
    payload: bytes | str,
    *,
    directory_mode: int = 0o777,
    prefix: str | None = None,
) -> None:
    """Stage in the target directory, fsync, replace, and fsync the directory.

    New files are private (mkstemp's 0600); directory_mode only applies when
    creating the target directory. Existing directory permissions are retained.
    Text uses the platform's normal newline translation, like open(..., 'w').
    """
    path = Path(path)
    path.parent.mkdir(mode=directory_mode, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=prefix if prefix is not None else f".{path.name}.",
        suffix=".tmp", dir=path.parent,
    )
    temporary: Path | None = Path(name)
    try:
        mode = "w" if isinstance(payload, str) else "wb"
        kwargs = {"encoding": "utf-8"} if isinstance(payload, str) else {}
        with os.fdopen(descriptor, mode, **kwargs) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

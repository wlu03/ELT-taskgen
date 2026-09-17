"""Setuptools backend wrapper that normalizes distribution container metadata.

Setuptools produces stable file contents, but ZIP/TAR timestamps, uid/gid and
archive member order otherwise depend on the build host and wall clock.  PEP
517 calls below delegate the actual build and then canonicalize only container
metadata. Wheel RECORD hashes remain valid because member bytes do not change.
"""

from __future__ import annotations

import gzip
import os
import stat
import tarfile
import zipfile
from pathlib import Path

from setuptools import build_meta as _setuptools


_DEFAULT_SOURCE_DATE_EPOCH = 946684800  # 2000-01-01 UTC


def _epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH", str(_DEFAULT_SOURCE_DATE_EPOCH))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("SOURCE_DATE_EPOCH must be an integer") from exc
    if value < 0:
        raise ValueError("SOURCE_DATE_EPOCH must not be negative")
    return value


def _normalize_wheel(path: Path) -> None:
    temporary = path.with_name(f".{path.name}.normalized")
    epoch = max(_epoch(), 315532800)  # ZIP cannot represent dates before 1980.
    date_time = __import__("time").gmtime(epoch)[:6]
    with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as destination:
        for original in sorted(source.infolist(), key=lambda item: item.filename):
            data = source.read(original.filename)
            info = zipfile.ZipInfo(original.filename, date_time=date_time)
            info.create_system = 3
            original_mode = (original.external_attr >> 16) & 0o777
            if original.is_dir():
                mode = stat.S_IFDIR | 0o755
                info.external_attr = (mode << 16) | 0x10
            else:
                permissions = 0o755 if original_mode & 0o111 else 0o644
                info.external_attr = (stat.S_IFREG | permissions) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            destination.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    os.replace(temporary, path)


def _normalize_sdist(path: Path) -> None:
    temporary = path.with_name(f".{path.name}.normalized")
    epoch = _epoch()
    with tarfile.open(path, "r:gz") as source:
        members = sorted(source.getmembers(), key=lambda item: item.name)
        with temporary.open("wb") as raw, gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw,
            compresslevel=9,
            mtime=epoch,
        ) as compressed, tarfile.open(
            fileobj=compressed,
            mode="w",
            format=tarfile.PAX_FORMAT,
        ) as destination:
            for member in members:
                member.uid = 0
                member.gid = 0
                member.uname = ""
                member.gname = ""
                member.mtime = epoch
                member.pax_headers = {}
                if member.isdir():
                    member.mode = 0o755
                elif member.isfile():
                    member.mode = 0o755 if member.mode & 0o111 else 0o644
                payload = source.extractfile(member) if member.isfile() else None
                destination.addfile(member, payload)
    os.replace(temporary, path)


def build_wheel(
    wheel_directory: str,
    config_settings=None,
    metadata_directory: str | None = None,
) -> str:
    filename = _setuptools.build_wheel(
        wheel_directory,
        config_settings=config_settings,
        metadata_directory=metadata_directory,
    )
    _normalize_wheel(Path(wheel_directory) / filename)
    return filename


def build_sdist(sdist_directory: str, config_settings=None) -> str:
    filename = _setuptools.build_sdist(
        sdist_directory,
        config_settings=config_settings,
    )
    _normalize_sdist(Path(sdist_directory) / filename)
    return filename


def __getattr__(name: str):
    """Delegate optional hooks such as editable builds and metadata prep."""

    return getattr(_setuptools, name)

"""Locate shipped project resources in a checkout or installed wheel."""

from __future__ import annotations

from pathlib import Path, PurePosixPath


_PACKAGE_DIR = Path(__file__).resolve().parent
_CHECKOUT_ROOT = _PACKAGE_DIR.parents[1]
_BUNDLED_ROOT = _PACKAGE_DIR / "_resources"


def _safe_relative_path(relative: str | Path) -> Path:
    raw = Path(relative)
    posix = PurePosixPath(raw.as_posix())
    if raw.is_absolute() or not posix.parts or any(
        part in {"", ".", ".."} for part in posix.parts
    ):
        raise ValueError("package resource path must be safe and relative")
    return Path(*posix.parts)


def checkout_root() -> Path | None:
    """Return the source checkout root, or ``None`` in an installed wheel."""

    if (
        (_CHECKOUT_ROOT / "pyproject.toml").is_file()
        and (_CHECKOUT_ROOT / "src" / "elt_taskgen").is_dir()
    ):
        return _CHECKOUT_ROOT
    return None


def resource_path(relative: str | Path) -> Path:
    """Return a resource path without requiring it to exist."""

    safe = _safe_relative_path(relative)
    source = checkout_root()
    if source is not None:
        candidate = source / safe
        if candidate.exists():
            return candidate
    return _BUNDLED_ROOT / safe


def resource_root() -> Path:
    """Root containing the packaged ``config/``, tools, lock and images."""

    return checkout_root() or _BUNDLED_ROOT


__all__ = ["checkout_root", "resource_path", "resource_root"]

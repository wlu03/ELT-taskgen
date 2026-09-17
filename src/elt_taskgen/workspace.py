"""Reject workspace paths that name protected artifact containers."""

from __future__ import annotations

import sys
from collections.abc import Collection
from pathlib import Path

from elt_taskgen.package_resources import checkout_root

#: Where a bare command works. Still relative (resolves against the caller's
#: cwd); the guard below refuses the container itself.
DEFAULT_WORKSPACE = Path("runs") / "default"

#: Directories that are structure, never a workspace.
NEVER_A_WORKSPACE = ("runs", "council")

#: Exit status of a container refusal (see the module docstring).
CONTAINER_REFUSAL_EXIT_CODE = 2


def repo_root() -> Path:
    """Return the checkout root or the current directory."""
    # Outside a checkout, resolve relative defaults from the current directory.
    return checkout_root() or Path.cwd().resolve()


def assert_workspace_is_not_a_container(
    workspace: Path, *, allow: Collection[str] = ()
) -> None:
    """Reject protected containers before writes, except names in ``allow``.

    Raises ``SystemExit`` with code 2 and the supplied message.
    """
    resolved = Path(workspace).resolve()
    allowed = {str(name) for name in allow}
    for name in NEVER_A_WORKSPACE:
        if name in allowed:
            continue
        if resolved == (repo_root() / name).resolve():
            message = (
                f"refusing --workspace {workspace}: '{name}/' is a container, "
                f"not a workspace. Use a child of it (e.g. {name}/scratch) or "
                f"the default {DEFAULT_WORKSPACE}."
            )
            print(message, file=sys.stderr)
            exc = SystemExit(message)
            exc.code = CONTAINER_REFUSAL_EXIT_CODE
            raise exc

"""Reusable release-verified fixture for artifact-proxy tests.

The committed semantic gate fixture obeys the portable DECIMAL(38,9)
contract directly, so consumers can copy and verify it without rewriting
private source data or re-pinning its release identity.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from elt_taskgen.export import release as release_mod


TASK_ID = "gate__five_backend_probe"
BASE_RELEASE = Path(__file__).resolve().parent / "fixtures" / "semantic_gate" / "release"


@contextmanager
def portable_five_backend_release() -> Iterator[Path]:
    """Yield an ephemeral schema-3.3 release that passes ``verify_release``."""

    with tempfile.TemporaryDirectory(prefix="elt-workspace-proxy-fixture-") as tmp:
        release_dir = Path(tmp) / "release"
        shutil.copytree(BASE_RELEASE, release_dir)
        verification = release_mod.verify_release(release_dir)
        if not verification.ok:
            raise AssertionError(
                "portable workspace fixture failed release verification: "
                + "; ".join(verification.failures[:3])
            )
        yield release_dir


__all__ = ["TASK_ID", "portable_five_backend_release"]

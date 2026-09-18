"""Reusable release-verified fixture for artifact-proxy tests.

The committed semantic gate fixture obeys the portable DECIMAL(38,9)
contract directly, so consumers can copy and verify it without rewriting
private source data or re-pinning its release identity.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from elt_taskgen.airbyte_connector_config import build_airbyte_connector_contract
from elt_taskgen.destinations import Destination
from elt_taskgen.export import eltbench as eltbench_mod
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


def add_extra_destinations(
    release_dir: Path, task, extras=(Destination.DATABRICKS, Destination.REDSHIFT)
) -> tuple[str, ...]:
    """Give a copied release the extra destinations a real package ships.

    The committed fixture was frozen with the bundle root alone. The public
    configs and private contracts written here come from the SAME writers the
    exporter uses, so the tree is shaped like a real multi-destination task
    rather than hand-built. It no longer matches the release manifest, so a
    caller loads it with ``verify=False``; the seal is what refuses a
    destination added to a frozen release.

    Returns every shipped destination name, bundle root first.
    """

    public_dir = release_dir / "public" / task.task_id
    answer_key = release_dir / "private" / task.task_id / "answer_key"
    configs = eltbench_mod.write_extra_destinations(public_dir, task, list(extras))
    stem, suffix = os.path.splitext(eltbench_mod.PRIVATE_AIRBYTE_CONNECTOR_CONTRACT)
    for name, config in configs.items():
        (answer_key / f"{stem}.{name}{suffix}").write_text(
            json.dumps(build_airbyte_connector_contract(config), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    return ("snowflake", *sorted(configs))


__all__ = ["TASK_ID", "add_extra_destinations", "portable_five_backend_release"]

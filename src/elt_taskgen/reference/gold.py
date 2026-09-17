"""Freeze private row counts and ordered mart outputs with content hashes.

Re-freezing replaces ``gold/`` and ``reference/`` in full. Loading rejects any
manifest mismatch.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from elt_taskgen.models import (
    PopulationName,
    TaskIR,
    canonical_json,
    sha256_hex,
)
from elt_taskgen.reference.runner import (
    RunDigests,
    RunResult,
    joint_digest_from_parts,
    mart_rows_to_csv,
    sort_mart_rows,
    stage1_digest_from_counts,
    stage2_digest_from_csv,
)
from elt_taskgen.reference.solution import build_reference
from elt_taskgen.verification.gates import GRADED_POPULATIONS

#: Reserved filename for stage-1 counts; no mart may claim it.
STAGE1_FILENAME = "stage1_counts.json"
MANIFEST_FILENAME = "manifest.json"


class GoldGrainError(ValueError):
    """Raised at freeze when gold is not unique on its declared key."""


class NullGrainKeyError(GoldGrainError):
    """Raised at freeze when a grain key is null, empty, or whitespace."""


class EmptyGoldMartError(ValueError):
    """Raised at freeze when a graded mart has no rows."""


class GoldBundle(BaseModel):
    """The frozen private gold: counts + ordered mart CSVs + file hashes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    task_content_hash: str
    #: population -> table -> expected stage-1 row count.
    stage1: dict[str, dict[str, int]]
    #: population -> mart -> canonical ordered CSV text.
    stage2_csv: dict[str, dict[str, str]]
    #: Freeze-owned rel_path (POSIX, relative to answer_key/) -> sha256.
    file_hashes: dict[str, str]


def _hash_file(path: Path) -> str:
    return sha256_hex(path.read_text(encoding="utf-8"))


def _hash_tree(answer_key_dir: Path) -> dict[str, str]:
    """Hash freeze-owned files, excluding the later derived export surface."""
    hashes: dict[str, str] = {}
    for owned_dir in (answer_key_dir / "gold", answer_key_dir / "reference"):
        for path in sorted(owned_dir.rglob("*")):
            if path.is_file():
                rel = path.relative_to(answer_key_dir).as_posix()
                hashes[rel] = _hash_file(path)
    return hashes


def freeze_gold(
    task: TaskIR,
    results: dict[PopulationName, RunResult],
    answer_key_dir: Path,
) -> GoldBundle:
    """Freeze complete, nonempty, key-valid gold for every population."""
    if not results:
        raise ValueError("freeze_gold: no runner results provided")
    declared = {p.name for p in task.populations}
    if declared and set(results) != declared:
        missing = sorted(p.value for p in declared - set(results))
        extra = sorted(p.value for p in set(results) - declared)
        raise ValueError(
            f"freeze_gold: results must cover exactly the task populations "
            f"(missing: {missing}, undeclared: {extra})"
        )
    for mart in task.marts:
        if mart.name == Path(STAGE1_FILENAME).stem:
            raise ValueError(
                f"mart name {mart.name!r} collides with the reserved "
                f"{STAGE1_FILENAME} gold file"
            )
    for pop, result in results.items():
        if result.population is not pop:
            raise ValueError(
                f"freeze_gold: result filed under {pop.value!r} was produced "
                f"for population {result.population.value!r}"
            )
        absent = [m.name for m in task.marts if m.name not in result.mart_rows]
        if absent:
            raise ValueError(
                f"freeze_gold: population {pop.value!r} result lacks marts: {absent}"
            )
        for mart in task.marts:
            rows = result.mart_rows[mart.name]
            # GRAIN-UNIQUENESS ASSERT. The declared grain ("one row per <key>")
            # must hold in the gold of EVERY population.
            seen_keys: set[tuple] = set()
            for row in rows:
                key = tuple(row.get(c) for c in mart.key_columns)
                # NON-EMPTINESS ASSERT — see `NullGrainKeyError`. A key that is
                # NULL (or the `''` a CSV backend flattens it to) names no row.
                blank = [
                    c
                    for c, v in zip(mart.key_columns, key)
                    if v is None or (isinstance(v, str) and not v.strip())
                ]
                if blank:
                    raise NullGrainKeyError(
                        f"freeze_gold: mart {mart.name!r} gold violates its "
                        f"declared grain on population {pop.value!r}: grain "
                        f"column(s) {blank} are NULL or empty on a row "
                        f"({dict(zip(mart.key_columns, key))!r}; declared "
                        f"grain: {mart.grain!r}). A NULL grain key identifies "
                        "no row, and a CSV backend makes it indistinguishable "
                        "from ''. Remedy: declare the source column(s) the "
                        "grain rests on NOT NULL at ingest, or do not claim "
                        "that grain; gold with an unidentifiable row is "
                        "refused, never frozen."
                    )
                if key in seen_keys:
                    raise GoldGrainError(
                        f"freeze_gold: mart {mart.name!r} gold violates its "
                        f"declared grain on population {pop.value!r}: key "
                        f"{dict(zip(mart.key_columns, key))!r} appears more "
                        f"than once ({len(rows)} rows total; declared grain: "
                        f"{mart.grain!r}). The reference SQL does not produce "
                        f"one row per {list(mart.key_columns)} — fix the "
                        "mart's key_columns/grain declaration or its plan; "
                        "gold at the wrong grain is refused, never frozen."
                    )
                seen_keys.add(key)
            # EMPTY-GOLD-MART GUARD. compare_mart gives free credit when gold
            # and submission are both empty, so refuse it here on graded
            # populations (development is solver-visible and ungraded).
            if not rows and pop in GRADED_POPULATIONS:
                raise EmptyGoldMartError(
                    f"freeze_gold: mart {mart.name!r} has ZERO gold rows on "
                    f"graded population {pop.value!r}. An empty gold mart "
                    "grants free credit (compare_mart scores empty-vs-empty "
                    "as 1.0). Remedy: relax the mart plan's filters/joins or "
                    "enrich the population's data conditions/literal rows so "
                    "at least one row survives on every graded population, "
                    "or drop the mart from the task."
                )

    gold_dir = answer_key_dir / "gold"
    reference_dir = answer_key_dir / "reference"
    for stale in (gold_dir, reference_dir):
        if stale.exists():
            shutil.rmtree(stale)  # re-freeze is a full rewrite, never a patch

    stage1: dict[str, dict[str, int]] = {}
    stage2_csv: dict[str, dict[str, str]] = {}
    for pop in sorted(results, key=lambda p: p.value):
        result = results[pop]
        pop_dir = gold_dir / pop.value
        pop_dir.mkdir(parents=True, exist_ok=True)
        counts = {t.name: int(result.stage1_counts[t.name]) for t in task.tables}
        (pop_dir / STAGE1_FILENAME).write_text(canonical_json(counts), encoding="utf-8")
        stage1[pop.value] = counts
        stage2_csv[pop.value] = {}
        for mart in task.marts:
            # Defensive re-sort: gold order is the total order.
            rows = sort_mart_rows(result.mart_rows[mart.name], mart)
            csv_text = mart_rows_to_csv(rows, mart)
            (pop_dir / f"{mart.name}.csv").write_text(csv_text, encoding="utf-8")
            stage2_csv[pop.value][mart.name] = csv_text

    # Freeze the trusted solution code alongside the gold it produced.
    reference = build_reference(task)
    reference_dir.mkdir(parents=True, exist_ok=True)
    for mart_name in sorted(reference.sql_by_mart):
        (reference_dir / f"{mart_name}.sql").write_text(
            reference.sql_by_mart[mart_name], encoding="utf-8"
        )
    (reference_dir / "solution.json").write_text(
        reference.to_canonical_json(), encoding="utf-8"
    )

    file_hashes = _hash_tree(answer_key_dir)
    manifest = {
        "task_id": task.task_id,
        "task_content_hash": task.content_hash(),
        "files": file_hashes,
    }
    (answer_key_dir / MANIFEST_FILENAME).write_text(
        canonical_json(manifest), encoding="utf-8"
    )
    return GoldBundle(
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        stage1=stage1,
        stage2_csv=stage2_csv,
        file_hashes=file_hashes,
    )


def gold_digests(gold: GoldBundle, population: PopulationName | str) -> RunDigests:
    """Recompute split determinism digests from frozen gold."""
    pop = population.value if isinstance(population, PopulationName) else str(population)
    if pop not in gold.stage1 or pop not in gold.stage2_csv:
        raise KeyError(
            f"gold bundle has no frozen population {pop!r} "
            f"(have: {sorted(set(gold.stage1) | set(gold.stage2_csv))})"
        )
    counts = {table: int(n) for table, n in gold.stage1[pop].items()}
    csv_by_mart = dict(gold.stage2_csv[pop])
    return RunDigests(
        stage1=stage1_digest_from_counts(pop, counts),
        stage2=stage2_digest_from_csv(pop, csv_by_mart),
        joint=joint_digest_from_parts(counts, csv_by_mart),
    )


def load_gold(answer_key_dir: Path) -> GoldBundle:
    """Load gold after rejecting missing, mismatched, or unmanifested files."""
    manifest_path = answer_key_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"gold manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("files"), dict)
        or not isinstance(manifest.get("task_id"), str)
        or not isinstance(manifest.get("task_content_hash"), str)
    ):
        raise ValueError(f"malformed gold manifest: {manifest_path}")
    file_hashes: dict[str, str] = dict(manifest["files"])

    for rel, expected in sorted(file_hashes.items()):
        path = answer_key_dir / rel
        if not path.is_file():
            raise FileNotFoundError(f"manifested gold file missing: {path}")
        actual = _hash_file(path)
        if actual != expected:
            raise ValueError(
                f"gold hash mismatch for {rel!r}: manifest {expected}, on disk {actual}"
            )

    gold_dir = answer_key_dir / "gold"
    if not gold_dir.is_dir():
        raise FileNotFoundError(f"gold dir missing: {gold_dir}")
    for path in sorted(gold_dir.rglob("*")):
        if path.is_file():
            rel = path.relative_to(answer_key_dir).as_posix()
            if rel not in file_hashes:
                raise ValueError(f"unmanifested file under gold/: {rel!r}")

    valid_populations = {p.value for p in PopulationName}
    stage1: dict[str, dict[str, int]] = {}
    stage2_csv: dict[str, dict[str, str]] = {}
    for pop_dir in sorted(p for p in gold_dir.iterdir() if p.is_dir()):
        pop = pop_dir.name
        if pop not in valid_populations:
            raise ValueError(f"unknown population dir under gold/: {pop!r}")
        counts_path = pop_dir / STAGE1_FILENAME
        if not counts_path.is_file():
            raise FileNotFoundError(f"stage-1 counts missing: {counts_path}")
        counts = json.loads(counts_path.read_text(encoding="utf-8"))
        if not isinstance(counts, dict) or not all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
            for k, v in counts.items()
        ):
            raise ValueError(f"malformed stage-1 counts: {counts_path}")
        stage1[pop] = counts
        stage2_csv[pop] = {}
        for csv_path in sorted(pop_dir.glob("*.csv")):
            stage2_csv[pop][csv_path.stem] = csv_path.read_text(encoding="utf-8")
        if not stage2_csv[pop]:
            raise ValueError(f"population {pop!r} has no stage-2 gold CSVs")
    if not stage1:
        raise ValueError(f"gold dir has no population subdirectories: {gold_dir}")

    return GoldBundle(
        task_id=manifest["task_id"],
        task_content_hash=manifest["task_content_hash"],
        stage1=stage1,
        stage2_csv=stage2_csv,
        file_hashes=file_hashes,
    )

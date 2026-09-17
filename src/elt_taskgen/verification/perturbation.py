"""Prove that reward depends on source values using a scratch-layer bijection.

Pinned population data is hashed before and after and is never modified. Fabricated data
remains in a temporary workspace. No perturbable cells, unchanged gold, or any exception
fails the probe.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from elt_taskgen.models import (
    ColumnSpec,
    ColumnType,
    PopulationName,
    Row,
    TaskIR,
    canonical_json,
)
# THE reward's comparator: "the gold moved" is asserted under it, not under CSV
# bytes (no cycle — but this module must still never import gates).
from elt_taskgen.verification.upstream_eval import compare_mart, parse_canonical_csv

__all__ = [
    "PERTURBATION_PROBE_EVIDENCE_REL",
    "PROBE_KIND",
    "link_classes",
    "perturb_population",
    "pinned_layer_digest",
    "record_perturbation_probe",
    "run_perturbation_probe",
]

#: Evidence location under tasks/<task_id>/, kept in sync with
#: gates.PERTURBATION_PROBE_EVIDENCE_REL (gates importing this would cycle).
PERTURBATION_PROBE_EVIDENCE_REL = "reports/perturbation_probe.json"

#: Recorded so a different probe cannot be mistaken for this one by filename.
PROBE_KIND = "value-bijection"

#: Fixed, not RNG draws: the probe must be byte-reproducible from the artifacts.
_INT_OFFSET = 1_000_003
_NUMERIC_OFFSET = 17.25  # two decimal places: survives NUMERIC(18,2)
_DAY_OFFSET = 3_671

#: Not perturbed: negating a boolean inverts predicate semantics, and free-form
#: JSON has no structure-preserving bijection.
_UNPERTURBED_TYPES: frozenset[ColumnType] = frozenset(
    {ColumnType.BOOLEAN, ColumnType.JSON}
)


# Link classes: columns that must move together or referential integrity dies

def link_classes(task: TaskIR) -> dict[tuple[str, str], tuple[tuple[str, str], ...]]:
    """(table, column) -> the sorted class of columns joined to it by links.

    Union-find over every Relationship, position by position, so ONE value map
    per class keeps every declared FK edge intact under relabeling.
    """
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(x: tuple[str, str]) -> tuple[str, str]:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: tuple[str, str], b: tuple[str, str]) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Deterministic representative: the lexicographically smaller root.
        lo, hi = (ra, rb) if ra <= rb else (rb, ra)
        parent[hi] = lo

    for table in task.tables:
        for col in table.columns:
            find((table.name, col.name))
    for rel in task.relationships:
        for child_col, parent_col in zip(rel.child_columns, rel.parent_columns):
            union((rel.child_table, child_col), (rel.parent_table, parent_col))

    members: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for key in parent:
        members.setdefault(find(key), []).append(key)
    return {
        key: tuple(sorted(members[find(key)]))
        for key in parent
    }


def _column_spec(task: TaskIR, table: str, column: str) -> ColumnSpec | None:
    try:
        tspec = task.table(table)
    except (KeyError, ValueError):
        return None
    for col in tspec.columns:
        if col.name == column:
            return col
    return None


# The bijection

def _shift_iso(value: str, days: int) -> str | None:
    """Shift an ISO date/timestamp string by `days`, preserving its shape."""
    text = value.strip()
    if not text:
        return None
    try:
        if "T" in text or " " in text.strip():
            stamp = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            return (stamp + _dt.timedelta(days=days)).isoformat()
        day = _dt.date.fromisoformat(text)
        return (day + _dt.timedelta(days=days)).isoformat()
    except ValueError:
        return None


def _class_value_map(
    ctype: ColumnType,
    enum_values: tuple[str, ...] | None,
    values: list[Any],
) -> dict[Any, Any]:
    """Build a type-preserving bijection over one non-null link class.

    Shift numbers and dates, relabel text by rank, and rotate enum values within their
    domain. Return an empty mapping when no value can move safely.
    """
    if ctype in _UNPERTURBED_TYPES:
        return {}
    if enum_values:
        domain = list(enum_values)
        if len(domain) < 2:
            return {}
        rotated = domain[1:] + domain[:1]
        return {a: b for a, b in zip(domain, rotated) if a != b}

    out: dict[Any, Any] = {}
    if ctype in (ColumnType.INTEGER, ColumnType.BIGINT):
        for v in values:
            if isinstance(v, bool) or not isinstance(v, int):
                continue
            out[v] = v + _INT_OFFSET
        return out
    if ctype in (ColumnType.FLOAT, ColumnType.DECIMAL):
        for v in values:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            out[v] = round(float(v) + _NUMERIC_OFFSET, 2)
        return out
    if ctype in (ColumnType.DATE, ColumnType.TIMESTAMP):
        for v in values:
            if not isinstance(v, str):
                continue
            shifted = _shift_iso(v, _DAY_OFFSET)
            if shifted is not None:
                out[v] = shifted
        return out
    # TEXT: rank relabeling over the sorted distinct domain — injective, stable.
    texts = sorted({v for v in values if isinstance(v, str)})
    width = max(6, len(str(len(texts))))
    for i, v in enumerate(texts):
        out[v] = f"pv{i:0{width}d}"
    return out


def perturb_population(
    task: TaskIR, rows: dict[str, list[Row]]
) -> tuple[dict[str, list[Row]], dict[str, Any]]:
    """Apply the structure-preserving value bijection to one population.

    Returns (perturbed rows, a deterministic report of what moved). Input rows
    are never mutated; every returned row is a fresh dict.
    """
    classes = link_classes(task)
    # One map per class representative, built from the union of member domains.
    reps: dict[tuple[tuple[str, str], ...], dict[Any, Any]] = {}
    unperturbed: list[str] = []

    for key in sorted(classes):
        members = classes[key]
        if members in reps:
            continue
        specs = [
            spec
            for (t, c) in members
            if (spec := _column_spec(task, t, c)) is not None
        ]
        if not specs:
            reps[members] = {}
            continue
        # One map per class, so a mixed-type or mixed-enum class would relabel
        # one side of the FK and not the other: skip it, recorded as unperturbed.
        types = {spec.type for spec in specs}
        enums = {
            tuple(spec.enum_values) if spec.enum_values else None for spec in specs
        }
        if len(types) != 1 or len(enums) != 1:
            reps[members] = {}
            unperturbed.append(
                f"{'|'.join(f'{t}.{c}' for t, c in members)}:"
                f"mixed-domain({sorted(x.value for x in types)})"
            )
            continue
        ctype = specs[0].type
        enum_values = enums.pop()
        domain: list[Any] = []
        seen: set[Any] = set()
        for (t, c) in members:
            for row in rows.get(t, ()):
                v = row.get(c)
                if v is None:
                    continue
                try:
                    if v in seen:
                        continue
                    seen.add(v)
                except TypeError:  # unhashable (json blobs) — not perturbed
                    continue
                domain.append(v)
        vmap = _class_value_map(ctype, enum_values, domain)
        reps[members] = vmap
        if not vmap:
            unperturbed.append(
                f"{'|'.join(f'{t}.{c}' for t, c in members)}:{ctype.value}"
            )

    out: dict[str, list[Row]] = {}
    cells = 0
    touched: set[str] = set()
    for table in sorted(rows):
        new_rows: list[Row] = []
        for row in rows[table]:
            new_row: Row = dict(row)
            for col, value in row.items():
                if value is None:
                    continue
                vmap = reps.get(classes.get((table, col), ()), {})
                if not vmap:
                    continue
                try:
                    replacement = vmap.get(value, value)
                except TypeError:
                    continue
                if replacement != value:
                    new_row[col] = replacement
                    cells += 1
                    touched.add(f"{table}.{col}")
            new_rows.append(new_row)
        out[table] = new_rows

    report = {
        "kind": PROBE_KIND,
        "perturbed_cells": cells,
        "perturbed_columns": sorted(touched),
        "unperturbed_classes": sorted(unperturbed),
        "link_classes": len(reps),
    }
    return out, report


# Level 1: proving the pinned layer was not written

def pinned_layer_digest(workspace: Path, task_id: str) -> str:
    """One digest over EVERY file under tasks/<id>/populations/.

    Taken before and after the probe: equal digests are the machine proof that
    the pinned population artifacts were read and never written. Deterministic
    (sorted relative POSIX paths).
    """
    root = Path(workspace) / "tasks" / task_id / "populations"
    hasher = hashlib.sha256()
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            hasher.update(path.relative_to(root).as_posix().encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(hashlib.sha256(path.read_bytes()).digest())
    return hasher.hexdigest()


def _load_population_rows(
    task: TaskIR, workspace: Path, population: PopulationName
) -> dict[str, list[Row]]:
    """Read the frozen rows/<table>.jsonl artifacts. READ-ONLY, by construction."""
    rows_dir = (
        Path(workspace) / "tasks" / task.task_id / "populations"
        / population.value / "rows"
    )
    out: dict[str, list[Row]] = {}
    for table in task.tables:
        path = rows_dir / f"{table.name}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(
                f"population {population.value!r}: no frozen rows for table "
                f"{table.name!r} at {path}"
            )
        rows: list[Row] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        out[table.name] = rows
    return out


@contextmanager
def _scratch_workspace() -> Iterator[Path]:
    """Level 2. A temp directory OUTSIDE the real workspace, always destroyed."""
    tmp = Path(tempfile.mkdtemp(prefix="elt-taskgen-perturb-"))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# The probe

def run_perturbation_probe(
    task: TaskIR,
    workspace: Path,
    gold: Any,
    *,
    population: PopulationName = PopulationName.PRIMARY,
) -> dict[str, Any]:
    """Perturb scratch values, rerun the reference, and compare with frozen gold.

    Pass only when cells moved, pinned data did not, no mart became empty, and every
    mart differs under the actual tolerant reward. Any exception fails.
    """
    from elt_taskgen.generation.source_data import render_population, write_rows
    from elt_taskgen.reference.runner import (
        mart_rows_to_csv,
        population_dir,
        rendered_dir,
        run_reference,
    )

    workspace = Path(workspace)
    record: dict[str, Any] = {
        "kind": PROBE_KIND,
        "task_id": task.task_id,
        "task_content_hash": task.content_hash(),
        "population": population.value,
        "passed": False,
        "details": "",
        "evidence": {},
    }
    before_digest = pinned_layer_digest(workspace, task.task_id)
    record["pinned_layer_sha256_before"] = before_digest
    record["pinned_layer_sha256_after"] = before_digest

    try:
        rows = _load_population_rows(task, workspace, population)
        perturbed, preport = perturb_population(task, rows)
        record["perturbation"] = preport

        if preport["perturbed_cells"] == 0:
            record["details"] = (
                "no source value could be perturbed type-preservingly — the "
                "probe has nothing to prove sensitivity with (fail closed); "
                f"unperturbable classes: {preport['unperturbed_classes']}"
            )
            return record

        with _scratch_workspace() as scratch:
            pdir = population_dir(scratch, task.task_id, population)
            write_rows(perturbed, pdir / "rows")
            render_population(
                task, population, perturbed,
                rendered_dir(scratch, task.task_id, population),
            )
            result = run_reference(task, population, scratch)

        frozen = dict(getattr(gold, "stage2_csv", {}).get(population.value, {}))
        moved: list[str] = []
        stuck: list[str] = []
        emptied: list[str] = []
        evidence: dict[str, str] = {}
        for mart in task.marts:
            want = frozen.get(mart.name)
            if want is None:
                record["details"] = (
                    f"no frozen gold for mart {mart.name!r} on "
                    f"{population.value!r} — nothing to compare the probe against"
                )
                return record
            got = mart_rows_to_csv(result.mart_rows[mart.name], mart)
            byte_same = got == want
            # Byte-different is not enough: the frozen gold must LOSE under THE
            # reward in BOTH directions — compare_mart's relative tolerance is
            # measured against the submitted side, so either match is a leak.
            try:
                _, want_rows = parse_canonical_csv(want)
            except ValueError as exc:
                record["details"] = (
                    f"frozen gold for mart {mart.name!r} on "
                    f"{population.value!r} is not parseable canonical CSV "
                    f"({exc}) — nothing to compare the probe against (fail closed)"
                )
                return record
            same = (
                byte_same
                or compare_mart(want, list(result.mart_rows[mart.name]), mart)
                or compare_mart(got, want_rows, mart)
            )
            probe_rows = max(0, len(got.strip().splitlines()) - 1)
            gold_rows = max(0, len(want.strip().splitlines()) - 1)
            evidence[f"{mart.name}:gold-vs-probe"] = (
                (
                    "identical"
                    if byte_same
                    else "differs (reward-equivalent)"
                    if same
                    else "differs (reward-distinct)"
                )
                + f" (gold {gold_rows} row(s), probe {probe_rows} row(s))"
            )
            if same:
                stuck.append(
                    mart.name
                    if byte_same
                    else f"{mart.name} (moved only within the reward's tolerance)"
                )
            elif probe_rows == 0 and gold_rows > 0:
                emptied.append(mart.name)
            else:
                moved.append(mart.name)
        record["evidence"] = evidence

        after_digest = pinned_layer_digest(workspace, task.task_id)
        record["pinned_layer_sha256_after"] = after_digest
        if after_digest != before_digest:
            record["details"] = (
                # No "licens"/"contamination" wording in runtime detail strings:
                # repair.route_for_failure reads those as FATAL, not repairable.
                "PINNED LAYER MOVED during the probe: the vendored population "
                "artifacts must be read-only "
                f"({before_digest[:16]} -> {after_digest[:16]})"
            )
            return record
        if stuck:
            record["details"] = (
                "the value bijection preserved every structural property and "
                "moved every source value, yet the gold did not change under "
                f"the reward for mart(s) {stuck} — the reward reads no source "
                "value (memorized / structure-only output; a mart that moved "
                "only within compare_mart's tolerance still pays a memorized "
                "gold full credit)"
            )
            return record
        if emptied:
            record["details"] = (
                f"INCONCLUSIVE: mart(s) {emptied} went empty under the probe. "
                "The relabeling broke a predicate rather than demonstrating "
                "value dependence; 'differs' here would be a false green"
            )
            return record
        record["passed"] = True
        record["details"] = (
            f"{preport['perturbed_cells']} source cell(s) rewritten by a "
            f"structure-preserving bijection across "
            f"{len(preport['perturbed_columns'])} column(s); every mart's gold "
            f"moved ({', '.join(moved)}); pinned layer digest unchanged"
        )
        return record
    except Exception as exc:  # noqa: BLE001 — an unrunnable probe is a failed probe
        record["details"] = (
            f"perturbation probe raised {type(exc).__name__}: {exc}"
        )
        record["pinned_layer_sha256_after"] = pinned_layer_digest(
            workspace, task.task_id
        )
        return record


def record_perturbation_probe(
    task: TaskIR, workspace: Path, gold: Any, **kwargs: Any
) -> Path:
    """Run the probe and persist it at tasks/<id>/reports/perturbation_probe.json.

    Canonical JSON, no wall clock: two runs over the same artifacts write the
    same bytes.
    """
    record = run_perturbation_probe(task, workspace, gold, **kwargs)
    path = (
        Path(workspace) / "tasks" / task.task_id / PERTURBATION_PROBE_EVIDENCE_REL
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(record), encoding="utf-8")
    return path

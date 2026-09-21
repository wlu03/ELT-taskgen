"""Generate constraint-aware rows and render five source environments.

Parents precede children, identity values are minted, and every column uses a
deterministic seeded random stream.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import dataclasses
import random
import re
import tempfile
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from elt_taskgen import __version__
from elt_taskgen.models import (
    Backend,
    ColumnSpec,
    ColumnType,
    PopulationName,
    PopulationSpec,
    Relationship,
    Row,
    TableSpec,
    TaskIR,
    canonical_json,
    derive_seed,
)
from elt_taskgen.sql_identifiers import quote_sql_identifier

# Fixed epoch for temporal synthesis — a constant, never the wall clock.
_EPOCH_DATE = date(2024, 1, 1)
_EPOCH_DATETIME = datetime(2024, 1, 1, 0, 0, 0)

#: Per-population id bases: disjoint ranges, so resampled ids never collide with
#: primary ids (memorization check).
_ID_BASE: dict[PopulationName, int] = {
    PopulationName.DEVELOPMENT: 100,
    PopulationName.PRIMARY: 10_000,
    PopulationName.RESAMPLED: 5_000_000,
    PopulationName.COUNTERFACTUAL: 900,
    PopulationName.STRESS: 7_000_000,
}

_S3_PART_ROWS = 5_000
_POSTGRES_INSERT_BATCH = 500


# --- Realized row counts ---
# Never realize the declared scale (it leaks the extract-load answer); derive from
# (task_id, table, declared) — not the population — so primary/resampled stay equal.

#: Divergence band in whole PERCENT of the declared scale (integer math, not
#: floats): MIN stops the declared number being the answer, MAX keeps prose true.
REALIZED_DIVERGENCE_MIN_PCT = 2
REALIZED_DIVERGENCE_MAX_PCT = 7

#: Below this scale the count is realized EXACTLY: small populations are
#: solver-visible and ungraded, and 50 is the smallest scale the band fits above.
REALIZED_DIVERGENCE_MIN_SCALE = 50

#: Seed namespace, so a divergence offset never collides with another stream.
_DIVERGENCE_PURPOSE = "realized-row-count"


def realized_row_count(task_id: str, table: str, declared: int) -> int:
    """How many rows one table ACTUALLY gets for a declared scale, deterministic
    in (task_id, table, declared). The population name is deliberately NOT part
    of the key, so primary and resampled realize identical counts."""
    declared = int(declared)
    if declared < REALIZED_DIVERGENCE_MIN_SCALE:
        return declared
    # ceil / floor of the band, in integers only (see the constants above).
    span_min = max(1, -(-declared * REALIZED_DIVERGENCE_MIN_PCT // 100))
    span_max = declared * REALIZED_DIVERGENCE_MAX_PCT // 100
    if span_max <= span_min:
        # FAIL CLOSED: returning `declared` would hand the extract-load answer
        # back to anyone who can read the task documentation.
        raise ValueError(
            f"realized_row_count({table!r}, declared={declared}): the "
            f"divergence band [{REALIZED_DIVERGENCE_MIN_PCT}%, "
            f"{REALIZED_DIVERGENCE_MAX_PCT}%] collapses to "
            f"[{span_min}, {span_max}] rows at this scale — raise "
            "REALIZED_DIVERGENCE_MIN_SCALE or widen the band. Realizing the "
            "declared count instead would hand the extract-load answer back "
            "to anyone who can read the task documentation."
        )
    # [span_min, span_max - 1]: the last row is headroom for the round-number nudge.
    magnitude = span_min + derive_seed(
        task_id, _DIVERGENCE_PURPOSE, table, declared, "magnitude"
    ) % (span_max - span_min)
    sign = (
        1
        if derive_seed(task_id, _DIVERGENCE_PURPOSE, table, declared, "sign") % 2
        else -1
    )
    realized = declared + sign * magnitude
    if realized % 10 == 0:
        # A round count is guessable: push one row FURTHER from the declared
        # value, never back towards it (which could land on it).
        realized += sign
    return realized


@dataclass(frozen=True)
class _Policy:
    """Population-name-driven generation knobs."""

    coverage: bool = False        # cover every (parent, enum value) pair
    childless_frac: float = 0.0   # optional-link parents left without children
    avoid_frac: float = 0.0       # per enum value: parents whose children avoid it
    null_fk_frac: float = 0.0     # optional nullable links forced NULL
    null_value_frac: float = 0.0  # nullable non-link columns forced NULL
    dup_frac: float = 0.0         # exact-duplicate rows for tables without a PK
    skew_frac: float = 0.0        # remainder children routed to hot parent(s)
    palette: bool = False         # small measure domains (ties)
    adversarial_values: bool = False  # portable boundary/value-shape coverage
    exact_bigint_only: bool = False    # backend rounds integers above 2^53
    skew_weights: tuple[int, ...] = ()  # hot-parent weights, in pool order
    tie_run_length: int = 1       # consecutive synthesized values per tie


_POLICIES: dict[PopulationName, _Policy] = {
    PopulationName.DEVELOPMENT: _Policy(coverage=True),
    PopulationName.PRIMARY: _Policy(
        childless_frac=0.15,
        avoid_frac=0.05,
        null_fk_frac=0.05,
        null_value_frac=0.10,
        adversarial_values=True,
    ),
    PopulationName.RESAMPLED: _Policy(
        childless_frac=0.15,
        avoid_frac=0.05,
        null_fk_frac=0.05,
        null_value_frac=0.10,
        adversarial_values=True,
    ),
    PopulationName.COUNTERFACTUAL: _Policy(coverage=True),
    PopulationName.STRESS: _Policy(
        coverage=True,
        dup_frac=0.05,
        skew_frac=0.80,
        palette=True,
        adversarial_values=True,
        skew_weights=(12, 3, 1),
        tie_run_length=4,
    ),
}

#: Dangling fraction for optional links with NON-nullable child columns, applied
#: only when the population's conditions declare dangling keys.
_DANGLING_FRAC = 0.05

#: Which generator produced a population; bump whenever a policy change alters
#: generated bytes (5 = v4 plus a schema-conditional counterfactual parent whose
#: real linked rows all carry NULL in a nullable bridge measure column; 7 = row
#: P, the period-ordered mirror of row A, on every latest_snapshot
#: counterfactual).
GENERATION_POLICY_VERSION = 7

#: Durable evidence written beside every materialized population.  The manifest
#: inventories ``rows/`` and ``rendered/`` but never itself; its own bytes are
#: covered by :func:`tree_digest`, avoiding an impossible recursive checksum.
POPULATION_MANIFEST_FILENAME = "materialization_manifest.json"
POPULATION_MANIFEST_SCHEMA_VERSION = "population-materialization-v1"
POPULATION_FILE_DIGEST_KIND = "sha256-file"
POPULATION_LOGICAL_ROWS_DIGEST_KIND = "sha256-canonical-json-v1"
GENERATOR_IMPLEMENTATION_DIGEST_KIND = "sha256-source-file-v1"

#: THE ONE dangling-lever reader (gates.py imports it, so generator and gate
#: cannot disagree). Whole WORD only, and a negated clause must NOT arm it.
_DANGLING_TERM = r"(?:dangl(?:e|es|ed|ing)|optional[- ]link witness)"
_DANGLING_RE = re.compile(rf"\b{_DANGLING_TERM}\b", re.IGNORECASE)
_DANGLING_NEGATION_RE = re.compile(
    rf"\b(?:no|not|never|without|zero|free of)\b[^.;]{{0,40}}"
    rf"\b{_DANGLING_TERM}\b",
    re.IGNORECASE,
)


def declares_dangling(conditions: Iterable[str]) -> bool:
    """Do these conditions arm the dangling-key lever?

    True for ``dangling`` (any inflection) or the generator's canonical
    ``OPTIONAL-LINK WITNESS`` marker as whole terms outside a negated clause.
    ``_generate_table`` and ``gates._gate_referential_integrity`` both read the
    lever here, so generated witness prose and integrity admission cannot drift.
    """
    return any(
        _DANGLING_RE.search(c) is not None and _DANGLING_NEGATION_RE.search(c) is None
        for c in conditions
    )


class FkIdentityCapacityError(ValueError):
    """A key made only of foreign-key columns cannot take `n` distinct tuples: the
    parent pools bound the key space, and emitting duplicates would contradict the
    documented PRIMARY KEY, so generation fails closed."""


class SourceKeyUniquenessError(ValueError):
    """`generate_rows` produced a repeated declared key, so the rows are refused
    rather than frozen. Business keys may repeat only across verbatim duplicate
    rows (the one sanctioned repetition, from stress duplicate injection)."""


# --- RNG streams ---

def column_rng(task_id: str, population: PopulationName, table: str, column: str) -> random.Random:
    """The per-column stream: random.Random(derive_seed(task_id, population, table, column))."""
    return random.Random(derive_seed(task_id, population.value, table, column))


def _stream(task_id: str, population: PopulationName, table: str, *purpose: str) -> random.Random:
    """Internal non-column streams (FK assignment, duplicates), same derivation."""
    return random.Random(derive_seed(task_id, population.value, table, *purpose))


# --- Row generation ---

# Sentinels stay portable: numeric DECIMAL, signed-int64 BIGINT, naive TIMESTAMP,
# and nonempty non-NA text that remains distinct from SQL NULL.
_BIGINT_EXACTNESS_BOUNDARY = 2**53
_BIGINT_ID_FLOOR = _BIGINT_EXACTNESS_BOUNDARY + 1
_ADVERSARIAL_BIGINT_VALUES: tuple[int, ...] = (
    _BIGINT_EXACTNESS_BOUNDARY + 1,
    _BIGINT_EXACTNESS_BOUNDARY + 17,
)
_ADVERSARIAL_DECIMAL_VALUES: tuple[float, ...] = (
    123456.123456789,
    987654.987654321,
    0.000000001,
    999999.999999999,
)
_ADVERSARIAL_TEXT_VALUES: tuple[str, ...] = (
    "MiXeD_Case",
    "nUlL",
    " Null ",
    "München 東京 Δ",
    "Cafe\u0301",
    "emoji_🧪",
)
_ADVERSARIAL_DATE_VALUES: tuple[str, ...] = (
    "2024-02-28",
    "2024-02-29",
    "2024-03-01",
    "2024-03-10",
    "2024-11-03",
    "2024-12-31",
    "2025-01-01",
)
_ADVERSARIAL_TIMESTAMP_VALUES: tuple[str, ...] = (
    "2024-02-29 23:59:59.999999",
    "2024-03-10 01:59:59",
    "2024-03-10 03:00:00",
    "2024-11-03 01:30:00",
    "2024-12-31 23:59:59.999999",
    "2025-01-01 00:00:00",
)
_ADVERSARIAL_JSON_VALUES: tuple[str, ...] = (
    canonical_json(
        {"meta": {"label": None, "tags": ["α", None]}, "value": 1}
    ),
    canonical_json({"meta": {"tags": ["α", None]}, "value": 1}),
    canonical_json({"meta": None, "value": 2}),
    canonical_json({"meta": {}, "value": 2}),
)
_POPULATION_VARIANT_OFFSET: dict[PopulationName, int] = {
    PopulationName.DEVELOPMENT: 0,
    PopulationName.PRIMARY: 0,
    PopulationName.RESAMPLED: 1,
    PopulationName.COUNTERFACTUAL: 2,
    PopulationName.STRESS: 3,
}


def _variant_value(values: tuple, *, sample_index: int, variant_offset: int):
    """Rotate fixed cases per column/population while preserving full coverage."""
    return values[(variant_offset + sample_index) % len(values)]


#: Backends whose connector cannot carry an integer above 2^53 exactly.
#: `source-mongodb-v2` types a BSON Int64 as JSON Schema `number`, which the
#: destinations map to a float, so ids above 2^53 arrive rounded.
_INEXACT_BIGINT_BACKENDS = frozenset({Backend.MONGODB})


def _carried_by_an_inexact_backend(task: TaskIR, table: str) -> bool:
    """Is ``table`` served from a backend that rounds ids above 2^53?"""
    try:
        assignment = task.backend_for(table)
    except KeyError:
        # A table with no assignment is not rendered through any connector.
        return False
    return assignment.backend in _INEXACT_BIGINT_BACKENDS


def _bigint_identity_is_portable(task: TaskIR, table: str, column: str) -> bool:
    """Can ``table.column`` and every referencing child carry >2^53 exactly?

    Two things can make it unportable. Some imported schemas preserve a numeric
    relationship while narrowing its child from BIGINT to INTEGER, which was
    safe with the old small ids but overflows if the parent alone crosses 2^53.
    And some backends round the value in transport, which corrupts the id
    itself rather than the solver's arithmetic. Either case keeps the original
    small identity range.
    """
    if _carried_by_an_inexact_backend(task, table):
        return False
    for rel in task.relationships:
        if rel.parent_table != table:
            continue
        for position, parent_column in enumerate(rel.parent_columns):
            if parent_column != column:
                continue
            # The child carries this id through its own backend too.
            if _carried_by_an_inexact_backend(task, rel.child_table):
                return False
            child = task.table(rel.child_table).column(rel.child_columns[position])
            if child.type is not ColumnType.BIGINT:
                return False
    return True


def _tie_value(
    values: tuple,
    *,
    sample_index: int,
    variant_offset: int,
    run_length: int,
):
    """Select from a palette in deterministic repeated runs.

    A run, rather than independent random draws, makes exact ties a construction
    property.  The per-column offset prevents every measure column from sharing
    the same pattern while retaining byte determinism.
    """
    run_length = max(1, int(run_length))
    return values[(variant_offset + sample_index // run_length) % len(values)]


def _hot_parent_index(
    eligible: list[int], weights: tuple[int, ...], stream: random.Random
) -> int:
    """Choose one deterministic hot parent from a weighted pool prefix."""
    hot = eligible[: len(weights)]
    usable_weights = weights[: len(hot)]
    total = sum(usable_weights)
    if not hot or total <= 0:
        return eligible[0]
    ticket = stream.randrange(total)
    for parent_index, weight in zip(hot, usable_weights):
        if ticket < weight:
            return parent_index
        ticket -= weight
    return hot[-1]  # pragma: no cover - integer partition is exhaustive


def _topo_order(task: TaskIR) -> list[str]:
    """Parents before children, declaration order breaking ties. A cycle re-sorts
    the stuck remainder by REQUIRED edges only, so only OPTIONAL links break — an
    optional link on an empty pool nulls out, a required one raises."""
    names = [t.name for t in task.tables]
    parents_of: dict[str, set[str]] = {n: set() for n in names}
    required_parents_of: dict[str, set[str]] = {n: set() for n in names}
    for rel in task.relationships:
        if rel.parent_table != rel.child_table:
            parents_of[rel.child_table].add(rel.parent_table)
            if rel.required:
                required_parents_of[rel.child_table].add(rel.parent_table)
    order: list[str] = []
    done: set[str] = set()
    remaining = list(names)
    while remaining:
        progressed = False
        for name in list(remaining):
            if parents_of[name] <= done:
                order.append(name)
                done.add(name)
                remaining.remove(name)
                progressed = True
        if not progressed:  # relationship cycle: keep the REQUIRED edges
            order.extend(_required_first(remaining, required_parents_of, done))
            break
    return order


def _required_first(
    remaining: list[str],
    required_parents_of: dict[str, set[str]],
    done: set[str],
) -> list[str]:
    """Order a cycle-stuck remainder by its REQUIRED edges only; ties keep
    declaration order. A remainder whose required edges also cycle is returned
    unchanged, preserving the caller's fail-closed behaviour."""
    stuck = list(remaining)
    placed: list[str] = []
    seen = set(done)
    while stuck:
        progressed = False
        for name in list(stuck):
            if required_parents_of[name] <= seen:
                placed.append(name)
                seen.add(name)
                stuck.remove(name)
                progressed = True
        if not progressed:  # required edges cycle too: nothing better to do
            placed.extend(stuck)
            break
    return placed


def _mint_id(
    col: ColumnSpec,
    table: str,
    base: int,
    i: int,
    *,
    oversized_bigint: bool = False,
):
    """Sequential identity value (PK / business key), population-disjoint via `base`."""
    if col.type is ColumnType.INTEGER:
        return base + i
    if col.type is ColumnType.BIGINT:
        floor = _BIGINT_ID_FLOOR if oversized_bigint else 0
        return floor + base + i
    if col.type in (ColumnType.FLOAT, ColumnType.DECIMAL):
        return float(base + i)
    if col.type is ColumnType.DATE:
        return (_EPOCH_DATE + timedelta(days=i)).isoformat()
    if col.type is ColumnType.TIMESTAMP:
        return (_EPOCH_DATETIME + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S")
    return f"{table}_{base + i}"


def _synth_value(
    col: ColumnSpec,
    stream: random.Random,
    policy: _Policy,
    *,
    sample_index: int = 0,
    variant_offset: int = 0,
):
    """Type-driven value synthesis from the column's own stream.

    ``sample_index`` counts non-NULL synthesized values in this column.  Fixed
    boundary cases therefore cannot disappear merely because an earlier row was
    selected for the population's NULL slice.
    """
    t = col.type
    if t is ColumnType.INTEGER:
        if policy.palette:
            return _tie_value(
                (1, 2, 3),
                sample_index=sample_index,
                variant_offset=variant_offset,
                run_length=policy.tie_run_length,
            )
        return stream.randint(1, 100)
    if t is ColumnType.BIGINT:
        if (
            policy.adversarial_values
            and not policy.exact_bigint_only
            and sample_index < len(_ADVERSARIAL_BIGINT_VALUES)
        ):
            return _variant_value(
                _ADVERSARIAL_BIGINT_VALUES,
                sample_index=sample_index,
                variant_offset=variant_offset,
            )
        if policy.palette:
            return _tie_value(
                (1, 2, 3),
                sample_index=sample_index,
                variant_offset=variant_offset,
                run_length=policy.tie_run_length,
            )
        return stream.randint(1, 1_000_000)
    if t is ColumnType.FLOAT:
        if policy.palette:
            return _tie_value(
                (5.0, 10.0, 15.0, 25.0),
                sample_index=sample_index,
                variant_offset=variant_offset,
                run_length=policy.tie_run_length,
            )
        return round(stream.uniform(1.0, 500.0), 2)
    if t is ColumnType.DECIMAL:
        if policy.adversarial_values and sample_index < len(_ADVERSARIAL_DECIMAL_VALUES):
            return _variant_value(
                _ADVERSARIAL_DECIMAL_VALUES,
                sample_index=sample_index,
                variant_offset=variant_offset,
            )
        if policy.palette:
            return _tie_value(
                (5.000000001, 10.123456789, 15.555555555, 25.987654321),
                sample_index=sample_index,
                variant_offset=variant_offset,
                run_length=policy.tie_run_length,
            )
        # Nine fractional digits exercise DECIMAL(38,9) without converting the
        # physical JSON value into a string (all five source readers stay typed).
        whole = stream.randint(1, 500)
        fraction = stream.randint(0, 999_999_999)
        return float(f"{whole}.{fraction:09d}")
    if t is ColumnType.TEXT:
        if policy.adversarial_values and sample_index < len(_ADVERSARIAL_TEXT_VALUES):
            return _variant_value(
                _ADVERSARIAL_TEXT_VALUES,
                sample_index=sample_index,
                variant_offset=variant_offset,
            )
        return f"{col.name}_{stream.randint(0, 999_999):06d}"
    if t is ColumnType.BOOLEAN:
        return stream.random() < 0.5
    if t is ColumnType.DATE:
        if policy.adversarial_values and sample_index < len(_ADVERSARIAL_DATE_VALUES):
            return _variant_value(
                _ADVERSARIAL_DATE_VALUES,
                sample_index=sample_index,
                variant_offset=variant_offset,
            )
        if policy.palette:
            return _tie_value(
                _ADVERSARIAL_DATE_VALUES,
                sample_index=sample_index,
                variant_offset=variant_offset,
                run_length=policy.tie_run_length,
            )
        return (_EPOCH_DATE + timedelta(days=stream.randint(0, 364))).isoformat()
    if t is ColumnType.TIMESTAMP:
        if policy.adversarial_values and sample_index < len(_ADVERSARIAL_TIMESTAMP_VALUES):
            return _variant_value(
                _ADVERSARIAL_TIMESTAMP_VALUES,
                sample_index=sample_index,
                variant_offset=variant_offset,
            )
        if policy.palette:
            return _tie_value(
                _ADVERSARIAL_TIMESTAMP_VALUES,
                sample_index=sample_index,
                variant_offset=variant_offset,
                run_length=policy.tie_run_length,
            )
        return (_EPOCH_DATETIME + timedelta(seconds=stream.randint(0, 31_535_999))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    if t is ColumnType.JSON:
        if policy.adversarial_values and sample_index < len(_ADVERSARIAL_JSON_VALUES):
            return _variant_value(
                _ADVERSARIAL_JSON_VALUES,
                sample_index=sample_index,
                variant_offset=variant_offset,
            )
        return canonical_json({"value": stream.randint(0, 999)})
    raise ValueError(f"unsupported column type {t!r}")  # pragma: no cover


#: Types that can never carry a unique identity, so relationship parent columns of
#: these types are not minted (enum parents are excluded separately: they must draw).
_UNMINTABLE_TYPES = frozenset({ColumnType.BOOLEAN, ColumnType.JSON})


def identity_columns(
    task: TaskIR, tspec: TableSpec, fk_cols: Collection[str]
) -> frozenset[str]:
    """The columns of `tspec` minted as sequential identities: (PK ∪ business key
    ∪ non-enum, non-BOOLEAN/JSON relationship parent columns) − FK columns. The
    UNION, never `pk or bk`, which lets a business key duplicate."""
    return identity_columns_for(task.relationships, tspec, fk_cols)


def identity_columns_for(
    relationships: Iterable[Relationship],
    tspec: TableSpec,
    fk_cols: Collection[str],
) -> frozenset[str]:
    """`identity_columns` without a TaskIR (populations.py sizes coverage budgets
    before the TaskIR exists)."""
    parents: set[str] = set()
    for rel in relationships:
        if rel.parent_table != tspec.name:
            continue
        for name in rel.parent_columns:
            col = tspec.column(name)
            if col.enum_values or col.type in _UNMINTABLE_TYPES:
                continue
            parents.add(name)
    return frozenset(
        (set(tspec.primary_key) | set(tspec.business_key) | parents) - set(fk_cols)
    )


def primary_enum_column(
    tspec: TableSpec, fk_cols: Collection[str], id_cols: Collection[str]
) -> str | None:
    """The FIRST enum column that is neither an FK nor an identity column — the one
    the coverage / avoid-slice policy pairs with the table's first relationship.
    `populations.coverage_budget_scale` must use the same definition."""
    for c in tspec.columns:
        if c.enum_values and c.name not in fk_cols and c.name not in id_cols:
            return c.name
    return None


def _dangling_key(pool: list[tuple], counter: int) -> tuple:
    """Mint a key guaranteed OUT of the parent pool: numeric keys sit above the
    pool's max, string keys get a placeholder."""
    sample = pool[0]
    key = []
    for j, v in enumerate(sample):
        if isinstance(v, int) and not isinstance(v, bool):
            top = max(x[j] for x in pool if isinstance(x[j], int))
            key.append(top + 1_000_000 + counter)
        else:
            key.append(f"__dangling_{counter}_{j}")
    return tuple(key)


def _generate_table(
    task: TaskIR,
    pop: PopulationSpec,
    policy: _Policy,
    tspec: TableSpec,
    n: int,
    generated: dict[str, list[Row]],
) -> list[Row]:
    task_id = task.task_id
    population = pop.name
    rels = [r for r in task.relationships if r.child_table == tspec.name]

    # column -> (relationship index, position in the key tuple)
    fk_cols: dict[str, tuple[int, int]] = {}
    for ri, rel in enumerate(rels):
        for pos, cname in enumerate(rel.child_columns):
            fk_cols.setdefault(cname, (ri, pos))

    # Parent pools: unique key tuples from ACTUALLY generated parent rows.
    pools: list[list[tuple]] = []
    for rel in rels:
        seen: set[tuple] = set()
        pool: list[tuple] = []
        for prow in generated.get(rel.parent_table, []):
            key = tuple(prow.get(c) for c in rel.parent_columns)
            if any(v is None for v in key) or key in seen:
                continue
            seen.add(key)
            pool.append(key)
        pools.append(pool)

    # Identity columns minted sequentially (see `identity_columns`).
    id_cols = identity_columns(task, tspec, fk_cols)
    id_base = _ID_BASE[population]

    # The first non-FK/identity enum column drives coverage / avoid-slice semantics.
    primary_enum = primary_enum_column(tspec, fk_cols, id_cols)
    primary_enum_domain: tuple[str, ...] = (
        tuple(tspec.column(primary_enum).enum_values or ()) if primary_enum else ()
    )

    dangling_frac = _DANGLING_FRAC if declares_dangling(pop.conditions) else 0.0

    assignments: list[list[tuple | None]] = []
    eligibles: list[list[int]] = []          # per relationship: drawable pool indexes
    forced_enum: dict[int, str] = {}          # row index -> forced enum value (coverage)
    avoid_by_parent: dict[tuple, str] = {}    # parent key -> enum value its children avoid
    dangling_counter = 0

    for ri, rel in enumerate(rels):
        pool = pools[ri]
        assign: list[tuple | None] = [None] * n
        if not pool:
            if rel.required and n > 0:
                raise ValueError(
                    f"table {tspec.name!r}: required link to {rel.parent_table!r} "
                    f"has an empty parent pool (population {population.value})"
                )
            assignments.append(assign)
            eligibles.append([])
            continue

        arng = _stream(task_id, population, tspec.name, "fk", *rel.child_columns)
        is_primary_rel = ri == 0
        nullable_link = (not rel.required) and all(
            tspec.column(c).nullable for c in rel.child_columns
        )
        dangling_ok = (not rel.required) and not nullable_link and dangling_frac > 0

        if is_primary_rel and policy.coverage and primary_enum:
            # PARENT-FIRST: round r hands every parent one child at value
            # (pi + r) % |domain|, so a short budget starves pairs, not parents.
            dom = len(primary_enum_domain)
            pairs = [
                (pi, primary_enum_domain[(pi + r) % dom])
                for r in range(dom)
                for pi in range(len(pool))
            ]
            covered = min(n, len(pairs))
            for i in range(covered):
                pi, v = pairs[i]
                assign[i] = pool[pi]
                forced_enum[i] = v
            start = covered
            eligible = list(range(len(pool)))
        else:
            childless = 0
            if is_primary_rel and not rel.required and policy.childless_frac > 0:
                childless = min(
                    len(pool) - 1, math.ceil(policy.childless_frac * len(pool))
                )
            cursor = childless
            if is_primary_rel and not rel.required and policy.avoid_frac > 0 and primary_enum:
                per = math.ceil(policy.avoid_frac * len(pool))
                for v in primary_enum_domain:
                    if per and cursor + per <= len(pool):
                        for pi in range(cursor, cursor + per):
                            avoid_by_parent[pool[pi]] = v
                        cursor += per
            eligible = list(range(childless, len(pool))) or list(range(len(pool)))
            # Forced round-robin while budget allows: avoid-slice parents MUST
            # have children.
            covered = min(n, len(eligible))
            for i in range(covered):
                assign[i] = pool[eligible[i]]
            start = covered

        for i in range(start, n):
            d_link = arng.random()
            d_skew = arng.random()
            if nullable_link and d_link < policy.null_fk_frac:
                assign[i] = None
            elif dangling_ok and d_link < dangling_frac:
                dangling_counter += 1
                assign[i] = _dangling_key(pool, dangling_counter)
            elif policy.skew_frac and d_skew < policy.skew_frac:
                # A deterministic heavy head with several hot parents is more
                # realistic (and harder for uniformity assumptions) than one
                # all-or-nothing whale.  An empty weight vector retains the
                # historical single-whale behavior.
                hot_index = (
                    _hot_parent_index(eligible, policy.skew_weights, arng)
                    if policy.skew_weights
                    else eligible[0]
                )
                assign[i] = pool[hot_index]
            else:
                assign[i] = pool[eligible[arng.randrange(len(eligible))]]
        assignments.append(assign)
        eligibles.append(eligible)

    # An FK-only key is drawn, not minted: make its tuples unique here (a no-op
    # consuming no RNG for every other table).
    _enforce_fk_identity_uniqueness(
        task_id, population, tspec, fk_cols, pools, eligibles, assignments, n
    )

    # ---- column values, one independent stream per column ----
    rows: list[Row] = [dict() for _ in range(n)]
    for col in tspec.columns:
        stream = column_rng(task_id, population, tspec.name, col.name)
        if col.name in fk_cols:
            ri, pos = fk_cols[col.name]
            for i in range(n):
                key = assignments[ri][i]
                rows[i][col.name] = None if key is None else key[pos]
        elif col.name in id_cols:
            oversized_bigint = (
                policy.adversarial_values
                and col.type is ColumnType.BIGINT
                and _bigint_identity_is_portable(task, tspec.name, col.name)
            )
            for i in range(n):
                rows[i][col.name] = _mint_id(
                    col,
                    tspec.name,
                    id_base,
                    i,
                    oversized_bigint=oversized_bigint,
                )
        elif col.enum_values:
            domain = tuple(col.enum_values)
            for i in range(n):
                if col.name == primary_enum:
                    if i in forced_enum:
                        rows[i][col.name] = forced_enum[i]
                        continue
                    parent_key = assignments[0][i] if assignments else None
                    avoid = avoid_by_parent.get(parent_key) if parent_key is not None else None
                    filtered = tuple(v for v in domain if v != avoid) or domain
                    rows[i][col.name] = filtered[stream.randrange(len(filtered))]
                else:
                    rows[i][col.name] = domain[stream.randrange(len(domain))]
        else:
            sample_index = 0
            variant_offset = _POPULATION_VARIANT_OFFSET[population] + derive_seed(
                task_id,
                tspec.name,
                col.name,
                "adversarial-variant",
            )
            for i in range(n):
                if col.nullable and stream.random() < policy.null_value_frac:
                    rows[i][col.name] = None
                else:
                    rows[i][col.name] = _synth_value(
                        col,
                        stream,
                        policy,
                        sample_index=sample_index,
                        variant_offset=variant_offset,
                    )
                    sample_index += 1

    # ---- temporal ordering: same-type temporal columns non-decreasing in schema order ----
    for ttype in (ColumnType.DATE, ColumnType.TIMESTAMP):
        tcols = [
            c.name
            for c in tspec.columns
            if c.type is ttype and c.name not in fk_cols and c.name not in id_cols
        ]
        if len(tcols) > 1:
            for row in rows:
                values = sorted(v for v in (row[c] for c in tcols) if v is not None)
                it = iter(values)
                for c in tcols:
                    if row[c] is not None:
                        row[c] = next(it)

    # ---- exact-duplicate injection (stress): PK-less tables only, and copies are
    # verbatim, so business-key uniqueness among DISTINCT rows survives ----
    if policy.dup_frac > 0 and not tspec.primary_key and rows:
        dup_count = int(round(len(rows) * policy.dup_frac))
        drng = _stream(task_id, population, tspec.name, "dup")
        base_len = len(rows)
        for _ in range(dup_count):
            rows.append(dict(rows[drng.randrange(base_len)]))
    return rows


#: Random redraws per colliding FK-only tuple before the deterministic pool scan.
_FK_UNIQUE_RANDOM_TRIES = 64


def _fk_only_key_groups(tspec: TableSpec, fk_cols: Collection[str]) -> list[tuple[str, ...]]:
    """The declared keys of `tspec` made ENTIRELY of foreign-key columns."""
    return [
        group
        for group in (tspec.primary_key, tspec.business_key)
        if group and set(group) <= set(fk_cols)
    ]


def _enforce_fk_identity_uniqueness(
    task_id: str,
    population: PopulationName,
    tspec: TableSpec,
    fk_cols: dict[str, tuple[int, int]],
    pools: list[list[tuple]],
    eligibles: list[list[int]],
    assignments: list[list[tuple | None]],
    n: int,
) -> None:
    """Make every FK-only key group's tuples unique across the `n` rows.

    Redraws the group's HIGHEST relationship first (so relationship 0 keeps its
    coverage / childless / avoid / whale semantics) from a DEDICATED stream — that
    is what keeps tables without such a key byte-identical. NULL tuples are not
    repeats; when every relationship is exhausted, FkIdentityCapacityError."""
    def key_of(i: int, positions: list[tuple[int, int]]) -> tuple | None:
        parts = []
        for ri, pos in positions:
            assigned = assignments[ri][i]
            if assigned is None:
                return None
            parts.append(assigned[pos])
        return tuple(parts)

    def redraw(i: int, ri: int, positions: list[tuple[int, int]], rng: random.Random, seen: set[tuple]) -> bool:
        """Redraw relationship `ri` of row `i` until the group tuple is unseen."""
        options = eligibles[ri]
        pool = pools[ri]
        if not options:
            return False
        for _ in range(_FK_UNIQUE_RANDOM_TRIES):
            assignments[ri][i] = pool[options[rng.randrange(len(options))]]
            key = key_of(i, positions)
            if key is not None and key not in seen:
                return True
        start = rng.randrange(len(options))
        for j in range(len(options)):
            assignments[ri][i] = pool[options[(start + j) % len(options)]]
            key = key_of(i, positions)
            if key is not None and key not in seen:
                return True
        return False

    for group in _fk_only_key_groups(tspec, fk_cols):
        positions = [fk_cols[c] for c in group]
        # Highest relationship first; lower ones only when it is exhausted.
        redraw_order = sorted({ri for ri, _ in positions}, reverse=True)

        rng: random.Random | None = None
        seen: set[tuple] = set()
        for i in range(n):
            key = key_of(i, positions)
            if key is None:
                continue
            if key not in seen:
                seen.add(key)
                continue
            if rng is None:
                rng = _stream(task_id, population, tspec.name, "fk-unique", *group)
            found = any(redraw(i, ri, positions, rng, seen) for ri in redraw_order)
            if not found:
                capacity = 1
                for ri in sorted({ri for ri, _ in positions}):
                    capacity *= max(1, len(eligibles[ri]))
                raise FkIdentityCapacityError(
                    f"table {tspec.name!r}: key {tuple(group)} is composed entirely "
                    "of foreign-key columns and its parent pools hold at most "
                    f"{capacity} distinct tuple(s), but population "
                    f"{population.value} realizes {n} rows — lower the scale or "
                    "declare a surrogate key"
                )
            key = key_of(i, positions)
            assert key is not None  # redraw only reports success on a non-NULL, unseen tuple
            seen.add(key)


def _assert_declared_keys_unique(task: TaskIR, rows: dict[str, list[Row]]) -> None:
    """FAIL CLOSED: raise unless every declared key is unique in the generated
    rows. Business keys may repeat only across verbatim duplicate rows (stress
    injection); tuples with a NULL component are never repeats."""
    for tspec in task.tables:
        table_rows = rows.get(tspec.name, [])
        if not table_rows:
            continue
        for group, label, distinct_only in (
            (tspec.primary_key, "primary key", False),
            (tspec.business_key, "business key", True),
        ):
            if not group:
                continue
            seen: dict[tuple, tuple] = {}
            for row in table_rows:
                key = tuple(row.get(c) for c in group)
                if any(v is None for v in key):
                    continue
                identity = tuple(sorted(row.items(), key=lambda kv: kv[0]))
                if key in seen:
                    if distinct_only and seen[key] == identity:
                        continue  # a verbatim duplicate row (stress injection)
                    raise SourceKeyUniquenessError(
                        f"table {tspec.name!r}: {label} {tuple(group)} repeats the "
                        f"tuple {key} across "
                        + ("distinct " if distinct_only else "")
                        + "generated rows — the generator's key invariant is broken; "
                        "refusing to freeze rows that contradict the declared key"
                    )
                seen[key] = identity


def generate_rows(task: TaskIR, population: PopulationName) -> dict[str, list[Row]]:
    """Constraint-aware rows for every table of one population, table -> rows.

    Literal rows are verbatim; the rest follow the population policy at their
    REALIZED (never declared) row count, parents before children. Fails closed on
    an undeclared population, an empty required parent pool, an over-capacity
    FK-only key, or a repeated declared key."""
    pop = task.population(population)  # KeyError if absent — fail closed
    policy = _POLICIES[population]
    out: dict[str, list[Row]] = {}
    for name in _topo_order(task):
        if name in pop.literal_rows:
            out[name] = [dict(r) for r in pop.literal_rows[name]]
            continue
        n = realized_row_count(task.task_id, name, pop.scale.get(name, 0))
        # A table whose backend rounds integers above 2^53 cannot carry the
        # boundary values, in keys or in measures.
        table_policy = (
            dataclasses.replace(policy, exact_bigint_only=True)
            if _carried_by_an_inexact_backend(task, name)
            else policy
        )
        out[name] = (
            _generate_table(task, pop, table_policy, task.table(name), n, out)
            if n > 0
            else []
        )
    _assert_declared_keys_unique(task, out)
    return out


# --- Canonical row artifacts ---

def write_rows(rows: dict[str, list[Row]], out_dir: Path) -> dict[str, str]:
    """Write <table>.jsonl in canonical generation order; return table -> sha256."""
    out_dir.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for table in sorted(rows):
        text = "".join(canonical_json(r) + "\n" for r in rows[table])
        (out_dir / f"{table}.jsonl").write_text(text, encoding="utf-8")
        hashes[table] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return hashes


# --- Renderers: one isolated source environment per backend ---

_POSTGRES_TYPES: dict[ColumnType, str] = {
    ColumnType.INTEGER: "INTEGER",
    ColumnType.BIGINT: "BIGINT",
    ColumnType.FLOAT: "DOUBLE PRECISION",
    # Bare NUMERIC, never NUMERIC(18,2) — INVARIANT: no source renderer may impose
    # a precision/scale that the trusted loader does not, which would round it.
    ColumnType.DECIMAL: "NUMERIC",
    ColumnType.TEXT: "TEXT",
    ColumnType.BOOLEAN: "BOOLEAN",
    ColumnType.DATE: "DATE",
    ColumnType.TIMESTAMP: "TIMESTAMP",
    ColumnType.JSON: "JSONB",
}


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def render_postgres(table: TableSpec, rows: list[Row], out_dir: Path) -> Path:
    """Deterministic load SQL: DROP + CREATE + batched INSERTs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cols = [c.name for c in table.columns]
    relation = quote_sql_identifier(table.name, dialect="postgres", force=True)
    lines: list[str] = [
        f"-- deterministic load script for {table.name} (elt-taskgen)",
        f"DROP TABLE IF EXISTS {relation};",
        f"CREATE TABLE {relation} (",
    ]
    defs = []
    for c in table.columns:
        null_sql = "" if c.nullable else " NOT NULL"
        column = quote_sql_identifier(c.name, dialect="postgres", force=True)
        defs.append(f"  {column} {_POSTGRES_TYPES[c.type]}{null_sql}")
    if table.primary_key:
        pk = ", ".join(
            quote_sql_identifier(c, dialect="postgres", force=True)
            for c in table.primary_key
        )
        defs.append(f"  PRIMARY KEY ({pk})")
    lines.append(",\n".join(defs))
    lines.append(");")
    col_list = ", ".join(
        quote_sql_identifier(c, dialect="postgres", force=True) for c in cols
    )
    for start in range(0, len(rows), _POSTGRES_INSERT_BATCH):
        batch = rows[start : start + _POSTGRES_INSERT_BATCH]
        values = ",\n".join(
            "(" + ", ".join(_sql_literal(r.get(c)) for c in cols) + ")" for r in batch
        )
        lines.append(f"INSERT INTO {relation} ({col_list}) VALUES\n{values};")
    path = out_dir / f"{table.name}.sql"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def render_mongodb(table: TableSpec, rows: list[Row], out_dir: Path) -> Path:
    """One canonical-JSON document per line."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{table.name}.jsonl"
    path.write_text("".join(canonical_json(r) + "\n" for r in rows), encoding="utf-8")
    return path


def render_rest(table: TableSpec, rows: list[Row], out_dir: Path, *, page_size: int = 100) -> Path:
    """Paginated fixture dir: page_0001.json ... plus index.json. The page boundary
    is EXACT — no trailing empty page, and zero rows yields zero pages (index.json
    still written)."""
    if page_size <= 0:
        raise ValueError(f"page_size must be positive, got {page_size}")
    tdir = out_dir / table.name
    tdir.mkdir(parents=True, exist_ok=True)
    pages = [rows[i : i + page_size] for i in range(0, len(rows), page_size)]
    names: list[str] = []
    for k, page in enumerate(pages, start=1):
        fname = f"page_{k:04d}.json"
        payload = {
            "data": page,
            "page": k,
            "page_size": page_size,
            "total_pages": len(pages),
            "total_rows": len(rows),
        }
        (tdir / fname).write_text(canonical_json(payload) + "\n", encoding="utf-8")
        names.append(fname)
    index = {
        "table": table.name,
        "row_count": len(rows),
        "page_size": page_size,
        "pages": names,
    }
    (tdir / "index.json").write_text(canonical_json(index) + "\n", encoding="utf-8")
    return tdir


def render_s3(table: TableSpec, rows: list[Row], out_dir: Path) -> Path:
    """Object-store jsonl layout: <table>/part-00000.jsonl ... (fixed chunking)."""
    tdir = out_dir / table.name
    tdir.mkdir(parents=True, exist_ok=True)
    chunks = [rows[i : i + _S3_PART_ROWS] for i in range(0, len(rows), _S3_PART_ROWS)] or [[]]
    for k, chunk in enumerate(chunks):
        (tdir / f"part-{k:05d}.jsonl").write_text(
            "".join(canonical_json(r) + "\n" for r in chunk), encoding="utf-8"
        )
    return tdir


def _csv_cell(value, *, table: str = "", column: str = "") -> str:
    """One CSV field, NULL rendering as the EMPTY field. A '' TEXT value is
    therefore unrepresentable (every CSV reader turns an empty field into NULL)
    and FAILS CLOSED rather than being silently aliased to NULL."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and value == "":
        raise ValueError(
            "FILES backend cannot represent the empty string distinct from NULL "
            f"for column {table + '.' if table else ''}{column or '?'} — every "
            "CSV reader (Airbyte Files/pandas, DuckDB read_csv, the trusted "
            "loader) turns an empty field into NULL; use NULL or a non-empty "
            "value, or assign the table to another backend"
        )
    return str(value)


def render_files(table: TableSpec, rows: list[Row], out_dir: Path) -> Path:
    """Flat CSV with header, LF line endings (byte-deterministic); NULL is the
    empty field and a '' TEXT value raises (see `_csv_cell`)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{table.name}.csv"
    cols = [c.name for c in table.columns]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(cols)
        for r in rows:
            writer.writerow(
                [_csv_cell(r.get(c), table=table.name, column=c) for c in cols]
            )
    return path


def render_population(
    task: TaskIR,
    population: PopulationName,
    rows: dict[str, list[Row]],
    out_dir: Path,
) -> dict[str, Path]:
    """Dispatch every table to its backend renderer under out_dir/<backend>/;
    returns table -> rendered artifact path."""
    out: dict[str, Path] = {}
    for tspec in task.tables:
        assignment = task.backend_for(tspec.name)
        table_rows = rows.get(tspec.name, [])
        bdir = out_dir / assignment.backend.value
        if assignment.backend is Backend.POSTGRES:
            path = render_postgres(tspec, table_rows, bdir)
        elif assignment.backend is Backend.MONGODB:
            path = render_mongodb(tspec, table_rows, bdir)
        elif assignment.backend is Backend.REST:
            page_size = int(assignment.options.get("page_size", "100"))
            path = render_rest(tspec, table_rows, bdir, page_size=page_size)
        elif assignment.backend is Backend.S3:
            path = render_s3(tspec, table_rows, bdir)
        elif assignment.backend is Backend.FILES:
            path = render_files(tspec, table_rows, bdir)
        else:  # pragma: no cover — Backend is a closed enum
            raise ValueError(f"unknown backend {assignment.backend!r}")
        out[tspec.name] = path
    return out


# --- Materialization: what is on disk must be certifiable as the IR's derivation ---

#: The two subtrees of a materialized population, under
#: tasks/<id>/populations/<pop>/.
POPULATION_ROWS_DIR = "rows"
POPULATION_RENDERED_DIR = "rendered"


def _file_sha256(path: Path) -> str:
    """SHA-256 of one generated file's exact bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _population_data_hashes(pop_dir: Path) -> dict[str, str]:
    """Hash every generated row/rendered file, excluding the manifest itself.

    Restricting the inventory to the two generator-owned subtrees makes the
    non-recursive boundary explicit and prevents a prior manifest from becoming
    an input when callers materialize into an existing directory.
    """
    hashes: dict[str, str] = {}
    for dirname in (POPULATION_ROWS_DIR, POPULATION_RENDERED_DIR):
        root = pop_dir / dirname
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                hashes[path.relative_to(pop_dir).as_posix()] = _file_sha256(path)
    return hashes


def _generator_implementation_sha256() -> str:
    """Exact source-module identity for the code executing materialization.

    ``GENERATION_POLICY_VERSION`` is the compatibility label; this byte digest
    additionally distinguishes local/editable builds that have not yet bumped
    the package or policy version.
    """
    return _file_sha256(Path(__file__))


def _write_population_manifest(
    task: TaskIR,
    population: PopulationName,
    rows: dict[str, list[Row]],
    pop_dir: Path,
) -> None:
    """Atomically persist deterministic, non-recursive materialization evidence."""
    spec = task.population(population)
    logical_rows = {
        table: {
            "row_count": len(rows[table]),
            "sha256": hashlib.sha256(
                canonical_json(rows[table]).encode("utf-8")
            ).hexdigest(),
        }
        for table in sorted(rows)
    }
    manifest = {
        "schema_version": POPULATION_MANIFEST_SCHEMA_VERSION,
        "task_id": task.task_id,
        "task_content_hash": task.content_hash(),
        "population_name": population.value,
        "population_spec_seed": spec.seed,
        "population_spec_hash": spec.content_hash(),
        "generator": {
            "package": "elt-taskgen",
            "package_version": __version__,
            "policy_version": GENERATION_POLICY_VERSION,
            "implementation_digest_kind": GENERATOR_IMPLEMENTATION_DIGEST_KIND,
            "implementation_sha256": _generator_implementation_sha256(),
        },
        "logical_rows_digest_kind": POPULATION_LOGICAL_ROWS_DIGEST_KIND,
        "logical_rows": logical_rows,
        "file_digest_kind": POPULATION_FILE_DIGEST_KIND,
        "files": _population_data_hashes(pop_dir),
    }
    encoded = (canonical_json(manifest) + "\n").encode("utf-8")
    pop_dir.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=pop_dir,
            prefix=f".{POPULATION_MANIFEST_FILENAME}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, pop_dir / POPULATION_MANIFEST_FILENAME)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def materialize_population(
    task: TaskIR, population: PopulationName, pop_dir: Path
) -> dict[str, str]:
    """Generate one population under `pop_dir` as rows/<table>.jsonl plus
    rendered/<backend>/... and a durable materialization manifest; returns
    table -> sha256 of the rows jsonl. Being deterministic is what lets
    `cli.run_generate` re-derive it every run."""
    rows = generate_rows(task, population)
    hashes = write_rows(rows, pop_dir / POPULATION_ROWS_DIR)
    render_population(task, population, rows, pop_dir / POPULATION_RENDERED_DIR)
    _write_population_manifest(task, population, rows, pop_dir)
    return hashes


def verify_population_manifest(
    task: TaskIR, population: PopulationName, pop_dir: Path
) -> dict:
    """Validate durable population evidence without regenerating the dataset.

    This is the fast resume/readiness check.  ``population_drift`` remains the
    independent fresh-derivation check used by reference and acceptance gates.
    """

    path = Path(pop_dir) / POPULATION_MANIFEST_FILENAME
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"materialization manifest is missing or invalid: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("materialization manifest must be a JSON object")
    spec = task.population(population)
    expected = {
        "schema_version": POPULATION_MANIFEST_SCHEMA_VERSION,
        "task_id": task.task_id,
        "task_content_hash": task.content_hash(),
        "population_name": population.value,
        "population_spec_seed": spec.seed,
        "population_spec_hash": spec.content_hash(),
        "logical_rows_digest_kind": POPULATION_LOGICAL_ROWS_DIGEST_KIND,
        "file_digest_kind": POPULATION_FILE_DIGEST_KIND,
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            raise ValueError(
                f"materialization manifest {field} is stale "
                f"({manifest.get(field)!r} != {value!r})"
            )
    generator = manifest.get("generator")
    if not isinstance(generator, dict):
        raise ValueError("materialization manifest generator block is missing")
    expected_generator = {
        "package": "elt-taskgen",
        "package_version": __version__,
        "policy_version": GENERATION_POLICY_VERSION,
        "implementation_digest_kind": GENERATOR_IMPLEMENTATION_DIGEST_KIND,
        "implementation_sha256": _generator_implementation_sha256(),
    }
    if generator != expected_generator:
        raise ValueError("materialization manifest generator identity is stale")
    files = manifest.get("files")
    if not isinstance(files, dict) or files != _population_data_hashes(Path(pop_dir)):
        raise ValueError("materialized population file inventory or digest changed")

    logical = manifest.get("logical_rows")
    if not isinstance(logical, dict):
        raise ValueError("materialization manifest logical_rows block is missing")
    expected_tables = {table.name for table in task.tables}
    if set(logical) != expected_tables:
        raise ValueError("materialization manifest table roster differs from TaskIR")
    for table in sorted(expected_tables):
        rows_path = Path(pop_dir) / POPULATION_ROWS_DIR / f"{table}.jsonl"
        try:
            rows = [
                json.loads(line)
                for line in rows_path.read_text(encoding="utf-8").splitlines()
            ]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"logical rows for table {table!r} are invalid: {exc}") from exc
        observed = {
            "row_count": len(rows),
            "sha256": hashlib.sha256(
                canonical_json(rows).encode("utf-8")
            ).hexdigest(),
        }
        if logical.get(table) != observed:
            raise ValueError(f"logical rows evidence for table {table!r} changed")
    return manifest


def tree_digest(root: Path) -> dict[str, str]:
    """{relative POSIX path: sha256} over every file under `root`, sorted; empty
    when `root` does not exist."""
    if not root.is_dir():
        return {}
    return {
        p.relative_to(root).as_posix(): _file_sha256(p)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def population_drift(
    task: TaskIR, population: PopulationName, pop_dir: Path
) -> tuple[str, ...]:
    """Regenerate and byte-compare rows, renderings, and their manifest against
    `pop_dir`, writing nothing there. Empty tuple == on disk IS the current
    derivation; otherwise one entry per path: differs / missing / not derived."""
    with tempfile.TemporaryDirectory(prefix="elt-taskgen-drift-") as tmp:
        fresh = Path(tmp) / population.value
        materialize_population(task, population, fresh)
        derived = tree_digest(fresh)
    on_disk = tree_digest(pop_dir)
    drift: list[str] = []
    for rel in sorted(set(derived) | set(on_disk)):
        if rel not in on_disk:
            drift.append(f"{rel}: missing on disk")
        elif rel not in derived:
            drift.append(f"{rel}: not derived from the IR")
        elif derived[rel] != on_disk[rel]:
            drift.append(f"{rel}: differs")
    return tuple(drift)

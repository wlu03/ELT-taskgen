"""Measure structural and empirical load and transform difficulty separately.

Structural scores use TaskIR; empirical scores use fixed runs on public inputs.
They combine only during selection and are keyed by content hash.
"""

from __future__ import annotations

from math import log1p

from elt_taskgen.generation.lineage import effective_lineage

from elt_taskgen.models import (
    Backend,
    DifficultyMeasurement,
    EmpiricalDifficulty,
    MartOpKind,
    PopulationName,
    TaskIR,
)

# Pagination / object layout / document parsing: harder to Extract+Load.
HARD_BACKENDS: frozenset[Backend] = frozenset(
    {Backend.REST, Backend.S3, Backend.MONGODB}
)

# Op kinds that ARE a GROUP BY: these differ from `aggregate` only in the
# measure EXPRESSION, not the operator, so they share one feature.
AGGREGATE_OP_KINDS: frozenset[MartOpKind] = frozenset(
    {
        MartOpKind.AGGREGATE,
        MartOpKind.FILTERED_AGGREGATE,
        MartOpKind.DISTINCT,
    }
)

# Op kinds that compile to a WINDOW function: EXTREMA is an ARGMAX emitted as
# `QUALIFY ROW_NUMBER() OVER (...)`, so it is a window by construction.
WINDOW_OP_KINDS: frozenset[MartOpKind] = frozenset(
    {MartOpKind.WINDOW, MartOpKind.EXTREMA}
)

# "Shaping" work: lighter than a join/aggregate/window. CONDITIONAL and RATIO
# are projections, so they belong here rather than with the aggregates.
SHAPING_OP_KINDS: frozenset[MartOpKind] = frozenset(
    {
        MartOpKind.FILTER,
        MartOpKind.DEDUPE,
        MartOpKind.DERIVE,
        MartOpKind.UNION,
        MartOpKind.TIE_BREAK,
        MartOpKind.CONDITIONAL,
        MartOpKind.RATIO,
    }
)

# Saturating caps: a count at or above its cap contributes a full 1.0. Chosen
# to bracket the ELT-Bench anchor range.
LOAD_CAPS: dict[str, float] = {
    "table_count": 20.0,
    "backend_count": 5.0,
    "hard_backend_count": 3.0,
    "source_column_count": 200.0,
    "relationship_count": 20.0,
    "primary_row_count": 100_000_000.0,
}
TRANSFORM_CAPS: dict[str, float] = {
    "join_count": 6.0,
    "aggregate_count": 6.0,
    "window_count": 3.0,
    "shaping_op_count": 10.0,
    "mart_count": 8.0,
    "mart_column_count": 60.0,
    "active_source_table_count": 20.0,
    "active_lineage_ratio": 1.0,
    "semantic_interaction_count": 8.0,
    "plan_signature_count": 4.0,
}

LOAD_WEIGHTS: dict[str, float] = {
    "table_count": 0.15,
    "backend_count": 0.15,
    "hard_backend_count": 0.10,
    "source_column_count": 0.15,
    "relationship_count": 0.10,
    "primary_row_count": 0.35,
}
TRANSFORM_WEIGHTS: dict[str, float] = {
    "join_count": 0.14,
    "aggregate_count": 0.13,
    "window_count": 0.09,
    "shaping_op_count": 0.09,
    "mart_count": 0.10,
    "mart_column_count": 0.08,
    "active_source_table_count": 0.12,
    "active_lineage_ratio": 0.10,
    "semantic_interaction_count": 0.10,
    "plan_signature_count": 0.05,
}


def _norm(count: float, cap: float) -> float:
    """Saturating normalization to [0, 1]."""
    if cap <= 0:
        return 0.0
    return min(float(count) / cap, 1.0)


def _log_norm(count: float, cap: float) -> float:
    """Log-scaled normalization for row volume spanning many orders of magnitude."""

    if cap <= 0 or count <= 0:
        return 0.0
    return min(log1p(float(count)) / log1p(cap), 1.0)


def _population_rows(task: TaskIR, name: PopulationName) -> tuple[int, bool]:
    """Return (declared/literal rows, is literal) without requiring all five specs."""

    population = next((p for p in task.populations if p.name is name), None)
    if population is None:
        return 0, False
    if population.literal_rows:
        return sum(len(rows) for rows in population.literal_rows.values()), True
    return sum(population.scale.values()), False


def structural_features(task: TaskIR) -> dict[str, float]:
    """Raw structural feature counts from the TaskIR alone (no execution).

    Floats, so they slot straight into `DifficultyMeasurement.structural`.
    """
    backends = {b.backend for b in task.backends}

    op_counts: dict[MartOpKind, int] = {kind: 0 for kind in MartOpKind}
    semantic_interactions = 0
    multi_family_marts = 0
    semantic_families: set[str] = set()
    signatures: set[tuple[str, ...]] = set()
    for mart in task.marts:
        mart_counts: dict[MartOpKind, int] = {kind: 0 for kind in MartOpKind}
        for op in mart.plan.ops:
            op_counts[op.kind] += 1
            mart_counts[op.kind] += 1
        families = {
            family
            for family, present in (
                ("join", bool(mart_counts[MartOpKind.JOIN])),
                (
                    "aggregate",
                    any(mart_counts[kind] for kind in AGGREGATE_OP_KINDS),
                ),
                ("window", any(mart_counts[kind] for kind in WINDOW_OP_KINDS)),
                ("shaping", any(mart_counts[kind] for kind in SHAPING_OP_KINDS)),
            )
            if present
        }
        semantic_families.update(families)
        semantic_interactions += max(0, len(families) - 1)
        multi_family_marts += int(len(families) >= 2)
        signatures.add(tuple(op.kind.value for op in mart.plan.ops))

    shaping = sum(op_counts[k] for k in SHAPING_OP_KINDS)
    aggregates = sum(op_counts[k] for k in AGGREGATE_OP_KINDS)
    windows = sum(op_counts[k] for k in WINDOW_OP_KINDS)
    try:
        lineage = effective_lineage(task)
        used_tables = set(lineage.used_tables)
        unused_tables = set(lineage.unused_tables)
        lineage_compiled = 1.0
    except ValueError:
        # Preserve the declared-operation fallback for incomplete drafts.
        declared = {table.name for table in task.tables}
        used_tables = {
            table
            for mart in task.marts
            for op in mart.plan.ops
            for table in op.tables
            if table in declared
        }
        unused_tables = declared - used_tables
        lineage_compiled = 0.0
    primary_rows, primary_is_literal = _population_rows(task, PopulationName.PRIMARY)
    stress_rows, stress_is_literal = _population_rows(task, PopulationName.STRESS)
    active_count = len(used_tables)
    active_ratio = active_count / len(task.tables) if task.tables else 0.0

    return {
        # load side
        "table_count": float(len(task.tables)),
        "backend_count": float(len(backends)),
        "hard_backend_count": float(sum(1 for b in backends if b in HARD_BACKENDS)),
        "source_column_count": float(sum(len(t.columns) for t in task.tables)),
        "relationship_count": float(len(task.relationships)),
        "primary_row_count": float(primary_rows),
        "stress_row_count": float(stress_rows),
        "provided_primary_row_count": float(primary_rows if primary_is_literal else 0),
        "synthetic_primary_row_count": float(0 if primary_is_literal else primary_rows),
        "provided_stress_row_count": float(stress_rows if stress_is_literal else 0),
        # transform side
        "join_count": float(op_counts[MartOpKind.JOIN]),
        "aggregate_count": float(aggregates),
        "window_count": float(windows),
        "filter_count": float(op_counts[MartOpKind.FILTER]),
        "dedupe_count": float(op_counts[MartOpKind.DEDUPE]),
        "derive_count": float(op_counts[MartOpKind.DERIVE]),
        "union_count": float(op_counts[MartOpKind.UNION]),
        "tie_break_count": float(op_counts[MartOpKind.TIE_BREAK]),
        # Per-kind detail reported ALONGSIDE the family totals, so an audit can
        # still decompose them; not double-counted into the score.
        "filtered_aggregate_count": float(op_counts[MartOpKind.FILTERED_AGGREGATE]),
        "distinct_count": float(op_counts[MartOpKind.DISTINCT]),
        "extrema_count": float(op_counts[MartOpKind.EXTREMA]),
        "conditional_count": float(op_counts[MartOpKind.CONDITIONAL]),
        "ratio_count": float(op_counts[MartOpKind.RATIO]),
        "shaping_op_count": float(shaping),
        "mart_count": float(len(task.marts)),
        "mart_column_count": float(sum(len(m.columns) for m in task.marts)),
        "active_source_table_count": float(active_count),
        "inactive_source_table_count": float(len(unused_tables)),
        "active_lineage_ratio": float(active_ratio),
        "active_lineage_compiled": lineage_compiled,
        "semantic_interaction_count": float(semantic_interactions),
        "semantic_family_count": float(len(semantic_families)),
        "multi_family_mart_count": float(multi_family_marts),
        "plan_signature_count": float(len(signatures)),
    }


def structural_difficulty(task: TaskIR) -> DifficultyMeasurement:
    """Score load and transform difficulty from the IR alone.

    `empirical` is left None for calibration to fill via :func:`with_empirical`.
    Binds to `task.content_hash()`, so a repair invalidates the measurement.
    """
    feats = structural_features(task)

    load_score = sum(
        LOAD_WEIGHTS[name]
        * (
            _log_norm(feats[name], LOAD_CAPS[name])
            if name == "primary_row_count"
            else _norm(feats[name], LOAD_CAPS[name])
        )
        for name in LOAD_WEIGHTS
    )
    transform_score = sum(
        TRANSFORM_WEIGHTS[name] * _norm(feats[name], TRANSFORM_CAPS[name])
        for name in TRANSFORM_WEIGHTS
    )

    # Guard against float drift out of the model's [0, 1] bounds.
    load_score = min(max(load_score, 0.0), 1.0)
    transform_score = min(max(transform_score, 0.0), 1.0)

    return DifficultyMeasurement(
        task_id=task.task_id,
        task_content_hash=task.content_hash(),
        load_score=load_score,
        transform_score=transform_score,
        structural=feats,
        empirical=None,
    )


def with_empirical(
    m: DifficultyMeasurement, e: EmpiricalDifficulty
) -> DifficultyMeasurement:
    """Attach empirical outcomes while preserving the structural content hash.

    Reject non-empty empirical hashes that do not match. Legacy empty hashes
    remain accepted.
    """
    if (
        e.measured_at_content_hash
        and e.measured_at_content_hash != m.task_content_hash
    ):
        raise ValueError(
            "stale empirical evidence: measured at content hash "
            f"{e.measured_at_content_hash!r} but the measurement binds to "
            f"{m.task_content_hash!r} — re-run calibration at the current hash"
        )
    return m.model_copy(update={"empirical": e})

"""Public mart-plan API and the single shape registry.

Shared operations, validation, and concrete builders live in their owning modules.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError
from elt_taskgen.generation.witness import WINDOW_MIRROR_WITNESSES, WITNESS_ALL_FAIL, WITNESS_ARGMAX_CASE_ORDER, WITNESS_ARGMAX_NULL_ORDER, WITNESS_BELOW_THRESHOLD, WITNESS_BRIDGE_NO_CHILD, WITNESS_CHILDLESS, WITNESS_CONTROL, WITNESS_DISTINCT_MEASURE, WITNESS_DUPLICATE, WITNESS_LATEST_FIRST, WITNESS_NULL_MEASURE, WITNESS_ON_THRESHOLD, WITNESS_ORDER, WITNESS_OUT_OF_DOMAIN, WITNESS_SAME_CHILD, WITNESS_SECOND_PERIOD, WITNESS_TIE
from elt_taskgen.models import AttackKind, ColumnType, JoinType, MartColumn, MartColumnKind, MartOp, MartOpKind, MartPlan, MartSpec, SemanticPattern, TaskIR
from elt_taskgen.sql_identifiers import quote_sql_identifier

from elt_taskgen.generation.plan_validation import (
    AGGREGATE_FAMILY_KINDS,
    MartBudgetError,
    NAVIGATION_WINDOW_FUNCTIONS,
    OP_COLUMN_KIND,
    PROJECTION_KINDS,
    RESERVED_AGGREGATE_DETAIL_KEYS,
    SEMANTIC_PATTERN_CERTIFIERS,
    ShapeNotSelectable,
    _CLAIM_MARKER,
    _COLUMN_KIND_PRECEDENCE,
    _DENOMINATOR_HINTS,
    _DIRECTION_RE,
    _NON_NULL_PRESERVING_BINARY,
    _NON_NULL_PRESERVING_UNARY,
    _NULLABLE_INPUT_AGGREGATES,
    _NULL_DEFAULT_HINTS,
    _NULL_ORDER_RE,
    _PLAN_LEVEL_KINDS,
    _aggregate_can_be_null,
    _aggregate_dependent_aliases,
    _aggregate_family_problems,
    _aggregates,
    _certifies_advanced_recipe,
    _certifies_aggregate_extrema,
    _certifies_aggregate_then_filter,
    _certifies_branched_fact_rollup,
    _certifies_calendar_streak,
    _certifies_global_baseline,
    _certifies_graph_recipe,
    _certifies_multiple_extrema,
    _certifies_scenario_recipe,
    _claimed_columns,
    _conditional_problems,
    _expression_proven_non_null,
    _extrema_problems,
    _identifier_names,
    _op_text,
    _order_segments,
    _order_terms,
    _parse_expression,
    _parse_select_list,
    _predicate_is_scoped,
    _produced_aliases,
    _ratio_problems,
    _select_projections,
    _window_problems,
    _zero_substituted_inputs,
    column_kind_problems,
    column_kind_problems_for,
    column_kinds_from_plan,
    computed_column_count,
    detect_semantic_patterns,
    guard_conditions,
    guard_literals,
    is_aggregate_expr,
    is_constant_divisor_expr,
    is_distinct_aggregate_expr,
    is_filtered_aggregate_expr,
    label_problems,
    measure_items,
    op_problems,
    predicate_governs,
    semantic_pattern_problems,
    unclassified_columns,
    validate_plan,
)

from elt_taskgen.generation.plan_ops import (
    AllNullAggregateWitness,
    BranchedFactCandidate,
    BranchedFactShape,
    BuiltPlan,
    ChainEvidence,
    Derived,
    Extremum,
    FactBranch,
    FactRoles,
    KeyColumn,
    MAX_DATASET_COLUMN_CHARS,
    MIN_MART_COLUMNS,
    MIN_MART_COMPUTED,
    MIN_MART_PASSTHROUGH,
    Measure,
    Passthrough,
    RoundBeforeSumWitness,
    SOLVER_PUBLIC_DETAIL_KEYS,
    StarJoin,
    StarShape,
    TEXT_ORDER_PROSE,
    WindowExpr,
    _COMPILER_ONLY_DETAIL_KEYS,
    _DATASET_COLUMN_RULES,
    _EXTREMUM_DEFAULTS,
    _GENERIC_ATTR_COLUMNS,
    _GENERIC_KEY_COLUMNS,
    _LADDER_ARM_RE,
    _LADDER_BOUNDARY_RE,
    _LADDER_ELSE_RE,
    _LADDER_TEST_RE,
    _NameEvidence,
    _PROSE_AMBIGUOUS_NAMES,
    _QUOTED_NAME_RE,
    _ROLE_GROUPS,
    _SEMANTIC_PARENTHETICAL_RE,
    _WITNESS_REQUIREMENTS,
    _article,
    _certified,
    _computed_aliases,
    _dataset_column_names,
    _dataset_name,
    _dataset_rename_map,
    _default_prose,
    _fmt,
    _in_list,
    _ladder_arm_at,
    _ladder_boundary_statement,
    _measure_alias,
    _outcome_sentence,
    _qualified,
    _rename_built_plan,
    _renamed_prose,
    _renamed_text,
    _require,
    _sayable_semantic_value,
    _select_list,
    _singular,
    _slug,
    _solver_public_semantic_details,
    _source_op_description,
    _sql_literal,
    _union_shape_names,
    _unique,
    _witness_problems,
    attack_surface,
    budget_problems,
    case_map_expr,
    conditional_op,
    distinct_count_expr,
    distinct_source_problems,
    extrema_op,
    extremum_default,
    filtered_count_expr,
    filtered_sum_expr,
    group_by_op,
    infer_aggregate_kind,
    kind_certification_problems,
    lag_delta_expr,
    plan_summary,
    plan_template_signature,
    quote,
    ratio_expr,
    ratio_op,
    relation_identifier,
    running_total_expr,
    solver_safe_condition_requirements,
    solver_safe_mart_requirements,
    solver_safe_plan_requirements,
    solver_safe_plan_summary,
    threshold_ladder_expr,
    window_op,
    wrong_window_mirror_problem,
)

from elt_taskgen.generation.shapes.rollup import (
    build_projection,
    build_rollup,
    build_star,
)

# Concrete builders share the vocabulary directly; these are public aliases.
from elt_taskgen.generation.shapes.aggregate_then_filter import (  # noqa: E402
    aggregate_then_filter,
)
from elt_taskgen.generation.shapes.argmax_profile import argmax_profile  # noqa: E402
from elt_taskgen.generation.shapes.branched_fact_rollup import branched_fact_rollup  # noqa: E402
from elt_taskgen.generation.shapes.categorical_ladder import (  # noqa: E402
    categorical_ladder,
)
from elt_taskgen.generation.shapes.fan_out_rollup import fan_out_rollup  # noqa: E402
from elt_taskgen.generation.shapes.latest_snapshot import latest_snapshot  # noqa: E402
from elt_taskgen.generation.shapes.measure_state_distribution import (  # noqa: E402
    measure_state_distribution,
)
from elt_taskgen.generation.shapes.orphan_coverage import orphan_coverage  # noqa: E402
from elt_taskgen.generation.shapes.status_cohort_union import (  # noqa: E402
    status_cohort_union,
)
from elt_taskgen.generation.shapes.temporal_grid import temporal_grid  # noqa: E402


ShapeBuilder = Callable[..., BuiltPlan]


@dataclass(frozen=True)
class ShapeRegistration:
    """One authoritative plan-library shape registration.

    ``shape_name`` is the builder's stable template identity;
    ``adapter_suffix`` is the schema adapter's mart-name suffix.
    ``adapter_auto_select`` controls policy, not capability. Registered shapes
    such as ``orphan_coverage`` can remain excluded from automatic selection to
    preserve existing mart rosters and task hashes.
    """

    shape_name: str
    builder: ShapeBuilder
    adapter_suffix: str
    adapter_auto_select: bool = True


#: The single authoritative plan-library registry.  Order is semantic policy:
#: richest evidence first, distinct relational programs before the generic
#: numeric argmax, with orphan coverage retained as an explicit final shape.
SHAPE_REGISTRY: tuple[ShapeRegistration, ...] = (
    ShapeRegistration("fan_out_rollup", fan_out_rollup, "rollup"),
    ShapeRegistration(
        "aggregate_then_filter",
        aggregate_then_filter,
        "having",
        adapter_auto_select=False,
    ),
    ShapeRegistration("status_cohort_union", status_cohort_union, "cohorts"),
    ShapeRegistration("temporal_grid", temporal_grid, "by_period"),
    ShapeRegistration("latest_snapshot", latest_snapshot, "snapshot"),
    ShapeRegistration("categorical_ladder", categorical_ladder, "bands"),
    ShapeRegistration(
        "measure_state_distribution", measure_state_distribution, "distribution"
    ),
    ShapeRegistration("argmax_profile", argmax_profile, "top"),
    ShapeRegistration(
        "orphan_coverage",
        orphan_coverage,
        "orphan_coverage",
        adapter_auto_select=False,
    ),
)


def registered_shape_builders(
    *, adapter_auto_select: bool = False
) -> tuple[tuple[str, ShapeBuilder], ...]:
    """Return a stable view of the registry for a concrete consumer.

    The default view uses canonical shape names and includes every registered
    shape (tests, proofs, and direct plan-library selection).  The adapter view
    uses mart suffixes and includes only shapes whose selection policy is
    enabled.  Returning tuples keeps callers deterministic and prevents them
    from mutating registry state.
    """

    if adapter_auto_select:
        return tuple(
            (registration.adapter_suffix, registration.builder)
            for registration in SHAPE_REGISTRY
            if registration.adapter_auto_select
        )
    return tuple(
        (registration.shape_name, registration.builder)
        for registration in SHAPE_REGISTRY
    )


#: Backward-compatible view used by ``select_shapes`` and by callers that
#: temporarily inject a builder to test the trial protocol.  It is derived
#: from ``SHAPE_REGISTRY``; do not add shapes directly to this tuple.
SHAPE_BUILDERS: tuple[tuple[str, ShapeBuilder], ...] = registered_shape_builders()


def select_shapes(
    evidence: ChainEvidence, *, mart_prefix: str, budget: int = 2
) -> tuple[BuiltPlan, ...]:
    """Every shape this schema's EVIDENCE supports, up to the mart budget, in
    deterministic SHAPE_BUILDERS order.

    Raises when the evidence supports nothing — no falling back to a narrow
    projection. Only `ShapeNotSelectable` and `MartBudgetError` mean "not on this
    schema"; any other ValueError from a builder is a bug and propagates.
    """
    built: list[BuiltPlan] = []
    for name, builder in SHAPE_BUILDERS:
        if len(built) >= budget:
            break
        try:
            built.append(builder(evidence, mart=f"{mart_prefix}_{name}"))  # type: ignore[operator]
        except (ShapeNotSelectable, MartBudgetError):
            # The two declines the trial protocol exists for; any OTHER
            # ValueError is a builder bug and propagates.
            continue
    if not built:
        raise ValueError(
            f"no plan-library shape is selectable on this schema (mart_prefix="
            f"{mart_prefix!r}); do not fall back to a narrow projection"
        )
    return tuple(built)

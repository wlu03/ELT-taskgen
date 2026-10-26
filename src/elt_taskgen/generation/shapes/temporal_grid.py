"""Build the temporal-grid mart-plan shape."""

from __future__ import annotations

from elt_taskgen.generation.witness import (
    WITNESS_ALL_FAIL,
    WITNESS_BRIDGE_NO_CHILD,
    WITNESS_CONTROL,
    WITNESS_ON_THRESHOLD,
    WITNESS_SECOND_PERIOD,
)

__all__ = ["temporal_grid"]


def temporal_grid(evidence: ChainEvidence, *, mart: str, notes: str = "") -> BuiltPlan:
    """Grain = (entity, truncated period). The only shape that changes the mart's
    ROW COUNT rather than its values — the purest wrong_grain target.

    The period delta is a CONDITIONAL AGGREGATE, never a self-join, which yields
    NULL instead of 0 for a single-period entity. Declines when a nullable owner
    fk or timestamp would put a NULL in the grain, which gold refuses to freeze.
    """
    e = evidence
    _require(
        e, "temporal_grid",
        "bridge", "bridge_key", "bridge_parent_fk", "bridge_timestamp",
        "bridge_amount", "bridge_status", "bridge_status_pass", "bridge_status_fail",
        "parent", "parent_key", "parent_attr",
    )
    # Grain keys cannot be NULL by construction: a NULL names no row and
    # gold refuses to freeze it (NullGrainKeyError). Decline here — selection is
    # by trial — rather than build a mart gold rejects three stages later.
    if e.owner_key_nullable:
        raise ShapeNotSelectable(
            "shape 'temporal_grid': entity_key would rest on a nullable optional "
            f"link ({e.bridge}.{e.bridge_parent_fk}); a NULL grain key is refused "
            "at freeze (gold.NullGrainKeyError) so the shape is not selectable here"
        )
    if e.bridge_timestamp_nullable:
        raise ShapeNotSelectable(
            "shape 'temporal_grid': period_start would rest on a nullable "
            f"timestamp ({e.bridge}.{e.bridge_timestamp}); a NULL month names no "
            "grain row (gold.NullGrainKeyError) so the shape is not selectable here"
        )
    passes = _in_list("link_status", e.bridge_status_pass)
    return build_rollup(
        mart=mart,
        shape_name="temporal_grid",
        carried_per_key=True,
        # The FACT table is the base here: the grain is (entity, period) and
        # the period is derived from the fact's own cursor column.
        parent=e.bridge,
        keys=(
            KeyColumn(
                column="entity_key",
                type=e.parent_key_type,
                description=f"The {e.bridge_parent_fk} the activity belongs to.",
                source=e.bridge_parent_fk,
            ),
            KeyColumn(
                column="period_start",
                type=ColumnType.DATE,
                description=(
                    f"First day of the calendar MONTH of {e.bridge_timestamp}. One "
                    "row per (entity, month) pair that has at least one row."
                ),
                expr=(
                    f"CAST(DATE_TRUNC('month', "
                    f"{relation_identifier(e.bridge)}.{quote(e.bridge_timestamp)}) AS DATE)"
                ),
            ),
        ),
        parent_carry=(
            (e.bridge_key, "link_key"),
            (e.bridge_amount, "link_amount"),
            (e.bridge_status, "link_status"),
            (e.bridge_timestamp, "link_ts"),
        ),
        passthrough=(
            Passthrough(
                column="entity_name",
                type=e.parent_attr_type,
                description=(
                    f"{e.parent_attr} of the {e.parent} row, copied unchanged; "
                    f"'(unknown)' when the entity has no matching {e.parent} row, "
                    f"and also when that row's {e.parent_attr} is itself missing."
                ),
                from_hop=e.parent,
                null_default="'(unknown)'",
            ),
        ),
        hops=(
            StarJoin(
                table=e.parent,
                on_pairs=(("entity_key", e.parent_key),),
                carry=((e.parent_attr, "entity_name"),),
                rel_columns=(e.parent_key, e.bridge_parent_fk),
                # Builder note, comment-only: an INNER join here would silently
                # discard activity whose entity row is missing.
                description=(
                    f"Bring in {e.parent} for the entity's name: activity whose "
                    f"{e.parent} row is missing is RETAINED and reports the "
                    "declared default name."
                ),
            ),
        ),
        measures=(
            Measure(
                column="event_count",
                expr='COUNT("link_key")',
                type=ColumnType.BIGINT,
                description="Rows in this (entity, month) cell.",
            ),
            Measure(
                column="period_amount",
                expr='SUM("link_amount")',
                null_default="0",
                type=e.bridge_amount_type,
                # No 'when empty' arm: a cell exists only when it has at least
                # one row (see period_start), so the only way the default
                # fires is the all-NULL arm — which is therefore the arm stated.
                description=(
                    f"Sum of {e.bridge_amount} in this cell; 0 when none of the "
                    f"cell's rows carries a {e.bridge_amount} value (a cell always "
                    "has at least one row)."
                ),
            ),
            Measure(
                column="distinct_day_count",
                expr='COUNT(DISTINCT CAST("link_ts" AS DATE))',
                type=ColumnType.BIGINT,
                description="How many DISTINCT calendar days in the month have rows.",
            ),
            Measure(
                column="active_event_count",
                expr=f'COUNT(CASE WHEN {passes} THEN "link_key" END)',
                type=ColumnType.BIGINT,
                description=(
                    f"Rows in the cell whose {e.bridge_status} is one of "
                    f"{list(e.bridge_status_pass)}; 0, never missing, when none are."
                ),
            ),
            Measure(
                column="distinct_status_count",
                expr='COUNT(DISTINCT "link_status")',
                type=ColumnType.BIGINT,
                description=(
                    f"How many DISTINCT {e.bridge_status} values occur in the cell."
                ),
            ),
        ),
        post_windows=(
            WindowExpr(
                alias="running_amount",
                expr=(
                    "SUM({period_amount}) OVER (PARTITION BY {entity_key} "
                    "ORDER BY {period_start} ASC "
                    "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
                ),
                type=e.bridge_amount_type,
                description=(
                    "period_amount accumulated from the entity's FIRST month up to "
                    "and INCLUDING this one. The frame is stated explicitly."
                ),
            ),
            WindowExpr(
                alias="prev_period_amount",
                expr=(
                    "LAG({period_amount}, 1, 0) OVER (PARTITION BY {entity_key} "
                    "ORDER BY {period_start} ASC)"
                ),
                type=e.bridge_amount_type,
                description=(
                    "period_amount of this entity's PREVIOUS month in the mart; 0 "
                    "for the entity's first month."
                ),
            ),
        ),
        derived=(
            Derived(
                column="period_share",
                expr=(
                    "COALESCE(ROUND(CAST({period_amount} AS DOUBLE) / "
                    "NULLIF({running_amount}, 0), 4), 0.0)"
                ),
                type=ColumnType.FLOAT,
                description=(
                    "period_amount divided by running_amount as a FRACTION between "
                    "0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when "
                    "running_amount is 0."
                ),
            ),
            Derived(
                column="trend",
                expr=(
                    "CASE WHEN {period_amount} > {prev_period_amount} THEN 'up' "
                    "WHEN {period_amount} = {prev_period_amount} THEN 'flat' "
                    "ELSE 'down' END"
                ),
                type=ColumnType.TEXT,
                description=(
                    "'up' when period_amount is STRICTLY greater than "
                    "prev_period_amount, 'flat' when they are exactly equal, 'down' "
                    "otherwise. Never NULL."
                ),
                kind=MartColumnKind.CATEGORICAL,
            ),
        ),
        attack_claims=(
            "no_dedup", "no_null_default",
            "dropped_filter", "dropped_filter@filter_to_where",
            "wrong_denominator", "wrong_grain",
            "custom@wrong_boundary_else", "custom@wrong_boundary_inclusive",
        )
        # NOT `wrong_window`, despite two OVER() expressions: both are
        # POST-aggregate, so the mutant's answer rides on the physical order
        # DuckDB returns grouped rows in — a coin flip, not a discrimination.
        # The base table IS the activity table, so an orphaned ACTIVITY is the
        # only witness a join-type error has here.
        + (("inner_join",) if e.owner_link_optional else ()),
        roles=FactRoles(
            link_key=e.bridge_key,
            measure=e.bridge_amount,
            predicate_column=e.bridge_status,
            predicate_pass=e.bridge_status_pass,
            predicate_fail=e.bridge_status_fail,
            period_column=e.bridge_timestamp,
            # An "orphan" here is an activity row whose ENTITY row is missing:
            # the LEFT JOIN keeps it and reports '(unknown)', an INNER join
            # deletes the activity outright.
            child_key=e.bridge_parent_fk,
            child_primary_key=e.parent_key,
        ),
        witnesses=(
            WITNESS_CONTROL,
            WITNESS_ALL_FAIL,
            WITNESS_ON_THRESHOLD,
        )
        + ((WITNESS_BRIDGE_NO_CHILD,) if e.owner_link_optional else ())
        + (WITNESS_SECOND_PERIOD,),
        child=e.parent,
        child_link_pairs=((e.bridge_parent_fk, e.parent_key),),
        witness_anchor=(e.parent, e.parent_key, e.bridge, e.bridge_parent_fk),
        grain_description=(
            f"One row per ({e.bridge_parent_fk}, calendar month of "
            f"{e.bridge_timestamp}) pair present in {e.bridge}."
        ),
        notes=notes,
    )


# Imported after the function is defined so this module can participate in the
# mart-plan compatibility cycle without observing a half-defined builder.
from elt_taskgen.generation.mart_plan import (  # noqa: E402
    BuiltPlan,
    ChainEvidence,
    ColumnType,
    Derived,
    FactRoles,
    KeyColumn,
    MartColumnKind,
    Measure,
    Passthrough,
    ShapeNotSelectable,
    StarJoin,
    WindowExpr,
    _in_list,
    _require,
    build_rollup,
    quote,
    relation_identifier,
)

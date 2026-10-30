"""Build the fan-out rollup mart-plan shape."""

from __future__ import annotations

from elt_taskgen.generation.witness import (
    WITNESS_ALL_FAIL,
    WITNESS_BRIDGE_NO_CHILD,
    WITNESS_CHILDLESS,
    WITNESS_CONTROL,
    WITNESS_DUPLICATE,
    WITNESS_ON_THRESHOLD,
    WITNESS_SAME_CHILD,
)

__all__ = ["fan_out_rollup"]


def fan_out_rollup(
    evidence: ChainEvidence, *, mart: str, notes: str = ""
) -> BuiltPlan:
    """THE WORKHORSE. Two hops: parent <- bridge <- child, with fan-out.

    Selected by an FK path P <- B <- C where B FANS OUT and carries a numeric
    measure, a categorical column or a timestamp. The fan-out is a precondition,
    not decoration: without it COUNT and COUNT(DISTINCT) agree, the filtered
    aggregate is a no-op and `wrong_grain` is undetectable.
    """
    e = evidence
    _require(
        e, "fan_out_rollup",
        "parent", "parent_key", "parent_attr",
        "bridge", "bridge_key", "bridge_parent_fk", "bridge_child_fk",
        "bridge_status", "bridge_status_pass", "bridge_status_fail",
        "bridge_amount", "child", "child_key", "child_label",
    )
    passes = _in_list("link_status", e.bridge_status_pass)
    # The dedupe belongs in the COLUMN DESCRIPTION, not only in the DEDUPE op:
    # the exported solver bundle ships descriptions but not plan ops, so a rule
    # stated only on the op is one the solver never receives while gold is
    # graded on it. No-op when the bridge has a primary key.
    # Where "DISTINCT" would sit next to a table or column name, the prose
    # gate rejects it as SQL mechanics (declarative_prose "distinct-operator"),
    # and an author who copies the description inherits the rejection
    # (wikidbs__c40096, batch50 2026-09-19). Those descriptions say it this way.
    once = ", each counted once even if the row repeats" if e.bridge_needs_dedupe else ""
    return build_rollup(
        mart=mart,
        shape_name="fan_out_rollup",
        carried_per_key=True,
        parent=e.parent,
        keys=(
            KeyColumn(
                column="parent_key",
                type=e.parent_key_type,
                description=f"Identifier of the {e.parent} row. One row per value.",
                source=e.parent_key,
            ),
        ),
        passthrough=(
            Passthrough(
                column="parent_name",
                type=e.parent_attr_type,
                description=f"{e.parent_attr} of the {e.parent} row, copied unchanged.",
                source=e.parent_attr,
            ),
        ),
        hops=(
            StarJoin(
                table=e.bridge,
                on_pairs=(("parent_key", e.bridge_parent_fk),),
                carry=(
                    (e.bridge_key, "link_key"),
                    (e.bridge_child_fk, "link_child_fk"),
                    (e.bridge_status, "link_status"),
                    (e.bridge_amount, "link_amount"),
                ),
                rel_columns=(e.bridge_parent_fk, e.parent_key),
                # Comment-only: this is the fan-out hop, what makes COUNT and
                # COUNT(DISTINCT) disagree; an INNER join here drops parents
                # with no bridge rows entirely.
                description=(
                    f"Hop 1: bring in {e.bridge} against the grain. One "
                    f"{e.parent} row may have many {e.bridge} rows, and a "
                    f"{e.parent} row with no {e.bridge} rows at all is RETAINED."
                ),
            ),
            StarJoin(
                table=e.child,
                on_pairs=(("link_child_fk", e.child_key),),
                carry=((e.child_key, "dim_key"), (e.child_label, "dim_label")),
                rel_columns=(e.child_key, e.bridge_child_fk),
                # Comment-only: an INNER join at THIS hop drops the childless
                # bridge row — a different error from one at hop 1.
                description=(
                    # Both sides of the match are named: "matching on
                    # scene_id" left the critic two readings when two
                    # relationships shared the column name (synsql__3d_object
                    # _positioning, batch10 2026-09-11).
                    f"Hop 2: bring in {e.child}, matching each linked {e.bridge} "
                    f"row's {e.bridge_child_fk} to the {e.child_key} of a "
                    f"{e.child} row. A {e.bridge} row whose {e.child} row is "
                    "missing still counts as a link and is RETAINED."
                ),
            ),
        ),
        measures=(
            Measure(
                column="link_count",
                expr='COUNT("link_key")',
                type=ColumnType.BIGINT,
                description=(
                    f"Number of {e.bridge} rows associated with this {e.parent} "
                    f"row{once}; 0 when there are none."
                ),
            ),
            Measure(
                column="distinct_child_count",
                expr='COUNT(DISTINCT "dim_key")',
                type=ColumnType.BIGINT,
                description=(
                    f"Number of unique {e.child} records associated through "
                    f"{e.bridge}; 0 when there are none. Missing {e.child} "
                    "records do not count."
                ),
            ),
            Measure(
                column="active_link_count",
                expr=f'COUNT(CASE WHEN {passes} THEN "link_key" END)',
                type=ColumnType.BIGINT,
                description=(
                    f"Number of associated {e.bridge} rows whose {e.bridge_status} "
                    f"is one of {list(e.bridge_status_pass)}{once}; 0 when none qualify."
                ),
            ),
            Measure(
                column="total_amount",
                expr='SUM("link_amount")',
                null_default="0",
                type=e.bridge_amount_type,
                description=(
                    f"Total non-missing {e.bridge_amount} across associated "
                    f"{e.bridge} rows{once}; 0 when there are no values."
                ),
            ),
            Measure(
                column="active_amount",
                expr=f'SUM(CASE WHEN {passes} THEN "link_amount" ELSE 0 END)',
                null_default="0",
                type=e.bridge_amount_type,
                description=(
                    f"Total non-missing {e.bridge_amount} for associated {e.bridge} "
                    f"rows whose {e.bridge_status} is one of {list(e.bridge_status_pass)}"
                    f"{once}; 0 when there are no qualifying values."
                ),
            ),
            Measure(
                column="max_amount",
                expr='MAX("link_amount")',
                null_default="0",
                type=e.bridge_amount_type,
                description=(
                    f"Largest non-missing {e.bridge_amount} across associated "
                    f"{e.bridge} rows; 0 when there are no values."
                ),
            ),
        ),
        derived=(
            Derived(
                column="active_amount_ratio",
                expr=(
                    "COALESCE(ROUND(CAST({active_amount} AS DOUBLE) / "
                    "NULLIF({total_amount}, 0), 4), 0.0)"
                ),
                type=ColumnType.FLOAT,
                description=(
                    "active_amount divided by total_amount, as a fraction rounded "
                    "to 4 decimal places; 0.0 when total_amount is 0."
                ),
                kind=MartColumnKind.DERIVED,
            ),
            Derived(
                column="size_band",
                expr=(
                    "CASE WHEN {link_count} = 0 THEN 'none' "
                    "WHEN {link_count} <= 2 THEN 'small' "
                    "WHEN {link_count} <= 5 THEN 'medium' ELSE 'large' END"
                ),
                type=ColumnType.TEXT,
                description=(
                    "Size band of link_count: 'none' for 0, 'small' for 1–2, "
                    "'medium' for 3–5, and 'large' for more than 5."
                ),
                kind=MartColumnKind.CATEGORICAL,
            ),
            Derived(
                column="has_links",
                expr="CASE WHEN {link_count} > 0 THEN 'yes' ELSE 'no' END",
                type=ColumnType.TEXT,
                description=(
                    "'yes' when this parent has at least one link, 'no' otherwise. "
                    "Never NULL."
                ),
                kind=MartColumnKind.CATEGORICAL,
            ),
        ),
        dedupe=(e.bridge, (e.bridge_key, e.bridge_parent_fk, e.bridge_child_fk,
                           e.bridge_status, e.bridge_amount))
        if e.bridge_needs_dedupe
        else (),
        attack_claims=(
            "inner_join", "no_dedup", "no_null_default",
            "dropped_filter", "dropped_filter@filter_to_where",
            "wrong_denominator", "wrong_grain",
            "custom@wrong_boundary_else", "custom@wrong_boundary_inclusive",
        )
        + (("inner_join@second_hop",) if e.child_link_optional else ()),
        roles=FactRoles(
            link_key=e.bridge_key,
            child_key=e.bridge_child_fk,
            measure=e.bridge_amount,
            label=e.child_label,
            predicate_column=e.bridge_status,
            predicate_pass=e.bridge_status_pass,
            predicate_fail=e.bridge_status_fail,
            child_primary_key=e.child_key,
        ),
        witnesses=(
            (WITNESS_CONTROL, WITNESS_CHILDLESS)
            + ((WITNESS_DUPLICATE,) if e.bridge_needs_dedupe else ())
            + (
                WITNESS_SAME_CHILD,
                WITNESS_ALL_FAIL,
                WITNESS_ON_THRESHOLD,
            )
            + ((WITNESS_BRIDGE_NO_CHILD,) if e.child_link_optional else ())
        ),
        thresholds=(2.0, 5.0),
        child=e.child,
        child_link_pairs=((e.bridge_child_fk, e.child_key),),
        grain_description=(
            f"One row per {e.parent} row, keyed by {e.parent_key}."
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
    StarJoin,
    _in_list,
    _require,
    build_rollup,
)

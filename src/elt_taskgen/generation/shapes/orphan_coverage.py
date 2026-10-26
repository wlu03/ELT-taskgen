"""Build the orphan-coverage mart-plan shape."""

from __future__ import annotations

from elt_taskgen.generation.witness import (
    WITNESS_BRIDGE_NO_CHILD,
    WITNESS_CHILDLESS,
    WITNESS_CONTROL,
)

__all__ = ["orphan_coverage"]


def orphan_coverage(evidence: ChainEvidence, *, mart: str, notes: str = "") -> BuiltPlan:
    """Move the grain to the CHILD side so genuinely orphaned rows stop hiding.

    Selected only where the pool ships REAL rows with genuinely violated FK
    edges; a parent roll-up's LEFT JOIN hides exactly those rows, so putting the
    grain on the child turns existing dirty data into a discriminating trap.
    """
    e = evidence
    _require(
        e, "orphan_coverage",
        "bridge", "bridge_key", "bridge_parent_fk", "bridge_amount",
        "parent", "parent_key", "parent_attr", "owner_link_optional",
    )
    return build_rollup(
        mart=mart,
        shape_name="orphan_coverage",
        carried_per_key=True,
        parent=e.bridge,
        keys=(
            KeyColumn(
                column="owner_bucket",
                type=ColumnType.TEXT,
                description=(
                    f"The {e.bridge_parent_fk} of the {e.bridge} rows, as text, with "
                    "the literal '__orphan__' standing for rows whose "
                    f"{e.bridge_parent_fk} is NULL. One row per bucket."
                ),
                expr=(
                    f"COALESCE(CAST({relation_identifier(e.bridge)}."
                    f"{quote(e.bridge_parent_fk)} AS VARCHAR), "
                    "'__orphan__')"
                ),
            ),
        ),
        parent_carry=(
            (e.bridge_key, "link_key"),
            (e.bridge_amount, "link_amount"),
            (e.bridge_parent_fk, "owner_raw"),
        ),
        passthrough=(
            Passthrough(
                column="owner_name",
                type=e.parent_attr_type,
                description=(
                    f"{e.parent_attr} of the matching {e.parent} row; the literal "
                    "'(unmatched)' when no such row exists, and also when that "
                    f"row's {e.parent_attr} is itself missing."
                ),
                from_hop=e.parent,
                null_default="'(unmatched)'",
            ),
        ),
        hops=(
            StarJoin(
                table=e.parent,
                on_pairs=(("owner_raw", e.parent_key),),
                carry=((e.parent_key, "owner_key"), (e.parent_attr, "owner_name")),
                rel_columns=(e.parent_key, e.bridge_parent_fk),
                # Builder note, comment-only: the join is LEFT precisely so the
                # orphans survive; an INNER join erases the whole finding.
                description=(
                    f"Bring in {e.parent}: orphaned {e.bridge} rows — rows with "
                    f"no matching {e.parent} row — SURVIVE and are counted in "
                    "their own bucket."
                ),
            ),
        ),
        measures=(
            Measure(
                column="child_count",
                expr='COUNT("link_key")',
                type=ColumnType.BIGINT,
                description=f"Number of {e.bridge} rows in this bucket.",
            ),
            Measure(
                column="matched_count",
                expr='COUNT(CASE WHEN "owner_key" IS NOT NULL THEN "link_key" END)',
                type=ColumnType.BIGINT,
                description=(
                    f"Of those, how many have a matching {e.parent} row. 0, never "
                    "missing, for the orphan bucket."
                ),
            ),
            Measure(
                column="orphan_count",
                expr='COUNT(CASE WHEN "owner_key" IS NULL THEN "link_key" END)',
                type=ColumnType.BIGINT,
                description=(
                    f"How many have NO matching {e.parent} row. 0 for every matched "
                    "bucket."
                ),
            ),
            Measure(
                column="distinct_child_count",
                expr='COUNT(DISTINCT "link_key")',
                type=ColumnType.BIGINT,
                description=(
                    f"Number of DISTINCT {e.bridge_key} values in the bucket."
                ),
            ),
            Measure(
                column="total_amount",
                expr='SUM("link_amount")',
                null_default="0",
                type=e.bridge_amount_type,
                # A bucket always holds at least one row (one per fk value
                # present), so the only reachable default arm is the all-NULL
                # one — stated, never 'when empty'.
                description=(
                    f"Sum of {e.bridge_amount} in the bucket; 0 when none of the "
                    f"bucket's rows carries a {e.bridge_amount} value (a bucket "
                    "always has at least one row)."
                ),
            ),
            Measure(
                column="matched_amount",
                expr=(
                    'SUM(CASE WHEN "owner_key" IS NOT NULL THEN "link_amount" '
                    "ELSE 0 END)"
                ),
                null_default="0",
                type=e.bridge_amount_type,
                description=(
                    f"Sum of {e.bridge_amount} over the rows that DO have a matching "
                    f"{e.parent} row; 0 when none do, and 0 when every matching row "
                    f"lacks a {e.bridge_amount} value."
                ),
            ),
        ),
        derived=(
            Derived(
                column="match_rate",
                expr=(
                    "COALESCE(ROUND(CAST({matched_count} AS DOUBLE) / "
                    "NULLIF({child_count}, 0), 4), 0.0)"
                ),
                type=ColumnType.FLOAT,
                description=(
                    "matched_count divided by child_count as a FRACTION between 0 "
                    "and 1 (not a percentage), rounded to 4 decimals, 0.0 when the "
                    "bucket is empty."
                ),
            ),
            Derived(
                column="coverage_band",
                expr=(
                    "CASE WHEN {match_rate} >= 1.0 THEN 'complete' "
                    "WHEN {match_rate} > 0.0 THEN 'partial' ELSE 'orphaned' END"
                ),
                type=ColumnType.TEXT,
                description=(
                    "'complete' when match_rate is exactly 1.0, 'partial' when it is "
                    "STRICTLY above 0.0 and below 1.0, 'orphaned' when it is exactly "
                    "0.0. Never NULL."
                ),
                kind=MartColumnKind.CATEGORICAL,
            ),
        ),
        aggregate_predicate=(
            f"a row counts toward matched_count and matched_amount when a {e.parent} "
            f"row with the same {e.parent_key} exists, and toward orphan_count when "
            "none does; every other measure counts every row of the bucket"
        ),
        attack_claims=(
            "inner_join", "no_null_default",
            "dropped_filter", "dropped_filter@filter_to_where",
            "wrong_denominator", "wrong_grain",
            "custom@wrong_boundary_else", "custom@wrong_boundary_inclusive",
        ),
        roles=FactRoles(
            link_key=e.bridge_key,
            measure=e.bridge_amount,
            # The "child" of the orphan witness is the OWNER: an orphan is a
            # bridge row whose owner key matches no dimension row. On a schema
            # where that edge is required and non-nullable the witness is
            # unconstructible and `_dangling_child_key` refuses it — which is
            # the correct answer, not a reason to declare it anyway.
            child_key=e.bridge_parent_fk,
            child_primary_key=e.parent_key,
        ),
        witnesses=(WITNESS_CONTROL, WITNESS_CHILDLESS, WITNESS_BRIDGE_NO_CHILD),
        witness_anchor=(e.parent, e.parent_key, e.bridge, e.bridge_parent_fk),
        child=e.parent,
        child_link_pairs=((e.bridge_parent_fk, e.parent_key),),
        # "each value once", not "per distinct <fk> value": 'distinct' beside an
        # identifier is operator-adjacency the declarative gate flags.
        grain_description=(
            f"One row per {e.bridge_parent_fk} value present in {e.bridge} — "
            f"each value once, including values that name no {e.parent} row — "
            f"plus a single '__orphan__' bucket for the rows whose "
            f"{e.bridge_parent_fk} is NULL."
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
    _require,
    build_rollup,
    quote,
    relation_identifier,
)

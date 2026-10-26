"""Build the categorical-ladder mart-plan shape."""

from __future__ import annotations

from elt_taskgen.generation.witness import (
    WITNESS_ALL_FAIL,
    WITNESS_CHILDLESS,
    WITNESS_CONTROL,
    WITNESS_ON_THRESHOLD,
    WITNESS_OUT_OF_DOMAIN,
)

__all__ = ["categorical_ladder"]


def categorical_ladder(
    evidence: ChainEvidence, *, mart: str, notes: str = ""
) -> BuiltPlan:
    """CASE ladders over a DECLARED domain, plus the filtered aggregates keyed
    on that same domain.

    FAIL CLOSED: no declared, machine-readable domain, no ladder — an invented
    one has no out-of-domain witness, so its ELSE branch is unfalsifiable.
    """
    e = evidence
    _require(
        e, "categorical_ladder",
        "parent", "parent_key", "parent_attr",
        "parent_domain_column", "domain", "out_of_domain",
        "bridge", "bridge_key", "bridge_parent_fk",
        "bridge_status", "bridge_status_pass", "bridge_status_fail",
    )
    named = tuple(v for v in e.domain if v != e.out_of_domain)
    if len(named) < 2:
        raise ShapeNotSelectable(
            "shape 'categorical_ladder': the declared domain must name at least two "
            "values BESIDES the out-of-domain witness value"
        )
    passes = _in_list("link_status", e.bridge_status_pass)
    fails = _in_list("link_status", e.bridge_status_fail)
    # The mapping targets are INVENTED tokens, producible only if the spec
    # enumerates the mapping value by value. The description is built from this
    # same pairs list, so expr and stated mapping cannot drift apart.
    mapping_pairs = tuple(
        (value, "active" if i == 0 else f"other_{i}")
        for i, value in enumerate(named)
    )
    ladder = "CASE " + " ".join(
        f"WHEN {{parent_status}} = '{src}' THEN '{dst}'"
        for src, dst in mapping_pairs
    ) + " ELSE 'unmapped' END"
    mapping_prose = "; ".join(
        f"'{src}' becomes '{dst}'" for src, dst in mapping_pairs
    )
    return build_rollup(
        mart=mart,
        shape_name="categorical_ladder",
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
            Passthrough(
                column="parent_status",
                type=ColumnType.TEXT,
                description=(
                    f"{e.parent_domain_column} of the {e.parent} row, copied "
                    f"unchanged. Declared domain: {list(e.domain)}."
                ),
                source=e.parent_domain_column,
            ),
        ),
        hops=(
            StarJoin(
                table=e.bridge,
                on_pairs=(("parent_key", e.bridge_parent_fk),),
                carry=((e.bridge_key, "link_key"), (e.bridge_status, "link_status")),
                rel_columns=(e.bridge_parent_fk, e.parent_key),
            ),
        ),
        measures=(
            Measure(
                column="link_count",
                expr='COUNT("link_key")',
                type=ColumnType.BIGINT,
                description=(
                    # The independent implementer counted the LEFT-join
                    # placeholder itself (COUNT(*) = 1 for a childless jobs
                    # row; dlt__workable, batch10 run K, 2026-09-11).
                    f"Number of {e.bridge} rows for this {e.parent} row; "
                    "0 when there are none. "
                    f"Every linked {e.bridge} row counts, whatever its "
                    f"{e.bridge_status} value. "
                    f"{_article(e.parent, capitalized=True)} {e.parent} row kept with no "
                    f"{e.bridge} row reports 0 here, never 1: its placeholder "
                    f"holds no {e.bridge} row to count."
                ),
            ),
            Measure(
                column="passing_count",
                expr=f'COUNT(CASE WHEN {passes} THEN "link_key" END)',
                type=ColumnType.BIGINT,
                description=(
                    f"Of those, how many have {e.bridge_status} in "
                    f"{list(e.bridge_status_pass)}. 0, never missing, when none do."
                ),
            ),
            Measure(
                column="failing_count",
                expr=f'COUNT(CASE WHEN {fails} THEN "link_key" END)',
                type=ColumnType.BIGINT,
                description=(
                    f"How many have {e.bridge_status} in "
                    f"{list(e.bridge_status_fail)}. 0 when none do."
                ),
            ),
            Measure(
                column="distinct_status_count",
                expr='COUNT(DISTINCT "link_status")',
                type=ColumnType.BIGINT,
                description=(
                    f"How many DISTINCT {e.bridge_status} values occur among them."
                ),
            ),
        ),
        # STATED, not derived: this shape counts PASSING rows and FAILING rows
        # in the same op, and `FactRoles` can only carry one condition. The
        # roles-derived fallback would describe `failing_count` with the
        # passing values, so `build_rollup` refuses to guess and this says it.
        aggregate_predicate=(
            f"a row counts toward passing_count when its {e.bridge_status} is in "
            f"{list(e.bridge_status_pass)}, and toward failing_count when it is in "
            f"{list(e.bridge_status_fail)}; link_count and distinct_status_count "
            "count every row of the group"
        ),
        derived=(
            Derived(
                column="passing_ratio",
                expr=(
                    "COALESCE(ROUND(CAST({passing_count} AS DOUBLE) / "
                    "NULLIF({link_count}, 0), 4), 0.0)"
                ),
                type=ColumnType.FLOAT,
                description=(
                    "passing_count divided by link_count as a FRACTION between 0 and "
                    "1 (not a percentage), rounded to 4 decimals, 0.0 when there are "
                    "no links."
                ),
            ),
            Derived(
                column="adoption_band",
                expr=(
                    "CASE WHEN {link_count} = 0 THEN 'no_activity' "
                    "WHEN {passing_ratio} >= 0.8 THEN 'high' "
                    "WHEN {passing_ratio} >= 0.5 THEN 'medium' ELSE 'low' END"
                ),
                type=ColumnType.TEXT,
                description=(
                    # The band is decided on the ROUNDED passing_ratio the mart
                    # reports, not the raw quotient: an independent build that
                    # banded the raw value disagreed with gold on every
                    # population with a ratio just under a boundary
                    # (synsql__3d_motion_tracking, batch10 run E).
                    "Band of passing_ratio, decided on the rounded passing_ratio "
                    "value this mart reports: 'no_activity' when there are no "
                    "links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' "
                    "from 0.5 up to but not including 0.8 (0.5 itself is medium), "
                    "'low' below 0.5. Boundaries are inclusive of the HIGHER band."
                ),
                kind=MartColumnKind.CATEGORICAL,
            ),
            Derived(
                column="status_group",
                expr=ladder,
                type=ColumnType.TEXT,
                description=(
                    f"parent_status mapped value by value: {mapping_prose}; "
                    "any other value — including legal values of the column "
                    "that the mapping does not name — becomes 'unmapped'. "
                    "Never NULL."
                ),
                kind=MartColumnKind.CATEGORICAL,
            ),
        ),
        attack_claims=(
            "inner_join", "no_dedup", "no_null_default",
            "dropped_filter", "dropped_filter@filter_to_where",
            "wrong_denominator", "wrong_grain",
            "custom@wrong_boundary_else", "custom@wrong_boundary_inclusive",
        ),
        roles=FactRoles(
            link_key=e.bridge_key,
            predicate_column=e.bridge_status,
            predicate_pass=e.bridge_status_pass,
            predicate_fail=e.bridge_status_fail,
            domain_column=e.parent_domain_column,
            domain=e.domain,
            out_of_domain=e.out_of_domain,
        ),
        witnesses=(
            WITNESS_CONTROL,
            WITNESS_CHILDLESS,
            WITNESS_ALL_FAIL,
            WITNESS_OUT_OF_DOMAIN,
            WITNESS_ON_THRESHOLD,
        ),
        thresholds=(0.5, 0.8),
        grain_description=f"One row per {e.parent} row, keyed by {e.parent_key}.",
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
    _article,
    _in_list,
    _require,
    build_rollup,
)

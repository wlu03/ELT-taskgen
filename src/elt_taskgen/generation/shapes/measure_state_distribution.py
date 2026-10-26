"""Build the measure-state-distribution mart-plan shape."""

from __future__ import annotations

from typing import TYPE_CHECKING

from elt_taskgen.generation.witness import (
    WITNESS_CHILDLESS,
    WITNESS_CONTROL,
    WITNESS_DISTINCT_MEASURE,
)

if TYPE_CHECKING:
    from elt_taskgen.generation.mart_plan import BuiltPlan, ChainEvidence

__all__ = ["measure_state_distribution"]


def measure_state_distribution(
    evidence: ChainEvidence, *, mart: str, notes: str = ""
) -> BuiltPlan:
    """Summarize present-value and absent-value measure states independently.

    This shape needs a parent grain, a bridge column that can distinguish two
    real links within one parent, and a numeric measure.  It does not invent a
    categorical domain.  Instead it partitions the LEFT-joined rows into two
    exhaustive, disjoint states backed by SQL null semantics:
    ``present`` has a linked row and a non-null measure; ``absent`` has a null
    measure, including the placeholder row for a parent with no activity.
    """
    e = evidence
    amount_may_be_missing = e.bridge_amount_nullable is not False
    _require(
        e,
        "measure_state_distribution",
        "parent",
        "parent_key",
        "parent_attr",
        "bridge",
        "bridge_key",
        "bridge_parent_fk",
        "bridge_amount",
    )
    if e.bridge_key_is_parent_fk:
        raise ShapeNotSelectable(
            "shape 'measure_state_distribution': bridge_key is the parent "
            "foreign key, not a row-distinguishing link key — the required "
            "distinct-measure witness cannot be constructed (fail closed)"
        )

    taken = {e.parent, e.bridge}
    base_rel = _unique("distribution_entities", taken)
    joined_rel = _unique("distribution_rows", taken)
    union_rel = _unique("measure_state_union", taken)
    final_columns = (
        "entity_key",
        "measure_state",
        "entity_name",
        "row_count",
        "distinct_amount_count",
        "total_amount",
        "max_amount",
        "max_amount_share",
    )
    ops: list[MartOp] = [
        MartOp(
            kind=MartOpKind.SOURCE,
            description=f"Read source table {e.parent}.",
            tables=(e.parent,),
        ),
        MartOp(
            kind=MartOpKind.SOURCE,
            description=f"Read source table {e.bridge}.",
            tables=(e.bridge,),
        ),
        MartOp(
            kind=MartOpKind.DERIVE,
            description=(
                f"Carry each {e.parent_key} and its {e.parent_attr} into the "
                "measure-state calculation."
            ),
            tables=(e.parent,),
            columns=("entity_key", "entity_name"),
            details={
                "select": (
                    f'{relation_identifier(e.parent)}.{quote(e.parent_key)} '
                    'AS "entity_key", '
                    f'{relation_identifier(e.parent)}.{quote(e.parent_attr)} '
                    'AS "entity_name"'
                ),
                "name": base_rel,
            },
        ),
        MartOp(
            kind=MartOpKind.JOIN,
            description=(
                f"Bring the linked {e.bridge} rows into each {e.parent} entity; "
                "retain an entity with no linked row so its absent state is visible."
            ),
            tables=(base_rel, e.bridge),
            columns=(
                "entity_key",
                "entity_name",
                "link_key",
                "link_amount",
                e.parent_key,
                e.bridge_parent_fk,
            ),
            join_type=JoinType.LEFT,
            predicate=(
                f'{relation_identifier(e.bridge)}.{quote(e.bridge_parent_fk)} = '
                f'{relation_identifier(base_rel)}."entity_key"'
            ),
            details={
                "select": (
                    f'{relation_identifier(base_rel)}."entity_key" AS "entity_key", '
                    f'{relation_identifier(base_rel)}."entity_name" AS "entity_name", '
                    f'{relation_identifier(e.bridge)}.{quote(e.bridge_key)} '
                    'AS "link_key", '
                    f'{relation_identifier(e.bridge)}.{quote(e.bridge_amount)} '
                    'AS "link_amount"'
                ),
                "name": joined_rel,
            },
        ),
    ]

    branches: list[str] = []
    for label, predicate in (
        ("present", '"link_key" IS NOT NULL AND "link_amount" IS NOT NULL'),
        ("absent", '"link_amount" IS NULL'),
    ):
        filtered_rel = _unique(f"{label}_measure_rows", taken)
        grouped_rel = _unique(f"{label}_measure_grouped", taken)
        ratio_rel = _unique(f"{label}_measure_ratio", taken)
        named_rel = _unique(f"{label}_measure_state", taken)
        ops.append(
            MartOp(
                kind=MartOpKind.FILTER,
                description=(
                    f"Keep the {label} measure-state rows: "
                    + (
                        (
                            f"a real {e.bridge} row whose {e.bridge_amount} has "
                            "a value."
                            if amount_may_be_missing
                            else (
                                f"a real {e.bridge} row; {e.bridge_amount} is "
                                "required on every such row."
                            )
                        )
                        if label == "present"
                        else (
                            (
                                f"{e.bridge_amount} is missing, including the "
                                f"retained placeholder for a {e.parent} row with "
                                f"no {e.bridge} rows. A real {e.bridge} row whose "
                                f"{e.bridge_amount} has a value belongs only to "
                                "the present state and never to this absent state."
                                if amount_may_be_missing
                                else (
                                    f"the retained placeholder for a {e.parent} "
                                    f"row with no {e.bridge} rows; no real row can "
                                    f"enter this state because {e.bridge_amount} "
                                    "is required."
                                )
                            )
                        )
                    )
                ),
                tables=(joined_rel,),
                columns=("entity_key", "entity_name", "link_key", "link_amount"),
                predicate=predicate,
                details={"name": filtered_rel},
            )
        )
        ops.append(
            group_by_op(
                source=filtered_rel,
                name=grouped_rel,
                group_by=("entity_key", "entity_name"),
                measures=(
                    ("row_count", "row_count", 'COUNT("link_key")'),
                    (
                        "distinct_amount_count",
                        "distinct_amount_count",
                        'COUNT(DISTINCT "link_amount")',
                    ),
                    (
                        "total_amount",
                        "total_amount",
                        'COALESCE(SUM("link_amount"), 0)',
                    ),
                    (
                        "max_amount",
                        "max_amount",
                        'COALESCE(MAX("link_amount"), 0)',
                    ),
                ),
                description=(
                    # "represented" was undefined for an entity with no rows in
                    # a state: the critic asked whether a parent with no linked
                    # row also gets a present row, and whether one with only
                    # value-bearing rows gets an absent row (synsql__
                    # educational_expenditure, batch10 2026-09-11).
                    (
                        # Define the retained placeholder row and its values for
                        # required measures in the absent state.
                        f"One row per {e.parent} entity with no linked "
                        f"{e.bridge} row at all, whose retained placeholder is "
                        "its one row in the absent measure state, and no row "
                        f"here for an entity that has a linked {e.bridge} row, "
                        "reporting a row count of 0, 0 different "
                        f"{e.bridge_amount} values, a total {e.bridge_amount} of "
                        f"0 and a largest {e.bridge_amount} of 0."
                        if label == "absent" and not amount_may_be_missing
                        else f"One row per {e.parent} entity that has at least one row "
                        f"in the {label} measure state, and no row here for an "
                        f"entity with none, reporting row count, how many different "
                        + ("non-missing " if amount_may_be_missing else "")
                        + f"{e.bridge_amount} values occur (each different value "
                        "counted once, however many rows repeat it), total "
                        f"{e.bridge_amount}, and largest {e.bridge_amount}."
                    )
                ),
            )
        )
        ops.append(
            ratio_op(
                source=grouped_rel,
                name=ratio_rel,
                projections=(
                    ('"entity_key"', "entity_key"),
                    ('"entity_name"', "entity_name"),
                    ('"row_count"', "row_count"),
                    ('"distinct_amount_count"', "distinct_amount_count"),
                    ('"total_amount"', "total_amount"),
                    ('"max_amount"', "max_amount"),
                    (
                        'ROUND(COALESCE(CAST("max_amount" AS DOUBLE) / '
                        'NULLIF("total_amount", 0), 0.0), 4)',
                        "max_amount_share",
                    ),
                ),
                units="fraction",
                null_result="0.0 when total_amount is 0",
                rounding="ROUND to 4 decimal places",
                description=(
                    "max_amount_share is max_amount divided by total_amount as a "
                    "fraction, rounded to 4 decimal places; 0.0 when total_amount "
                    "is 0."
                ),
            )
        )
        ops.append(
            MartOp(
                kind=MartOpKind.DERIVE,
                description=f"Label these measures as the {label} measure state.",
                tables=(ratio_rel,),
                columns=final_columns,
                details={
                    "select": (
                        '"entity_key" AS "entity_key", '
                        f"'{label}' AS \"measure_state\", "
                        '"entity_name" AS "entity_name", '
                        '"row_count" AS "row_count", '
                        '"distinct_amount_count" AS "distinct_amount_count", '
                        '"total_amount" AS "total_amount", '
                        '"max_amount" AS "max_amount", '
                        '"max_amount_share" AS "max_amount_share"'
                    ),
                    "name": named_rel,
                },
            )
        )
        branches.append(named_rel)

    ops.extend(
        (
            MartOp(
                kind=MartOpKind.UNION,
                # "Combine" was read as a JOIN of the two summaries, giving
                # one row per entity and dropping every absent row of an
                # entity that also has a present row (dlt__personio, batch10
                # run H, 2026-09-11). The stacking is stated.
                description=(
                    "Stack the present-state summary and the absent-state summary "
                    "into one output list: every present-state row and every "
                    "absent-state row is its own output row, an entity with rows "
                    "in both states appears twice, once per state, and the two "
                    "summaries are never matched to each other."
                ),
                tables=(branches[0], branches[1]),
                columns=final_columns,
                details={"mode": "all", "name": union_rel},
            ),
            MartOp(
                kind=MartOpKind.TIE_BREAK,
                description="Deterministic output order: entity, then measure state.",
                columns=("entity_key", "measure_state"),
            ),
        )
    )

    plan = MartPlan(mart=mart, ops=tuple(ops), notes=notes)
    columns = (
        MartColumn(
            name="entity_key",
            type=e.parent_key_type,
            description=f"Identifier of the {e.parent} row.",
            kind=MartColumnKind.PASSTHROUGH,
        ),
        MartColumn(
            name="measure_state",
            type=ColumnType.TEXT,
            description=(
                (
                    f"'present' for a linked {e.bridge} row whose "
                    f"{e.bridge_amount} has a value; 'absent' when "
                    f"{e.bridge_amount} is missing, including a {e.parent} row "
                    f"with no linked {e.bridge} row. A linked {e.bridge} row "
                    f"whose {e.bridge_amount} has a value belongs only to the "
                    "present state and never to the absent state."
                    if amount_may_be_missing
                    else (
                        f"'present' for a linked {e.bridge} row; 'absent' only "
                        f"for a {e.parent} row with no linked {e.bridge} row. "
                        f"{e.bridge_amount} is required on every real "
                        f"{e.bridge} row."
                    )
                )
            ),
            kind=MartColumnKind.DERIVED,
        ),
        MartColumn(
            name="entity_name",
            type=e.parent_attr_type,
            description=f"{e.parent_attr} of the {e.parent} row, copied unchanged.",
            kind=MartColumnKind.PASSTHROUGH,
        ),
        MartColumn(
            name="row_count",
            type=ColumnType.BIGINT,
            description=(
                f"Number of linked {e.bridge} rows in this entity/state cell; "
                + (
                    # An absent cell with linked NULL-valued rows counts those
                    # rows; only the no-activity placeholder reports zero.
                    f"{_article(e.bridge, capitalized=False)} absent cell "
                    f"holding real {e.bridge} rows whose {e.bridge_amount} is "
                    "missing COUNTS those rows, and only the placeholder cell "
                    f"of {_article(e.parent)} {e.parent} row with no linked "
                    f"{e.bridge} row at all reports 0."
                    if amount_may_be_missing
                    else "0 for a no-activity absent cell."
                )
            ),
            kind=MartColumnKind.AGGREGATED,
        ),
        MartColumn(
            name="distinct_amount_count",
            type=ColumnType.BIGINT,
            description=(
                f"Number of unique "
                + ("non-missing " if amount_may_be_missing else "")
                + f"{e.bridge_amount} values in this cell; each unique "
                + ("non-missing " if amount_may_be_missing else "")
                + "value is counted once, however many rows repeat it; "
                + (
                    # Zero for TWO different cells, and saying only
                    # "no-activity" names one of them.
                    f"0 whenever the cell holds no {e.bridge_amount} value "
                    f"at all — both for {_article(e.parent)} {e.parent} row "
                    f"with no linked {e.bridge} row and for an absent cell "
                    f"whose rows all have a missing {e.bridge_amount}."
                    if amount_may_be_missing
                    else "0 for a no-activity absent cell."
                )
            ),
            kind=MartColumnKind.AGGREGATED,
        ),
        MartColumn(
            name="total_amount",
            type=e.bridge_amount_type,
            description=(
                f"Sum of {e.bridge_amount} in this cell; "
                + (
                    f"0 for a no-activity cell that has no rows at all, "
                    f"and 0 when none of the cell's rows carries an "
                    f"{e.bridge_amount} value."
                    if amount_may_be_missing
                    else "0 for a no-activity absent cell."
                )
            ),
            kind=MartColumnKind.AGGREGATED,
        ),
        MartColumn(
            name="max_amount",
            type=e.bridge_amount_type,
            description=(
                f"Largest {e.bridge_amount} in this cell; "
                + (
                    f"0 for a no-activity cell that has no rows at all, "
                    f"and 0 when none of the cell's rows carries an "
                    f"{e.bridge_amount} value."
                    if amount_may_be_missing
                    else "0 for a no-activity absent cell."
                )
            ),
            kind=MartColumnKind.AGGREGATED,
        ),
        MartColumn(
            name="max_amount_share",
            type=ColumnType.FLOAT,
            description=(
                "max_amount divided by total_amount as a fraction, rounded to 4 "
                "decimal places; 0.0 when total_amount is 0."
            ),
            kind=MartColumnKind.DERIVED,
        ),
    )
    built = BuiltPlan(
        plan=plan,
        columns=columns,
        shape=StarShape(
            mart=mart,
            parent=e.parent,
            parent_keys=(e.parent_key,),
            key_columns=("entity_key", "measure_state"),
            fact=e.bridge,
            fact_link_columns=(e.bridge_parent_fk,),
            has_join=True,
            shape_name="measure_state_distribution",
            join_hops=1,
            distinct_measures=("distinct_amount_count",),
            ratio_measures=("max_amount_share",),
            witnesses=(
                WITNESS_CONTROL,
                WITNESS_CHILDLESS,
                WITNESS_DISTINCT_MEASURE,
            ),
            attack_claims=(
                "dropped_filter",
                "inner_join",
                "no_dedup",
                "no_null_default",
                "wrong_denominator",
            ),
            roles=FactRoles(link_key=e.bridge_key, measure=e.bridge_amount),
            witness_anchor_table=e.parent,
            witness_anchor_key=e.parent_key,
            witness_bridge_table=e.bridge,
            witness_bridge_fk=e.bridge_parent_fk,
        ),
    )
    # This shape assembles its own ops, so it renames the assembled plan.
    built = _rename_built_plan(
        built, _union_shape_names(e, tuple(column.name for column in built.columns))
    )
    plan = built.plan
    op_faults = [
        problem
        for index, op in enumerate(plan.ops)
        for problem in op_problems(
            op, loc=f"mart {mart!r} op[{index}] ({op.kind.value})"
        )
    ]
    if op_faults:
        raise ValueError(f"mart {mart!r}: ill-formed ops — " + "; ".join(op_faults))
    kind_faults = kind_certification_problems(built)
    if kind_faults:
        raise ValueError(
            f"mart {mart!r}: uncertified column kinds — " + "; ".join(kind_faults)
        )
    budget = budget_problems(built)
    if budget:
        raise MartBudgetError(f"mart {mart!r}: " + "; ".join(budget))
    return built


# Resolve the builder's globals only after its definition so importing this
# module first cannot create an import-time cycle with ``mart_plan``.
from elt_taskgen.generation.mart_plan import (  # noqa: E402
    BuiltPlan,
    ChainEvidence,
    ColumnType,
    FactRoles,
    JoinType,
    MartBudgetError,
    MartColumn,
    MartColumnKind,
    MartOp,
    MartOpKind,
    MartPlan,
    ShapeNotSelectable,
    StarShape,
    _article,
    _rename_built_plan,
    _require,
    _union_shape_names,
    _unique,
    budget_problems,
    group_by_op,
    kind_certification_problems,
    op_problems,
    quote,
    ratio_op,
    relation_identifier,
)

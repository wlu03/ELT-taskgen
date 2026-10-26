"""Build the status-cohort-union mart-plan shape."""

from __future__ import annotations

from typing import TYPE_CHECKING

from elt_taskgen.generation.witness import (
    WITNESS_ALL_FAIL,
    WITNESS_CHILDLESS,
    WITNESS_CONTROL,
)

if TYPE_CHECKING:
    from elt_taskgen.generation.mart_plan import BuiltPlan, ChainEvidence

__all__ = ["status_cohort_union"]


def status_cohort_union(
    evidence: ChainEvidence, *, mart: str, notes: str = ""
) -> BuiltPlan:
    """Aggregate disjoint status cohorts independently, then union them.

    The two branches make FILTER and UNION first-class plan structure instead
    of hiding cohort membership inside another conditional aggregate.  A
    declared status domain is required so both branches have source-backed
    values and the dropped-filter attack has a constructed witness.
    """
    e = evidence
    _require(
        e,
        "status_cohort_union",
        "parent",
        "parent_key",
        "parent_attr",
        "bridge",
        "bridge_key",
        "bridge_parent_fk",
        "bridge_status",
        "bridge_status_pass",
        "bridge_status_fail",
        "bridge_amount",
    )

    taken = {e.parent, e.bridge}
    base_rel = _unique("cohort_entities", taken)
    joined_rel = _unique("cohort_rows", taken)
    represented_union_rel = _unique("represented_cohort_union", taken)
    union_rel = _unique("cohort_union", taken)
    final_columns = (
        "entity_key",
        "cohort",
        "entity_name",
        "link_count",
        "distinct_status_count",
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
                "cohort calculation."
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
                f"Bring the linked {e.bridge} rows into each {e.parent} entity "
                "before assigning status cohorts."
            ),
            tables=(base_rel, e.bridge),
            columns=(
                "entity_key",
                "entity_name",
                "link_key",
                "link_status",
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
                    f'{relation_identifier(e.bridge)}.{quote(e.bridge_status)} '
                    'AS "link_status", '
                    f'{relation_identifier(e.bridge)}.{quote(e.bridge_amount)} '
                    'AS "link_amount"'
                ),
                "name": joined_rel,
            },
        ),
    ]

    branches: list[str] = []
    for label, values, predicate in (
        (
            "passing",
            e.bridge_status_pass,
            _in_list("link_status", e.bridge_status_pass),
        ),
        (
            "failing",
            e.bridge_status_fail,
            _in_list("link_status", e.bridge_status_fail),
        ),
        ("no_activity", (), '"link_key" IS NULL'),
    ):
        filtered_rel = _unique(f"{label}_cohort_rows", taken)
        grouped_rel = _unique(f"{label}_cohort_grouped", taken)
        ratio_rel = _unique(f"{label}_cohort_ratio", taken)
        named_rel = _unique(f"{label}_cohort", taken)
        ops.append(
            MartOp(
                kind=MartOpKind.FILTER,
                description=(
                    (
                        f"Keep rows whose {e.bridge_status} belongs to the {label} "
                        f"cohort values {list(values)}."
                    )
                    if values
                    else (
                        # "no linked activity" read as "no cohort-bearing
                        # activity" gave a parent whose rows all lack a status
                        # a placeholder it does not get (the predicate is
                        # link_key IS NULL). Say "no linked row at all" and
                        # name the excluded case (batch10 2026-09-11).
                        f"Keep the placeholder row for a {e.parent} entity with "
                        f"no linked {e.bridge} row at all; a {e.parent} entity "
                        f"that has linked {e.bridge} rows gets no placeholder, "
                        f"even when every one of those rows lacks a "
                        f"{e.bridge_status} value."
                    )
                ),
                tables=(joined_rel,),
                columns=(
                    "entity_key",
                    "entity_name",
                    "link_key",
                    "link_status",
                    "link_amount",
                ),
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
                    ("link_count", "link_count", 'COUNT("link_key")'),
                    (
                        "distinct_status_count",
                        "distinct_status_count",
                        'COUNT(DISTINCT "link_status")',
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
                    # Name each measure's source column and cohort membership
                    # test; avoid undefined shorthand such as “represented.”
                    (
                        f"One row per {e.parent} entity that has at least one "
                        f"linked {e.bridge} row in the {label} cohort, reporting "
                        f"the number of those rows, how many different "
                        f"{e.bridge_status} values occur among them, the total of "
                        f"their {e.bridge_amount}, and their largest "
                        f"{e.bridge_amount}. The total and the largest value read "
                        f"only the rows that carry {_article(e.bridge_amount)} "
                        f"{e.bridge_amount} value; a cohort whose rows all lack one "
                        "reports 0 for both, never empty."
                    )
                    if values
                    else (
                        f"One row per {e.parent} entity with no linked "
                        f"{e.bridge} row at all, reporting 0 rows, 0 different "
                        f"{e.bridge_status} values, a {e.bridge_amount} total of "
                        f"0 and a largest {e.bridge_amount} of 0."
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
                    ('"link_count"', "link_count"),
                    ('"distinct_status_count"', "distinct_status_count"),
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
                description=f"Label these measures as the {label} cohort.",
                tables=(ratio_rel,),
                columns=final_columns,
                details={
                    "select": (
                        '"entity_key" AS "entity_key", '
                        f"'{label}' AS \"cohort\", "
                        '"entity_name" AS "entity_name", '
                        '"link_count" AS "link_count", '
                        '"distinct_status_count" AS "distinct_status_count", '
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
                description=(
                    "Combine the disjoint passing and failing cohort summaries."
                ),
                tables=(branches[0], branches[1]),
                columns=final_columns,
                details={"mode": "all", "name": represented_union_rel},
            ),
            MartOp(
                kind=MartOpKind.UNION,
                description=(
                    "Add the no-activity summaries, so an entity with no linked "
                    "rows is retained as one explicit cohort row."
                ),
                tables=(represented_union_rel, branches[2]),
                columns=final_columns,
                details={"mode": "all", "name": union_rel},
            ),
            MartOp(
                kind=MartOpKind.TIE_BREAK,
                description="Deterministic output order: entity, then cohort.",
                columns=("entity_key", "cohort"),
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
            name="cohort",
            type=ColumnType.TEXT,
            description=(
                f"'passing' for {e.bridge_status} values {list(e.bridge_status_pass)}; "
                f"'failing' for values {list(e.bridge_status_fail)}; 'no_activity' "
                f"when the {e.parent} row has no linked {e.bridge} row. "
                # A linked row with NULL status belongs to no cohort but still
                # prevents `no_activity`, which means no linked row at all.
                f"A linked {e.bridge} row whose {e.bridge_status} has no "
                f"value belongs to no cohort: it is not counted in any cell, "
                f"and it does not make the {e.parent} row 'no_activity'."
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
            name="link_count",
            type=ColumnType.BIGINT,
            description=f"Number of {e.bridge} rows in this entity/cohort cell.",
            kind=MartColumnKind.AGGREGATED,
        ),
        MartColumn(
            name="distinct_status_count",
            type=ColumnType.BIGINT,
            description=(
                f"Number of distinct {e.bridge_status} values represented in this cell."
            ),
            kind=MartColumnKind.AGGREGATED,
        ),
        MartColumn(
            name="total_amount",
            type=e.bridge_amount_type,
            description=(
                f"Sum of {e.bridge_amount} in this cell; 0 for a no-activity "
                f"cell that has no rows at all, and 0 when none of the cell's "
                f"rows carries an {e.bridge_amount} value."
            ),
            kind=MartColumnKind.AGGREGATED,
        ),
        MartColumn(
            name="max_amount",
            type=e.bridge_amount_type,
            description=(
                f"Largest {e.bridge_amount} in this cell; 0 for a no-activity "
                f"cell that has no rows at all, and 0 when none of the "
                f"cell's rows carries an {e.bridge_amount} value."
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
            key_columns=("entity_key", "cohort"),
            fact=e.bridge,
            fact_link_columns=(e.bridge_parent_fk,),
            has_join=True,
            shape_name="status_cohort_union",
            join_hops=1,
            distinct_measures=("distinct_status_count",),
            ratio_measures=("max_amount_share",),
            witnesses=(WITNESS_CONTROL, WITNESS_CHILDLESS, WITNESS_ALL_FAIL),
            attack_claims=(
                "dropped_filter",
                "inner_join",
                "no_dedup",
                "no_null_default",
                "wrong_denominator",
            ),
            roles=FactRoles(
                link_key=e.bridge_key,
                measure=e.bridge_amount,
                predicate_column=e.bridge_status,
                predicate_pass=e.bridge_status_pass,
                predicate_fail=e.bridge_status_fail,
            ),
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
    StarShape,
    _article,
    _in_list,
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

"""Build the aggregate-then-filter mart-plan shape."""

from __future__ import annotations

from elt_taskgen.generation.witness import WITNESS_BELOW_THRESHOLD

__all__ = ["aggregate_then_filter"]


def aggregate_then_filter(
    evidence: ChainEvidence, *, mart: str, notes: str = ""
) -> BuiltPlan:
    """A grouped rollup followed by an inclusive aggregate threshold.

    This is the first compound-pattern builder: it deliberately reuses the
    fully witnessed fan-out rollup and adds a FILTER *after* aggregation.  The
    distinction is semantic, not cosmetic.  A raw-row presence filter retains
    the below-threshold witness, while the correct grouped predicate removes
    it.  The plan therefore exposes HAVING-style placement without teaching the
    compiler a second spelling of GROUP BY.
    """
    base = fan_out_rollup(evidence, mart=mart, notes=notes)
    if not base.plan.ops or base.plan.ops[-1].kind is not MartOpKind.TIE_BREAK:
        raise ValueError(
            f"mart {mart!r}: aggregate_then_filter expected the rollup to end "
            "in a deterministic tie-break"
        )

    body = list(base.plan.ops[:-1])
    if not body or not body[-1].details.get("name"):
        raise ValueError(
            f"mart {mart!r}: aggregate_then_filter cannot identify the grouped "
            "relation to filter"
        )
    current = body[-1].details["name"]
    taken = {
        name
        for op in body
        for name in ((op.details.get("name") or ""), *op.tables)
        if name
    }
    filtered_rel = _unique("mart_having", taken)
    threshold = 2
    mart_columns = tuple(column.name for column in base.columns)
    # The rollup names its own columns after the chain, so the threshold names
    # the count column this mart actually has.
    link_count = base.column_named("link_count")
    body.append(
        MartOp(
            kind=MartOpKind.FILTER,
            description=(
                f"Retain a grouped {evidence.parent} row only when {link_count} "
                f"is at least {threshold}, inclusive. Apply this rule after the "
                "per-parent measures are computed: a parent with one linked row "
                "does not appear, while a parent with exactly two does."
            ),
            tables=(current,),
            columns=mart_columns,
            predicate=f'{quote(link_count)} >= {threshold}',
            details={"name": filtered_rel},
        )
    )
    body.append(base.plan.ops[-1])

    threshold_note = (
        f" This mart emits the column only for parent groups whose {link_count} "
        f"is at least {threshold}; lower-count groups produce no output row."
    )
    columns = tuple(
        column.model_copy(
            update={"description": column.description + threshold_note}
        )
        for column in base.columns
    )
    plan = base.plan.model_copy(
        update={
            "ops": tuple(body),
            "template_id": "aggregate_then_filter",
            "semantic_patterns": (SemanticPattern.AGGREGATE_THEN_FILTER,),
        }
    )
    shape = replace(
        base.shape,
        shape_name="aggregate_then_filter",
        witnesses=base.shape.witnesses + (WITNESS_BELOW_THRESHOLD,),
        # Do not inherit attacks whose only witness is removed by this shape's
        # threshold.  For example, converting the parent LEFT JOIN to INNER no
        # longer changes output once the correct post-aggregate filter already
        # excludes childless parents.  Every claim below is execution-killed by
        # a row that survives (or deliberately crosses) the threshold.
        attack_claims=(
            "no_dedup",
            "dropped_filter",
            "dropped_filter@filter_to_where",
            "wrong_denominator",
            "wrong_grain",
            "custom@wrong_boundary_inclusive",
            "wrong_agg_stage@filter_before_aggregate",
        ),
    )
    built = BuiltPlan(
        plan=plan,
        shape=shape,
        columns=columns,
        renamed_columns=base.renamed_columns,
    )
    faults = [
        problem
        for index, op in enumerate(plan.ops)
        for problem in op_problems(
            op, loc=f"mart {mart!r} op[{index}] ({op.kind.value})"
        )
    ]
    if faults:
        raise ValueError(
            f"mart {mart!r}: aggregate_then_filter emitted ill-formed ops — "
            + "; ".join(faults)
        )
    return built


from dataclasses import replace  # noqa: E402

from elt_taskgen.generation.mart_plan import (  # noqa: E402
    BuiltPlan,
    ChainEvidence,
    MartOp,
    MartOpKind,
    SemanticPattern,
    _unique,
    op_problems,
    quote,
)
from elt_taskgen.generation.shapes.fan_out_rollup import (  # noqa: E402
    fan_out_rollup,
)

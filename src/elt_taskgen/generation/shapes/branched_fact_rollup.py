"""Summarize two fact tables independently at a complete parent grain."""

from __future__ import annotations

__all__ = ["branched_fact_rollup"]


def branch_measures(evidence: BranchedFactCandidate) -> tuple[tuple[tuple[str, str, ColumnType], ...], ...]:
    """Stable output names and aggregate functions, in branch order."""
    taken = set(evidence.grain_key) | {name for name, _ in evidence.attributes}
    out = []
    for index, branch in enumerate(evidence.branches):
        prefix = _slug(branch.table)[:32] or f"fact_{index + 1}"
        if prefix[0].isdigit():
            prefix = "fact_" + prefix
        measures = [(_unique(f"{prefix}_count", taken), "COUNT", ColumnType.BIGINT)]
        if branch.measure:
            for function in ("SUM", "MIN", "MAX"):
                name = _unique(f"{prefix}_{_slug(branch.measure)[:12]}_{function.lower()}", taken)
                ctype = branch.measure_type
                if function == "SUM" and ctype is ColumnType.INTEGER:
                    ctype = ColumnType.BIGINT
                measures.append((name, function, ctype))
        out.append(tuple(measures))
    return tuple(out)


def _plan(
    evidence: BranchedFactCandidate, *, mart: str, notes: str = "",
    variant: str = "", branch_index: int = -1, component: int = -1,
) -> MartPlan:
    """One plan constructor; explicit variants are used only by the attack engine."""
    e = evidence
    raw_join = variant == "join_before_aggregate"
    if variant not in ("", "join_before_aggregate", "missing_composite_component", "sum_distinct"):
        raise ValueError(f"unknown branched mutation {variant!r}")
    if variant in ("missing_composite_component", "sum_distinct"):
        if branch_index not in range(len(e.branches)):
            raise ValueError("branched mutation requires a valid branch index")
    if variant == "missing_composite_component" and (
        len(e.grain_key) < 2 or component not in range(len(e.grain_key))
    ):
        raise ValueError("component-dropping mutation requires a composite key component")
    if variant == "sum_distinct" and not e.branches[branch_index].measure:
        raise ValueError("SUM(DISTINCT) mutation requires a numeric branch")

    used = {e.grain_table, *(b.table for b in e.branches)}
    taken = used | set(e.extra_sources)
    base = _unique("branched_grain", taken)
    keys = e.grain_key
    parent_columns = keys + tuple(name for name, _ in e.attributes)
    group_by = ", ".join(quote(k) for k in keys)
    ops = [MartOp(kind=MartOpKind.SOURCE, tables=(table,),
                  description=_source_op_description(table, used=table in used))
           for table in (e.grain_table, *(b.table for b in e.branches), *e.extra_sources)]
    ops.append(MartOp(
        kind=MartOpKind.DERIVE, tables=(e.grain_table,), columns=parent_columns,
        description=f"Retain every {e.grain_table} row at its complete key ({', '.join(keys)}).",
        details={"name": base, "select": ", ".join(f"{quote(k)} AS {quote(k)}" for k in parent_columns)},
    ))
    measure_groups = branch_measures(e)
    inputs = []
    raw_measures = []
    internal_columns = set(parent_columns) | {name for group in measure_groups for name, _, _ in group}
    for index, (branch, measures) in enumerate(zip(e.branches, measure_groups, strict=True)):
        source = _unique(f"branched_input_{index}", taken)
        present = _unique(f"branch_{index}_present", internal_columns)
        value = _unique(f"branch_{index}_value", internal_columns)
        projection = [f"{quote(c)} AS {quote(p)}" for c, p in zip(branch.child_columns, keys, strict=True)]
        projection.append(f"1 AS {quote(present)}")
        if branch.measure:
            projection.append(f"{quote(branch.measure)} AS {quote(value)}")
        ops.append(MartOp(
            kind=MartOpKind.DERIVE, tables=(branch.table,), columns=keys,
            description=f"Use the complete relationship from {branch.table} to {e.grain_table}.",
            details={"name": source, "select": ", ".join(projection)},
        ))
        expressions = {}
        for name, function, _ in measures:
            argument = quote(present if function == "COUNT" else value)
            if variant == "sum_distinct" and index == branch_index and function == "SUM":
                argument = "DISTINCT " + argument
            expressions[name] = f"{function}({argument})"
        if raw_join:
            carried = (present,) + ((value,) if branch.measure else ())
            raw_measures.extend(expressions.items())
        else:
            summary = _unique(f"branched_summary_{index}", taken)
            ops.append(MartOp(
                kind=MartOpKind.AGGREGATE, tables=(source,),
                columns=keys + tuple(expressions),
                description=f"Compute the {branch.table} measures for each complete parent key.",
                details={"name": summary, "group_by": group_by, **expressions},
            ))
            source, carried = summary, tuple(expressions)
        inputs.append((source, carried))

    current, carried = base, list(parent_columns)
    for index, (source, right_columns) in enumerate(inputs):
        joined = _unique(f"branched_join_{index}", taken)
        on_keys = [key for position, key in enumerate(keys) if not (
            variant == "missing_composite_component" and index == branch_index and position == component
        )]
        predicate = " AND ".join(f"{quote(current)}.{quote(k)} = {quote(source)}.{quote(k)}" for k in on_keys)
        select = [f"{quote(current)}.{quote(c)} AS {quote(c)}" for c in carried]
        select += [f"{quote(source)}.{quote(c)} AS {quote(c)}" for c in right_columns]
        ops.append(MartOp(
            kind=MartOpKind.JOIN, tables=(current, source), columns=keys,
            join_type=JoinType.LEFT, predicate=predicate,
            description=f"Retain all parent rows, including those with no {e.branches[index].table} activity.",
            details={"name": joined, "select": ", ".join(select)},
        ))
        current = joined
        carried.extend(right_columns)
    names = tuple(name for group in measure_groups for name, _, _ in group)
    if raw_join:
        summary = _unique("branched_late_summary", taken)
        ops.append(MartOp(
            kind=MartOpKind.AGGREGATE, tables=(current,), columns=parent_columns + names,
            description="Incorrectly compute measures after joining the raw facts.",
            details={"name": summary, "group_by": ", ".join(quote(k) for k in parent_columns), **dict(raw_measures)},
        ))
        current = summary
    ops.append(MartOp(
        kind=MartOpKind.DERIVE, tables=(current,), columns=parent_columns + names,
        description="Report zero for missing counts, totals, minima, and maxima.",
        details={"name": _unique("branched_final", taken), "select": ", ".join(
            [quote(k) for k in parent_columns] + [f"COALESCE({quote(c)}, 0) AS {quote(c)}" for c in names]
        )},
    ))
    ops.append(MartOp(kind=MartOpKind.TIE_BREAK, columns=keys,
                     description="Order deterministically by the complete parent key."))
    return MartPlan(
        mart=mart, ops=tuple(ops), notes=notes, template_id="branched_fact_rollup",
        semantic_patterns=() if variant else (SemanticPattern.BRANCHED_FACT_ROLLUP,),
    )


def branched_fact_rollup(evidence: BranchedFactCandidate, *, mart: str, notes: str = "") -> BuiltPlan:
    e = evidence
    if (len(e.branches) != 2 or len({b.table for b in e.branches}) != 2
            or e.grain_table in {b.table for b in e.branches}
            or not e.grain_key or len(e.key_types) != len(e.grain_key)
            or not any(b.measure for b in e.branches)
            or any(b.parent_columns != e.grain_key or len(b.child_columns) != len(e.grain_key)
                   for b in e.branches)):
        raise ShapeNotSelectable("branched_fact_rollup needs two facts, a full grain key, and a numeric measure")
    columns = [MartColumn(name=key, type=ctype, kind=MartColumnKind.PASSTHROUGH,
                          description=f"{key} from {e.grain_table}; one row per complete ({', '.join(e.grain_key)}) key.")
               for key, ctype in zip(e.grain_key, e.key_types, strict=True)]
    columns.extend(MartColumn(name=name, type=ctype, kind=MartColumnKind.PASSTHROUGH,
                              description=f"{name} of the {e.grain_table} row, copied unchanged.")
                   for name, ctype in e.attributes)
    groups = branch_measures(e)
    for branch, measures in zip(e.branches, groups, strict=True):
        relationship = " and ".join(f"{branch.table}.{c} equals {e.grain_table}.{p}"
                                    for c, p in zip(branch.child_columns, branch.parent_columns, strict=True))
        for name, function, ctype in measures:
            meaning = {"COUNT": f"Number of {branch.table} source rows",
                       "SUM": f"Sum of {branch.table}.{branch.measure}",
                       "MIN": f"Smallest {branch.table}.{branch.measure}",
                       "MAX": f"Largest {branch.table}.{branch.measure}"}[function]
            columns.append(MartColumn(
                name=name, type=ctype, kind=MartColumnKind.AGGREGATED,
                description=f"{meaning} where {relationship}. Every matching source row contributes, even when values repeat. "
                            "Report 0 when there are no matching rows or no non-missing values.",
            ))
    plan = _plan(e, mart=mart, notes=notes)
    measures = tuple(c.name for c in columns if c.computed)
    built = BuiltPlan(plan=plan, columns=tuple(columns), shape=BranchedFactShape(
        mart=mart, parent=e.grain_table, parent_keys=e.grain_key, key_columns=e.grain_key,
        shape_name="branched_fact_rollup", has_join=True, join_hops=2, branches=e.branches,
        null_capable_measures=measures, fanout_capable_measures=measures,
        witnesses=(WITNESS_BRANCH_CROSS_PRODUCT, WITNESS_CHILDLESS)
                  + ((WITNESS_COMPOSITE_COLLISION,) if len(e.grain_key) > 1 else ()),
        attack_claims=("inner_join", "no_null_default"),
    ))
    problems = budget_problems(built)
    if problems:
        raise MartBudgetError("; ".join(problems))
    if not certifies_branched_plan(plan):
        raise ValueError("branched builder did not preserve independent complete-grain aggregates")
    return built


def certifies_branched_plan(plan: MartPlan) -> bool:
    """Recognize this bounded motif from its bindings, group keys and join ASTs."""
    # Preserve unrelated legacy pattern declarations; this shape owns its certifier.
    if plan.template_id != "branched_fact_rollup":
        return False
    sources = {op.tables[0] for op in plan.ops if op.kind is MartOpKind.SOURCE and len(op.tables) == 1}
    derives = [op for op in plan.ops if op.kind is MartOpKind.DERIVE]
    aggregates = [op for op in plan.ops if op.kind is MartOpKind.AGGREGATE]
    joins = [op for op in plan.ops if op.kind is MartOpKind.JOIN]
    if len(sources) < 3 or len(aggregates) != 2 or len(joins) != 2 or len(derives) != 4:
        return False
    base = derives[0]
    keys = tuple(plan.ops[-1].columns) if plan.ops[-1].kind is MartOpKind.TIE_BREAK else ()
    if not keys or len(base.tables) != 1 or base.tables[0] not in sources or not base.details.get("name"):
        return False
    bindings = {}
    summaries = {}
    for op in plan.ops:
        if op.kind is MartOpKind.AGGREGATE:
            if len(op.tables) != 1 or not op.details.get("name"):
                return False
            upstream = bindings.get(op.tables[0])
            if (upstream is None or upstream.kind is not MartOpKind.DERIVE
                    or len(upstream.tables) != 1 or upstream.tables[0] not in sources - set(base.tables)):
                return False
            try:
                expressions = parse_one("SELECT " + op.details["group_by"]).expressions
            except (ParseError, KeyError):
                return False
            if any(not isinstance(node, exp.Column) for node in expressions) or tuple(node.name for node in expressions) != keys:
                return False
            projections = _select_projections(upstream) or []
            if any(not isinstance(node, exp.Column) for name, node in projections if name in keys):
                return False
            if not set(keys) <= {name for name, _ in projections}:
                return False
            summaries[op.details.get("name")] = upstream.tables[0]
        if op.details.get("name"):
            bindings[op.details["name"]] = op
    if len(set(summaries.values())) != 2:
        return False
    current = base.details["name"]
    consumed = set()
    for join in joins:
        if len(join.tables) != 2 or not join.details.get("name"):
            return False
        if join.tables[0] != current or join.tables[1] not in summaries or join.join_type is not JoinType.LEFT:
            return False
        predicate = _parse_expression(join.predicate)
        terms = list(predicate.flatten()) if isinstance(predicate, exp.And) else [predicate]
        if len(terms) != len(keys):
            return False
        actual = set()
        for term in terms:
            if not isinstance(term, exp.EQ) or not all(isinstance(n, exp.Column) for n in (term.this, term.expression)):
                return False
            actual.add(frozenset(((term.this.table, term.this.name), (term.expression.table, term.expression.name))))
        expected = {frozenset(((current, k), (join.tables[1], k))) for k in keys}
        if actual != expected:
            return False
        consumed.add(join.tables[1])
        current = join.details["name"]
    return consumed == set(summaries) and derives[-1].tables == (current,)


from sqlglot import exp, parse_one  # noqa: E402
from sqlglot.errors import ParseError  # noqa: E402

from elt_taskgen.generation.plan_ops import (
    BranchedFactCandidate,
    BranchedFactShape,
    BuiltPlan,
    _slug,
    _source_op_description,
    _unique,
    budget_problems,
    quote,
)
from elt_taskgen.generation.plan_validation import (
    MartBudgetError,
    ShapeNotSelectable,
    _parse_expression,
    _select_projections,
)
from elt_taskgen.generation.witness import WITNESS_CHILDLESS  # noqa: E402
from elt_taskgen.generation.witness.branched import (  # noqa: E402
    WITNESS_BRANCH_CROSS_PRODUCT, WITNESS_COMPOSITE_COLLISION,
)
from elt_taskgen.models import (  # noqa: E402
    ColumnType, JoinType, MartColumn, MartColumnKind, MartOp, MartOpKind, MartPlan, SemanticPattern,
)

"""Structural certificates follow live data dependencies, not template tags."""
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from elt_taskgen.generation.compositions import _reachable
from elt_taskgen.generation.mart_plan import _aggregate_dependent_aliases, AGGREGATE_FAMILY_KINDS, measure_items
from elt_taskgen.models import JoinType, MartOpKind


def certifies_aggregate_extrema(plan):
    live, final = _reachable(plan)
    visible = _aggregate_dependent_aliases(plan)
    bindings = {o.details.get("name"): o for o in plan.ops}
    try:
        final_names = {c.name for c in parse_one("SELECT " + final).find_all(exp.Column)}
        for i, op in enumerate(plan.ops):
            if op.kind is not MartOpKind.EXTREMA or op.details.get("name") not in live:
                continue
            upstream = bindings.get(op.tables[0]) if len(op.tables) == 1 else None
            if upstream is None or upstream.kind is not MartOpKind.FILTER:
                continue
            referenced = {c.name for c in parse_one(upstream.predicate).find_all(exp.Column)}
            if not referenced & visible[plan.ops.index(upstream)]:
                continue
            order = parse_one("SELECT * FROM t ORDER BY " + op.details["order_by"])
            projections = parse_one("SELECT " + op.details["select"]).expressions
            if ({c.name for c in order.find_all(exp.Column)} & visible[i]
                    and any(p.alias in final_names for p in projections if isinstance(p, exp.Alias))):
                return True
    except (ParseError, KeyError, ValueError):
        return False
    return False


def certifies_entity_baseline(plan):
    live, final = _reachable(plan)
    bindings = {o.details.get("name"): o for o in plan.ops}
    try:
        final_ast = parse_one("SELECT " + final)
        for mean in plan.ops:
            if mean.kind not in AGGREGATE_FAMILY_KINDS or mean.details.get("name") not in live:
                continue
            source = bindings.get(mean.tables[0]) if len(mean.tables) == 1 else None
            if source is None or source.kind is not MartOpKind.DERIVE:
                continue
            projections = parse_one("SELECT " + source.details.get("select", "")).expressions
            groups = parse_one("SELECT " + mean.details["group_by"]).expressions
            constants = {p.alias for p in projections if isinstance(p, exp.Alias) and isinstance(p.this, exp.Literal)}
            filled = {p.alias for p in projections if isinstance(p, exp.Alias) and p.find(exp.Coalesce)}
            if len(groups) != 1 or groups[0].name not in constants:
                continue
            averages = {alias for alias, value in measure_items(mean)
                        if isinstance(parse_one(value), exp.Avg)
                        and any(c.name in filled for c in parse_one(value).find_all(exp.Column))}
            parent_join = bindings.get(source.tables[0]) if len(source.tables) == 1 else None
            if not averages or parent_join is None or parent_join.kind is not MartOpKind.JOIN or parent_join.join_type is not JoinType.LEFT:
                continue
            totals = bindings.get(parent_join.tables[1]) if len(parent_join.tables) == 2 else None
            if totals is None or totals.kind not in AGGREGATE_FAMILY_KINDS:
                continue
            joined = any(o.kind is MartOpKind.JOIN and o.join_type is JoinType.CROSS
                         and o.tables == (source.details["name"], mean.details["name"])
                         and o.details.get("name") in live for o in plan.ops)
            compared = any(averages & {c.name for c in node.find_all(exp.Column)}
                           and filled & {c.name for c in node.find_all(exp.Column)}
                           for node in final_ast.find_all(exp.GT))
            if joined and compared:
                return True
    except (ParseError, KeyError, ValueError):
        return False
    return False


def certifies_calendar_streak(plan):
    """A live two-year lag is compared to calendar distance, after year grouping.

    Conservative certificate for this three-year recipe; a generic LAG or a
    difference in observed row numbers does not establish calendar continuity.
    """
    live, final = _reachable(plan)
    bindings = {o.details.get("name"): o for o in plan.ops}
    try:
        final_names = {c.name for c in parse_one("SELECT " + final).find_all(exp.Column)}
        for window in plan.ops:
            if window.kind is not MartOpKind.WINDOW or window.details.get("name") not in live:
                continue
            projections = parse_one("SELECT " + window.details["select"]).expressions
            for projection in projections:
                lag = projection.find(exp.Lag)
                if lag is None or lag.args.get("offset") != exp.Literal.number(2):
                    continue
                year = lag.this.name
                ordering = parse_one("SELECT * FROM t ORDER BY " + window.details["order_by"]).args["order"].expressions
                if len(ordering) != 1 or ordering[0].this.name != year or ordering[0].args.get("desc"):
                    continue
                eligible = bindings.get(window.tables[0]) if len(window.tables) == 1 else None
                if eligible is None or eligible.kind is not MartOpKind.FILTER:
                    continue
                grouped = bindings.get(eligible.tables[0]) if len(eligible.tables) == 1 else None
                if grouped is None or grouped.kind not in AGGREGATE_FAMILY_KINDS:
                    continue
                groups = {c.name for c in parse_one("SELECT " + grouped.details["group_by"]).find_all(exp.Column)}
                partition = {c.name for c in parse_one("SELECT " + window.details["partition_by"]).find_all(exp.Column)}
                if groups != partition | {year}:
                    continue
                source = bindings.get(grouped.tables[0]) if len(grouped.tables) == 1 else None
                if source and source.kind is MartOpKind.FILTER:
                    source = bindings.get(source.tables[0]) if len(source.tables) == 1 else None
                if source is None:
                    continue
                derived = parse_one("SELECT " + source.details.get("select", "")).expressions
                if not any(p.alias == year and isinstance(p.this, exp.Extract)
                           and p.this.this.name.upper() == "YEAR" for p in derived):
                    continue
                expected = parse_one(f"{year} - {projection.alias} = 2")
                for op in plan.ops:
                    if op.kind is MartOpKind.FILTER and op.tables == (window.details["name"],) and op.details.get("name") in live:
                        if parse_one(op.predicate) == expected:
                            # A joined but unused streak branch is not evidence.
                            for summary in plan.ops:
                                if (summary.kind in AGGREGATE_FAMILY_KINDS
                                        and summary.tables == (op.details["name"],)
                                        and summary.details.get("name") in live
                                        and final_names & {alias for alias, _ in measure_items(summary)}):
                                    return True
    except (ParseError, KeyError, ValueError, AttributeError):
        return False
    return False

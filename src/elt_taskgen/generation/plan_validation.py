"""SQL-aware validation and semantic certification of declarative mart plans."""

from __future__ import annotations
import re
from collections.abc import Callable, Sequence

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from elt_taskgen.models import (
    AttackKind,
    MartColumn,
    MartColumnKind,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    SemanticPattern,
    TaskIR,
)

RESERVED_AGGREGATE_DETAIL_KEYS = frozenset(
    {
        "name", "select", "mode", "sql", "group_by", "function", "expression",
        "description",
        "partition_by", "order_by", "tie_break", "units", "null_result",
        "rounding", "domain", "boundary",
    }
)

AGGREGATE_FAMILY_KINDS: frozenset[MartOpKind] = frozenset(
    {MartOpKind.AGGREGATE, MartOpKind.FILTERED_AGGREGATE, MartOpKind.DISTINCT}
)

PROJECTION_KINDS: frozenset[MartOpKind] = frozenset(
    {MartOpKind.DERIVE, MartOpKind.WINDOW, MartOpKind.CONDITIONAL, MartOpKind.RATIO}
)

OP_COLUMN_KIND: dict[MartOpKind, MartColumnKind] = {
    MartOpKind.AGGREGATE: MartColumnKind.AGGREGATED,
    MartOpKind.FILTERED_AGGREGATE: MartColumnKind.AGGREGATED,
    MartOpKind.DISTINCT: MartColumnKind.AGGREGATED,
    MartOpKind.EXTREMA: MartColumnKind.RANKED,
    MartOpKind.WINDOW: MartColumnKind.RANKED,
    MartOpKind.CONDITIONAL: MartColumnKind.CATEGORICAL,
    MartOpKind.RATIO: MartColumnKind.DERIVED,
}

_COLUMN_KIND_PRECEDENCE: tuple[MartColumnKind, ...] = (
    MartColumnKind.RANKED,
    MartColumnKind.AGGREGATED,
    MartColumnKind.CATEGORICAL,
    MartColumnKind.DERIVED,
    MartColumnKind.PASSTHROUGH,
)

NAVIGATION_WINDOW_FUNCTIONS: frozenset[str] = frozenset(
    {"Lag", "Lead", "RowNumber", "Rank", "DenseRank", "PercentRank", "CumeDist", "NTile"}
)

_DIRECTION_RE = re.compile(r"\b(ASC|DESC)\b", re.IGNORECASE)

_NULL_ORDER_RE = re.compile(r"\bNULLS\s+(FIRST|LAST)\b", re.IGNORECASE)

_DENOMINATOR_HINTS = ("denominator", "ratio", "rate", "percent", "share", "avg", "average")

_NULL_DEFAULT_HINTS = ("coalesce", "null", "default", "ifnull", "nvl")

_PLAN_LEVEL_KINDS = (
    AttackKind.CONSTANTS,
    AttackKind.KEYS_ONLY,
    AttackKind.NO_OP,
    AttackKind.SKIP_EXTRACTION,
)


class ShapeNotSelectable(ValueError):
    """The schema EVIDENCE does not fund this shape — decline, do not invent.

    Trial selection builds every shape and keeps the ones that build, so this is
    how a shape says "not on this schema". Any OTHER ValueError out of a builder
    is a BUILDER BUG and must propagate, never be caught as a decline.
    """


class MartBudgetError(ValueError):
    """The built mart is under the anchor's measured column minima
    (`budget_problems`) — the second thing the trial protocol may skip."""


def _op_text(op: MartOp) -> str:
    parts = [op.description, op.predicate]
    for k in sorted(op.details):
        parts.append(k)
        parts.append(op.details[k])
    return " ".join(parts).lower()


def _parse_expression(text: str) -> exp.Expression | None:
    try:
        return parse_one(text, read="duckdb")
    except (ParseError, ValueError):
        return None


def _parse_select_list(select: str) -> exp.Select | None:
    parsed = _parse_expression(f"SELECT {select}")
    return parsed if isinstance(parsed, exp.Select) else None


def _aggregates(node: exp.Expression) -> list[exp.AggFunc]:
    found = [n for n in node.find_all(exp.AggFunc)]
    if isinstance(node, exp.AggFunc):
        found.append(node)
    return list({id(n): n for n in found}.values())


def is_filtered_aggregate_expr(text: str) -> bool:
    """Does this measure filter INSIDE the aggregate (``SUM(CASE WHEN ...)``)?

    Placement is the construct: a WHERE drops the childless parent's zero row.
    """
    node = _parse_expression(text)
    if node is None:
        return False
    return any(agg.find(exp.Case) is not None for agg in _aggregates(node))


def is_distinct_aggregate_expr(text: str) -> bool:
    """Is this measure a COUNT(DISTINCT ...)-style fan-out-sensitive count?"""
    node = _parse_expression(text)
    if node is None:
        return False
    return any(agg.find(exp.Distinct) is not None for agg in _aggregates(node))


def is_aggregate_expr(text: str) -> bool:
    node = _parse_expression(text)
    return node is not None and bool(_aggregates(node))

_NON_NULL_PRESERVING_BINARY = (
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    exp.Pow,
)

_NON_NULL_PRESERVING_UNARY = (exp.Alias, exp.Cast, exp.Neg, exp.Paren)

_NULLABLE_INPUT_AGGREGATES = (exp.Sum, exp.Avg, exp.Min, exp.Max)


def _expression_proven_non_null(
    node: exp.Expression, *, non_empty_group: bool
) -> bool:
    """Whether SQL syntax alone proves ``node`` cannot return NULL.

    This is deliberately a small proof system, not an optimistic classifier.
    Unknown functions and source columns return False.  In particular, an
    ``ELSE 0`` proves only the ELSE arm of a CASE: it says nothing about a
    nullable THEN arm.  A non-empty aggregate is non-NULL only when every row
    is syntactically guaranteed to feed it a non-NULL value.
    """
    if isinstance(node, (exp.Literal, exp.Boolean)):
        return True
    if isinstance(node, exp.Null):
        return False
    if isinstance(node, exp.Count):
        # COUNT and COUNT(DISTINCT ...) return 0, not NULL, even for no rows.
        return True
    if isinstance(node, exp.TryCast):
        # TRY_CAST is a Cast subclass, but conversion failure returns NULL.
        return False
    if isinstance(node, exp.Filter):
        # FILTER can remove every row. COUNT remains 0; value aggregates then
        # have no input and are nullable regardless of their argument.
        return isinstance(node.this, exp.Count)
    if isinstance(node, exp.Coalesce):
        # If any fallback is itself guaranteed non-NULL, COALESCE is too.
        return any(
            _expression_proven_non_null(child, non_empty_group=non_empty_group)
            for child in node.iter_expressions()
        )
    if isinstance(node, exp.Case):
        default = node.args.get("default")
        branches = [item.args.get("true") for item in node.args.get("ifs", ())]
        results = [default, *branches]
        return bool(default) and all(
            isinstance(result, exp.Expression)
            and _expression_proven_non_null(
                result, non_empty_group=non_empty_group
            )
            for result in results
        )
    if isinstance(node, _NULLABLE_INPUT_AGGREGATES):
        argument = node.this
        if isinstance(argument, exp.Distinct):
            arguments = list(argument.iter_expressions())
            argument_non_null = bool(arguments) and all(
                _expression_proven_non_null(child, non_empty_group=False)
                for child in arguments
            )
        else:
            argument_non_null = isinstance(argument, exp.Expression) and (
                _expression_proven_non_null(argument, non_empty_group=False)
            )
        return non_empty_group and argument_non_null
    if isinstance(node, _NON_NULL_PRESERVING_UNARY):
        return isinstance(node.this, exp.Expression) and _expression_proven_non_null(
            node.this, non_empty_group=non_empty_group
        )
    if isinstance(node, _NON_NULL_PRESERVING_BINARY):
        left = node.this
        right = node.expression
        return (
            isinstance(left, exp.Expression)
            and isinstance(right, exp.Expression)
            and _expression_proven_non_null(
                left, non_empty_group=non_empty_group
            )
            and _expression_proven_non_null(
                right, non_empty_group=non_empty_group
            )
        )
    return False


def _aggregate_can_be_null(measure: "Measure") -> bool:
    """Return whether a measure can be null for a nonempty group.

    Return false only when syntax proves a non-null result: COUNT, an effective
    final default, or an aggregate with a non-null argument on every branch.
    Unknown expressions are nullable. ``input_nullable=False`` is insufficient
    because left joins and CASE branches can introduce nulls.
    """
    if measure.null_default is not None:
        default = _parse_expression(measure.null_default)
        if default is not None and _expression_proven_non_null(
            default, non_empty_group=False
        ):
            return False
    expression = _parse_expression(measure.expr)
    if expression is None:
        return True
    return not _expression_proven_non_null(expression, non_empty_group=True)


def _zero_substituted_inputs(measure: "Measure") -> bool:
    """Does this aggregate count a MISSING input value as 0?

    True when a COALESCE/IFNULL/NVL to the literal 0 sits INSIDE the aggregate
    (per value), so a group whose values are all missing reports 0 rather than
    an empty result. A default wrapped around the whole aggregate is a
    different promise (`null_default`) and is stated elsewhere.
    """
    expression = _parse_expression(measure.expr)
    if expression is None:
        return False
    for aggregate in expression.find_all(exp.AggFunc):
        for guard in aggregate.find_all(exp.Coalesce):
            fallbacks = [guard.this, *guard.expressions]
            last = fallbacks[-1] if fallbacks else None
            if (
                isinstance(last, exp.Literal)
                and last.is_number
                and float(last.name) == 0.0
            ):
                return True
    return False


def is_constant_divisor_expr(text: str) -> bool:
    """Does this measure divide by a LITERAL constant (a unit conversion)?

    Such a Div hides inside an AGGREGATE-classified expression, invisible to the
    ratio classification, yet `wrong_denominator` still mutates it. Only a
    constant divisor counts — a column denominator is a genuine ratio.
    """
    node = _parse_expression(text)
    if node is None:
        return False
    return any(
        isinstance(div.args.get("expression"), exp.Literal)
        for div in node.find_all(exp.Div)
    )


def measure_items(op: MartOp) -> list[tuple[str, str]]:
    """The ``alias -> aggregate expression`` measures of an aggregate-family op,
    sorted so the compiled SQL is deterministic regardless of dict order."""
    return sorted(
        (alias, expr)
        for alias, expr in op.details.items()
        if alias not in RESERVED_AGGREGATE_DETAIL_KEYS
    )


def _order_terms(order_by: str) -> list[exp.Expression] | None:
    parsed = _parse_expression(f"SELECT 1 ORDER BY {order_by}")
    if not isinstance(parsed, exp.Select):
        return None
    order = parsed.args.get("order")
    return list(order.expressions) if order is not None else None


def _order_segments(order_by: str) -> list[str] | None:
    """The AUTHORED text of each ORDER BY term, split on top-level commas.

    Needed because a sqlglot round-trip cannot report stated null placement (see
    `_NULL_ORDER_RE`). Returns None when the split is ambiguous — unclosed
    quotes or parens — so the caller fails closed rather than guessing.
    """
    segments: list[str] = []
    depth = 0
    quote_char = ""
    current: list[str] = []
    for ch in order_by:
        if quote_char:
            current.append(ch)
            if ch == quote_char:
                quote_char = ""
            continue
        if ch in "\"'`":
            quote_char = ch
            current.append(ch)
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
            if depth < 0:
                return None
        if ch == "," and depth == 0:
            segments.append("".join(current))
            current = []
            continue
        current.append(ch)
    if quote_char or depth != 0:
        return None
    segments.append("".join(current))
    stripped = [s.strip() for s in segments]
    return stripped if all(stripped) else None


def guard_conditions(expr: str) -> list[str]:
    """The WHEN conditions filtering INSIDE this measure's aggregates.

    Empty for a measure that aggregates every row of its group, which makes this
    the test for "is this measure governed by the op's predicate?".
    """
    node = _parse_expression(expr)
    if node is None:
        return []
    out: list[str] = []
    for agg in _aggregates(node):
        for case in agg.find_all(exp.Case):
            for when in case.args.get("ifs") or ():
                if when.this is None:
                    continue
                text = when.this.sql(dialect="duckdb")
                if text and text not in out:
                    out.append(text)
    return out


def guard_literals(op: MartOp) -> list[tuple[str, str, str]]:
    """(measure alias, column name, string literal) the op's guards select on.

    The only statement of what the plan needs the DATA to contain: a guard whose
    literal the generator never emits is a dead measure. Only EQUALITY-shaped
    guards are reported (`=`, `IN`, negations); an inequality or LIKE ranges over
    unenumerable values, and this gate refuses to guess.
    """
    out: list[tuple[str, str, str]] = []
    for alias, expr in measure_items(op):
        node = _parse_expression(expr)
        if node is None:
            continue
        for agg in _aggregates(node):
            for case in agg.find_all(exp.Case):
                for when in case.args.get("ifs") or ():
                    condition = when.this
                    if condition is None:
                        continue
                    for cmp_node in list(condition.find_all(exp.EQ, exp.NEQ, exp.In)) + (
                        [condition] if isinstance(condition, (exp.EQ, exp.NEQ, exp.In)) else []
                    ):
                        column = cmp_node.this
                        if not isinstance(column, exp.Column):
                            continue
                        others = (
                            list(cmp_node.expressions)
                            if isinstance(cmp_node, exp.In)
                            else [cmp_node.expression]
                        )
                        for other in others:
                            if isinstance(other, exp.Literal) and other.is_string:
                                pair = (alias, column.name, other.this)
                                if pair not in out:
                                    out.append(pair)
    return out


def predicate_governs(predicate: str, column: str) -> bool:
    """Does `predicate` NAME `column` — i.e. does it say it governs that column?

    Whole-word, case-sensitive, quote-tolerant: a substring test would let
    `count` match `tied_count` and turn the gate into a rubber stamp.
    """
    if not predicate or not column:
        return False
    return re.search(rf"(?<![\w.]){re.escape(column)}(?![\w])", predicate) is not None


def _predicate_is_scoped(op: MartOp) -> bool:
    """Does this op's predicate say WHICH of the op's columns it governs?

    Necessary condition only (the alias -> mart-column map is not recoverable
    from a MartOp): a scoped predicate names at least one column the op emits.
    """
    return any(predicate_governs(op.predicate, c) for c in op.columns)


def _aggregate_family_problems(op: MartOp, loc: str) -> list[str]:
    problems: list[str] = []
    measures = measure_items(op)
    group_by = op.details.get("group_by", "").strip()
    if op.kind is MartOpKind.AGGREGATE and not group_by and not measures:
        # Legacy descriptive-only aggregate op from a plan shipping its own
        # ReferenceSolution; `compile_plan_sql` still refuses it. New kinds are
        # never let through this door.
        return []
    if not group_by:
        problems.append(f"{loc}: aggregate op needs details['group_by']")
    if not measures:
        problems.append(
            f"{loc}: aggregate op needs at least one 'alias: expression' measure"
        )
    filtered = [a for a, e in measures if is_filtered_aggregate_expr(e)]
    distinct = [a for a, e in measures if is_distinct_aggregate_expr(e)]
    for alias, expr in measures:
        if not is_aggregate_expr(expr):
            problems.append(
                f"{loc}: measure {alias!r} = {expr!r} contains no aggregate function"
            )
    if op.kind is MartOpKind.DISTINCT:
        if not distinct:
            problems.append(
                f"{loc}: a 'distinct' op needs at least one COUNT(DISTINCT ...) measure"
            )
        if filtered:
            problems.append(
                f"{loc}: measures {filtered} filter inside the aggregate, so the op "
                "kind must be 'filtered_aggregate'"
            )
    if op.kind is MartOpKind.FILTERED_AGGREGATE:
        if not filtered:
            problems.append(
                f"{loc}: a 'filtered_aggregate' op needs at least one measure whose "
                "predicate lives INSIDE the aggregate (SUM(CASE WHEN p THEN x ELSE 0 END))"
            )
        if not op.predicate:
            problems.append(
                f"{loc}: a 'filtered_aggregate' op must state its predicate on "
                "op.predicate (the solver has to be told which rows count)"
            )
        elif filtered and len(filtered) != len(measures) and not _predicate_is_scoped(op):
            # compile_plan_sql never emits op.predicate for an aggregate-family
            # op, so an op-wide-sounding predicate over unguarded measures is
            # documentation the SQL does not implement. The rule is SCOPE, not
            # universality: a WHERE would delete the childless group.
            problems.append(
                f"{loc}: measures {sorted(set(a for a, _ in measures) - set(filtered))} "
                f"are NOT guarded by the op predicate {op.predicate!r}, which names "
                "none of the op's own columns; a filtered_aggregate whose predicate "
                "governs only SOME of its measures must SCOPE the predicate to the "
                "measures it governs (name them), or a reader of the plan and a "
                "reader of the SQL disagree on every unguarded measure"
            )
    return problems


def _extrema_problems(op: MartOp, loc: str) -> list[str]:
    problems: list[str] = []
    select = op.details.get("select", "").strip()
    partition_by = op.details.get("partition_by", "").strip()
    order_by = op.details.get("order_by", "").strip()
    tie_break = op.details.get("tie_break", "").strip()
    if not select:
        problems.append(
            f"{loc}: extrema op needs an explicit details['select'] "
            "(the projected ARGMAX attributes; SELECT * is ambiguous)"
        )
    else:
        parsed = _parse_select_list(select)
        if parsed is None:
            problems.append(
                f"{loc}: extrema details['select'] does not parse as a select list"
            )
        elif list(parsed.find_all(exp.AggFunc)):
            # ARGMAX, not MAX: an aggregate here means the plan kept the
            # extremal VALUE and threw away the row it came from.
            problems.append(
                f"{loc}: extrema op projects an aggregate; an extremum projects a "
                "NON-aggregate attribute OF the extremal row (argmax, not MAX)"
            )
    if not partition_by:
        problems.append(
            f"{loc}: extrema op needs details['partition_by'] (without it the "
            "extremum is global, not per-group)"
        )
    if not order_by:
        problems.append(f"{loc}: extrema op needs details['order_by']")
        return problems
    terms = _order_terms(order_by)
    if terms is None:
        problems.append(f"{loc}: extrema details['order_by']={order_by!r} does not parse")
        return problems
    if len(terms) < 2:
        problems.append(
            f"{loc}: extrema details['order_by']={order_by!r} has {len(terms)} term(s); "
            "a measure alone does not break ties, so the order is not total"
        )
    segments = _order_segments(order_by)
    if segments is None or len(segments) != len(terms):
        problems.append(
            f"{loc}: extrema details['order_by']={order_by!r} could not be split "
            f"into one authored segment per parsed term; the null-placement rule "
            "below cannot be checked against it"
        )
        segments = []
    for term, authored in zip(terms, segments):
        rendered = term.sql(dialect="duckdb")
        if not _DIRECTION_RE.search(rendered):
            problems.append(
                f"{loc}: extrema order term {rendered!r} has no "
                "explicit ASC/DESC direction"
            )
        # Every term must state NULLS FIRST/LAST: otherwise the winner is
        # inherited from the reader's `default_null_order` session setting and
        # the answer is not a function of the input.
        if not _NULL_ORDER_RE.search(authored):
            problems.append(
                f"{loc}: extrema order term {authored!r} has no explicit "
                "NULLS FIRST/NULLS LAST; the winner would then depend on the "
                "reader's `default_null_order` session setting rather than on "
                "the input"
            )
    if not tie_break:
        problems.append(
            f"{loc}: extrema op must name its tie-break column in "
            "details['tie_break'] (it has to be quotable in the shipped prose)"
        )
    elif tie_break.lower() not in order_by.lower():
        problems.append(
            f"{loc}: declared tie-break {tie_break!r} does not appear in "
            f"details['order_by']={order_by!r}"
        )
    return problems


def _window_problems(op: MartOp, loc: str) -> list[str]:
    problems: list[str] = []
    select = op.details.get("select", "").strip()
    if not select:
        problems.append(f"{loc}: window op needs an explicit details['select']")
        return problems
    parsed = _parse_select_list(select)
    if parsed is None:
        problems.append(f"{loc}: window details['select'] does not parse as a select list")
        return problems
    windows = list(parsed.find_all(exp.Window))
    if not windows:
        problems.append(
            f"{loc}: window op declares no OVER() expression (use 'derive' for a "
            "plain projection)"
        )
    for window in windows:
        fn = type(window.this).__name__
        rendered = window.sql(dialect="duckdb")
        if not window.args.get("order"):
            problems.append(
                f"{loc}: window expression {rendered!r} has no ORDER BY inside OVER(), "
                "so its result depends on physical row order"
            )
        if fn not in NAVIGATION_WINDOW_FUNCTIONS and window.args.get("spec") is None:
            problems.append(
                f"{loc}: window expression {rendered!r} has no explicit frame; the "
                "RANGE default lumps together every row with an equal ORDER BY key, "
                "so a running total is wrong exactly on ties"
            )
    return problems


def _conditional_problems(op: MartOp, loc: str) -> list[str]:
    problems: list[str] = []
    select = op.details.get("select", "").strip()
    if not select:
        problems.append(f"{loc}: conditional op needs an explicit details['select']")
        return problems
    parsed = _parse_select_list(select)
    if parsed is None:
        problems.append(
            f"{loc}: conditional details['select'] does not parse as a select list"
        )
        return problems
    cases = list(parsed.find_all(exp.Case))
    if not cases:
        problems.append(f"{loc}: conditional op declares no CASE expression")
    for case in cases:
        if case.args.get("default") is None:
            problems.append(
                f"{loc}: CASE {case.sql(dialect='duckdb')!r} has no ELSE branch; an "
                "unmapped value would silently become NULL where the spec demands "
                "a value"
            )
    return problems


def _ratio_problems(op: MartOp, loc: str) -> list[str]:
    problems: list[str] = []
    select = op.details.get("select", "").strip()
    if not select:
        problems.append(f"{loc}: ratio op needs an explicit details['select']")
        return problems
    units = op.details.get("units", "").strip().lower()
    if units not in ("fraction", "percent"):
        problems.append(
            f"{loc}: ratio op must declare details['units'] as 'fraction' or "
            "'percent' — the ELT-Bench audit traced 178 values scoring 0% to a "
            "silent 100x scale mismatch"
        )
    if not op.details.get("null_result", "").strip():
        problems.append(
            f"{loc}: ratio op must declare details['null_result'] (what the column "
            "is when the denominator is 0 or NULL)"
        )
    if not op.details.get("rounding", "").strip():
        problems.append(f"{loc}: ratio op must declare details['rounding']")
    parsed = _parse_select_list(select)
    if parsed is None:
        problems.append(f"{loc}: ratio details['select'] does not parse as a select list")
        return problems
    divisions = list(parsed.find_all(exp.Div))
    if not divisions:
        problems.append(f"{loc}: ratio op declares no division")
    for div in divisions:
        rendered = div.sql(dialect="duckdb")
        denominator = div.expression
        if not (
            isinstance(denominator, exp.Nullif) or denominator.find(exp.Nullif) is not None
        ):
            problems.append(
                f"{loc}: division {rendered!r} has no NULLIF guard on its denominator"
            )
        if div.find(exp.Cast) is None:
            problems.append(
                f"{loc}: division {rendered!r} has no CAST; both operands are integer "
                "measures and integer division truncates the column to 0"
            )
        ancestor = div.parent
        rounded = False
        while ancestor is not None:
            if isinstance(ancestor, exp.Round):
                rounded = True
                break
            ancestor = ancestor.parent
        if not rounded:
            problems.append(
                f"{loc}: division {rendered!r} is not wrapped in ROUND(); the shipped "
                "precision must be pinned, not left to the engine"
            )
    return problems


def label_problems(op: MartOp, *, loc: str = "op") -> list[str]:
    """Is the op's KIND the honest label of the strongest construct it carries?

    A CONSTRUCTION-time gate only, deliberately NOT enforced by `validate_plan`
    or the compiler: legacy plans mislabel, and re-labelling them would rewrite
    frozen task hashes. Routing reads the measure AST, not the label.
    """
    if op.kind not in AGGREGATE_FAMILY_KINDS:
        return []
    measures = measure_items(op)
    filtered = [a for a, e in measures if is_filtered_aggregate_expr(e)]
    distinct = [a for a, e in measures if is_distinct_aggregate_expr(e)]
    if op.kind is MartOpKind.AGGREGATE and (filtered or distinct):
        return [
            f"{loc}: measures {sorted(set(filtered) | set(distinct))} use "
            "CASE-in-aggregate/DISTINCT, so the op kind must be "
            "'filtered_aggregate' (or 'distinct'), not 'aggregate'"
        ]
    if op.kind is MartOpKind.DISTINCT and filtered:
        return [
            f"{loc}: measures {filtered} filter inside the aggregate, so the op "
            "kind must be 'filtered_aggregate'"
        ]
    return []


def op_problems(op: MartOp, *, loc: str = "op") -> list[str]:
    """Structural problems with ONE op, independent of any task.

    The single definition of a well-formed op: `validate_plan` reports these and
    `compile_plan_sql` raises on them. No raw-SQL escape hatch — ``details['sql']``
    is refused, because a construct the compiler cannot see is one attacks cannot
    mutate and the author cannot verbalize.
    """
    if "sql" in op.details:
        return [
            f"{loc}: details['sql'] (raw SQL) is not a plan construct; express "
            "the op structurally"
        ]
    if op.kind is MartOpKind.TRANSITIVE_CLOSURE:
        from elt_taskgen.generation.recursive import closure_keys
        try:
            closure_keys(op)
        except (ValueError, TypeError, KeyError) as exc:
            return [f"{loc}: {exc}"]
        return []
    if op.kind in AGGREGATE_FAMILY_KINDS:
        return _aggregate_family_problems(op, loc)
    if op.kind is MartOpKind.EXTREMA:
        return _extrema_problems(op, loc)
    if op.kind is MartOpKind.WINDOW:
        return _window_problems(op, loc)
    if op.kind is MartOpKind.CONDITIONAL:
        return _conditional_problems(op, loc)
    if op.kind is MartOpKind.RATIO:
        return _ratio_problems(op, loc)
    return []


def _identifier_names(text: str) -> set[str]:
    """Bare column names mentioned in a select-list fragment."""
    parsed = _parse_select_list(text)
    if parsed is None:
        return set()
    return {c.name for c in parsed.find_all(exp.Column)}


def _select_projections(op: MartOp) -> list[tuple[str, exp.Expression]] | None:
    """(output alias, projected expression) for an op with a select list."""
    parsed = _parse_select_list(op.details.get("select", "").strip())
    if parsed is None:
        return None
    out: list[tuple[str, exp.Expression]] = []
    for projection in parsed.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        out.append((projection.alias_or_name, inner))
    return out

_CLAIM_MARKER: dict[MartOpKind, type[exp.Expression]] = {
    MartOpKind.WINDOW: exp.Window,
    MartOpKind.CONDITIONAL: exp.Case,
    MartOpKind.RATIO: exp.Div,
}


def _claimed_columns(op: MartOp) -> dict[str, MartColumnKind]:
    """Which columns THIS op claims to compute, and as what kind.

    Read off the projected EXPRESSION, not the op's column list: a bare column
    reference is carried forward, a CASE/OVER()/division is computed.
    """
    claim = OP_COLUMN_KIND.get(op.kind)
    if claim is None:
        return {}
    if op.kind in AGGREGATE_FAMILY_KINDS:
        # No select list: the op names grain AND measures; the grain stays
        # passthrough.
        group = _identifier_names(op.details.get("group_by", ""))
        return {c: claim for c in op.columns if c not in group}
    projections = _select_projections(op)
    if projections is None:
        # Unparseable select list: claim everything rather than nothing
        # (op_problems reports the parse failure).
        return {c: claim for c in op.columns}
    if op.kind is MartOpKind.EXTREMA:
        # The ARGMAX payload is a bare renamed column (picking the row IS the
        # computation), so everything but the partition key is ranked.
        partition = _identifier_names(op.details.get("partition_by", ""))
        return {
            alias: claim
            for alias, node in projections
            if alias not in partition
            and not (isinstance(node, exp.Column) and node.name in partition)
        }
    marker = _CLAIM_MARKER[op.kind]
    return {
        alias: claim
        for alias, node in projections
        if isinstance(node, marker) or node.find(marker) is not None
    }


def _produced_aliases(op: MartOp) -> set[str]:
    """Output aliases the op's OWN select list introduces (empty if it has none)."""
    if op.kind in AGGREGATE_FAMILY_KINDS:
        return {alias for alias, _expression in measure_items(op)}
    projections = _select_projections(op) if op.details.get("select") else None
    return {alias for alias, _ in projections} if projections else set()


def column_kinds_from_plan(plan: MartPlan) -> dict[str, MartColumnKind]:
    """The MartColumnKind each column's ops IMPLY — the certified bridge from
    op-typed routing to column-typed measurement.

    Strongest claim wins: a measure is named by its aggregate op and again by
    the final COALESCE derive.
    """
    implied: dict[str, MartColumnKind] = {}
    for op in plan.ops:
        for column, claim in _claimed_columns(op).items():
            current = implied.get(column)
            if current is None or _COLUMN_KIND_PRECEDENCE.index(
                claim
            ) < _COLUMN_KIND_PRECEDENCE.index(current):
                implied[column] = claim
    return implied


def column_kind_problems_for(
    plan: MartPlan, columns: Sequence[MartColumn], *, mart_name: str
) -> list[str]:
    """Disagreements between DECLARED column kinds and the kinds `plan`'s ops
    imply — the core the other kind gates all call.

    Tolerant of an UNDECLARED kind (legacy tasks carry ``kind=None``);
    `unclassified_columns` is the fail-closed half.
    """
    implied = column_kinds_from_plan(plan)
    problems: list[str] = []
    for column in columns:
        if column.kind is None:
            continue
        claim = implied.get(column.name, MartColumnKind.PASSTHROUGH)
        if column.kind is claim:
            continue
        # 'derived' over a passthrough claim is honest (the base DERIVE op has
        # no op kind of its own); every other disagreement is a lie.
        if claim is MartColumnKind.PASSTHROUGH and column.kind is MartColumnKind.DERIVED:
            continue
        problems.append(
            f"mart {mart_name!r} column {column.name!r}: declared kind "
            f"{column.kind.value!r} but the plan's ops imply {claim.value!r}"
        )
    return problems


def column_kind_problems(mart: MartSpec) -> list[str]:
    """Disagreements between declared MartColumn.kind and the plan's ops.

    Wired, not advisory: `validate_plan`, `budget_problems` and the
    structural-completeness gate all refuse on it.
    """
    return column_kind_problems_for(mart.plan, mart.columns, mart_name=mart.name)


def unclassified_columns(mart: MartSpec) -> list[str]:
    """Mart columns with no declared kind — what a fail-closed gate rejects."""
    return [c.name for c in mart.columns if c.kind is None]


def computed_column_count(mart: MartSpec) -> int:
    """How many of this mart's columns are COMPUTED (the anchor's statistic)."""
    return sum(1 for c in mart.columns if c.computed)


def _aggregate_dependent_aliases(plan: MartPlan) -> tuple[set[str], ...]:
    """Aggregate-derived aliases visible to each op before it executes.

    Pattern certification follows the declared relation bindings rather than
    searching SQL text for keywords.  Aggregate builders initially expose
    compiler-local aliases such as ``m_0``; projection ops may rename those to
    public mart columns, so dependency has to be propagated through every
    select list.  The returned tuple is indexed exactly like ``plan.ops`` and
    records the aliases available on that op's input relations.
    """

    by_relation: dict[str, set[str]] = {}
    visible_before: list[set[str]] = []

    for op in plan.ops:
        incoming: set[str] = set()
        for relation in op.tables:
            incoming |= by_relation.get(relation, set())
        visible_before.append(set(incoming))

        outgoing = set(incoming)
        if op.kind in AGGREGATE_FAMILY_KINDS:
            # Aggregate-family details use stable compiler aliases (m_0, ...)
            # until a following named projection exposes the mart names.
            outgoing |= {alias for alias, _ in measure_items(op)}

        projections = (
            _select_projections(op) if op.details.get("select", "").strip() else None
        )
        if projections is not None:
            projected: set[str] = set()
            for alias, expression in projections:
                dependencies = {
                    column.name for column in expression.find_all(exp.Column)
                }
                if isinstance(expression, exp.Column):
                    dependencies.add(expression.name)
                if dependencies & outgoing:
                    projected.add(alias)
            outgoing = projected

        binding = op.details.get("name", "")
        if binding:
            by_relation[binding] = outgoing
        elif op.kind is MartOpKind.SOURCE:
            for relation in op.tables:
                by_relation.setdefault(relation, set())

    return tuple(visible_before)


def _certifies_aggregate_then_filter(plan: MartPlan) -> bool:
    """Whether a downstream predicate consumes an aggregate-derived value.

    This is the structural core of the aggregate-then-filter/HAVING slice: an
    aggregate result must flow through named relations into the predicate.  A
    raw-row filter followed by aggregation does not satisfy it, nor does a
    post-aggregate filter on an unrelated passthrough attribute.
    """

    visible_before = _aggregate_dependent_aliases(plan)
    for index, op in enumerate(plan.ops):
        if op.kind is not MartOpKind.FILTER or not op.predicate:
            continue
        predicate = _parse_expression(op.predicate)
        if predicate is None:
            continue
        referenced = {column.name for column in predicate.find_all(exp.Column)}
        if isinstance(predicate, exp.Column):
            referenced.add(predicate.name)
        if referenced & visible_before[index]:
            return True
    return False


def _certifies_branched_fact_rollup(plan: MartPlan) -> bool:
    from elt_taskgen.generation.shapes.branched_fact_rollup import certifies_branched_plan

    return certifies_branched_plan(plan)


def _certifies_global_baseline(plan: MartPlan) -> bool:
    from elt_taskgen.generation.compositions import certifies_global_baseline
    from elt_taskgen.generation.recipe_certification import certifies_entity_baseline
    return certifies_global_baseline(plan) or certifies_entity_baseline(plan)


def _certifies_multiple_extrema(plan: MartPlan) -> bool:
    from elt_taskgen.generation.compositions import certifies_multiple_extrema
    return certifies_multiple_extrema(plan)


def _certifies_aggregate_extrema(plan: MartPlan) -> bool:
    from elt_taskgen.generation.recipe_certification import certifies_aggregate_extrema
    return certifies_aggregate_extrema(plan)


def _certifies_calendar_streak(plan: MartPlan) -> bool:
    from elt_taskgen.generation.recipe_certification import certifies_calendar_streak
    return certifies_calendar_streak(plan)


def _certifies_advanced_recipe(plan: MartPlan, recipe: str) -> bool:
    from elt_taskgen.generation.advanced_certification import certifies_advanced
    return certifies_advanced(plan, recipe)


def _certifies_scenario_recipe(plan: MartPlan, recipe: str) -> bool:
    from elt_taskgen.generation.scenario_plan import certifies_scenario
    return certifies_scenario(plan, recipe)


def _certifies_graph_recipe(plan: MartPlan, recipe: str) -> bool:
    from elt_taskgen.generation.graph_plan import certifies_graph
    return certifies_graph(plan, recipe)


def _certifies_composite_recipe(plan: MartPlan, recipe: str) -> bool:
    from elt_taskgen.generation.composite_plan import certifies_composite
    return certifies_composite(plan, recipe)


def _certifies_discovered_group_report(plan: MartPlan) -> bool:
    if plan.template_id != "discovered_group_report_v1":
        return False
    from elt_taskgen.generation.discovered_recipes import certifies_discovered
    return certifies_discovered(plan, plan.template_id)


SEMANTIC_PATTERN_CERTIFIERS: dict[SemanticPattern, Callable[[MartPlan], bool]] = {
    SemanticPattern.DISCOVERED_GROUP_REPORT: _certifies_discovered_group_report,
    SemanticPattern.ELIGIBLE_GROUP_REPORT: lambda p: _certifies_composite_recipe(p, "composite_eligible_group_report_v1"),
    SemanticPattern.DOCUMENT_BALANCE: lambda p: _certifies_composite_recipe(p, "composite_document_balance_v1"),
    SemanticPattern.ROLE_PLAYING_DIMENSION: lambda p: _certifies_graph_recipe(p, "role_playing_dimension_v1"),
    SemanticPattern.AMBIGUOUS_PATH: lambda p: _certifies_graph_recipe(p, "ambiguous_path_rollup_v1"),
    SemanticPattern.MULTI_BRIDGE_DEDUP: lambda p: _certifies_graph_recipe(p, "multi_bridge_dedup_v1"),
    SemanticPattern.OPTIONAL_CHAIN: lambda p: _certifies_graph_recipe(p, "optional_chain_coverage_v1"),
    SemanticPattern.HETEROGENEOUS_KEY_JOIN: lambda p: _certifies_graph_recipe(p, "heterogeneous_key_join_v1"),
    SemanticPattern.COMPOSITE_KEY_BRIDGE: lambda p: _certifies_graph_recipe(p, "composite_key_bridge_v1"),
    SemanticPattern.AGGREGATE_THEN_FILTER: _certifies_aggregate_then_filter,
    SemanticPattern.BRANCHED_FACT_ROLLUP: _certifies_branched_fact_rollup,
    SemanticPattern.GROUP_GLOBAL_BASELINE: _certifies_global_baseline,
    SemanticPattern.MULTIPLE_EXTREMA: _certifies_multiple_extrema,
    SemanticPattern.AGGREGATE_THEN_EXTREMA: _certifies_aggregate_extrema,
    SemanticPattern.CONSECUTIVE_STREAK: _certifies_calendar_streak,
    SemanticPattern.SCD_ASOF: lambda p: _certifies_advanced_recipe(p, "historical_asof_v1"),
    SemanticPattern.COHORT_RETENTION: lambda p: _certifies_advanced_recipe(p, "cohort_retention_v1"),
    SemanticPattern.WEIGHTED_ALLOCATION: lambda p: _certifies_advanced_recipe(p, "weighted_allocation_v1"),
    SemanticPattern.INTERVAL_COVERAGE: lambda p: _certifies_scenario_recipe(p, "interval_coverage_v1"),
    SemanticPattern.SESSIONIZATION: lambda p: _certifies_scenario_recipe(p, "event_sessionization_v1"),
    SemanticPattern.ACTIVITY_LIFECYCLE: lambda p: _certifies_scenario_recipe(p, "activity_lifecycle_v1"),
    SemanticPattern.DOCUMENT_RECONCILIATION: lambda p: _certifies_scenario_recipe(p, "document_reconciliation_v1"),
    SemanticPattern.LEDGER_RECONCILIATION: lambda p: _certifies_scenario_recipe(p, "ledger_reconciliation_v1"),
    SemanticPattern.RECURSIVE_HIERARCHY: lambda p: _certifies_scenario_recipe(p, "recursive_hierarchy_v1"),
}


def detect_semantic_patterns(plan: MartPlan) -> tuple[SemanticPattern, ...]:
    """Independently derive every currently implemented compound pattern."""

    return tuple(
        sorted(
            (
                pattern
                for pattern, certifier in SEMANTIC_PATTERN_CERTIFIERS.items()
                if certifier(plan)
            ),
            key=lambda pattern: pattern.value,
        )
    )


def semantic_pattern_problems(plan: MartPlan) -> list[str]:
    """Return dishonest or as-yet-unimplemented semantic declarations.

    Coverage metadata is never accepted as its own proof.  A declared pattern
    needs a registered detector and that detector must reconstruct the claim
    from the ordered ops.  Once a plan opts into semantic identity (a template
    id or at least one pattern), implemented patterns detected in its structure
    must also be declared so reports cannot selectively hide them.
    """

    declared = set(plan.semantic_patterns)
    implemented = set(SEMANTIC_PATTERN_CERTIFIERS)
    detected = set(detect_semantic_patterns(plan))
    problems: list[str] = []

    for pattern in sorted(declared - implemented, key=lambda item: item.value):
        problems.append(
            f"semantic pattern {pattern.value!r} has no registered structural "
            "certifier"
        )
    for pattern in sorted(
        (declared & implemented) - detected, key=lambda item: item.value
    ):
        problems.append(
            f"semantic pattern {pattern.value!r} is declared but not certified "
            "by the ordered plan ops"
        )

    if plan.template_id or plan.semantic_patterns:
        for pattern in sorted(detected - declared, key=lambda item: item.value):
            problems.append(
                f"semantic pattern {pattern.value!r} is structurally present but "
                "missing from semantic_patterns"
            )
    return problems


def mart_inputs(task: TaskIR, plan: MartPlan) -> tuple[str, ...]:
    names = {mart.name for mart in task.marts}
    inputs: dict[str, None] = {}
    bound: set[str] = set()
    for op in plan.ops:
        if op.kind is MartOpKind.TIE_BREAK:
            continue
        for name in op.tables:
            if name in names and name not in bound:
                inputs[name] = None
        if op.kind is not MartOpKind.SOURCE:
            binding = op.details.get("name") or (op.tables[0] if op.tables else "")
            bound.add(binding)
    return tuple(inputs)


def validate_plan(task: TaskIR, plan: MartPlan) -> list[str]:
    """Return problem strings; empty list = the plan is coherent with the task.

    Covers op structure (`op_problems`), task coherence (tables, column
    resolution, FK-backed joins, mart columns produced, declared column kinds)
    and the compiler's own dry run, so a plan can never validate and then fail
    to compile. The converse does not hold: incoherent-but-compilable is refused.
    """
    problems: list[str] = list(semantic_pattern_problems(plan))
    try:
        mart = task.mart(plan.mart)
    except KeyError:
        return [f"plan targets mart {plan.mart!r}, which is not a mart of task {task.task_id!r}"]

    table_names = {t.name for t in task.tables}
    inputs = {table.name: table.columns for table in task.tables}
    inputs.update({name: task.mart(name).columns for name in mart_inputs(task, plan)})
    mart_cols = {c.name for c in mart.columns}
    all_task_cols = {column.name for columns in inputs.values() for column in columns}
    produced: set[str] = set()
    #: Intermediate relations bound by earlier ops — the same namespace
    #: compile_plan_sql builds (details['name'], else the op's single table).
    bound: set[str] = set()

    for idx, op in enumerate(plan.ops):
        loc = f"op[{idx}] ({op.kind.value})"
        problems.extend(op_problems(op, loc=loc))

        unknown_tables = [t for t in op.tables if t not in inputs and t not in bound]
        if unknown_tables:
            problems.append(f"{loc}: unknown tables {unknown_tables}")
        known_op_tables = [t for t in op.tables if t in table_names]
        touches_intermediate = any(t in bound and t not in table_names for t in op.tables)

        known_inputs = [t for t in op.tables if t in inputs]
        if known_inputs and not touches_intermediate:
            valid_cols: set[str] = set()
            for name in known_inputs:
                valid_cols |= {column.name for column in inputs[name]}
        else:
            # An intermediate relation is opaque here, so column resolution
            # falls back to the permissive task-wide set.
            valid_cols = set(all_task_cols)
        # An op may also name the aliases IT produces (a running total consumed
        # by a later extremum is neither a source nor a mart column).
        valid_cols |= mart_cols | produced | _produced_aliases(op)
        bad_cols = [c for c in op.columns if c not in valid_cols]
        if bad_cols:
            problems.append(
                f"{loc}: columns {bad_cols} not found in referenced tables, "
                "prior ops, or mart columns"
            )

        if op.kind is not MartOpKind.TIE_BREAK:
            binding = op.details.get("name") or (op.tables[0] if op.tables else "")
            if binding:
                bound.add(binding)

        if (
            op.kind is MartOpKind.JOIN
            and not unknown_tables
            and not touches_intermediate
            and len(known_op_tables) == 2
        ):
            a, b = known_op_tables
            pair = {a, b}
            backing = [
                rel
                for rel in task.relationships
                if {rel.child_table, rel.parent_table} == pair
            ]
            if not backing:
                problems.append(
                    f"{loc}: join between {a!r} and {b!r} is not backed by a "
                    "declared relationship"
                )
            elif op.columns:
                rel_cols = set()
                for rel in backing:
                    rel_cols |= set(rel.child_columns) | set(rel.parent_columns)
                if not (set(op.columns) & rel_cols):
                    problems.append(
                        f"{loc}: join columns {list(op.columns)} do not overlap the "
                        f"declared relationship keys {sorted(rel_cols)}"
                    )

        produced |= set(op.columns)

    missing = sorted(mart_cols - produced)
    if missing:
        problems.append(f"mart columns not produced by any op: {missing}")

    # Declared kinds are certified, not trusted: `budget_problems` would count a
    # passthrough falsely declared 'ranked' as computed.
    problems.extend(column_kind_problems_for(plan, mart.columns, mart_name=mart.name))

    # The other half of the one contract: sequence-level refusals (name
    # resolution, "the plan produces a relation") no per-op check can see. Uses
    # the compiler's OWN dry run, never a re-implementation. Imported here
    # because solution.py imports this module — the call, not the arrow, defers.
    from elt_taskgen.reference.solution import plan_compilation_problems

    problems.extend(
        p for p in plan_compilation_problems(task, mart, plan=plan) if p not in problems
    )
    return problems
